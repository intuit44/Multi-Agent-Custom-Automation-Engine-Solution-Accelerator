import ChatInput from '@/coral/modules/ChatInput';
import { PlanChatProps } from '@/models';
import { resolveApiUrl } from '@/api/config';
import {
  Button,
  Menu,
  MenuTrigger,
  MenuPopover,
  MenuList,
  MenuItem,
  Divider,
  Switch,
  Tooltip,
} from '@fluentui/react-components';
import { Send } from '@/coral/imports/bundleicons';
import {
  Attach20Regular,
  Dismiss20Regular,
  ArrowDownload20Regular,
  Eye20Regular,
  Image20Regular,
  DocumentRegular,
  FolderRegular,
  MoreHorizontal20Regular,
} from '@fluentui/react-icons';
import React, { useRef, useState, useCallback } from 'react';
import { HtmlPreview, useHtmlPreview } from './HtmlPreview';

interface SimplifiedPlanChatProps extends PlanChatProps {
  planData: any;
  input: string;
  setInput: (input: string) => void;
  submittingChatDisableInput: boolean;
  OnChatSubmit: (input: string) => void;
  waitingForPlan: boolean;
  attachedFiles?: Array<{ name: string; file_id: string }>;
  generatedFiles?: Array<{
    file_id: string;
    filename: string;
    download_url: string;
  }>;
  onFileSelect?: (file: File) => void;
  onRemoveFile?: (file_id: string) => void;
  onRemoveGeneratedFile?: (file_id: string) => void;
  /** Chat|Plan selector: true = this message creates a formal plan. */
  planLane?: boolean;
  onPlanLaneChange?: (checked: boolean) => void;
  /** Hidden while inside an existing plan (in-plan messages never create plans). */
  showPlanLaneToggle?: boolean;
}

// ─── HTML file card: download link + optional live Preview for .html files ───

interface HtmlFileCardProps {
  file: { file_id: string; filename: string; download_url: string };
  onRemove?: (file_id: string) => void;
}

const HtmlFileCard: React.FC<HtmlFileCardProps> = ({ file, onRemove }) => {
  const isHtml = file.filename.toLowerCase().endsWith('.html');
  const previewCtx = useHtmlPreview();
  const [showPreview, setShowPreview] = useState(false);
  const [htmlContent, setHtmlContent] = useState<string | null>(null);
  const [loadingHtml, setLoadingHtml] = useState(false);

  const handlePreviewToggle = useCallback(async () => {
    let content = htmlContent;
    if (content === null) {
      setLoadingHtml(true);
      try {
        const res = await fetch(resolveApiUrl(file.download_url));
        content = await res.text();
      } catch {
        content = '<p style="color:red">Failed to load preview.</p>';
      } finally {
        setLoadingHtml(false);
      }
      setHtmlContent(content);
    }
    // With a provider mounted the preview opens in the RIGHT PANEL — never
    // inline here: this card lives in the composer, directly above ChatInput,
    // and an inline iframe swallows the input. Inline is the fallback only.
    if (previewCtx) {
      previewCtx.open({
        id: `file:${file.file_id}`,
        title: file.filename,
        html: content ?? '',
      });
      return;
    }
    setShowPreview((v) => !v);
  }, [htmlContent, file.download_url, file.file_id, file.filename, previewCtx]);

  return (
    <div>
      {/* Download card row */}
      <div
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: '10px',
          padding: '10px 14px',
          backgroundColor: 'var(--colorBrandBackground2)',
          borderRadius: '8px',
          fontSize: '13px',
          fontWeight: 500,
          color: 'var(--colorNeutralForeground1)',
          border: '1px solid var(--colorBrandStroke1)',
          boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
        }}
      >
        <a
          href={resolveApiUrl(file.download_url)}
          download={file.filename}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
            color: 'inherit',
            textDecoration: 'none',
          }}
        >
          <ArrowDownload20Regular
            style={{ color: 'var(--colorBrandForeground1)' }}
          />
          <span
            style={{
              maxWidth: '200px',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {file.filename}
          </span>
        </a>
        {isHtml && (
          <Button
            appearance="subtle"
            size="small"
            icon={<Eye20Regular />}
            onClick={handlePreviewToggle}
            style={{ minWidth: 'auto', padding: '2px 6px' }}
          >
            {showPreview ? 'Hide' : 'Preview'}
          </Button>
        )}
        {onRemove && (
          <Button
            appearance="transparent"
            icon={<Dismiss20Regular />}
            size="small"
            aria-label={`Discard ${file.filename}`}
            onClick={() => onRemove(file.file_id)}
            style={{
              minWidth: 'auto',
              padding: '4px',
              height: '20px',
              width: '20px',
            }}
          />
        )}
      </div>
      {/* Inline preview (only for .html) */}
      {isHtml && showPreview && (
        <div style={{ marginTop: '8px' }}>
          {loadingHtml ? (
            <span
              style={{
                fontSize: '12px',
                color: 'var(--colorNeutralForeground3)',
              }}
            >
              Loading…
            </span>
          ) : (
            htmlContent !== null && (
              <HtmlPreview html={htmlContent} title={file.filename} />
            )
          )}
        </div>
      )}
    </div>
  );
};

// ─────────────────────────────────────────────────────────────────────────────

const PlanChatBody: React.FC<SimplifiedPlanChatProps> = ({
  planData,
  input,
  setInput,
  submittingChatDisableInput,
  OnChatSubmit,
  waitingForPlan,
  attachedFiles = [],
  generatedFiles = [],
  onFileSelect,
  onRemoveFile,
  onRemoveGeneratedFile,
  planLane = false,
  onPlanLaneChange,
  showPlanLaneToggle = false,
}) => {
  const isDisabled = submittingChatDisableInput || waitingForPlan;
  const placeholder = waitingForPlan
    ? 'Waiting for plan...'
    : planLane && showPlanLaneToggle
      ? 'Describe the objective for your plan...'
      : planData
        ? 'Type your message here...'
        : 'Describe your task...';
  const fileInputRef = useRef<HTMLInputElement>(null);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file && onFileSelect) {
      onFileSelect(file);
    }
    // Reset input
    if (fileInputRef.current) fileInputRef.current.value = '';
    setMenuOpen(false);
  };

  const handleImageChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file && onFileSelect) {
      onFileSelect(file);
    }
    // Reset input
    if (imageInputRef.current) imageInputRef.current.value = '';
    setMenuOpen(false);
  };

  const getFileIcon = (fileName: string) => {
    const ext = fileName.split('.').pop()?.toLowerCase();
    if (['jpg', 'jpeg', 'png', 'gif', 'svg', 'webp'].includes(ext || '')) {
      return '🖼️';
    } else if (['pdf'].includes(ext || '')) {
      return '📄';
    } else if (['doc', 'docx'].includes(ext || '')) {
      return '📝';
    } else if (['xls', 'xlsx', 'csv'].includes(ext || '')) {
      return '📊';
    } else if (['zip', 'rar', '7z'].includes(ext || '')) {
      return '📦';
    }
    return '📎';
  };

  return (
    <div
      style={{
        bottom: 0,
        padding: '16px 24px',
        maxWidth: '800px',
        margin: '0 auto',
        marginBottom: '40px',
        width: '100%',
        boxSizing: 'border-box',
        zIndex: 10,
      }}
    >
      {/* Hidden file inputs */}
      <input
        type="file"
        ref={fileInputRef}
        accept=".csv,.xlsx,.json,.txt,.pdf,.doc,.docx,.zip,.rar"
        onChange={handleFileChange}
        style={{ display: 'none' }}
      />
      <input
        type="file"
        ref={imageInputRef}
        accept="image/*"
        onChange={handleImageChange}
        style={{ display: 'none' }}
      />

      {/* Attached files display - Professional card style */}
      {attachedFiles.length > 0 && (
        <div
          style={{
            marginBottom: '12px',
            display: 'flex',
            flexWrap: 'wrap',
            gap: '10px',
          }}
        >
          {attachedFiles.map((f) => (
            <div
              key={f.file_id}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '10px',
                padding: '10px 14px',
                backgroundColor: 'var(--colorNeutralBackground2)',
                border: '1px solid var(--colorNeutralStroke1)',
                borderRadius: '8px',
                fontSize: '13px',
                fontWeight: 500,
                boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
                transition: 'all 0.2s ease',
                cursor: 'default',
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.backgroundColor =
                  'var(--colorNeutralBackground3)';
                e.currentTarget.style.boxShadow = '0 2px 4px rgba(0,0,0,0.08)';
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.backgroundColor =
                  'var(--colorNeutralBackground2)';
                e.currentTarget.style.boxShadow = '0 1px 2px rgba(0,0,0,0.05)';
              }}
            >
              <span style={{ fontSize: '18px' }}>{getFileIcon(f.name)}</span>
              <span
                style={{
                  color: 'var(--colorNeutralForeground1)',
                  maxWidth: '200px',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
              >
                {f.name}
              </span>
              <Button
                appearance="transparent"
                icon={<Dismiss20Regular />}
                size="small"
                onClick={() => onRemoveFile?.(f.file_id)}
                style={{
                  minWidth: 'auto',
                  padding: '4px',
                  height: '20px',
                  width: '20px',
                }}
              />
            </div>
          ))}
        </div>
      )}

      {/* Generated files display - Professional download cards */}
      {generatedFiles.length > 0 && (
        <div style={{ marginBottom: '14px' }}>
          <div
            style={{
              fontSize: '12px',
              fontWeight: 600,
              marginBottom: '8px',
              color: 'var(--colorNeutralForeground3)',
              textTransform: 'uppercase',
              letterSpacing: '0.5px',
            }}
          >
            Generated Files
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '10px' }}>
            {generatedFiles.map((f) => (
              <HtmlFileCard
                key={f.file_id}
                file={f}
                onRemove={onRemoveGeneratedFile}
              />
            ))}
          </div>
        </div>
      )}

      <ChatInput
        value={input}
        onChange={setInput}
        onEnter={() => OnChatSubmit(input)}
        disabledChat={isDisabled}
        placeholder={placeholder}
        style={{
          fontSize: '16px',
          borderRadius: '12px',
          width: '100%',
          boxSizing: 'border-box',
        }}
      >
        {/* Professional Attach Menu */}
        <Menu
          open={menuOpen}
          onOpenChange={(_e, data) => setMenuOpen(data.open)}
        >
          <MenuTrigger disableButtonEnhancement>
            <Button
              appearance="subtle"
              icon={<Attach20Regular />}
              disabled={isDisabled}
              aria-label="Attach files and media"
              style={{
                height: '32px',
                width: '32px',
                borderRadius: '6px',
                backgroundColor: 'transparent',
                border: 'none',
                color: isDisabled
                  ? 'var(--colorNeutralForegroundDisabled)'
                  : 'var(--colorNeutralForeground2)',
                flexShrink: 0,
              }}
            />
          </MenuTrigger>
          <MenuPopover>
            <MenuList>
              <MenuItem
                icon={<DocumentRegular />}
                onClick={() => fileInputRef.current?.click()}
              >
                Add files and documents
              </MenuItem>
              <MenuItem
                icon={<Image20Regular />}
                onClick={() => imageInputRef.current?.click()}
              >
                Add photos and images
              </MenuItem>
              <Divider />
              <MenuItem icon={<FolderRegular />} disabled>
                Recent files
              </MenuItem>
              <Divider />
              <MenuItem icon={<MoreHorizontal20Regular />} disabled>
                More options
              </MenuItem>
            </MenuList>
          </MenuPopover>
        </Menu>

        {/* Chat|Plan selector — the explicit switch that decides the lane:
                     OFF = pure chat (the message can never create a plan);
                     ON  = this message goes to the formal Plan lane. */}
        {showPlanLaneToggle && (
          <Tooltip
            content={
              planLane
                ? 'This message will create a formal multi-agent plan (with your approval).'
                : 'Pure chat: this message can never create a plan.'
            }
            relationship="description"
          >
            <Switch
              checked={planLane}
              onChange={(_e, data) => onPlanLaneChange?.(data.checked)}
              disabled={isDisabled}
              label={planLane ? 'Plan' : 'Chat'}
              style={{ minWidth: '96px' }}
            />
          </Tooltip>
        )}

        {/* Send Button */}
        <Button
          appearance="subtle"
          className="home-input-send-button"
          onClick={() => OnChatSubmit(input)}
          disabled={isDisabled || !input.trim()}
          icon={<Send />}
          aria-label="Send message"
          style={{
            height: '32px',
            width: '32px',
            borderRadius: '6px',
            backgroundColor:
              isDisabled || !input.trim()
                ? 'transparent'
                : 'var(--colorBrandBackground)',
            border: 'none',
            color:
              isDisabled || !input.trim()
                ? 'var(--colorNeutralForegroundDisabled)'
                : 'var(--colorNeutralBackgroundStatic)',
            flexShrink: 0,
            transition: 'all 0.2s ease',
          }}
        />
      </ChatInput>
    </div>
  );
};

export default PlanChatBody;
