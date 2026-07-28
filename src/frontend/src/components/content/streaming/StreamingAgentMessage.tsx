import React from 'react';
import { AgentMessageData, AgentMessageType } from '@/models';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypePrism from 'rehype-prism';
import { Body1, Tag, makeStyles, tokens } from '@fluentui/react-components';
import { TaskService } from '@/services';
import { PersonRegular } from '@fluentui/react-icons';
import { getAgentIcon, getAgentDisplayName } from '@/utils/agentIconUtils';
import { resolveApiUrl } from '@/api/config';

interface StreamingAgentMessageProps {
  agentMessages: AgentMessageData[];
  planData?: any;
  planApprovalRequest?: any;
}

const useStyles = makeStyles({
  container: {
    maxWidth: '800px',
    margin: '0 auto 32px auto',
    padding: '0 24px',
    display: 'flex',
    alignItems: 'flex-start',
    gap: '16px',
    fontFamily: tokens.fontFamilyBase,
  },
  avatar: {
    width: '32px',
    height: '32px',
    borderRadius: '50%',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    flexShrink: 0,
  },
  humanAvatar: {
    backgroundColor: 'var(--colorBrandBackground)',
  },
  botAvatar: {
    backgroundColor: 'var(--colorNeutralBackground3)',
  },
  messageContent: {
    flex: 1,
    maxWidth: 'calc(100% - 48px)',
    display: 'flex',
    flexDirection: 'column',
  },
  humanMessageContent: {
    alignItems: 'flex-end',
  },
  botMessageContent: {
    alignItems: 'flex-start',
  },
  agentHeader: {
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    marginBottom: '8px',
  },
  agentName: {
    fontWeight: '600',
    fontSize: '14px',
    color: 'var(--colorNeutralForeground1)',
    lineHeight: '20px',
  },
  messageBubble: {
    padding: '12px 16px',
    borderRadius: '8px',
    fontSize: '14px',
    lineHeight: '1.5',
    wordWrap: 'break-word',
  },
  humanBubble: {
    backgroundColor: 'var(--colorBrandBackground)',
    color: 'white !important', // Force white text in both light and dark modes
    maxWidth: '80%',
    padding: '12px 16px',
    lineHeight: '1.5',
    alignSelf: 'flex-end',
  },
  botBubble: {
    backgroundColor: 'var(--colorNeutralBackground2)',
    color: 'var(--colorNeutralForeground1)',
    maxWidth: '100%',
    alignSelf: 'flex-start',
  },

  clarificationBubble: {
    backgroundColor: 'var(--colorNeutralBackground2)',
    color: 'var(--colorNeutralForeground1)',
    padding: '6px 8px',
    borderRadius: '8px',
    fontSize: '14px',
    lineHeight: '1.5',
    wordWrap: 'break-word',
    maxWidth: '100%',
    alignSelf: 'flex-start',
  },

  actionContainer: {
    display: 'flex',
    alignItems: 'center',
    marginTop: '12px',
    paddingTop: '8px',
    borderTop: '1px solid var(--colorNeutralStroke2)',
  },

  copyButton: {
    height: '28px',
    width: '28px',
  },
  sampleTag: {
    fontSize: '11px',
    opacity: 0.7,
  },
});

// Check if message is a clarification request
const isClarificationMessage = (content: string): boolean => {
  const clarificationKeywords = [
    'need clarification',
    'please clarify',
    'could you provide more details',
    'i need more information',
    'please specify',
    'what do you mean by',
    'clarification about',
  ];

  const lowerContent = content.toLowerCase();
  return clarificationKeywords.some((keyword) =>
    lowerContent.includes(keyword)
  );
};

// Static markdown renderers — hoisted to module scope so the `components`
// object is stable across renders. An inline object here would be a new
// reference on every render and defeat ReactMarkdown's internal memoization.
// Shared by every agent-bubble ReactMarkdown (this file + StreamingBufferMessage)
// so links and generated images render identically everywhere.
export const markdownComponents = {
  img: ({ node, alt, src, ...props }: any) => (
    <img
      alt={alt ?? ''}
      src={resolveApiUrl(src)}
      {...props}
      style={{
        maxWidth: '100%',
        height: 'auto',
        display: 'block',
        borderRadius: '8px',
        margin: '8px 0',
      }}
    />
  ),
  a: ({ node, children, href, ...props }: any) => (
    <a
      href={resolveApiUrl(href)}
      {...props}
      style={{
        color: 'var(--colorNeutralBrandForeground1)',
        textDecoration: 'none',
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.textDecoration = 'underline';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.textDecoration = 'none';
      }}
    >
      {children}
    </a>
  ),
};

// Isolated, memoized Markdown. Re-parses (and re-highlights) ONLY when its
// content string changes. This is the key fix: appending a streaming token to
// the last message no longer re-parses the Markdown of every previous message.
const AgentMarkdown = React.memo(({ content }: { content: string }) => (
  <ReactMarkdown
    remarkPlugins={[remarkGfm]}
    rehypePlugins={[rehypePrism]}
    components={markdownComponents}
  >
    {content}
  </ReactMarkdown>
));
AgentMarkdown.displayName = 'AgentMarkdown';

interface AgentMessageItemProps {
  msg: AgentMessageData;
  planData?: any;
  planApprovalRequest?: any;
}

// A single chat row, memoized. Even if it re-renders because planData/approval
// references churn, the expensive Markdown inside stays protected by
// AgentMarkdown's content-based memoization.
export const AgentMessageItem = React.memo(
  ({ msg, planData, planApprovalRequest }: AgentMessageItemProps) => {
    const styles = useStyles();
    const isHuman = msg.agent_type === AgentMessageType.HUMAN_AGENT;
    const isClarification =
      !isHuman && isClarificationMessage(msg.content || '');
    const content = TaskService.cleanHRAgent(msg.content) || '';

    return (
      <div
        className={styles.container}
        style={{ flexDirection: isHuman ? 'row-reverse' : 'row' }}
      >
        {/* Avatar */}
        <div
          className={`${styles.avatar} ${isHuman ? styles.humanAvatar : styles.botAvatar}`}
        >
          {isHuman ? (
            <PersonRegular style={{ fontSize: '16px', color: 'white' }} />
          ) : (
            getAgentIcon(msg.agent, planData, planApprovalRequest)
          )}
        </div>

        {/* Message Content */}
        <div
          className={`${styles.messageContent} ${isHuman ? styles.humanMessageContent : styles.botMessageContent}`}
        >
          {/* Agent Header (only for bots) */}
          {!isHuman && (
            <div className={styles.agentHeader}>
              <Body1 className={styles.agentName}>
                {getAgentDisplayName(msg.agent)}
              </Body1>
              <Tag appearance="brand">AI Agent</Tag>
            </div>
          )}

          {/* Message Bubble */}
          <div
            className={
              isHuman
                ? `${styles.messageBubble} ${styles.humanBubble}`
                : isClarification
                  ? styles.clarificationBubble
                  : `${styles.messageBubble} ${styles.botBubble}`
            }
          >
            <AgentMarkdown content={content} />
          </div>
        </div>
      </div>
    );
  }
);
AgentMessageItem.displayName = 'AgentMessageItem';

const RenderAgentMessages: React.FC<StreamingAgentMessageProps> = ({
  agentMessages,
  planData,
  planApprovalRequest,
}) => {
  if (!agentMessages?.length) return null;

  // Filter out messages with empty content
  const validMessages = agentMessages.filter((msg) => msg.content?.trim());
  if (!validMessages.length) return null;

  return (
    <>
      {validMessages.map((msg, index) => (
        <AgentMessageItem
          key={`${msg.agent}-${msg.timestamp}-${index}`}
          msg={msg}
          planData={planData}
          planApprovalRequest={planApprovalRequest}
        />
      ))}
    </>
  );
};

export default RenderAgentMessages;
