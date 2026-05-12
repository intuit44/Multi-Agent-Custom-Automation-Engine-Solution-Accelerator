import React, { useState, useEffect, useRef, useImperativeHandle, forwardRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypePrism from "rehype-prism";
import {
  Body1,
  Button,
  Tag,
  ToolbarDivider,
} from "@fluentui/react-components";
import { Attach20Regular, ChatDismiss20Regular, Dismiss20Regular, HeartRegular } from "@fluentui/react-icons";
import { Copy, Send } from "../imports/bundleicons";
import { apiClient } from "../../api/apiClient";
import { APIService } from "../../api/apiService";
import ChatInput from "./ChatInput";
import WidgetFrame from "../components/WidgetFrame";
import HeaderTools from "../components/Header/HeaderTools";
import "./Chat.css";
import "./prism-material-oceanic.css";
import { ChatService } from "../../services/ChatService";
const _apiService = new APIService();

interface Message {
  role: string;
  content: string;
  generatedFiles?: Array<{ file_id: string; filename: string; download_url: string }>;
  _meta?: {
    ui?: {
      resourceUri?: string;
      fallback?: 'markdown' | 'text';
    };
  };
}

// Response can be either string (legacy) or Message object with _meta
type MessageResponse = string | Message;

/** Imperative handle exposed via ref — allows parent to push messages externally (e.g. from WebSocket). */
export interface ChatHandle {
  /** Append a new message to the chat list. */
  pushMessage: (msg: { role: string; content: string; _meta?: Message['_meta'] }) => void;
  /** Update the content of the last message (used for WS streaming chunks). */
  updateLastMessage: (updater: (prev: string) => string) => void;
  /** Attach generated files to the last assistant message. */
  attachGeneratedFilesToLastMessage: (files: Array<{ file_id: string; filename: string; download_url: string }>) => void;
  /** Clear all messages. */
  clearMessages: () => void;
}

interface ChatProps {
  userId: string;
  children?: React.ReactNode;
  externalMessages?: Message[];
  externalIsTyping?: boolean;
  onSendMessage?: (
    input: string,
    history: Message[],
    fileIds?: string[]
  ) => AsyncIterable<MessageResponse> | Promise<MessageResponse>;
  onSaveMessage?: (
    userId: string,
    messages: Message[]
  ) => void;
  onLoadHistory?: (
    userId: string
  ) => Promise<Message[]>;
  onClearHistory?: (userId: string) => void;
}

const Chat = forwardRef<ChatHandle, ChatProps>(function Chat(
  {
    userId,
    children,
    externalMessages,
    externalIsTyping,
    onSendMessage,
    onSaveMessage,
    onLoadHistory,
    onClearHistory,
  },
  ref
) {
  const getGeneratedFileKind = (filename: string): "pdf" | "image" | "text" | "other" => {
    const lower = filename.toLowerCase();
    if (lower.endsWith(".pdf")) return "pdf";
    if (/\.(png|jpg|jpeg|gif|webp|svg)$/.test(lower)) return "image";
    if (/\.(txt|md|csv|json|html)$/.test(lower)) return "text";
    return "other";
  };

  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isTyping, setIsTyping] = useState(false);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const [inputHeight, setInputHeight] = useState(0);
  const [, setAvailableWidgets] = useState<any[]>([]);
  const [attachedFiles, setAttachedFiles] = useState<Array<{ name: string; file_id: string }>>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [currentConversationId, setCurrentConversationId] = useState<string | undefined>();

  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const inputContainerRef = useRef<HTMLDivElement>(null);

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const result = await _apiService.uploadChatFile(file);
      setAttachedFiles(prev => [...prev, { name: file.name, file_id: result.file_id }]);
    } catch (err) {
      console.error("File upload failed:", err);
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const removeAttachedFile = (file_id: string) => {
    setAttachedFiles(prev => prev.filter(f => f.file_id !== file_id));
  };

  useImperativeHandle(ref, () => ({
    pushMessage: (msg) => setMessages((prev) => [...prev, { role: msg.role, content: msg.content, _meta: msg._meta }]),
    updateLastMessage: (updater) =>
      setMessages((prev) => {
        if (prev.length === 0) return prev;
        const updated = [...prev];
        updated[updated.length - 1] = { ...updated[updated.length - 1], content: updater(updated[updated.length - 1].content) };
        return updated;
      }),
    attachGeneratedFilesToLastMessage: (files) =>
      setMessages((prev) => {
        if (prev.length === 0) return prev;
        const updated = [...prev];
        const last = updated[updated.length - 1];
        updated[updated.length - 1] = {
          ...last,
          generatedFiles: files,
        };
        return updated;
      }),
    clearMessages: () => setMessages([]),
  }));

  useEffect(() => {
    if (externalMessages) {
      setMessages(externalMessages);
    }
  }, [externalMessages]);

  // MCP Discovery: Load available widgets proactively
  useEffect(() => {
    const discoverWidgets = async () => {
      try {
        const result = await apiClient.get('/v4/mcp/discovery');
        setAvailableWidgets(result?.widgets || []);
        console.log(`✅ Discovered ${result?.widgets?.length || 0} widgets`);
      } catch (err) {
        console.warn('Widget discovery failed:', err);
      }
    };

    discoverWidgets();
  }, [userId]);

  useEffect(() => {
    const loadHistory = async () => {
      try {
        if (onLoadHistory) {
          const historyMessages = await onLoadHistory(userId);
          if (historyMessages && historyMessages.length > 0) {
            setMessages(historyMessages);
            return;
          }
        }
        // const chatMessages = await chatService.getUserHistory(userId);
        // setMessages(chatMessages);
      } catch (err) {
        console.log("Failed to load chat history.", err);
      }
    };
    loadHistory();
  }, [onLoadHistory, userId]);

  useEffect(() => {
    if (messagesContainerRef.current) {
      messagesContainerRef.current.scrollTop = messagesContainerRef.current.scrollHeight;
      setShowScrollButton(false);
    }
  }, [messages]);

  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;
    const handleScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container;
      setShowScrollButton(scrollTop + clientHeight < scrollHeight - 100);
    };
    container.addEventListener("scroll", handleScroll);
    return () => container.removeEventListener("scroll", handleScroll);
  }, []);

  useEffect(() => {
    if (inputContainerRef.current) {
      setInputHeight(inputContainerRef.current.offsetHeight);
    }
  }, [input]);

  const scrollToBottom = () => {
    messagesContainerRef.current?.scrollTo({
      top: messagesContainerRef.current.scrollHeight,
      behavior: "smooth",
    });
    setShowScrollButton(false);
  };

  const handleCopy = (text: string) => {
    navigator.clipboard.writeText(text).catch((err) => {
      console.log("Failed to copy text:", err);
    });
  };

  const isAsyncIterable = (value: any): value is AsyncIterable<any> => {
    return value !== null && typeof value === 'object' && Symbol.asyncIterator in value;
  };

  const sendMessage = async () => {
    if (!input.trim()) return;

    if (externalMessages) {
      const outbound = input;
      const currentFileIds = attachedFiles.map(f => f.file_id);
      setInput("");
      setAttachedFiles([]);
      setIsTyping(true);

      try {
        const response = onSendMessage?.(
          outbound,
          messages,
          currentFileIds.length > 0 ? currentFileIds : undefined
        );

        if (response) {
          if (isAsyncIterable(response)) {
            for await (const chunk of response) {
              void chunk;
              // External mode renders from Redux; callback side effects update the UI.
            }
          } else {
            await response;
          }
        }
      } catch (err) {
        console.log("Send Message Error:", err);
      } finally {
        setIsTyping(false);
      }
      return;
    }

    const updatedMessages = [...messages, { role: "user", content: input }];
    setMessages(updatedMessages);
    const currentFileIds = attachedFiles.map(f => f.file_id);
    setInput("");
    setAttachedFiles([]);
    setIsTyping(true);

    try {
      if (onSendMessage) {
        setMessages([...updatedMessages, { role: "assistant", content: "" }]);
        const response = onSendMessage(input, updatedMessages, currentFileIds.length > 0 ? currentFileIds : undefined);

        if (isAsyncIterable(response)) {
          for await (const chunk of response) {
            setMessages((prev) => {
              const updated = [...prev];
              const lastMsg = updated[updated.length - 1];

              // Handle both string chunks and Message objects
              if (typeof chunk === 'string') {
                updated[updated.length - 1] = {
                  role: "assistant",
                  content: (lastMsg?.content || "") + chunk,
                };
              } else {
                // Message object with potential _meta
                updated[updated.length - 1] = {
                  role: "assistant",
                  content: (lastMsg?.content || "") + chunk.content,
                  _meta: chunk._meta || lastMsg._meta,
                };
              }
              return updated;
            });
          }
          // Get final message from state for saving
          setMessages((prev) => {
            const finalMsg = prev[prev.length - 1];
            onSaveMessage?.(userId, [...updatedMessages, finalMsg]);
            return prev;
          });
        } else {
          const assistantResponse = await response;

          // Handle both string response and Message object
          const assistantMessage: Message = typeof assistantResponse === 'string'
            ? { role: "assistant", content: assistantResponse }
            : { role: "assistant", content: assistantResponse.content, _meta: assistantResponse._meta };

          const newHistory = [...updatedMessages, assistantMessage];
          setMessages(newHistory);
          onSaveMessage?.(userId, newHistory);
        }
      } else {
        // TODO: Implement chatService integration when not using onSendMessage
        const response = await ChatService.sendMessage(input, currentConversationId);
        setCurrentConversationId(response.session_id);
        const assistantMessage = { role: "assistant", content: response.response };
        setMessages([...updatedMessages, assistantMessage]);
        console.warn("No onSendMessage handler provided, message not sent");
      }
    } catch (err) {
      console.log("Send Message Error:", err);
      setMessages([
        ...updatedMessages,
        { role: "assistant", content: "Oops! Something went wrong sending your message." },
      ]);
    } finally {
      setIsTyping(false);
    }
  };

  const clearChat = async () => {
    try {
      if (onClearHistory) {
        onClearHistory(userId);
      } else {
        // await chatService.clearChatHistory(userId);
      }
      setMessages([]);
    } catch (err) {
      console.log("Failed to clear chat history:", err);
    }
  };

  return (
    <div className="chat-container">
      <div className="messages" ref={messagesContainerRef}>
        <div className="message-wrapper">
        {messages.map((msg, index) => (
          <div key={index} className={`message ${msg.role}`}>
            {(() => {
              const generatedFiles = msg.generatedFiles ?? [];
              return (
            <Body1>
              <div style={{ display: "flex", flexDirection: "column", whiteSpace: "pre-wrap", width: "100%" }}>
                {/* MCP Protocol 2025-11-25: Render widget if _meta.ui.resourceUri present */}
                {msg._meta?.ui?.resourceUri ? (
                  <WidgetFrame
                    resourceUri={msg._meta.ui.resourceUri}
                    fallbackContent={msg.content}
                    fallbackFormat={msg._meta.ui.fallback || 'markdown'}
                  />
                ) : (
                  <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypePrism]}>
                    {msg.content}
                  </ReactMarkdown>
                )}
                {msg.role === "assistant" && (
                  <div className="assistant-footer">
                    <div className="assistant-actions">
                      <Button
                        onClick={() => handleCopy(msg.content)}
                        title="Copy Response"
                        appearance="subtle"
                        style={{ height: 28, width: 28 }}
                        icon={<Copy />}
                      />
                      <Button
                        onClick={() => console.log("Heart clicked for response:", msg.content)}
                        title="Like"
                        appearance="subtle"
                        style={{ height: 28, width: 28 }}
                        icon={<HeartRegular />}
                      />
                    </div>
                    {/* Generated files from code_interpreter */}
                    {generatedFiles.length > 0 && (
                      <div style={{ marginTop: "6px" }}>
                        <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
                          {generatedFiles.map((f) => (
                            <a
                              key={f.file_id}
                              href={f.download_url}
                              target="_blank"
                              rel="noreferrer"
                              style={{
                                display: "inline-flex", alignItems: "center", gap: "4px",
                                background: "var(--colorBrandBackground2)",
                                borderRadius: "12px", padding: "4px 10px",
                                fontSize: "12px", color: "var(--colorBrandForeground1)",
                                textDecoration: "none",
                              }}
                            >
                              ⬇️ {f.filename}
                            </a>
                          ))}
                        </div>
                        {generatedFiles.slice(0, 1).map((f) => {
                          const kind = getGeneratedFileKind(f.filename);
                          if (kind === "pdf") {
                            return (
                              <iframe
                                key={`${f.file_id}-preview`}
                                src={f.download_url}
                                title={f.filename}
                                style={{
                                  width: "100%",
                                  height: "420px",
                                  border: "1px solid var(--colorNeutralStroke2)",
                                  borderRadius: "12px",
                                  marginTop: "10px",
                                  background: "white",
                                }}
                              />
                            );
                          }
                          if (kind === "image") {
                            return (
                              <img
                                key={`${f.file_id}-preview`}
                                src={f.download_url}
                                alt={f.filename}
                                style={{
                                  maxWidth: "100%",
                                  borderRadius: "12px",
                                  marginTop: "10px",
                                  border: "1px solid var(--colorNeutralStroke2)",
                                }}
                              />
                            );
                          }
                          if (kind === "text") {
                            return (
                              <iframe
                                key={`${f.file_id}-preview`}
                                src={f.download_url}
                                title={f.filename}
                                style={{
                                  width: "100%",
                                  height: "260px",
                                  border: "1px solid var(--colorNeutralStroke2)",
                                  borderRadius: "12px",
                                  marginTop: "10px",
                                  background: "var(--colorNeutralBackground1)",
                                }}
                              />
                            );
                          }
                          return null;
                        })}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </Body1>
              );
            })()}
          </div>
        ))}</div>


        {(isTyping || externalIsTyping) && (
          <div className="typing-indicator">
            <span>Thinking...</span>
          </div>
        )}
      </div>

      {showScrollButton && (
        <Tag
          onClick={scrollToBottom}
          className="scroll-to-bottom"
          shape="circular"
          style={{
            bottom: inputHeight,
            backgroundColor: "transparent",
            border: '1px solid var(--colorNeutralStroke3)',
            backdropFilter: "saturate(180%) blur(16px)",
          }}
        >
          Back to bottom
        </Tag>
      )}

      {/* Plan-specific overlays: approval buttons, clarification banners, spinners */}
      {children && (
        <div className="chat-children-slot">
          {children}
        </div>
      )}

      <div ref={inputContainerRef} style={{ display: 'flex', width: '100%', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ display: 'flex', width: '100%', maxWidth: '768px', margin: '0px 16px' }}>
          <ChatInput
            value={input}
            onChange={setInput}
            onEnter={sendMessage}
          >
            {/* Hidden file input */}
            <input
              ref={fileInputRef}
              type="file"
              accept=".csv,.xlsx,.json,.txt,.pdf"
              style={{ display: "none" }}
              onChange={handleFileSelect}
            />

            {/* Attached file chips */}
            {attachedFiles.length > 0 && (
              <div style={{ display: "flex", gap: "6px", flexWrap: "wrap", padding: "4px 0" }}>
                {attachedFiles.map(f => (
                  <span
                    key={f.file_id}
                    style={{
                      display: "inline-flex", alignItems: "center", gap: "4px",
                      background: "var(--colorNeutralBackground3)",
                      borderRadius: "12px", padding: "2px 8px",
                      fontSize: "12px", color: "var(--colorNeutralForeground1)",
                    }}
                  >
                    📎 {f.name}
                    <Button
                      appearance="subtle"
                      size="small"
                      icon={<Dismiss20Regular />}
                      onClick={() => removeAttachedFile(f.file_id)}
                      style={{ minWidth: "auto", padding: "0", height: "16px", width: "16px" }}
                    />
                  </span>
                ))}
              </div>
            )}

            {/* Attach button */}
            <Button
              appearance="transparent"
              onClick={() => fileInputRef.current?.click()}
              icon={<Attach20Regular />}
              aria-label="Attach file"
              disabled={isTyping}
            />

            <Button
              appearance="transparent"
              onClick={sendMessage}
              icon={<Send />}
              aria-label="Send message"
              disabled={isTyping || !input.trim()}
            />

            {messages.length > 0 && (
              <HeaderTools>
                <ToolbarDivider />
                <Button
                  onClick={clearChat}
                  appearance="transparent"
                  icon={<ChatDismiss20Regular />}
                  aria-label="Clear chat"
                  disabled={isTyping || messages.length === 0} />
              </HeaderTools>
            )}

          </ChatInput>
        </div>

      </div>


    </div>
  );
});

Chat.displayName = 'Chat';

export default Chat;
