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
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import { Button } from '@fluentui/react-components';
import { apiClient } from '../../api/apiClient';
import {
  Code20Regular,
  Dismiss20Regular,
  Eye20Regular,
} from '@fluentui/react-icons';

const MIN_HEIGHT = 120;
const MAX_HEIGHT = 600;
const EXPAND_HEIGHT = 1200;

/** Storage shim injected as the FIRST script in every sandboxed document.
 *
 * Opaque-origin iframes (sandbox="allow-scripts" without allow-same-origin)
 * throw SecurityError on ANY sessionStorage/localStorage access.  That aborts
 * the user's script at line 1, before ANY event-listeners are registered —
 * SPA navigation, form persistence, everything is dead even though HTML/CSS
 * render fine.
 *
 * This shim runs first, replaces both storage globals with Map-backed
 * in-memory equivalents ONLY when the real APIs throw.  The user's script
 * then runs without errors, click-listeners register and SPA navigation
 * works.  Values persist for the iframe's lifetime (reset on reload), which
 * is correct for a sandboxed preview.
 */
const STORAGE_SHIM = `<script>
(function(){
  function MemStorage(){
    var s=Object.create(null);
    return {
      getItem:function(k){return Object.prototype.hasOwnProperty.call(s,k)?s[k]:null;},
      setItem:function(k,v){s[String(k)]=String(v);},
      removeItem:function(k){delete s[k];},
      clear:function(){s=Object.create(null);},
      key:function(i){return Object.keys(s)[i]||null;},
      get length(){return Object.keys(s).length;}
    };
  }
  ['sessionStorage','localStorage'].forEach(function(name){
    try{ window[name].getItem('__probe__'); }
    catch(e){
      try{
        Object.defineProperty(window,name,{
          value:MemStorage(),configurable:true,writable:true
        });
      }catch(_){}
    }
  });

  // Fragment-navigation shim. In an opaque origin, clicking <a href="#/x">,
  // location.hash= and location.replace('#/x') are ALL silently ignored
  // (measured 2026-08-25) — a hash-routed SPA renders once and never
  // navigates. history.pushState IS permitted and really updates
  // location.hash, so: intercept fragment-link clicks, pushState, and fire a
  // synthetic hashchange for the app's router. Direct location.hash
  // assignments remain dead — location is unforgeable; that part cannot be
  // shimmed, only <a href="#..."> navigation is covered.
  document.addEventListener('click', function(ev){
    if(ev.defaultPrevented) return;
    if(ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    var a = ev.target && ev.target.closest ? ev.target.closest('a[href]') : null;
    if(!a) return;
    var href = a.getAttribute('href') || '';
    if(href.charAt(0) !== '#') return;
    ev.preventDefault();
    var oldURL = location.href;
    try{ history.pushState(null, '', href); }catch(e){ return; }
    try{
      window.dispatchEvent(new HashChangeEvent('hashchange',
        {oldURL: oldURL, newURL: location.href}));
    }catch(e){
      var legacy = document.createEvent('Event');
      legacy.initEvent('hashchange', true, false);
      window.dispatchEvent(legacy);
    }
  });
})();
</script>`;

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
  // Re-post on timers: a warm renderer can fire DOMContentLoaded BEFORE the
  // parent has attached its message listener (bites on the SECOND open of
  // the same doc — faster load, message lost, stuck at min height). Timed
  // re-posts make the height report self-healing regardless of who wins.
  setTimeout(report,150);
  setTimeout(report,600);
})();
</script>`;

  let result = html;

  // Storage shim must be the FIRST script — inject right after <head> opening
  // tag so it runs before any inline <script> in the user's HTML.
  if (/<head[^>]*>/i.test(result)) {
    result = result.replace(/(<head[^>]*>)/i, `$1${STORAGE_SHIM}`);
  } else {
    result = STORAGE_SHIM + result;
  }

  // Height reporter before </body>, or appended.
  if (/<\/body>/i.test(result)) {
    return result.replace(/<\/body>/i, `${reporter}</body>`);
  }
  return result + reporter;
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
  // the document on a live sandboxed iframe commits the new document (DOM
  // readable, reporter runs) but the frame NEVER repaints — stays white. A
  // fresh iframe whose document is set at creation paints every time. Keying
  // the iframe by document forces that remount. Isolated pages don't reproduce
  // it; this app tree does — do not remove without re-running the paint probe.
  const docKey = React.useMemo(() => {
    let h = 5381;
    for (let i = 0; i < srcDoc.length; i++) {
      h = ((h << 5) + h + srcDoc.charCodeAt(i)) | 0;
    }
    return `${h.toString(36)}:${srcDoc.length}`;
  }, [srcDoc]);

  // The preview document needs a REAL isolated origin — the Blob storage
  // account (≠ frontend ≠ backend, dev and prod; no cookies, no credentials).
  // POST /chat/preview publishes the HTML there and returns a short-lived SAS
  // URL; on that origin the iframe carries allow-same-origin SAFELY ("same
  // origin" = the storage origin, never MACAE) so location/history/hash
  // routing/storage all behave like a normal page. Anything less is shim
  // territory: an opaque origin silently ignores location.hash entirely.
  // Fallback (backend unreachable): an object URL WITHOUT allow-same-origin —
  // an object URL is minted on the APP origin, granting same-origin there
  // would hand the preview the app itself.
  const [doc, setDoc] = useState<{ url: string; isolated: boolean } | null>(
    null
  );
  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    (async () => {
      try {
        const r: { url?: string } = await apiClient.post('/v4/chat/preview', {
          html: srcDoc,
        });
        if (!cancelled && r?.url) {
          setDoc({ url: r.url, isolated: true });
          return;
        }
      } catch {
        /* fall through to the sandboxed object-URL fallback */
      }
      objectUrl = URL.createObjectURL(new Blob([srcDoc], { type: 'text/html' }));
      if (!cancelled) setDoc({ url: objectUrl, isolated: false });
    })();
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [srcDoc]);

  // Listen for height messages from this specific iframe
  const handleMessage = useCallback((ev: MessageEvent) => {
    if (
      ev.data?.type === '__html_preview_height__' &&
      iframeRef.current &&
      ev.source === iframeRef.current.contentWindow
    ) {
      const next = Number(ev.data.height);
      if (!Number.isFinite(next)) return;
      const natural = Math.max(MIN_HEIGHT, next);
      setHeight(natural);
    }
  }, []);

  // useLayoutEffect, NOT useEffect: it runs synchronously right after the
  // commit that inserts the iframe, BEFORE the browser can deliver any task
  // (the child's postMessage arrives as a task). A passive effect runs after
  // paint and can lose the first height message to a fast-loading srcdoc.
  useLayoutEffect(() => {
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
      {doc ? (
        <iframe
          key={docKey}
          ref={iframeRef}
          src={doc.url}
          sandbox={
            doc.isolated
              ? 'allow-scripts allow-same-origin'
              : 'allow-scripts'
          }
          title={title}
          style={{
            display: 'block',
            width: '100%',
            height: `${cappedHeight}px`,
            border: 0,
            transition: 'height 0.2s ease',
          }}
        />
      ) : (
        <div
          style={{
            height: `${MIN_HEIGHT}px`,
            display: 'grid',
            placeItems: 'center',
            color: 'var(--colorNeutralForeground3)',
            fontSize: '12px',
          }}
        >
          Publishing preview…
        </div>
      )}
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
