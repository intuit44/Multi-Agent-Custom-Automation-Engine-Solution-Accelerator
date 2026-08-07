import React from 'react';
import { AgentMessageData, AgentMessageType } from '@/models';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypePrism from 'rehype-prism';
import { Body1, Tag, makeStyles, tokens } from '@fluentui/react-components';
import { TaskService } from '@/services';
import {
  ArrowDownloadRegular,
  CopyRegular,
  PersonRegular,
  ShareRegular,
} from '@fluentui/react-icons';
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
    // Padding must count INSIDE maxWidth: 100% — content-box made the bubble
    // 736px in a 704px parent, giving the whole chat a horizontal scrollbar.
    boxSizing: 'border-box',
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
  // Botón del overlay de imagen (patrón Gemini): base transparente — el
  // feedback es el hover-state animado, no un chip sólido. :hover no existe
  // en estilos inline; por eso vive aquí.
  imageOverlayButton: {
    width: '32px',
    height: '32px',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    border: 'none',
    borderRadius: '8px',
    backgroundColor: 'rgba(0, 0, 0, 0)',
    color: '#fff',
    cursor: 'pointer',
    padding: '0',
    // Legibilidad del icono blanco sobre zonas claras de la imagen.
    filter: 'drop-shadow(0 1px 2px rgba(0, 0, 0, 0.6))',
    transitionProperty: 'background-color, transform',
    transitionDuration: '120ms',
    ':hover': {
      backgroundColor: 'rgba(0, 0, 0, 0.35)',
    },
    ':active': {
      transform: 'scale(0.92)',
    },
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

// Acciones SOBRE la imagen (patrón Gemini): share / copy / download viven en
// un overlay al hover de la imagen generada — la imagen es el artefacto y sus
// acciones van con ella, no en una ristra de chips aparte. El click en la
// imagen sigue abriendo el archivo completo.
//
// Los botones están SIEMPRE en el DOM (como en Gemini) y se ocultan/revelan
// por CSS: montarlos solo en hover los hacía in-inspeccionables (mouseleave
// los desmontaba antes de que el picker de DevTools llegara) y no animables.
const GeneratedImage = ({ alt, src, ...props }: any) => {
  const styles = useStyles();
  const url = resolveApiUrl(src);
  const filename = alt || 'generated.png';
  const [hover, setHover] = React.useState(false);

  const download = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const blob = await (await fetch(url)).blob();
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = objectUrl;
      a.download = filename;
      a.click();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    } catch {
      // ignore (network/permission errors)
    }
  };
  const copy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const blob = await (await fetch(url)).blob();
      await navigator.clipboard.write([
        new ClipboardItem({ [blob.type]: blob }),
      ]);
    } catch {
      // Safari no permite ClipboardItem con fetch async / permiso denegado:
      // al menos queda el enlace en el portapapeles.
      await navigator.clipboard.writeText(url);
    }
  };
  const share = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      if (navigator.share) {
        await navigator.share({ title: filename, url });
      } else {
        await navigator.clipboard.writeText(url);
      }
    } catch {
      // usuario cerró el share sheet — no es un error
    }
  };

  return (
    // span (no div): el markdown envuelve la imagen en un <p>; un div ahí es
    // HTML inválido y React lo advierte. display:block conserva el layout
    // exacto que tenía la imagen sola.
    <span
      style={{ position: 'relative', display: 'block', margin: '8px 0' }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onFocus={() => setHover(true)}
      onBlur={() => setHover(false)}
    >
      <img
        alt={alt ?? ''}
        src={url}
        {...props}
        style={{
          // Rectangular card, visually paired with the chat input: full column
          // width, 12px radius, 3:2 (the generator now produces 1536×1024 —
          // the same ratio — so the image fills the card with nothing to crop).
          width: '100%',
          aspectRatio: '3 / 2',
          objectFit: 'cover',
          maxHeight: '480px',
          display: 'block',
          borderRadius: '12px',
          cursor: 'zoom-in',
        }}
        onClick={() => window.open(url, '_blank', 'noopener')}
      />
      <span
        style={{
          position: 'absolute',
          top: '8px',
          right: '8px',
          display: 'flex',
          gap: '4px',
          // Siempre en DOM; visibilidad animada por CSS (fade + deslizamiento).
          opacity: hover ? 1 : 0,
          visibility: hover ? 'visible' : 'hidden',
          transform: hover ? 'translateY(0)' : 'translateY(-4px)',
          pointerEvents: hover ? 'auto' : 'none',
          transition: 'opacity 150ms ease, transform 150ms ease',
      >
        <button
          type="button"
          title="Compartir"
          aria-label="Compartir imagen"
          className={styles.imageOverlayButton}
          onClick={share}
        >
          <ShareRegular fontSize={16} />
        </button>
        <button
          type="button"
          title="Copiar imagen"
          aria-label="Copiar imagen"
          className={styles.imageOverlayButton}
          onClick={copy}
        >
          <CopyRegular fontSize={16} />
        </button>
        <button
          type="button"
          title="Descargar"
          aria-label="Descargar imagen"
          className={styles.imageOverlayButton}
          onClick={download}
        >
          <ArrowDownloadRegular fontSize={16} />
        </button>
      </span>
    </span>
  );
};

// Static markdown renderers — hoisted to module scope so the `components`
// object is stable across renders. An inline object here would be a new
// reference on every render and defeat ReactMarkdown's internal memoization.
// Shared by every agent-bubble ReactMarkdown (this file + StreamingBufferMessage)
// so links and generated images render identically everywhere.
export const markdownComponents = {
  // Wide code blocks scroll inside their own box; without this a long
  // unbreakable line widens the whole message column (horizontal scrollbar
  // on the chat itself).
  pre: ({ node, ...props }: any) => (
    <pre
      {...props}
      style={{
        maxWidth: '100%',
        boxSizing: 'border-box',
        overflowX: 'auto',
        borderRadius: '8px',
      }}
    />
  ),
  img: ({ node, ...props }: any) => <GeneratedImage {...props} />,
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
