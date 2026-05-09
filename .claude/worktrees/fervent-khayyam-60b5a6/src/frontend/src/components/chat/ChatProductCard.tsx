/**
 * ChatProductCard — Renders a product card inside chat message bubbles.
 *
 * Extracted from: microsoft/customer-chatbot-solution-accelerator
 *   (src/App/src/components/ChatProductCard.tsx)
 * Adapted: shadcn → FluentUI v9
 */
import React, { useState } from 'react';
import {
    Body1,
    Body1Strong,
    Caption1,
    Card,
    CardHeader,
} from '@fluentui/react-components';
import type { Product } from '../../lib/types';

interface ChatProductCardProps {
    product: Product;
    onAddToCart?: (product: Product) => void;
}

export const ChatProductCard: React.FC<ChatProductCardProps> = ({ product }) => {
    const [imgError, setImgError] = useState(false);

    return (
        <Card
            size="small"
            style={{
                display: 'flex',
                flexDirection: 'row',
                gap: '12px',
                padding: '12px',
                marginTop: '8px',
                marginBottom: '8px',
                maxWidth: '420px',
            }}
        >
            {product.image && !imgError ? (
                <img
                    src={product.image}
                    alt={product.title}
                    onError={() => setImgError(true)}
                    style={{
                        width: '64px',
                        height: '64px',
                        borderRadius: '8px',
                        objectFit: 'cover',
                        flexShrink: 0,
                    }}
                />
            ) : (
                <div
                    style={{
                        width: '64px',
                        height: '64px',
                        borderRadius: '8px',
                        backgroundColor: 'var(--colorNeutralBackground3)',
                        flexShrink: 0,
                    }}
                />
            )}

            <div style={{ flex: 1, minWidth: 0 }}>
                <Body1Strong style={{ display: 'block' }}>{product.title}</Body1Strong>
                {product.description && (
                    <Caption1
                        style={{
                            display: 'block',
                            color: 'var(--colorNeutralForeground3)',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                        }}
                    >
                        {product.description}
                    </Caption1>
                )}
                <Body1Strong style={{ color: 'var(--colorBrandForeground1)' }}>
                    ${product.price.toFixed(2)} USD
                </Body1Strong>
            </div>
        </Card>
    );
};

export default ChatProductCard;
