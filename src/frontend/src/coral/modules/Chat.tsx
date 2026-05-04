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
import { Copy, Send } from "../imports/bundleicons";
import { ChatDismiss20Regular, HeartRegular } from "@fluentui/react-icons";
import ChatInput from "./ChatInput";
import WidgetFrame from "../components/WidgetFrame";
import { apiClient } from "../../api/apiClient";
import "./Chat.css";
import "./prism-material-oceanic.css";
// import { chatService } from "../services/chatService"; // TODO: Re-enable when chatService integration is complete
import HeaderTools from "../components/Header/HeaderTools";

interface Message {
  role: string;
  content: string;
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
  /** Clear all messages. */
  clearMessages: () => void;
}

interface ChatProps {
  userId: string;
  children?: React.ReactNode;
  onSendMessage?: (
    input: string,
    history: Message[]
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
    onSendMessage,
    onSaveMessage,
    onLoadHistory,
    onClearHistory,
  },
  ref
) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [isTyping, setIsTyping] = useState(false);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const [inputHeight, setInputHeight] = useState(0);
  const [, setAvailableWidgets] = useState<any[]>([]);

  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const inputContainerRef = useRef<HTMLDivElement>(null);

  useImperativeHandle(ref, () => ({
    pushMessage: (msg) => setMessages((prev) => [...prev, { role: msg.role, content: msg.content, _meta: msg._meta }]),
    updateLastMessage: (updater) =>
      setMessages((prev) => {
        if (prev.length === 0) return prev;
        const updated = [...prev];
        updated[updated.length - 1] = { ...updated[updated.length - 1], content: updater(updated[updated.length - 1].content) };
        return updated;
      }),
    clearMessages: () => setMessages([]),
  }));

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

    const updatedMessages = [...messages, { role: "user", content: input }];
    setMessages(updatedMessages);
    setInput("");
    setIsTyping(true);

    try {
      if (onSendMessage) {
        setMessages([...updatedMessages, { role: "assistant", content: "" }]);
        const response = onSendMessage(input, updatedMessages);

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
        // const response = await chatService.sendMessage(userId, input, currentConversationId);
        // setCurrentConversationId(response.conversation_id);
        // const assistantMessage = { role: "assistant", content: response.assistant_response };
        // setMessages([...updatedMessages, assistantMessage]);
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
                  </div>
                )}
              </div>
            </Body1>
          </div>
        ))}</div>


        {isTyping && (
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
