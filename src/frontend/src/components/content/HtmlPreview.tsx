/**
 * HtmlPreview — sandboxed iframe for model-generated HTML.
 *
 * Security contract:
 *   sandbox="allow-scripts"  — scripts run in an opaque origin
 *   NO allow-same-origin     — iframe cannot touch the app's DOM,
 *                              storage, cookies or call /api/* as the user
 *   srcDoc                   — no network request, HTML injected via attribute
 *
 * Auto-height: the srcdoc embeds a tiny postMessage script that reports
 * document.body.scrollHeight. Works with allow-scripts only (no same-origin).
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
} from 'react';
import { Button } from '@fluentui/react-components';
import {
  Code20Regular,
  Dismiss20Regular,
  Eye20Regular,
} from '@fluentui/react-icons';

const MIN_HEIGHT = 120;
const MAX_HEIGHT = 600;
const EXPAND_HEIGHT = 1200;

/** Wraps raw HTML so the iframe can postMessage its scroll height back. */
function wrapWithHeightReporter(html: string): string {
  const reporter = `<script>
(function(){
  function report(){
    var h = document.documentElement.scrollHeight || document.body.scrollHeight || 0;
    parent.postMessage({type:'__html_preview_height__',height:h},'*');
  }
  if(document.readyState==='loading'){document.addEventListener('DOMContentLoaded',report);}
  else{report();}
  // re-report after images / fonts settle
  window.addEventListener('load',report);
})();
</script>`;
  // Insert before </body> if present, otherwise append
  if (/<\/body>/i.test(html)) {
    return html.replace(/<\/body>/i, `${reporter}</body>`);
  }
  return html + reporter;
}

interface HtmlPreviewProps {
  /** Raw HTML string to render. */
  html: string;
  /** Shown in the iframe title attribute (accessibility). */
  title?: string;
}

export const HtmlPreview: React.FC<HtmlPreviewProps> = ({
  html,
  title = 'HTML preview',
}) => {
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const [height, setHeight] = useState(MIN_HEIGHT);
  const [expanded, setExpanded] = useState(false);

  const srcDoc = wrapWithHeightReporter(html);
  // Chromium (verified in THIS app, headed and headless, 2026-08-24): updating
  // `srcdoc` on a live sandboxed iframe commits the new document (DOM readable,
  // reporter runs) but the frame NEVER repaints — stays white. A fresh iframe
  // whose srcdoc is set at creation paints every time. Keying the iframe by
  // document forces that remount. Isolated pages don't reproduce it; this app
  // tree does — do not remove without re-running the paint probe.
  const docKey = React.useMemo(() => {
    let h = 5381;
    for (let i = 0; i < srcDoc.length; i++) {
      h = ((h << 5) + h + srcDoc.charCodeAt(i)) | 0;
    }
    return `${h.toString(36)}:${srcDoc.length}`;
  }, [srcDoc]);

  // Listen for height messages from this specific iframe
  const handleMessage = useCallback((ev: MessageEvent) => {
    if (
      ev.data?.type === '__html_preview_height__' &&
      iframeRef.current &&
      ev.source === iframeRef.current.contentWindow
    ) {
      const natural = Math.max(MIN_HEIGHT, ev.data.height as number);
      setHeight(natural);
    }
  }, []);

  useEffect(() => {
    window.addEventListener('message', handleMessage);
    return () => window.removeEventListener('message', handleMessage);
  }, [handleMessage]);

  const cappedHeight = expanded
    ? Math.min(height, EXPAND_HEIGHT)
    : Math.min(height, MAX_HEIGHT);

  const showExpand = height > MAX_HEIGHT;

  return (
    <div
      style={{
        border: '1px solid var(--colorNeutralStroke1)',
        borderRadius: '8px',
        overflow: 'hidden',
        marginTop: '6px',
        background: '#fff',
      }}
    >
      <iframe
        key={docKey}
        ref={iframeRef}
        srcDoc={srcDoc}
        sandbox="allow-scripts"
        title={title}
        style={{
          display: 'block',
          width: '100%',
          height: `${cappedHeight}px`,
          border: 0,
          transition: 'height 0.2s ease',
        }}
      />
      {showExpand && (
        <div
          style={{
            display: 'flex',
            justifyContent: 'center',
            padding: '4px',
            borderTop: '1px solid var(--colorNeutralStroke1)',
            background: 'var(--colorNeutralBackground2)',
          }}
        >
          <Button
            appearance="subtle"
            size="small"
            onClick={() => setExpanded((v) => !v)}
          >
            {expanded ? 'Collapse' : 'Expand'}
          </Button>
        </div>
      )}
    </div>
  );
};

// ─── Right-panel preview: one destination for every HTML source ─────────────
// The preview does NOT belong in the composer (a 600px iframe above ChatInput
// swallows the input) nor inline in a bubble when a side slot exists. This
// context routes any source — generated .html file, ```html code block — to
// the SAME right-hand panel slot PlanPanelRight uses. Components fall back to
// their inline preview when no provider is mounted, so they stay standalone.

interface PreviewDoc {
  id: string;
  title: string;
  html: string;
}

interface HtmlPreviewContextValue {
  active: PreviewDoc | null;
  open: (doc: PreviewDoc) => void;
  /** Live update (streaming): applied only while `id` is the active doc. */
  update: (id: string, html: string) => void;
  close: () => void;
}

const HtmlPreviewContext = createContext<HtmlPreviewContextValue | null>(null);

export const useHtmlPreview = () => useContext(HtmlPreviewContext);

export const HtmlPreviewProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const [active, setActive] = useState<PreviewDoc | null>(null);
  const open = useCallback((doc: PreviewDoc) => setActive(doc), []);
  const close = useCallback(() => setActive(null), []);
  const update = useCallback((id: string, html: string) => {
    setActive((cur) => (cur && cur.id === id ? { ...cur, html } : cur));
  }, []);
  return (
    <HtmlPreviewContext.Provider value={{ active, open, update, close }}>
      {children}
    </HtmlPreviewContext.Provider>
  );
};

/** The right-slot panel. Renders `fallback` (e.g. PlanPanelRight) when no
 *  preview is open, so the slot keeps its normal occupant. */
export const PreviewRightSlot: React.FC<{ fallback?: React.ReactNode }> = ({
  fallback = null,
}) => {
  const ctx = useHtmlPreview();
  // Debounce srcDoc updates: every srcDoc change reloads the iframe document,
  // so streaming token-by-token would thrash it. 300ms keeps it live.
  const [html, setHtml] = useState('');
  const activeId = ctx?.active?.id;
  const activeHtml = ctx?.active?.html ?? '';
  useEffect(() => {
    setHtml(activeHtml); // new doc: render immediately
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);
  useEffect(() => {
    const t = setTimeout(() => setHtml(activeHtml), 300);
    return () => clearTimeout(t);
  }, [activeHtml]);

  if (!ctx?.active) return <>{fallback}</>;
  return (
    <div
      style={{
        width: '440px',
        height: '100vh',
        display: 'flex',
        flexDirection: 'column',
        borderLeft: '1px solid var(--colorNeutralStroke1)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '8px 12px',
          borderBottom: '1px solid var(--colorNeutralStroke1)',
        }}
      >
        <span
          style={{
            fontSize: '13px',
            fontWeight: 600,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
          }}
        >
          {ctx.active.title}
        </span>
        <Button
          appearance="subtle"
          size="small"
          icon={<Dismiss20Regular />}
          aria-label="Close preview"
          onClick={ctx.close}
        />
      </div>
      <div style={{ flex: 1, overflow: 'auto', padding: '8px' }}>
        <HtmlPreview html={html} title={ctx.active.title} />
      </div>
    </div>
  );
};

/** Toggle between raw-code view and live HTML preview. */
interface HtmlCodeToggleProps {
  code: string;
  /** The already-rendered <pre><code> element to show in code mode. */
  codeBlock: React.ReactNode;
}

export const HtmlCodeToggle: React.FC<HtmlCodeToggleProps> = ({
  code,
  codeBlock,
}) => {
  const [showPreview, setShowPreview] = useState(false);
  const ctx = useHtmlPreview();
  const id = useId();
  const isActiveInPanel = ctx?.active?.id === id;

  // Streaming: while this block is the panel's active doc, push new tokens to
  // it (the panel debounces the actual iframe reload).
  useEffect(() => {
    if (isActiveInPanel) ctx?.update(id, code);
  }, [code, isActiveInPanel, ctx, id]);

  const onPreview = () => {
    if (ctx) {
      ctx.open({ id, title: 'HTML', html: code });
    } else {
      setShowPreview(true);
    }
  };

  return (
    <div>
      <div
        style={{
          display: 'flex',
          gap: '4px',
          marginBottom: '4px',
        }}
      >
        <Button
          appearance={showPreview || isActiveInPanel ? 'subtle' : 'primary'}
          size="small"
          icon={<Code20Regular />}
          onClick={() => {
            setShowPreview(false);
            if (isActiveInPanel) ctx?.close();
          }}
        >
          Code
        </Button>
        <Button
          appearance={showPreview || isActiveInPanel ? 'primary' : 'subtle'}
          size="small"
          icon={<Eye20Regular />}
          onClick={onPreview}
        >
          Preview
        </Button>
      </div>
      {showPreview && !ctx ? <HtmlPreview html={code} /> : codeBlock}
    </div>
  );
};
