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
import {
  Button,
  Menu,
  MenuItem,
  MenuList,
  MenuPopover,
  MenuTrigger,
} from '@fluentui/react-components';
import ReactMarkdown from 'react-markdown';
import rehypePrism from 'rehype-prism';
import { apiClient } from '../../api/apiClient';
import {
  ArrowDownload20Regular,
  ChevronDown20Regular,
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

// ─── Artifact registry: every generated file, one panel ─────────────────────
// A generated file (a named code fence in a message, a generated_file from the
// SSE) is an ARTIFACT with identity = its filename. The registry keeps the
// latest content per identity, so when the model re-emits the same file with a
// correction, the SAME entry updates — the panel shows the living file. The
// right-hand panel is the single viewer: HTML renders in the isolated-origin
// iframe, code renders with prism, and a dropdown lists every artifact of the
// conversation so twenty files never occupy the chat UI.

export interface Artifact {
  /** Identity — the filename. Re-emitting it UPDATES this entry. */
  id: string;
  title: string;
  lang: string;
  /** Latest full content; empty until fetched for downloadUrl-only files. */
  content: string;
  downloadUrl?: string;
  updatedAt: number;
}

interface ArtifactContextValue {
  artifacts: Artifact[];
  activeId: string | null;
  upsert: (a: Omit<Artifact, 'updatedAt'>) => void;
  open: (id: string) => void;
  close: () => void;
}

const HtmlPreviewContext = createContext<ArtifactContextValue | null>(null);

export const useHtmlPreview = () => useContext(HtmlPreviewContext);

export const HtmlPreviewProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);

  const upsert = useCallback((a: Omit<Artifact, 'updatedAt'>) => {
    setArtifacts((prev) => {
      const i = prev.findIndex((x) => x.id === a.id);
      if (i >= 0) {
        const cur = prev[i];
        const nextDownload = a.downloadUrl ?? cur.downloadUrl;
        // Empty content never CLOBBERS fetched content: downloadUrl feeders
        // re-register with content:'' on every list change.
        const nextContent = a.content !== '' ? a.content : cur.content;
        // Bail out on no-op updates: chips re-register on every render tick
        // during streaming and an unconditional new array would render-loop.
        if (cur.content === nextContent && cur.downloadUrl === nextDownload) {
          return prev;
        }
        const next = prev.slice();
        next[i] = {
          ...cur,
          ...a,
          content: nextContent,
          downloadUrl: nextDownload,
          updatedAt: Date.now(),
        };
        return next;
      }
      return [...prev, { ...a, updatedAt: Date.now() }];
    });
  }, []);

  const open = useCallback((id: string) => setActiveId(id), []);
  const close = useCallback(() => setActiveId(null), []);

  const value = React.useMemo(
    () => ({ artifacts, activeId, upsert, open, close }),
    [artifacts, activeId, upsert, open, close]
  );
  return (
    <HtmlPreviewContext.Provider value={value}>
      {children}
    </HtmlPreviewContext.Provider>
  );
};

const IMAGE_EXT = /\.(png|jpe?g|webp|gif|svg)$/i;

/** Prism-highlighted code body for non-HTML artifacts. */
const ArtifactCodeView: React.FC<{ lang: string; content: string }> = ({
  lang,
  content,
}) => {
  // Fence length must exceed any backtick run inside the content.
  const fence = React.useMemo(() => {
    const runs: string[] = content.match(/`+/g) || [];
    const longest = runs.reduce((m, s) => Math.max(m, s.length), 0);
    return '`'.repeat(Math.max(4, longest + 1));
  }, [content]);
  return (
    <ReactMarkdown
      rehypePlugins={[rehypePrism]}
      components={{
        pre: ({ node: _n, ...props }: any) => (
          <pre
            {...props}
            style={{
              maxWidth: '100%',
              boxSizing: 'border-box',
              overflowX: 'auto',
              borderRadius: '8px',
              margin: 0,
            }}
          />
        ),
      }}
    >
      {`${fence}${lang}\n${content}\n${fence}`}
    </ReactMarkdown>
  );
};

/** The right-slot artifact panel. Renders `fallback` (e.g. PlanPanelRight)
 *  when nothing is open, so the slot keeps its normal occupant. */
export const PreviewRightSlot: React.FC<{ fallback?: React.ReactNode }> = ({
  fallback = null,
}) => {
  const ctx = useHtmlPreview();
  const active = ctx?.artifacts.find((a) => a.id === ctx.activeId) || null;

  // Debounce content: streaming updates the active artifact per tick; the
  // HTML iframe reloads per srcDoc change, so give it 300ms of quiet.
  const [content, setContent] = useState('');
  const activeContent = active?.content ?? '';
  const activeKey = active?.id ?? '';
  useEffect(() => {
    setContent(activeContent); // artifact switch: render immediately
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey]);
  useEffect(() => {
    const t = setTimeout(() => setContent(activeContent), 300);
    return () => clearTimeout(t);
  }, [activeContent]);

  // downloadUrl-only artifact opened without content yet: fetch it once.
  const upsert = ctx?.upsert;
  useEffect(() => {
    if (!active || !upsert) return;
    if (active.content || !active.downloadUrl) return;
    if (IMAGE_EXT.test(active.title)) return; // images render via <img>
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(active.downloadUrl as string);
        const text = await res.text();
        if (!cancelled) {
          upsert({
            id: active.id,
            title: active.title,
            lang: active.lang,
            content: text,
            downloadUrl: active.downloadUrl,
          });
        }
      } catch {
        /* stays download-only */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [active, upsert]);

  if (!ctx || !active) return <>{fallback}</>;

  const isHtml =
    active.lang === 'html' || /\.html?$/i.test(active.title);
  const isImage = IMAGE_EXT.test(active.title);

  const handleDownload = () => {
    const a = document.createElement('a');
    let objectUrl: string | null = null;

    if (active.downloadUrl) {
      a.href = active.downloadUrl;
    } else {
      objectUrl = URL.createObjectURL(
        new Blob([active.content], { type: 'text/plain' })
      );
      a.href = objectUrl;
    }

    a.download = active.title;
    a.click();

    if (objectUrl) {
      const urlToRevoke = objectUrl;
      setTimeout(() => URL.revokeObjectURL(urlToRevoke), 0);
    }
  };

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
          gap: '4px',
          padding: '6px 8px',
          borderBottom: '1px solid var(--colorNeutralStroke1)',
        }}
      >
        {/* Selector: every artifact of the conversation in ONE dropdown */}
        <Menu>
          <MenuTrigger disableButtonEnhancement>
            <Button
              appearance="subtle"
              size="small"
              icon={<ChevronDown20Regular />}
              style={{
                flex: 1,
                justifyContent: 'flex-start',
                overflow: 'hidden',
              }}
            >
              <span
                style={{
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  fontWeight: 600,
                }}
              >
                {active.title}
              </span>
            </Button>
          </MenuTrigger>
          <MenuPopover>
            <MenuList>
              {ctx.artifacts.map((a) => (
                <MenuItem key={a.id} onClick={() => ctx.open(a.id)}>
                  {a.title}
                </MenuItem>
              ))}
            </MenuList>
          </MenuPopover>
        </Menu>
        <Button
          appearance="subtle"
          size="small"
          icon={<ArrowDownload20Regular />}
          aria-label={`Download ${active.title}`}
          onClick={handleDownload}
        />
        <Button
          appearance="subtle"
          size="small"
          icon={<Dismiss20Regular />}
          aria-label="Close preview"
          onClick={ctx.close}
        />
      </div>
      <div style={{ flex: 1, overflow: 'auto', padding: '8px' }}>
        {isImage && active.downloadUrl ? (
          <img
            src={active.downloadUrl}
            alt={active.title}
            style={{ maxWidth: '100%' }}
          />
        ) : isHtml ? (
          <HtmlPreview html={content} title={active.title} />
        ) : content ? (
          <ArtifactCodeView lang={active.lang} content={content} />
        ) : (
          <div
            style={{
              padding: '16px',
              fontSize: '12px',
              color: 'var(--colorNeutralForeground3)',
            }}
          >
            {active.downloadUrl ? 'Loading…' : 'Empty file.'}
          </div>
        )}
      </div>
    </div>
  );
};

/** File chip for generated code blocks.
 *
 * A NAMED fence (the model writes "**app.py**" above the block) or any
 * ```html fence is a FILE, not prose: it registers in the artifact panel and
 * the message shows a compact chip instead of the full dump. Re-emitting the
 * same filename UPDATES the same artifact — the correction flow. Unnamed
 * non-html fences stay inline (conversational snippets), and without a
 * provider everything falls back to the old inline behavior. */
interface HtmlCodeToggleProps {
  code: string;
  /** The already-rendered <pre><code> element for inline fallbacks. */
  codeBlock: React.ReactNode;
  filename?: string;
  lang?: string;
}

export const HtmlCodeToggle: React.FC<HtmlCodeToggleProps> = ({
  code,
  codeBlock,
  filename,
  lang = 'html',
}) => {
  const [showInline, setShowInline] = useState(false);
  const ctx = useHtmlPreview();
  const reactId = useId();
  const isFile = Boolean(filename) || lang === 'html';
  const id = filename || `snippet-${reactId}.${lang || 'txt'}`;
  const title =
    filename || (lang === 'html' ? 'snippet.html' : `snippet.${lang}`);
  const isActive = ctx?.activeId === id;
  const upsert = ctx?.upsert;

  // Registration + correction-by-identity: every content change lands on the
  // SAME entry (the provider bails out on no-op updates).
  useEffect(() => {
    if (upsert && isFile) {
      upsert({ id, title, lang: lang || '', content: code });
    }
  }, [upsert, isFile, id, title, lang, code]);

  if (!ctx || !isFile) {
    // Standalone / unnamed snippet: previous inline behavior.
    if (lang !== 'html') return <>{codeBlock}</>;
    return (
      <div>
        <div style={{ display: 'flex', gap: '4px', marginBottom: '4px' }}>
          <Button
            appearance={showInline ? 'primary' : 'subtle'}
            size="small"
            icon={<Eye20Regular />}
            onClick={() => setShowInline((v) => !v)}
          >
            Preview
          </Button>
        </div>
        {showInline ? <HtmlPreview html={code} /> : codeBlock}
      </div>
    );
  }

  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: '8px',
        padding: '6px 10px',
        margin: '2px 0',
        border: '1px solid var(--colorNeutralStroke1)',
        borderRadius: '8px',
        background: 'var(--colorNeutralBackground2)',
        fontSize: '13px',
      }}
    >
      <Code20Regular />
      <span style={{ fontWeight: 600 }}>{title}</span>
      <span style={{ color: 'var(--colorNeutralForeground3)' }}>{lang}</span>
      <Button
        appearance={isActive ? 'primary' : 'secondary'}
        size="small"
        icon={<Eye20Regular />}
        onClick={() => (isActive ? ctx.close() : ctx.open(id))}
      >
        {isActive ? 'Abierto' : 'Abrir'}
      </Button>
    </span>
  );
};
