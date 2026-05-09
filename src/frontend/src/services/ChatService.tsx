/**
 * ChatService — manages conversational chat sessions (P0)
 *
 * Handles:
 * - Sending messages through IntentRouter
 * - Managing local chat session state
 * - Persisting/loading sessions via Cosmos DB endpoints
 *
 * Extracted patterns from: microsoft/customer-chatbot-solution-accelerator
 *   (src/App/src/lib/api.ts — session management)
 */
import { apiService } from '../api/apiService';
import {
    ChatMessageRequest,
    ChatMessageResponse,
} from '../models/chatMessage';
import type {
    ChatMessage,
    ChatSessionSummary,
} from '../lib/types';

/** Callbacks for streaming chat responses. */
export interface StreamCallbacks {
    onToken: (token: string) => void;
    onIntent: (data: { intent: string; confidence: number; session_id: string }) => void;
    onDone: (data: { intent: string; agent: string; confidence: number; session_id: string }) => void;
    /** Called when intent router detects a task and creates a plan inline. */
    onPlanCreated?: (planId: string) => void;
    /** Legacy redirect — kept for backward compat. */
    onRedirect?: (planId: string) => void;
    onError: (error: string) => void;
    onToolActivity?: (data: { activity: string; tool: string; server?: string; success?: boolean }) => void;
    /** Called when code_interpreter generates a downloadable file. */
    onGeneratedFile?: (data: { file_id: string; filename: string; download_url: string }) => void;
}

// In-memory cache of chat sessions (persists during page lifetime)
const chatSessions = new Map<string, ChatMessage[]>();

export class ChatService {
    /**
     * Send a message and stream the response via SSE.
     * The LLM tokens arrive one-by-one via callbacks.
     */
    static async sendMessageStream(
        message: string,
        sessionId: string | undefined,
        callbacks: StreamCallbacks,
        fileIds?: string[],
    ): Promise<void> {
        const request: ChatMessageRequest = {
            session_id: sessionId || '',
            message,
            ...(fileIds && fileIds.length > 0 ? { file_ids: fileIds } : {}),
        };
        await apiService.sendChatMessageStream(request, callbacks);
    }

    /**
     * Send a message to the IntentRouter endpoint (non-streaming fallback).
     * Returns the full response including intent classification.
     */
    static async sendMessage(
        message: string,
        sessionId?: string,
    ): Promise<ChatMessageResponse> {
        const request: ChatMessageRequest = {
            session_id: sessionId || '',
            message,
        };

        const response = await apiService.sendChatMessage(request);

        // Store messages in local cache
        const sid = response.session_id;
        const msgs = ChatService.getMessages(sid);

        // Add user message
        msgs.push({
            id: `msg_${Date.now()}_user`,
            content: message,
            role: 'user',
            timestamp: new Date(),
        });

        // Add assistant response (only for non-task intents)
        if (response.intent !== 'task') {
            msgs.push({
                id: `msg_${Date.now()}_assistant`,
                content: response.response,
                role: 'assistant',
                timestamp: new Date(),
                intent: response.intent,
                agent: response.agent,
                confidence: response.confidence,
            });
        }

        chatSessions.set(sid, msgs);
        return response;
    }

    /**
     * Stream a chat response as an AsyncIterable<string>.
     *
     * Adapts the callback-based sendMessageStream API to the AsyncIterable<string>
     * contract expected by Chat.tsx coral onSendMessage.
     *
     * Behaviour:
     *   - Yields each LLM token as a plain string chunk.
     *   - On plan_created the generator stops; the navigate callback handles routing.
     *   - On error the generator yields an error string and closes.
     */
    static streamAsAsyncIterable(
        message: string,
        sessionId: string | undefined,
        onPlanCreated?: (planId: string) => void,
        onSessionId?: (sid: string) => void,
        fileIds?: string[],
        onGeneratedFile?: (f: { file_id: string; filename: string; download_url: string }) => void,
    ): AsyncIterable<string> {
        return {
            [Symbol.asyncIterator](): AsyncIterator<string> {
                // Queue of resolved chunks; resolve/reject hooks for the consumer.
                const queue: string[] = [];
                let done = false;
                let error: unknown = null;
                let notify: (() => void) | null = null;

                const push = (chunk: string) => {
                    queue.push(chunk);
                    notify?.();
                };
                const finish = () => { done = true; notify?.(); };
                const fail   = (e: unknown) => { error = e; done = true; notify?.(); };

                // Fire the SSE connection — don't await; run in background.
                ChatService.sendMessageStream(message, sessionId, {
                    onToken:       (token) => push(token),
                    onIntent:      (data)  => { if (data.session_id) onSessionId?.(data.session_id); },
                    onToolActivity:(data)  => {
                        // Surface tool-call activity as a minimal UI hint.
                        if (data.activity === 'calling') push(`\n_🔧 Calling **${data.tool}**${data.server ? ` on \`${data.server}\`` : ''}…_\n`);
                    },
                    onPlanCreated: (planId) => { onPlanCreated?.(planId); finish(); },
                    onDone:        () => finish(),
                    onError:       (msg) => fail(new Error(msg)),
                    onGeneratedFile: (f) => { onGeneratedFile?.(f); },
                }, fileIds).catch(fail);

                return {
                    next(): Promise<IteratorResult<string>> {
                        // Fast-path: data already queued or terminal state.
                        if (error) return Promise.reject(error);
                        if (queue.length > 0) return Promise.resolve({ value: queue.shift()!, done: false });
                        if (done) return Promise.resolve({ value: '', done: true });
                        // Slow-path: nothing ready yet — park until push/finish/fail.
                        // notify is assigned once per call; no loop variable capture issue.
                        return new Promise<IteratorResult<string>>((resolve, reject) => {
                            notify = () => {
                                notify = null;
                                if (error) { reject(error); return; }
                                if (queue.length > 0) { resolve({ value: queue.shift()!, done: false }); return; }
                                resolve({ value: '', done: true });
                            };
                        });
                    },
                };
            },
        };
    }

    /**
     * Get all messages for a session (local cache).
     */
    static getMessages(sessionId: string): ChatMessage[] {
        if (!chatSessions.has(sessionId)) {
            chatSessions.set(sessionId, []);
        }
        return chatSessions.get(sessionId)!;
    }

    /**
     * Load session from Cosmos DB into local cache.
     */
    static async loadSession(sessionId: string): Promise<ChatMessage[]> {
        try {
            const session = await apiService.getChatSession(sessionId);
            if (!session || !session.messages) return [];

            const messages: ChatMessage[] = session.messages.map((m) => {
                const meta = (m.metadata as any) ?? {};
                const gf: Array<{ file_id: string; filename: string; container_id?: string | null; download_url: string }> =
                    Array.isArray(meta.generated_files) ? meta.generated_files : [];
                // Strip sandbox: markdown links unconditionally — replace with real
                // download_url when the filename matches a known generated file.
                let content = m.content ?? '';
                content = content.replace(
                    /\[([^\]]+)\]\(sandbox:[^)]+\)/g,
                    (_match: string, label: string) => {
                        const known = gf.find(
                            (f) => label.includes(f.filename) || f.filename.includes(label)
                        );
                        // Use clean filename as label; if unknown, extract basename
                        const cleanLabel = known?.filename ?? label.split('/').pop() ?? label;
                        return known
                            ? `[${cleanLabel}](${known.download_url})`
                            : cleanLabel;
                    }
                );
                return {
                    id: m.id,
                    content,
                    role: (m as any).role || (m as any).sender as 'user' | 'assistant',
                    timestamp: new Date(m.timestamp),
                    intent: meta.intent,
                    agent: meta.agent,
                    generatedFiles: gf.length > 0 ? gf : undefined,
                };
            });

            chatSessions.set(sessionId, messages);
            return messages;
        } catch {
            return ChatService.getMessages(sessionId);
        }
    }

    /**
     * Get all recent chat sessions from Cosmos DB.
     */
    static async getRecentSessions(): Promise<ChatSessionSummary[]> {
        try {
            const result = await apiService.getChatSessions();
            return result.sessions || [];
        } catch {
            return [];
        }
    }

    /**
     * Create a new chat session via backend.
     */
    static async createNewSession(): Promise<string> {
        try {
            const result = await apiService.createChatSession();
            return result.data.session_id;
        } catch {
            return ChatService.generateSessionId();
        }
    }

    /**
     * Delete a chat session.
     */
    static async deleteSession(sessionId: string): Promise<boolean> {
        chatSessions.delete(sessionId);
        try {
            await apiService.deleteChatSession(sessionId);
            return true;
        } catch {
            return false;
        }
    }

    /**
     * Check if a session exists in local cache.
     */
    static hasSession(sessionId: string): boolean {
        return chatSessions.has(sessionId) && chatSessions.get(sessionId)!.length > 0;
    }

    /**
     * Generate a unique session ID for chat (fallback).
     */
    static generateSessionId(): string {
        const timestamp = Date.now();
        const random = Math.floor(Math.random() * 10000);
        return `chat_${timestamp}_${random}`;
    }
}

export default ChatService;
