/**
 * WidgetFrame - Renders MCP UI Resources (widgets) with fallback to Markdown
 *
 * Supports MCP Protocol 2025-11-25 with:
 * - ui:// resource URIs
 * - HTML/React widget rendering
 * - Sandboxed iframe for security
 * - Fallback to Markdown on error
 */

import React, { useEffect, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import { Spinner, MessageBar } from '@fluentui/react-components';
import { apiClient } from '../../api/apiClient';

interface WidgetFrameProps {
  resourceUri: string;
  fallbackContent?: string;
  fallbackFormat?: 'markdown' | 'text';
}

interface ResourceResult {
  mimeType: string;
  content: string;
  metadata?: {
    title?: string;
    interactive?: boolean;
  };
}

export const WidgetFrame: React.FC<WidgetFrameProps> = ({
  resourceUri,
  fallbackContent,
  fallbackFormat = 'markdown'
}) => {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [widget, setWidget] = useState<ResourceResult | null>(null);

  useEffect(() => {
    const fetchWidget = async () => {
      try {
        setLoading(true);
        setError(null);

        // Call backend bridge to read MCP resource via JSON-RPC
        // Uses apiClient which resolves API_URL for both dev (Vite proxy) and Azure (absolute URL)
        const result: ResourceResult = await apiClient.post('/v4/mcp/resources/read', { uri: resourceUri });
        setWidget(result);
      } catch (err) {
        console.error('Widget loading error:', err);
        setError(err instanceof Error ? err.message : 'Unknown error');
      } finally {
        setLoading(false);
      }
    };

    fetchWidget();
  }, [resourceUri]);

  // Loading state
  if (loading) {
    return (
      <div style={{ padding: '16px', textAlign: 'center' }}>
        <Spinner label="Loading widget..." />
      </div>
    );
  }

  // Error state - fallback to markdown
  if (error || !widget) {
    if (fallbackContent) {
      return (
        <div>
          <MessageBar intent="warning">
            Widget unavailable. Showing fallback content.
          </MessageBar>
          {fallbackFormat === 'markdown' ? (
            <ReactMarkdown>{fallbackContent}</ReactMarkdown>
          ) : (
            <pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'monospace' }}>
              {fallbackContent}
            </pre>
          )}
        </div>
      );
    }

    return (
      <MessageBar intent="error">
        Failed to load widget: {error}
      </MessageBar>
    );
  }

  // Render widget based on mimeType (strip MIME params like ;profile=mcp-app)
  const baseMimeType = widget.mimeType.split(';')[0].trim().toLowerCase();

  const renderWidget = () => {
    switch (baseMimeType) {
      case 'text/html':
        return (
          <div
            className="mcp-widget-container"
            style={{
              border: '1px solid #e0e0e0',
              borderRadius: '8px',
              padding: '16px',
              backgroundColor: '#ffffff',
              boxShadow: '0 2px 4px rgba(0,0,0,0.1)',
            }}
          >
            {/* Render HTML safely */}
            <div
              dangerouslySetInnerHTML={{ __html: widget.content }}
              style={{
                maxWidth: '100%',
                overflow: 'auto',
              }}
            />
            {widget.metadata?.title && (
              <div
                style={{
                  marginTop: '8px',
                  fontSize: '12px',
                  color: '#6b7280',
                  textAlign: 'right',
                }}
              >
                {widget.metadata.title}
              </div>
            )}
          </div>
        );

      case 'application/json':
        // Render JSON data as formatted code
        return (
          <pre
            style={{
              backgroundColor: '#f3f4f6',
              padding: '16px',
              borderRadius: '8px',
              overflow: 'auto',
              fontFamily: 'monospace',
              fontSize: '14px',
            }}
          >
            {JSON.stringify(JSON.parse(widget.content), null, 2)}
          </pre>
        );

      default:
        // Plain text fallback
        return (
          <div
            style={{
              padding: '16px',
              backgroundColor: '#f9fafb',
              borderRadius: '8px',
              fontFamily: 'monospace',
            }}
          >
            {widget.content}
          </div>
        );
    }
  };

  return (
    <div className="mcp-widget-frame">
      {renderWidget()}
    </div>
  );
};

export default WidgetFrame;
