/**
 * ChatView — Conversational chat component (P0)
 *
 * Displays a list of chat messages and a bottom input.
 * Used for conversational & MCP query intents (no plan steps).
 * Reuses the existing Coral ChatInput component.
 */
import React, { useRef, useEffect, useState, useCallback } from 'react';
import {
    Body1,
    Body1Strong,
    Caption1,
    Badge,
    Spinner,
} from '@fluentui/react-components';
import { useParams, useNavigate } from 'react-router-dom';

import ChatInput from '@/coral/modules/ChatInput';
import { Send } from '@/coral/imports/bundleicons';
import { Button } from '@fluentui/react-components';
import { ChatService } from '../../services/ChatService';
import type { ChatMessage } from '../../lib/types';
import InlineToaster, { useInlineToaster } from '../toast/InlineToaster';

import '../../styles/Chat.css';
import '../../styles/ChatView.css';

interface ChatViewProps {
    /** Pre-loaded initial messages (passed from navigation state) */
    initialMessages?: ChatMessage[];
}

const ChatView: React.FC<ChatViewProps> = ({ initialMessages }) => {
    const { sessionId } = useParams<{ sessionId: string }>();
    const navigate = useNavigate();
    const { showToast, dismissToast } = useInlineToaster();

    const [messages, setMessages] = useState<ChatMessage[]>(
        initialMessages || []
    );
    const [input, setInput] = useState('');
    const [sending, setSending] = useState(false);

    const textareaRef = useRef<HTMLTextAreaElement>(null);
    const messagesEndRef = useRef<HTMLDivElement>(null);

    // Load existing session messages on mount
    useEffect(() => {
        if (sessionId && !initialMessages?.length) {
            const stored = ChatService.getMessages(sessionId);
            if (stored.length > 0) {
                setMessages(stored);
            }
        }
    }, [sessionId, initialMessages]);

    // Auto-scroll to bottom on new messages
    useEffect(() => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages]);

    // Focus input on mount
    useEffect(() => {
        textareaRef.current?.focus();
    }, []);

    const handleSend = useCallback(async () => {
        const trimmed = input.trim();
        if (!trimmed || sending) return;

        setSending(true);
        const toastId = showToast('Sending…', 'progress');

        // Optimistically add user message
        const userMsg: ChatMessage = {
            id: `msg_${Date.now()}_user`,
            role: 'user',
            content: trimmed,
            timestamp: new Date(),
        };
        setMessages((prev) => [...prev, userMsg]);
        setInput('');

        try {
            const response = await ChatService.sendMessage(
                trimmed,
                sessionId,
            );

            dismissToast(toastId);

            // If IntentRouter says this is a task → redirect to plan
            if (response.intent === 'task' && response.redirect_to_plan) {
                showToast('Creating plan…', 'success');
                navigate(`/plan/${response.redirect_to_plan}`);
                return;
            }

            // Add assistant response
            const assistantMsg: ChatMessage = {
                id: `msg_${Date.now()}_assistant`,
                role: 'assistant',
                content: response.response,
                timestamp: new Date(),
                intent: response.intent,
                agent: response.agent,
                confidence: response.confidence,
            };
            setMessages((prev) => [...prev, assistantMsg]);
        } catch (error: any) {
            dismissToast(toastId);
            console.error('Chat send error:', error);
            showToast(
                error?.message || 'Failed to send message',
                'error'
            );
        } finally {
            setSending(false);
        }
    }, [input, sending, sessionId, navigate, showToast, dismissToast]);

    const formatTime = (date: Date) => {
        return new Intl.DateTimeFormat(undefined, {
            hour: '2-digit',
            minute: '2-digit',
        }).format(new Date(date));
    };

    const getIntentBadge = (intent?: string) => {
        if (!intent) return null;
        const colorMap: Record<string, 'informative' | 'success' | 'warning'> = {
            conversational: 'informative',
            mcp_query: 'success',
            task: 'warning',
        };
        return (
            <Badge
                appearance="outline"
                color={colorMap[intent] || 'informative'}
                size="small"
            >
                {intent === 'mcp_query' ? 'MCP' : intent}
            </Badge>
        );
    };

    return (
        <div className="chat-view-container">
            {/* Messages area */}
            <div className="chat-view-messages">
                {messages.length === 0 && (
                    <div className="chat-view-empty">
                        <Body1>
                            Start a conversation — ask anything or describe a task.
                        </Body1>
                    </div>
                )}

                {messages.map((msg) => (
                    <div
                        key={msg.id}
                        className={`chat-view-bubble chat-view-bubble--${msg.role}`}
                    >
                        <div className="chat-view-bubble-header">
                            <Body1Strong>
                                {msg.role === 'user' ? 'You' : msg.agent || 'Assistant'}
                            </Body1Strong>
                            <div className="chat-view-bubble-meta">
                                {msg.role === 'assistant' && getIntentBadge(msg.intent)}
                                <Caption1>{formatTime(msg.timestamp)}</Caption1>
                            </div>
                        </div>
                        <div className="chat-view-bubble-content">
                            <Body1 style={{ whiteSpace: 'pre-wrap' }}>
                                {msg.content}
                            </Body1>
                        </div>
                    </div>
                ))}

                {sending && (
                    <div className="chat-view-typing">
                        <Spinner size="tiny" label="Thinking…" />
                    </div>
                )}

                <div ref={messagesEndRef} />
            </div>

            {/* Input area */}
            <div className="chat-view-input-area">
                <InlineToaster />
                <ChatInput
                    ref={textareaRef}
                    value={input}
                    placeholder="Continue the conversation or describe a new task…"
                    onChange={setInput}
                    onEnter={handleSend}
                    disabledChat={sending}
                >
                    <Button
                        appearance="subtle"
                        className="home-input-send-button"
                        onClick={handleSend}
                        disabled={sending}
                        icon={<Send />}
                        aria-label="Send message"
                    />
                </ChatInput>
            </div>
        </div>
    );
};

export default ChatView;
