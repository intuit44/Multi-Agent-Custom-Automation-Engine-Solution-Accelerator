/**
 * Text parsers for extracting structured data from AI agent responses.
 *
 * Extracted from: microsoft/customer-chatbot-solution-accelerator (src/App/src/lib/textParsers.ts)
 * The AI agent returns markdown-formatted text about products/orders.
 * These parsers convert that text into structured Product/Order objects
 * that can be rendered as rich cards in the chat UI.
 */

import type { Order, OrderItem, Product } from './types';

/* ── Content Type Detection ──────────────────────────────────── */

/**
 * Detect whether the AI response contains orders, products, or plain text.
 */
export function detectContentType(text: string): 'orders' | 'products' | 'text' {
    if (!text) return 'text';

    // Order indicators
    const hasOrderNumber = text.includes('**Order Number:**') || text.includes('**Order #');
    const hasOrderContext = /recent orders?|order history|order status/i.test(text) && text.includes('**Status:**');
    if (hasOrderNumber || hasOrderContext) return 'orders';

    // Product indicators
    const hasPriceRating = text.includes('**Price:**') && text.includes('**Rating:**');
    const hasNumberedProducts = /\d+\.\s+\*\*[^*]+\*\*/.test(text) && /!\[.*?\]\(.*?\)|\[.*?\]\(.*?\.(jpg|jpeg|png|webp|gif)/i.test(text);
    const hasImageLinks = /\[.*?\]\(https?:\/\/.*?\.(jpg|jpeg|png|webp|gif)/i.test(text) && /\*\*[^*]+\*\*/.test(text);
    if (hasPriceRating || hasNumberedProducts || hasImageLinks) return 'products';

    return 'text';
}

/* ── Order Parsing ───────────────────────────────────────────── */

export function parseOrdersFromText(text: string): { orders: Order[]; introText: string } {
    const orders: Order[] = [];
    let introText = '';

    // Split on order boundaries
    const orderBlocks = text.split(/(?=\d+\.\s+\*\*Order\s+(?:Number|#))/i);

    if (orderBlocks.length > 1) {
        introText = orderBlocks[0].trim();
    }

    for (const block of orderBlocks) {
        if (!block.includes('**Order') && !block.includes('**Status')) continue;

        const order = _parseOrderBlock(block);
        if (order) orders.push(order);
    }

    // Fallback: try single-order format
    if (orders.length === 0 && text.includes('**Order Number:**')) {
        const order = _parseOrderBlock(text);
        if (order) {
            orders.push(order);
            introText = '';
        }
    }

    return { orders, introText };
}

function _parseOrderBlock(block: string): Order | null {
    const orderNumber = _extractField(block, /\*\*Order\s*(?:Number|#)[:\s]*\*\*\s*([^\n*]+)/i) || 'Unknown';
    const status = _extractField(block, /\*\*Status[:\s]*\*\*\s*([^\n*]+)/i) || 'Unknown';
    const orderDate = _extractField(block, /\*\*(?:Order\s*)?Date[:\s]*\*\*\s*([^\n*]+)/i) || '';

    // Parse items
    const items: OrderItem[] = [];
    const itemPattern = /[-•]\s*(.+?):\s*(\d+)\s*x\s*\$?([\d.]+)(?:\s*=\s*\$?([\d.]+))?/g;
    let match: RegExpExecArray | null;
    while ((match = itemPattern.exec(block)) !== null) {
        items.push({
            name: match[1].trim(),
            quantity: parseInt(match[2]),
            unitPrice: parseFloat(match[3]),
            totalPrice: match[4] ? parseFloat(match[4]) : parseInt(match[2]) * parseFloat(match[3]),
        });
    }

    // Parse financial summary
    const subtotal = _extractNumber(block, /\*\*Subtotal[:\s]*\*\*\s*\$?([\d.]+)/i);
    const tax = _extractNumber(block, /\*\*Tax[:\s]*\*\*\s*\$?([\d.]+)/i);
    const total = _extractNumber(block, /\*\*Total[:\s]*\*\*\s*\$?([\d.]+)/i);
    const shippingAddress = _extractField(block, /\*\*(?:Shipping\s*)?Address[:\s]*\*\*\s*([^\n]+(?:\n(?!\*\*)[^\n]+)*)/i) || '';

    if (orderNumber === 'Unknown' && items.length === 0) return null;

    return {
        orderNumber,
        status,
        orderDate,
        items,
        subtotal: subtotal || items.reduce((sum, item) => sum + item.totalPrice, 0),
        tax: tax || 0,
        total: total || (subtotal || 0) + (tax || 0),
        shippingAddress: shippingAddress.trim(),
    };
}

/* ── Product Parsing ─────────────────────────────────────────── */

export function parseProductsFromText(
    text: string,
): { products: Product[]; introText: string; outroText: string } {
    const products: Product[] = [];
    let introText = '';
    let outroText = '';

    // Split on numbered product entries: "1. **Product Name**"
    const parts = text.split(/(?=\d+\.\s+\*\*[^*]+\*\*)/);

    if (parts.length > 1) {
        introText = parts[0].trim();
    }

    for (let i = 1; i < parts.length; i++) {
        const block = parts[i];
        const product = _parseProductBlock(block, i);
        if (product) {
            products.push(product);
        } else if (i === parts.length - 1) {
            // Last block may be outro text
            outroText = block.trim();
        }
    }

    return { products, introText, outroText };
}

function _parseProductBlock(block: string, index: number): Product | null {
    // Extract title from "1. **Product Name**"
    const titleMatch = block.match(/\d+\.\s+\*\*(.+?)\*\*/);
    if (!titleMatch) return null;

    const title = titleMatch[1].trim();
    const price = _extractNumber(block, /\*\*Price[:\s]*\*\*\s*\$?([\d.]+)/i) || 0;
    const rating = _extractNumber(block, /\*\*Rating[:\s]*\*\*\s*([\d.]+)/i) || 0;
    const reviewCount = _extractNumber(block, /\*\*Reviews?[:\s]*\*\*\s*(\d+)/i) || 0;

    // Extract description
    const descMatch = block.match(/\*\*Description[:\s]*\*\*\s*([^\n]+)/i);
    const description = descMatch ? descMatch[1].trim() : '';

    // Extract category
    const catMatch = block.match(/\*\*Category[:\s]*\*\*\s*([^\n]+)/i);
    const category = catMatch ? catMatch[1].trim() : 'General';

    // Extract image URL from markdown image syntax
    const imgMatch = block.match(/!\[.*?\]\((https?:\/\/[^\s)]+)\)/) ||
                     block.match(/\[.*?\]\((https?:\/\/[^\s)]+\.(jpg|jpeg|png|webp|gif)[^\s)]*)\)/i);
    const image = imgMatch ? imgMatch[1] : '';

    return {
        id: `parsed_product_${index}`,
        title,
        price,
        rating,
        reviewCount,
        image,
        category,
        inStock: true,
        description,
    };
}

/* ── Helpers ─────────────────────────────────────────────────── */

function _extractField(text: string, pattern: RegExp): string | null {
    const match = text.match(pattern);
    return match ? match[1].trim() : null;
}

function _extractNumber(text: string, pattern: RegExp): number {
    const match = text.match(pattern);
    return match ? parseFloat(match[1]) : 0;
}

/* ── Timestamp Formatting ────────────────────────────────────── */

export function formatTimestamp(timestamp: Date): string {
    return new Intl.DateTimeFormat(undefined, {
        hour: '2-digit',
        minute: '2-digit',
    }).format(new Date(timestamp));
}
