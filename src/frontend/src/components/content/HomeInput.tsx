import {
  Body1Strong,
  Button,
  Caption1,
  Title2,
  Menu,
  MenuTrigger,
  MenuPopover,
  MenuList,
  MenuItem,
  Divider,
} from '@fluentui/react-components';

import React, { useRef, useEffect, useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { resolveApiUrl } from '@/api/config';

import './../../styles/Chat.css';
import '../../styles/prism-material-oceanic.css';
import './../../styles/HomeInput.css';

import { HomeInputProps, iconMap, QuickTask } from '../../models/homeInput';
import { NewTaskService } from '../../services/NewTaskService';
import { ChatService } from '../../services/ChatService';

import ChatInput from '@/coral/modules/ChatInput';
import InlineToaster, { useInlineToaster } from '../toast/InlineToaster';
import PromptCard from '@/coral/components/PromptCard';
import { Send } from '@/coral/imports/bundleicons';
import MicButton from './MicButton';
import {
  isVoiceLiveActive,
  onVoiceBargeIn,
  voiceLiveAck,
  voiceLiveNarrate,
  voiceLiveSpeak,
} from '../../hooks/useVoiceLive';
import {
  Attach20Regular,
  Clipboard20Regular,
  Dismiss20Regular,
  Image20Regular,
  DocumentRegular,
  FolderRegular,
  MoreHorizontal20Regular,
  ArrowDownload20Regular,
} from '@fluentui/react-icons';
import { apiService } from '../../api/apiService';
import { useAppDispatch, useAppSelector } from '../../store/hooks';
import {
  addUserMessage,
  initAssistantMessage,
  startStreaming,
  addStreamToken,
  finishStreaming,
  setSessionId,
  setSubmittingDisabled,
  selectSessionId,
} from '../../store/slices/chatSlice';

// Icon mapping function to convert string icons to FluentUI icons
const getIconFromString = (
  iconString: string | React.ReactNode
): React.ReactNode => {
  // If it's already a React node, return it
  if (typeof iconString !== 'string') {
    return iconString;
  }

  return iconMap[iconString] || iconMap['default'] || <Clipboard20Regular />;
};

const truncateDescription = (
  description: string,
  maxLength: number = 180
): string => {
  if (!description) return '';

  if (description.length <= maxLength) {
    return description;
  }

  const truncated = description.substring(0, maxLength);
  const lastSpaceIndex = truncated.lastIndexOf(' ');

  const cutPoint = lastSpaceIndex > maxLength - 20 ? lastSpaceIndex : maxLength;

  return description.substring(0, cutPoint) + '...';
};

// Extended QuickTask interface to store both truncated and full descriptions
interface ExtendedQuickTask extends QuickTask {
  fullDescription: string; // Store the full, untruncated description
}

const HomeInput: React.FC<HomeInputProps> = ({ selectedTeam }) => {
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [input, setInput] = useState<string>('');
  const [attachedFiles, setAttachedFiles] = useState<
    Array<{ name: string; file_id: string }>
  >([]);
  const [generatedFiles, setGeneratedFiles] = useState<
    Array<{ file_id: string; filename: string; download_url: string }>
  >([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const dispatch = useAppDispatch();
  const currentSessionId = useAppSelector(selectSessionId);

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const result = await apiService.uploadChatFile(file);
      setAttachedFiles((prev) => [
        ...prev,
        { name: file.name, file_id: result.file_id },
      ]);
    } catch (err) {
      console.error('File upload failed:', err);
    }
    // Reset input so the same file can be re-selected
    if (fileInputRef.current) fileInputRef.current.value = '';
    setMenuOpen(false);
  };

  const handleImageSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const result = await apiService.uploadChatFile(file);
      setAttachedFiles((prev) => [
        ...prev,
        { name: file.name, file_id: result.file_id },
      ]);
    } catch (err) {
      console.error('Image upload failed:', err);
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

  const removeAttachedFile = (file_id: string) => {
    setAttachedFiles((prev) => prev.filter((f) => f.file_id !== file_id));
  };

  const MAX_INPUT_CHARS = 5000;

  const handlePaste = async (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    const pasted = e.clipboardData.getData('text');
    const combined = input + pasted;
    if (combined.length <= MAX_INPUT_CHARS) return; // normal paste, browser handles it

    // Exceeds limit — prevent default truncation and attach as .txt instead
    e.preventDefault();
    const filename = `pasted-text-${Date.now()}.txt`;
    const file = new File([combined], filename, { type: 'text/plain' });
    try {
      const result = await apiService.uploadChatFile(file);
      setAttachedFiles((prev) => [
        ...prev,
        { name: filename, file_id: result.file_id },
      ]);
      // Keep only what fits in the textarea (first 5000 chars of the combined text)
      setInput(combined.slice(0, MAX_INPUT_CHARS));
    } catch (err) {
      console.error('Auto-attach of pasted text failed:', err);
      // Fallback: just truncate silently
      setInput(combined.slice(0, MAX_INPUT_CHARS));
    }
  };

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const navigate = useNavigate();
  const location = useLocation(); // ✅ location.state used to control focus
  const { showToast, dismissToast } = useInlineToaster();

  // Check if the selected team is the Contract Compliance Review Team
  const isLegalTeam = selectedTeam?.name
    ?.toLowerCase()
    .includes('contract compliance');

  useEffect(() => {
    if (location.state?.focusInput) {
      textareaRef.current?.focus();
    }
  }, [location]);

  const resetTextarea = () => {
    setInput('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.focus();
    }
  };

  useEffect(() => {
    const cleanup = NewTaskService.addResetListener(resetTextarea);
    return cleanup;
  }, []);

  // Un solo stream en vuelo por composer. Un nuevo envío (tecleado o por voz) o
  // un barge-in abortan el anterior ANTES de abrir otra burbuja: así los tokens
  // del turno viejo nunca caen en el mensaje assistant del turno nuevo.
  const streamAbortRef = useRef<AbortController | null>(null);
  const abortInFlightStream = () => {
    if (streamAbortRef.current) {
      streamAbortRef.current.abort();
      streamAbortRef.current = null;
    }
  };
  useEffect(() => {
    const unsubscribe = onVoiceBargeIn(() => abortInFlightStream());
    return () => {
      unsubscribe();
      abortInFlightStream();
    };
  }, []);

  const handleSubmit = async (overrideMessage?: string) => {
    const messageToSend = overrideMessage || input.trim();
    if (messageToSend) {
      setSubmitting(true);
      dispatch(setSubmittingDisabled(true));
      let id = showToast('Analyzing your request…', 'progress');

      try {
        // Send through streaming endpoint — same FoundryAgent from message 1.
        // For task intent, the stream returns a redirect event.
        // For conversational/mcp, it streams the real response.
        const randomValues = crypto.getRandomValues(new Uint32Array(1));
        const sessionId =
          currentSessionId || `chat_${Date.now()}_${randomValues[0]}`;
        const userMessage = messageToSend;
        const fileIds = attachedFiles.map((f) => f.file_id);

        // Transición explícita de turno: cerrar el stream anterior (si sigue vivo)
        // antes de crear la burbuja nueva.
        abortInFlightStream();
        const abort = new AbortController();
        streamAbortRef.current = abort;
        const voiceTurn = isVoiceLiveActive();

        dispatch(setSessionId(sessionId));
        // Dispatch user message to Redux
        dispatch(addUserMessage(userMessage));
        // Initialize assistant message placeholder
        dispatch(initAssistantMessage());
        dispatch(startStreaming());

        // Carril 1 — acuse hablado inmediato (plantilla, TTS literal). Mata el
        // silencio mientras el router clasifica / llama tools.
        if (voiceTurn) voiceLiveAck();

        if (!overrideMessage) {
          setInput('');
          setAttachedFiles([]);
          if (textareaRef.current) {
            textareaRef.current.style.height = 'auto';
          }
        }

        let redirectPlan: string | null = null;
        let intent = '';
        let fullResponse = '';
        const collectedFiles: Array<{
          file_id: string;
          filename: string;
          download_url: string;
        }> = [];

        await ChatService.sendMessageStream(
          userMessage,
          sessionId,
          {
            onToken: (token) => {
              fullResponse += token;
              dispatch(addStreamToken(token));
            },
            onIntent: (data) => {
              intent = data.intent;
              if (data.session_id) {
                dispatch(setSessionId(data.session_id));
              }
            },
            onDone: (data) => {
              intent = data.intent;
            },
            onRedirect: (planId) => {
              redirectPlan = planId;
            },
            onPlanCreated: (planId) => {
              redirectPlan = planId;
            },
            onError: (errorMsg) => {
              fullResponse = `Error: ${errorMsg}`;
              dispatch(finishStreaming({ metadata: { intent } }));
            },
            onGeneratedFile: (f) => {
              collectedFiles.push(f);
            },
            // Carril 2 — narración de la tool en el momento ("Consultando X…").
            onToolActivity: (data) => {
              if (voiceTurn && data.activity === 'calling')
                voiceLiveNarrate(data.tool, data.server);
            },
            onOAuthConsentRequest: (consentLink) => {
              // NOTE: do NOT pass 'noopener' — with it window.open() returns null
              // (per spec), so popup.closed polling never runs and the auto-retry
              // after consent approval is silently skipped (user had to re-send).
              const popup = window.open(
                consentLink,
                'oauth_consent',
                'width=620,height=720'
              );
              if (popup) {
                const timer = setInterval(() => {
                  if (popup.closed) {
                    clearInterval(timer);
                    // Retry the same message now that the user has approved
                    handleSubmit(userMessage);
                  }
                }, 500);
              }
            },
          },
          fileIds,
          undefined, // planId — no aplicable desde este carril
          undefined, // allowPlan — usa default
          typeof window !== 'undefined'
            ? window.localStorage.getItem('macae_active_workspace_id')
            : undefined,
          abort.signal
        );

        const aborted = abort.signal.aborted;
        if (streamAbortRef.current === abort) streamAbortRef.current = null;

        if (collectedFiles.length > 0) {
          setGeneratedFiles(collectedFiles);
        }

        dispatch(
          finishStreaming({
            metadata: { intent, generatedFiles: collectedFiles, fullResponse },
          })
        );
        // Carril 3 — contenido final del router (parafraseo). Sólo si el turno
        // sigue vivo: un barge-in ya lo canceló y esta respuesta no debe hablar.
        if (!aborted && isVoiceLiveActive() && fullResponse) {
          voiceLiveSpeak(fullResponse);
        }
        dismissToast(id);

        if (aborted) {
          // Turno interrumpido por el usuario: burbuja cerrada con lo recibido,
          // sin navegación ni toasts.
          return;
        }

        if (redirectPlan) {
          showToast('Plan created!', 'success');
          navigate(`/plan/${redirectPlan}`);
        } else {
          // CONVERSATIONAL / MCP stays on HomePage; HomePage renders Chat from Redux messages.
        }
      } catch (error: any) {
        console.log('Error processing message:', error);
        let errorMessage = 'Unable to process message. Please try again.';
        dismissToast(id);
        try {
          errorMessage = error?.message || errorMessage;
        } catch (parseError) {
          console.error('Error parsing error detail:', parseError);
        }
        showToast(errorMessage, 'error');
      } finally {
        setInput('');
        setSubmitting(false);
        dispatch(setSubmittingDisabled(false));
      }
    }
  };

  const handleQuickTaskClick = (task: ExtendedQuickTask) => {
    setInput(task.fullDescription);
    if (textareaRef.current) {
      textareaRef.current.focus();
    }
  };

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      textareaRef.current.style.height = `${textareaRef.current.scrollHeight}px`;
    }
  }, [input]);

  // Convert team starting_tasks to ExtendedQuickTask format
  const tasksToDisplay: ExtendedQuickTask[] =
    selectedTeam && selectedTeam.starting_tasks
      ? selectedTeam.starting_tasks.map((task, index) => {
          // Handle both string tasks and StartingTask objects
          if (typeof task === 'string') {
            return {
              id: `team-task-${index}`,
              title: task,
              description: truncateDescription(task),
              fullDescription: task, // Store the full description
              icon: getIconFromString('📋'),
            };
          } else {
            // Handle StartingTask objects
            const startingTask = task as any; // Type assertion for now
            const taskDescription =
              startingTask.prompt || startingTask.name || 'Task description';
            return {
              id: startingTask.id || `team-task-${index}`,
              title: startingTask.name || startingTask.prompt || 'Task',
              description: truncateDescription(taskDescription),
              fullDescription: taskDescription, // Store the full description
              icon: getIconFromString(startingTask.logo || '📋'),
            };
          }
        })
      : [];

  return (
    <div className="home-input-container">
      <div className="home-input-content">
        <div className="home-input-center-content">
          <div className="home-input-title-wrapper">
            <Title2>How can I help you?</Title2>
          </div>

          {/* Legal Disclaimer for Contract Compliance Review Team */}
          {isLegalTeam && (
            <div
              style={{
                color: 'var(--colorNeutralForeground3)',
                marginTop: '8px',
                paddingBottom: '8px',
                textAlign: 'center',
              }}
            >
              <Caption1>
                <strong>Disclaimer:</strong> This tool is not intended to give
                legal advice; it is intended solely for the purpose of assessing
                contract compliance against internal guidance and policy
                frameworks.
              </Caption1>
            </div>
          )}

          {/* Show RAI error if present */}
          {/* {raiError && (
                        <RAIErrorCard
                            error={raiError}
                            onRetry={() => {
                                setRAIError(null);
                                if (textareaRef.current) {
                                    textareaRef.current.focus();
                                }
                            }}
                            onDismiss={() => setRAIError(null)}
                        />
                    )} */}

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
                    e.currentTarget.style.boxShadow =
                      '0 2px 4px rgba(0,0,0,0.08)';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.backgroundColor =
                      'var(--colorNeutralBackground2)';
                    e.currentTarget.style.boxShadow =
                      '0 1px 2px rgba(0,0,0,0.05)';
                  }}
                >
                  <span style={{ fontSize: '18px' }}>
                    {getFileIcon(f.name)}
                  </span>
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
                    onClick={() => removeAttachedFile(f.file_id)}
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

          <ChatInput
            ref={textareaRef} // forwarding
            value={input}
            placeholder="Tell us what needs planning, building, or connecting—we'll handle the rest."
            onChange={setInput}
            onEnter={handleSubmit}
            onPaste={handlePaste}
            disabledChat={submitting}
          >
            {/* Hidden file inputs */}
            <input
              ref={fileInputRef}
              type="file"
              accept=".csv,.xlsx,.json,.txt,.pdf,.doc,.docx,.zip,.rar"
              style={{ display: 'none' }}
              onChange={handleFileSelect}
            />
            <input
              ref={imageInputRef}
              type="file"
              accept="image/*"
              style={{ display: 'none' }}
              onChange={handleImageSelect}
            />

            {/* Professional Attach Menu */}
            <Menu
              open={menuOpen}
              onOpenChange={(_e, data) => setMenuOpen(data.open)}
            >
              <MenuTrigger disableButtonEnhancement>
                <Button
                  appearance="subtle"
                  onClick={() => {}}
                  disabled={submitting}
                  icon={<Attach20Regular />}
                  aria-label="Attach files and media"
                  style={{
                    height: '32px',
                    width: '32px',
                    borderRadius: '6px',
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

            <MicButton
              mode="voicelive"
              disabled={submitting}
              onUserTranscript={(t) => handleSubmit(t)}
            />
            <MicButton
              mode="dictation"
              disabled={submitting}
              onTranscript={(t) => setInput((v) => v + t)}
            />

            <Button
              appearance="subtle"
              className="home-input-send-button"
              onClick={() => handleSubmit()}
              disabled={submitting}
              icon={<Send />}
              aria-label="Send message"
              style={{
                height: '32px',
                width: '32px',
                borderRadius: '6px',
                backgroundColor: submitting
                  ? 'transparent'
                  : 'var(--colorBrandBackground)',
                color: submitting
                  ? 'var(--colorNeutralForegroundDisabled)'
                  : 'var(--colorNeutralBackgroundStatic)',
              }}
            />
          </ChatInput>

          <InlineToaster />

          {/* Generated files panel - Professional download cards */}
          {generatedFiles.length > 0 && (
            <div style={{ marginTop: '12px', marginBottom: '12px' }}>
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
                  <a
                    key={f.file_id}
                    href={resolveApiUrl(f.download_url)}
                    download={f.filename}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: '10px',
                      padding: '10px 14px',
                      backgroundColor: 'var(--colorBrandBackground2)',
                      borderRadius: '8px',
                      fontSize: '13px',
                      fontWeight: 500,
                      textDecoration: 'none',
                      color: 'var(--colorNeutralForeground1)',
                      border: '1px solid var(--colorBrandStroke1)',
                      boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
                      transition: 'all 0.2s ease',
                    }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.backgroundColor =
                        'var(--colorBrandBackgroundHover)';
                      e.currentTarget.style.boxShadow =
                        '0 2px 4px rgba(0,0,0,0.1)';
                      e.currentTarget.style.transform = 'translateY(-1px)';
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.backgroundColor =
                        'var(--colorBrandBackground2)';
                      e.currentTarget.style.boxShadow =
                        '0 1px 2px rgba(0,0,0,0.05)';
                      e.currentTarget.style.transform = 'translateY(0)';
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
                      {f.filename}
                    </span>
                  </a>
                ))}
              </div>
            </div>
          )}

          <div className="home-input-quick-tasks-section">
            {tasksToDisplay.length > 0 && (
              <>
                <div className="home-input-quick-tasks-header">
                  <Body1Strong>Quick tasks</Body1Strong>
                </div>

                <div className="home-input-quick-tasks">
                  <div>
                    {tasksToDisplay.map((task) => (
                      <PromptCard
                        key={task.id}
                        title={task.title}
                        icon={task.icon}
                        description={task.description}
                        onClick={() => handleQuickTaskClick(task)}
                        disabled={submitting}
                      />
                    ))}
                  </div>
                </div>
              </>
            )}
            {tasksToDisplay.length === 0 && selectedTeam && (
              <div
                style={{
                  textAlign: 'center',
                  padding: '32px 16px',
                  color: '#666',
                }}
              >
                <Caption1>No starting tasks available for this team</Caption1>
              </div>
            )}
            {!selectedTeam && (
              <div
                style={{
                  textAlign: 'center',
                  padding: '32px 16px',
                  color: '#666',
                }}
              >
                <Caption1>Select a team to see available tasks</Caption1>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default HomeInput;
