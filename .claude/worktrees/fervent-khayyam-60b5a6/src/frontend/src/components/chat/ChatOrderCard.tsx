/**
 * ChatOrderCard — Renders an order card inside chat message bubbles.
 *
 * Extracted from: microsoft/customer-chatbot-solution-accelerator
 *   (src/App/src/components/ChatOrderCard.tsx)
 * Adapted: shadcn Badge → FluentUI Badge, Tailwind → CSS variables.
 */
import React from 'react';
import {
    Badge,
    Body1,
    Body1Strong,
    Caption1,
    Card,
    Divider,
} from '@fluentui/react-components';
import type { Order } from '../../lib/types';

interface ChatOrderCardProps {
    order: Order;
}

const statusColorMap: Record<string, 'success' | 'informative' | 'warning' | 'danger' | 'important'> = {
    delivered: 'success',
    shipped: 'informative',
    processing: 'warning',
    cancelled: 'danger',
    refunded: 'important',
};

export const ChatOrderCard: React.FC<ChatOrderCardProps> = ({ order }) => {
    const badgeColor = statusColorMap[order.status.toLowerCase()] || 'informative';

    return (
        <Card
            size="small"
            style={{
                padding: '16px',
                marginTop: '8px',
                marginBottom: '8px',
                maxWidth: '460px',
            }}
        >
            {/* Header */}
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                <div>
                    <Body1Strong style={{ display: 'block' }}>
                        Order #{order.orderNumber}
                    </Body1Strong>
                    {order.orderDate && (
                        <Caption1 style={{ color: 'var(--colorNeutralForeground3)' }}>
                            {order.orderDate}
                        </Caption1>
                    )}
                </div>
                <Badge appearance="filled" color={badgeColor} size="medium">
                    {order.status}
                </Badge>
            </div>

            <Divider style={{ margin: '8px 0' }} />

            {/* Items */}
            {order.items.length > 0 && (
                <div style={{ marginBottom: '8px' }}>
                    {order.items.map((item, idx) => (
                        <div
                            key={idx}
                            style={{
                                display: 'flex',
                                justifyContent: 'space-between',
                                padding: '4px 0',
                            }}
                        >
                            <Caption1>
                                {item.name} ({item.quantity} × ${item.unitPrice.toFixed(2)})
                            </Caption1>
                            <Caption1 style={{ fontWeight: 600 }}>
                                ${item.totalPrice.toFixed(2)}
                            </Caption1>
                        </div>
                    ))}
                </div>
            )}

            <Divider style={{ margin: '8px 0' }} />

            {/* Summary */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                {order.subtotal > 0 && (
                    <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                        <Caption1>Subtotal</Caption1>
                        <Caption1>${order.subtotal.toFixed(2)}</Caption1>
                    </div>
                )}
                {order.tax > 0 && (
                    <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                        <Caption1>Tax</Caption1>
                        <Caption1>${order.tax.toFixed(2)}</Caption1>
                    </div>
                )}
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                    <Body1Strong>Total</Body1Strong>
                    <Body1Strong>${order.total.toFixed(2)}</Body1Strong>
                </div>
            </div>

            {/* Shipping */}
            {order.shippingAddress && (
                <>
                    <Divider style={{ margin: '8px 0' }} />
                    <Caption1 style={{ color: 'var(--colorNeutralForeground3)' }}>
                        Ship to: {order.shippingAddress}
                    </Caption1>
                </>
            )}
        </Card>
    );
};

export default ChatOrderCard;
