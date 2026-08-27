import React, {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import { useParams, useNavigate, useSearchParams } from 'react-router-dom';
import { Spinner, Text } from '@fluentui/react-components';
import { PlanDataService } from '../services/PlanDataService';
import {
  ProcessedPlanData,
  WebsocketMessageType,
  MPlanData,
  AgentMessageData,
  AgentMessageType,
  ParsedUserClarification,
  AgentType,
  PlanStatus,
  TeamConfig,
  StreamMessage,
} from '../models';
import PlanChat from '../components/content/PlanChat';
import PlanPanelLeft from '../components/content/PlanPanelLeft';
import CoralShellColumn from '../coral/components/Layout/CoralShellColumn';
import CoralShellRow from '../coral/components/Layout/CoralShellRow';
import Content from '../coral/components/Content/Content';
import ContentToolbar from '../coral/components/Content/ContentToolbar';
import InspectorLink from '@/components/inspector/InspectorLink';
import { useInlineToaster } from '../components/toast/InlineToaster';
import Octo from '../coral/imports/Octopus.png';
import LoadingMessage, {
  loadingMessages,
} from '../coral/components/LoadingMessage';
import webSocketService from '../services/WebSocketService';
import { apiService } from '../api/apiService';
import { ChatService } from '../services/ChatService';
import { usePlanCancellationAlert } from '../hooks/usePlanCancellationAlert';
import PlanCancellationDialog from '../components/common/PlanCancellationDialog';
import {
  HtmlPreviewProvider,
  PreviewRightSlot,
} from '../components/content/HtmlPreview';
import '../styles/PlanPage.css';
import { useAppDispatch, useAppSelector } from '../store/hooks';
import {
  selectPlanData,
  selectApprovalRequest,
  selectProcessingApproval,
  setPlanData,
  setApprovalRequest,
  setProcessingApproval,
} from '../store/slices/planSlice';

// Interruptor chat|plan: the plan tree (right panel) is code-split out of the
// initial bundle and only downloaded/mounted once a real plan exists.
const PlanPanelRight = React.lazy(
  () => import('../components/content/PlanPanelRight')
);

/**
 * Page component for displaying a specific plan
 * Accessible via the route /plan/{plan_id}
 */
const PlanPage: React.FC = () => {
  const { planId: routePlanId, sessionId: routeSessionId } = useParams<{
    planId?: string;
    sessionId?: string;
  }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryPlanId = searchParams.get('planId');
  const planId = queryPlanId || routePlanId;
  const navigate = useNavigate();
  const { showToast, dismissToast } = useInlineToaster();

  // Active workspace from WorkspaceSelector — reactive so switching in the
  // left panel immediately re-points Monaco and MCP at the new path.
  const [activeWorkspaceOverride, setActiveWorkspaceOverride] = useState<
    string | null
  >(() => localStorage.getItem('macae_active_workspace_id'));

  // Redux hooks for plan state management
  const dispatch = useAppDispatch();
  const planData = useAppSelector(selectPlanData);
  const planApprovalRequest = useAppSelector(selectApprovalRequest);
  const processingApproval = useAppSelector(selectProcessingApproval);

  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const [input, setInput] = useState<string>('');
  const [loading, setLoading] = useState<boolean>(true);
  const [submittingChatDisableInput, setSubmittingChatDisableInput] =
    useState<boolean>(true);
  const [errorLoading, setErrorLoading] = useState<boolean>(false);
  const [clarificationMessage, setClarificationMessage] =
    useState<ParsedUserClarification | null>(null);
  const [reloadLeftList, setReloadLeftList] = useState<boolean>(true);
  // waitingForPlan is ONLY true when a plan is being loaded/created.
  // Sessions without a planId must never trigger the "Creating your plan..." spinner.
  const [waitingForPlan, setWaitingForPlan] = useState<boolean>(!!planId);
  // Chat|Plan selector (the interruptor). OFF = pure chat: the stream carries
  // allow_plan=false, so a chat message can NEVER create a plan. ON = the next
  // message goes to the formal lane (/process_request) explicitly — the
  // "future UI selector" the backend anticipates. One-shot: resets to Chat
  // after firing so a stray second message never clones another plan.
  const [planLane, setPlanLane] = useState<boolean>(false);
  // Return-to-chat is STATE, not routes (navigate()-based returns are
  // rejected: they remount and refetch). When the final arrives on
  // /plan/:id, the SAME canvas flips back to chat where it already is.
  const [planClosed, setPlanClosed] = useState<boolean>(false);
  const [closedSessionId, setClosedSessionId] = useState<string>('');
  // Instant the plan ended: anchors the result summary in the timeline so the
  // Hosted Agent's later turns render below it instead of pushing it down.
  const [bufferAt, setBufferAt] = useState<number>(0);
  // Interruptor chat|plan: without a planId this page IS chat — every piece of
  // plan machinery (WS subscriptions below, right panel) stays off until a real
  // plan exists; when the plan completes, planClosed flips it back off.
  const isPlanMode = Boolean(planId) && !planClosed;
  const [showProcessingPlanSpinner, setShowProcessingPlanSpinner] =
    useState<boolean>(false);
  const [showApprovalButtons, setShowApprovalButtons] = useState<boolean>(true);
  const [continueWithWebsocketFlow, setContinueWithWebsocketFlow] =
    useState<boolean>(false);
  const [selectedTeam, setSelectedTeam] = useState<TeamConfig | null>(null);
  const [streamingMessageBuffer, setStreamingMessageBuffer] =
    useState<string>('');
  const [showBufferingText, setShowBufferingText] = useState<boolean>(false);
  const [agentMessages, setAgentMessages] = useState<AgentMessageData[]>([]);
  const [attachedFiles, setAttachedFiles] = useState<
    Array<{ name: string; file_id: string }>
  >([]);
  const [generatedFiles, setGeneratedFiles] = useState<
    Array<{ file_id: string; filename: string; download_url: string }>
  >([]);
  // Accumulates WS streaming buffer content for final processAgentMessage call
  const wsStreamingBufferRef = useRef<string>('');
  const formatErrorMessage = useCallback((content: string): string => {
    // Split content by newlines and add proper indentation
    const lines = content.split('\n');
    const formattedLines = lines.map((line, index) => {
      if (index === 0) {
        return `⚠️ ${line}`;
      } else if (line.trim() === '') {
        return ''; // Preserve blank lines
      } else {
        return `&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;${line}`;
      }
    });
    return formattedLines.join('\n');
  }, []);

  const handleFileSelect = useCallback(
    async (file: File) => {
      try {
        const result = await apiService.uploadChatFile(file);
        setAttachedFiles((prev) => [
          ...prev,
          { name: file.name, file_id: result.file_id },
        ]);
        showToast(`File "${file.name}" uploaded successfully`, 'success');
      } catch (err) {
        console.error('File upload failed:', err);
        showToast('File upload failed. Please try again.', 'error');
      }
    },
    [showToast]
  );

  const removeAttachedFile = useCallback((file_id: string) => {
    setAttachedFiles((prev) => prev.filter((f) => f.file_id !== file_id));
  }, []);

  const removeGeneratedFile = useCallback((file_id: string) => {
    setGeneratedFiles((prev) => prev.filter((f) => f.file_id !== file_id));
  }, []);

  // Plan cancellation dialog state
  const [showCancellationDialog, setShowCancellationDialog] =
    useState<boolean>(false);
  const [pendingNavigation, setPendingNavigation] = useState<
    (() => void) | null
  >(null);
  const [cancellingPlan, setCancellingPlan] = useState<boolean>(false);

  const [loadingMessage, setLoadingMessage] = useState<string>(
    loadingMessages[0]
  );

  // Plan cancellation alert hook
  const { isPlanActive } = usePlanCancellationAlert({
    planData,
    planApprovalRequest,
    onNavigate: pendingNavigation || (() => {}),
  });

  // Handle navigation with plan cancellation check
  const handleNavigationWithAlert = useCallback(
    (navigationFn: () => void) => {
      if (!isPlanActive()) {
        // Plan is not active, proceed with navigation
        navigationFn();
        return;
      }

      // Plan is active, show confirmation dialog
      setPendingNavigation(() => navigationFn);
      setShowCancellationDialog(true);
    },
    [isPlanActive]
  );

  // Handle confirmation dialog response
  const handleConfirmCancellation = useCallback(async () => {
    setCancellingPlan(true);

    try {
      if (planApprovalRequest?.id) {
        await apiService.approvePlan({
          m_plan_id: planApprovalRequest.id,
          plan_id: planData?.plan?.id,
          approved: false,
          feedback: 'Plan cancelled by user navigation',
        });
      }

      // Limpiar el estado del plan en Redux
      dispatch(setPlanData({ planId: planData?.plan?.id || '', data: null }));
      dispatch(setApprovalRequest(null));

      // Si estamos en /session/:sessionId?planId=..., solo remover planId
      if (routeSessionId && queryPlanId) {
        setSearchParams({});
      } else if (pendingNavigation) {
        // Execute the pending navigation para otros casos
        pendingNavigation();
      }
      webSocketService.disconnect();
    } catch (error) {
      console.error('❌ Failed to cancel plan:', error);
      showToast(
        'Failed to cancel the plan properly, but navigation will continue.',
        'error'
      );

      // Limpiar el estado del plan incluso si la cancelación falló
      dispatch(setPlanData({ planId: planData?.plan?.id || '', data: null }));
      dispatch(setApprovalRequest(null));

      // Still proceed with navigation even if cancellation failed
      if (routeSessionId && queryPlanId) {
        setSearchParams({});
      } else if (pendingNavigation) {
        pendingNavigation();
      }
    } finally {
      setCancellingPlan(false);
      setShowCancellationDialog(false);
      setPendingNavigation(null);
    }
  }, [
    planApprovalRequest,
    planData,
    pendingNavigation,
    routeSessionId,
    queryPlanId,
    setSearchParams,
    showToast,
    dispatch,
  ]);

  const handleCancelDialog = useCallback(() => {
    setShowCancellationDialog(false);
    setPendingNavigation(null);
  }, []);

  const processAgentMessage = useCallback(
    (
      _agentMessageData: AgentMessageData,
      _planData: ProcessedPlanData,
      is_final: boolean = false,
      _streaming_message: string = ''
    ) => {
      // SINGLE WRITER: the backend persists every plan utterance itself
      // (_persist_agent_message + final write-back). The old POST to
      // /agent_message here was a second writer re-persisting the SAME
      // utterance (fingerprinted in Cosmos: duplicate contents ~0.5s apart
      // with a {"type":"agent_message",...} raw shape). Do NOT echo.
      if (is_final) {
        // Refresh the task list once the backend has settled the final state.
        setTimeout(() => {
          setReloadLeftList(true);
        }, 1000);
      }
      return Promise.resolve();
    },
    [setReloadLeftList]
  );

  const resetPlanVariables = useCallback(() => {
    setInput('');
    dispatch(setPlanData({ planId: planId || '', data: null }));
    dispatch(setProcessingApproval(false));
    dispatch(setApprovalRequest(null));
    setLoading(true);
    setSubmittingChatDisableInput(true);
    setErrorLoading(false);
    setClarificationMessage(null);
    setReloadLeftList(true);
    // Mirror the planId guard: only signal "waiting for plan" when there IS a plan to wait for.
    setWaitingForPlan(!!planId);
    setPlanClosed(false);
    setClosedSessionId('');
    setBufferAt(0);
    setShowProcessingPlanSpinner(false);
    setShowApprovalButtons(true);
    setContinueWithWebsocketFlow(false);
    setStreamingMessageBuffer('');
    setShowBufferingText(false);
    setAgentMessages([]);
    wsStreamingBufferRef.current = '';
  }, [dispatch, planId]);

  // Auto-scroll helper.
  // requestAnimationFrame, NOT requestIdleCallback: Safari/WebKit never
  // implemented rIC, so a bare call throws ReferenceError — it killed every
  // mobile send right after the optimistic bubble (message painted, POST
  // never fired). rAF exists in every engine and fires pre-paint.
  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    requestAnimationFrame(() => {
      messagesContainerRef.current?.scrollTo({
        top: messagesContainerRef.current.scrollHeight,
        behavior,
      });
    });
  }, []);

  // Initial scroll on load/refresh.
  // loadPlanData / loadSessionHistory populate agentMessages while loading=true,
  // and PlanChat (which owns the scroll container) is unmounted during that time,
  // so messagesContainerRef.current is null and any scroll there is a no-op.
  // Wait until loading finishes (container mounted) and then jump to the last
  // message exactly once per load — subsequent appends are handled by the
  // WebSocket/send handlers that call scrollToBottom themselves.
  // Chat identity: also keys PlanChat so a session switch replaces the whole
  // message tree instead of mutating the old one in place.
  const chatKey = routeSessionId || routePlanId || 'new';
  const didInitialScrollRef = useRef('');
  useLayoutEffect(() => {
    if (loading) {
      // New load in flight — re-arm the one-shot for when it completes.
      didInitialScrollRef.current = '';
      return;
    }
    // One-shot PER CHAT: on a session switch the remounted container briefly
    // holds the previous chat's messages (state clears on the next commit);
    // a boolean one-shot skips that commit and it paints at the top.
    if (didInitialScrollRef.current === chatKey) return;
    if (agentMessages.length === 0) return;
    const el = messagesContainerRef.current;
    if (!el) return; // container not mounted yet
    didInitialScrollRef.current = chatKey;
    // Layout effect + direct assignment: runs after the DOM commit but BEFORE
    // paint, so the frame with the full history is never presented at the top.
    // scrollToBottom (requestAnimationFrame) runs after paint — too late here.
    el.scrollTop = el.scrollHeight;
  }, [loading, planData, agentMessages.length, chatKey]);

  //WebsocketMessageType.PLAN_APPROVAL_REQUEST
  useEffect(() => {
    if (!isPlanMode) return;
    const unsubscribe = webSocketService.on(
      WebsocketMessageType.PLAN_APPROVAL_REQUEST,
      (approvalRequest: any) => {
        console.log('📋 Plan received:', approvalRequest);

        let mPlanData: MPlanData | null = null;

        // Handle the different message structures
        if (approvalRequest.parsedData) {
          // Direct parsedData property
          mPlanData = approvalRequest.parsedData;
        } else if (
          approvalRequest.data &&
          typeof approvalRequest.data === 'object'
        ) {
          // Data property with nested object
          if (approvalRequest.data.parsedData) {
            mPlanData = approvalRequest.data.parsedData;
          } else {
            // Try to parse the data object directly
            mPlanData = approvalRequest.data;
          }
        } else if (approvalRequest.rawData) {
          // Parse the raw data string
          mPlanData = PlanDataService.parsePlanApprovalRequest(
            approvalRequest.rawData
          );
        } else {
          // Try to parse the entire object
          mPlanData = PlanDataService.parsePlanApprovalRequest(approvalRequest);
        }

        if (mPlanData) {
          console.log('✅ Parsed plan data:', mPlanData);
          dispatch(setApprovalRequest(mPlanData));
          setWaitingForPlan(false);
          setShowProcessingPlanSpinner(false);
          scrollToBottom();
        } else {
          console.error('❌ Failed to parse plan data', approvalRequest);
        }
      }
    );

    return () => unsubscribe();
  }, [isPlanMode, scrollToBottom, dispatch]);

  //(WebsocketMessageType.AGENT_MESSAGE_STREAMING
  useEffect(() => {
    if (!isPlanMode) return;
    const unsubscribe = webSocketService.on(
      WebsocketMessageType.AGENT_MESSAGE_STREAMING,
      (streamingMessage: any) => {
        const line = PlanDataService.simplifyHumanClarification(
          streamingMessage.data.content
        );
        wsStreamingBufferRef.current += line;
        setShowBufferingText(true);
        setStreamingMessageBuffer((prev) => prev + line);
      }
    );

    return () => unsubscribe();
  }, [isPlanMode]);

  //WebsocketMessageType.USER_CLARIFICATION_REQUEST
  useEffect(() => {
    if (!isPlanMode) return;
    const unsubscribe = webSocketService.on(
      WebsocketMessageType.USER_CLARIFICATION_REQUEST,
      (clarificationMessage: any) => {
        console.log('📋 Clarification Message', clarificationMessage);
        if (!clarificationMessage) {
          console.warn(
            '⚠️ clarification message missing data:',
            clarificationMessage
          );
          return;
        }
        const agentMessageData = {
          agent: AgentType.GROUP_CHAT_MANAGER,
          agent_type: AgentMessageType.AI_AGENT,
          timestamp: clarificationMessage.timestamp || Date.now(),
          steps: [],
          next_steps: [],
          content: clarificationMessage.data.question || '',
          raw_data: clarificationMessage.data || '',
        } as AgentMessageData;
        setClarificationMessage(
          clarificationMessage.data as ParsedUserClarification | null
        );
        setAgentMessages((prev) => [...prev, agentMessageData]);
        setShowBufferingText(false);
        setStreamingMessageBuffer('');
        wsStreamingBufferRef.current = '';
        setShowProcessingPlanSpinner(false);
        setSubmittingChatDisableInput(false);
        scrollToBottom();
        processAgentMessage(agentMessageData, planData);
      }
    );

    return () => unsubscribe();
  }, [isPlanMode, scrollToBottom, planData, processAgentMessage]);
  //WebsocketMessageType.AGENT_TOOL_MESSAGE
  useEffect(() => {
    if (!isPlanMode) return;
    const unsubscribe = webSocketService.on(
      WebsocketMessageType.AGENT_TOOL_MESSAGE,
      (toolMessage: any) => {
        console.log('📋 Tool Message', toolMessage);
      }
    );

    return () => unsubscribe();
  }, [isPlanMode]);

  //WebsocketMessageType.FINAL_RESULT_MESSAGE
  useEffect(() => {
    if (!isPlanMode) return;
    const unsubscribe = webSocketService.on(
      WebsocketMessageType.FINAL_RESULT_MESSAGE,
      (finalMessage: any) => {
        console.log('📋 Final Result Message', finalMessage);
        if (!finalMessage) {
          console.warn('⚠️ Final result message missing data:', finalMessage);
          return;
        }
        // parseFinalResultMessage returns a FLAT shape { content, status, ... } —
        // there is NO `.data` wrapper here. Reading finalMessage.data.status made
        // the completion gate below always undefined → the spinner never cleared,
        // the plan never flipped to COMPLETED in the UI, and the URL never reset.
        // Read defensively (flat first, nested as fallback) so it works either way.
        const finalContent =
          finalMessage?.content ?? finalMessage?.data?.content ?? '';
        const finalStatus = finalMessage?.status ?? finalMessage?.data?.status;
        // The consolidated result is THE answer, not a duplicate of the agents'
        // intermediate turns — it used to be dropped whenever any agent had
        // spoken, so the synthesis never reached the screen (it only survived
        // in Cosmos). Render it; the raw per-agent turns are the trace and live
        // in the collapsible. Only guard against the SAME text arriving twice.
        if (finalContent.trim()) {
          setAgentMessages((prev) => {
            const already = prev.some(
              (m) => (m.content || '').trim() === finalContent.trim()
            );
            if (already) return prev;
            const finalMsg = {
              agent: AgentType.GROUP_CHAT_MANAGER,
              agent_type: AgentMessageType.AI_AGENT,
              timestamp: Date.now(),
              steps: [],
              next_steps: [],
              content: finalContent,
              raw_data: finalMessage,
            } as AgentMessageData;
            return [...prev, finalMsg];
          });
        }
        if (finalStatus === PlanStatus.COMPLETED) {
          setShowBufferingText(true);
          setBufferAt(Date.now());
          setShowProcessingPlanSpinner(false);
          setSelectedTeam(planData?.team || null);
          setSubmittingChatDisableInput(false);
          scrollToBottom();

          // Actualizar el plan a COMPLETED en el estado
          if (planData?.plan) {
            const updatedPlanData = {
              ...planData,
              plan: { ...planData.plan, overall_status: PlanStatus.COMPLETED },
            };
            dispatch(
              setPlanData({ planId: planId || '', data: updatedPlanData })
            );
          }

          webSocketService.disconnect();
          const capturedBuffer = wsStreamingBufferRef.current;
          wsStreamingBufferRef.current = '';
          // processAgentMessage for side-effects (plan state), no duplicate push needed
          const dummyMsg = {
            agent: AgentType.GROUP_CHAT_MANAGER,
            agent_type: AgentMessageType.AI_AGENT,
            timestamp: Date.now(),
            steps: [],
            next_steps: [],
            content: finalContent,
            raw_data: finalMessage,
          } as AgentMessageData;
          processAgentMessage(dummyMsg, planData, true, capturedBuffer);

          // Eliminar planId de la URL y limpiar el estado del plan cuando se completa
          if (routeSessionId && queryPlanId) {
            console.log(
              '🧹 Plan completed - removing planId from URL and clearing plan state'
            );
            setSearchParams({});
            // Limpiar el estado del plan en Redux para que no se envíe en futuros mensajes
            setTimeout(() => {
              dispatch(setPlanData({ planId: planId || '', data: null }));
              dispatch(setApprovalRequest(null));
            }, 500);
          } else if (routePlanId) {
            // Estado, no rutas: el mismo lienzo vuelve a chat donde ya está.
            setClosedSessionId(planData?.plan?.session_id || '');
            setPlanClosed(true);
            setTimeout(() => {
              dispatch(setPlanData({ planId: planId || '', data: null }));
              dispatch(setApprovalRequest(null));
            }, 500);
          }
        }
      }
    );

    return () => unsubscribe();
  }, [
    isPlanMode,
    scrollToBottom,
    planData,
    processAgentMessage,
    setSelectedTeam,
    dispatch,
    planId,
    routePlanId,
    routeSessionId,
    queryPlanId,
    setSearchParams,
    navigate,
  ]);

  // WebsocketMessageType.ERROR_MESSAGE
  useEffect(() => {
    if (!isPlanMode) return;
    const unsubscribe = webSocketService.on(
      WebsocketMessageType.ERROR_MESSAGE,
      (errorMessage: any) => {
        let errorContent =
          'An unexpected error occurred. Please try again later.';
        if (errorMessage?.data?.data?.content) {
          const c = errorMessage.data.data.content.trim();
          if (c.length > 0) errorContent = c;
        } else if (errorMessage?.data?.content) {
          const c = errorMessage.data.content.trim();
          if (c.length > 0) errorContent = c;
        } else if (errorMessage?.content) {
          const c = errorMessage.content.trim();
          if (c.length > 0) errorContent = c;
        } else if (typeof errorMessage === 'string') {
          const c = errorMessage.trim();
          if (c.length > 0) errorContent = c;
        }
        const errorAgentMessage: AgentMessageData = {
          agent: 'system',
          agent_type: AgentMessageType.SYSTEM_AGENT,
          timestamp: Date.now(),
          steps: [],
          next_steps: [],
          content: formatErrorMessage(errorContent),
          raw_data: errorMessage || '',
        };
        setAgentMessages((prev) => [...prev, errorAgentMessage]);
        setShowProcessingPlanSpinner(false);
        setShowBufferingText(false);
        setSubmittingChatDisableInput(false);
        scrollToBottom();
        showToast(errorContent, 'error');
      }
    );

    return () => unsubscribe();
  }, [isPlanMode, scrollToBottom, showToast, formatErrorMessage]);

  //WebsocketMessageType.AGENT_MESSAGE
  useEffect(() => {
    if (!isPlanMode) return;
    const unsubscribe = webSocketService.on(
      WebsocketMessageType.AGENT_MESSAGE,
      (agentMessage: any) => {
        const agentMessageData = agentMessage.data as AgentMessageData;
        if (agentMessageData) {
          agentMessageData.content = PlanDataService.simplifyHumanClarification(
            agentMessageData?.content
          );
          setAgentMessages((prev) => [...prev, agentMessageData]);
          setShowProcessingPlanSpinner(true);
          scrollToBottom();
          processAgentMessage(agentMessageData, planData);
        }
      }
    );

    return () => unsubscribe();
  }, [isPlanMode, scrollToBottom, planData, processAgentMessage]);

  // Loading message rotation effect
  useEffect(() => {
    let interval: NodeJS.Timeout;
    if (loading) {
      let index = 0;
      interval = setInterval(() => {
        index = (index + 1) % loadingMessages.length;
        setLoadingMessage(loadingMessages[index]);
      }, 3000);
    }
    return () => clearInterval(interval);
  }, [loading]);

  // WebSocket connection with proper error handling and v4 backend compatibility
  useEffect(() => {
    if (planId && continueWithWebsocketFlow) {
      console.log('🔌 Connecting WebSocket:', {
        planId,
        continueWithWebsocketFlow,
      });

      const connectWebSocket = async () => {
        try {
          await webSocketService.connect(planId);
          console.log('✅ WebSocket connected successfully');
        } catch (error) {
          console.error('❌ WebSocket connection failed:', error);
          // Continue without WebSocket - the app should still work
        }
      };

      connectWebSocket();

      const handleConnectionChange = (connected: boolean) => {
        console.log('🔗 WebSocket connection status:', connected);
      };

      const handlePlanApprovalResponse = (message: StreamMessage) => {
        console.log('✅ Plan approval response received:', message);
      };

      // Subscribe to connection status and plan approval response
      // Note: AGENT_MESSAGE and PLAN_APPROVAL_REQUEST are handled by dedicated useEffect hooks above
      const unsubscribeConnection = webSocketService.on(
        'connection_status',
        (message) => {
          handleConnectionChange(message.data?.connected || false);
        }
      );

      const unsubscribePlanApproval = webSocketService.on(
        WebsocketMessageType.PLAN_APPROVAL_RESPONSE,
        handlePlanApprovalResponse
      );

      return () => {
        console.log('🔌 Cleaning up WebSocket connections');
        unsubscribeConnection();
        unsubscribePlanApproval();
        webSocketService.disconnect();
      };
    }
  }, [planId, continueWithWebsocketFlow]);

  // Create loadPlanData function with useCallback to memoize it
  const loadPlanData = useCallback(
    async (useCache = true): Promise<ProcessedPlanData | null> => {
      if (!planId) return null;
      resetPlanVariables();
      setLoading(true);
      try {
        let planResult: ProcessedPlanData | null = null;
        console.log('Fetching plan with ID:', planId);
        planResult = await PlanDataService.fetchPlanData(planId, useCache);
        console.log('Plan data fetched:', planResult);
        const isInProgress =
          planResult?.plan?.overall_status === PlanStatus.IN_PROGRESS;

        if (isInProgress) {
          setShowApprovalButtons(true);
          // Connect WebSocket so infrastructure events are received.
          setContinueWithWebsocketFlow(true);

          // Orphan recovery: plan is in_progress but backend never sent m_plan
          // (e.g. page refreshed mid-orchestration). Re-trigger the workflow.
          if (!planResult?.mplan) {
            console.warn(
              '⚠️ Plan in_progress but m_plan is null — re-triggering orchestration.'
            );
            try {
              await apiService.triggerPlanOrchestration(planId);
              console.log('✅ Orchestration re-triggered for plan:', planId);
            } catch (triggerErr) {
              console.warn(
                '⚠️ Could not re-trigger orchestration (non-fatal):',
                triggerErr
              );
            }
          }
        } else {
          setShowApprovalButtons(false);
        }

        if (planResult?.mplan) {
          dispatch(setApprovalRequest(planResult.mplan));
        }
        if (planResult?.messages !== undefined) {
          setAgentMessages([...planResult.messages]);
        }
        if (
          planResult?.streaming_message &&
          planResult.streaming_message.trim() !== ''
        ) {
          wsStreamingBufferRef.current = planResult.streaming_message;
          setStreamingMessageBuffer(planResult.streaming_message);
          setShowBufferingText(true);
        }
        const isCompleted =
          planResult?.plan?.overall_status === PlanStatus.COMPLETED;
        if (isCompleted) {
          setWaitingForPlan(false);
          setSubmittingChatDisableInput(false);
        } else if (planResult?.mplan) {
          setWaitingForPlan(false);
          setSubmittingChatDisableInput(true);
        }
        dispatch(setPlanData({ planId: planId || '', data: planResult }));
        return planResult;
      } catch (err) {
        console.log('Failed to load plan data:', err);
        setErrorLoading(true);
        dispatch(setPlanData({ planId: planId || '', data: null }));
        return null;
      } finally {
        setLoading(false);
      }
    },
    [planId, resetPlanVariables, dispatch]
  );

  const loadSessionHistory = useCallback(
    async (sessionId: string): Promise<void> => {
      resetPlanVariables();
      setLoading(true);
      try {
        const sessionData = await apiService.getChatSession(sessionId);
        const chatHistory = (sessionData?.messages || []).map(
          (msg): AgentMessageData => {
            const role = (msg as any).role || (msg as any).sender;
            const metadata = (msg as any).metadata || {};
            // Plan-lane messages carry metadata.agent (e.g. ProductAgent);
            // chat-lane ones carry metadata.selected_agent. Reading only the
            // latter relabeled every plan utterance as Group_Chat_Manager.
            const content = (msg.content || '').split('\n\n[turn-log]\n')[0];
            return {
              agent:
                role === 'user'
                  ? 'human'
                  : metadata.agent ||
                    metadata.selected_agent ||
                    AgentType.GROUP_CHAT_MANAGER,
              agent_type:
                role === 'user'
                  ? AgentMessageType.HUMAN_AGENT
                  : AgentMessageType.AI_AGENT,
              timestamp: new Date(msg.timestamp).getTime(),
              steps: [],
              next_steps: [],
              content,
              raw_data: content,
            };
          }
        );
        setAgentMessages(chatHistory);
        dispatch(setPlanData({ planId: '', data: null }));
        setWaitingForPlan(false);
        setSubmittingChatDisableInput(false);
        setShowApprovalButtons(false);
        setShowProcessingPlanSpinner(false);
        setErrorLoading(false);
      } catch (err) {
        console.log('Failed to load chat session history:', err);
        setErrorLoading(true);
      } finally {
        setLoading(false);
      }
    },
    [dispatch, resetPlanVariables]
  );

  // Handle plan approval
  const handleApprovePlan = useCallback(async () => {
    if (!planApprovalRequest) return;

    dispatch(setProcessingApproval(true));
    let id = showToast('Submitting Approval', 'progress');

    try {
      await apiService.approvePlan({
        m_plan_id: planApprovalRequest.id,
        plan_id: planData?.plan?.id,
        approved: true,
        feedback: 'Plan approved by user',
      });

      dismissToast(id);
      setShowProcessingPlanSpinner(true);
      setShowApprovalButtons(false);
    } catch (error) {
      dismissToast(id);
      showToast('Failed to submit approval', 'error');
      console.error('❌ Failed to approve plan:', error);
    } finally {
      dispatch(setProcessingApproval(false));
    }
  }, [
    dispatch,
    dismissToast,
    planApprovalRequest,
    planData?.plan?.id,
    showToast,
  ]);

  // Handle plan rejection
  const handleRejectPlan = useCallback(async () => {
    if (!planApprovalRequest) return;

    dispatch(setProcessingApproval(true));
    let id = showToast('Submitting cancellation', 'progress');
    try {
      await apiService.approvePlan({
        m_plan_id: planApprovalRequest.id,
        plan_id: planData?.plan?.id,
        approved: false,
        feedback: 'Plan rejected by user',
      });

      dismissToast(id);

      // Limpiar el estado del plan en Redux
      dispatch(setPlanData({ planId: planData?.plan?.id || '', data: null }));
      dispatch(setApprovalRequest(null));

      navigate('/');
    } catch (error) {
      dismissToast(id);
      showToast('Failed to submit cancellation', 'error');
      console.error('❌ Failed to reject plan:', error);

      // Limpiar el estado del plan incluso si la cancelación falló
      dispatch(setPlanData({ planId: planData?.plan?.id || '', data: null }));
      dispatch(setApprovalRequest(null));

      navigate('/');
    } finally {
      dispatch(setProcessingApproval(false));
    }
  }, [
    dispatch,
    planApprovalRequest,
    showToast,
    planData?.plan?.id,
    dismissToast,
    navigate,
  ]);

  const handleOnchatSubmit = useCallback(
    async (chatInput: string) => {
      if (!chatInput.trim()) return;
      setInput('');

      // ── Mode 1: Clarification intercept ────────────────────────────
      if (clarificationMessage) {
        if (!planData?.plan) return;
        const userMsg: AgentMessageData = {
          agent: 'human',
          agent_type: AgentMessageType.HUMAN_AGENT,
          timestamp: Date.now(),
          steps: [],
          next_steps: [],
          content: chatInput,
          raw_data: chatInput,
        };
        setAgentMessages((prev) => [...prev, userMsg]);
        setSubmittingChatDisableInput(true);
        const toastId = showToast('Submitting clarification', 'progress');
        try {
          await PlanDataService.submitClarification({
            request_id: clarificationMessage.request_id || '',
            answer: chatInput,
            plan_id: planData.plan.id,
            m_plan_id: planApprovalRequest?.id || '',
          });
          dismissToast(toastId);
          showToast('Clarification submitted successfully', 'success');
          setClarificationMessage(null);
          setShowProcessingPlanSpinner(true);
        } catch {
          dismissToast(toastId);
          showToast('Failed to submit clarification', 'error');
          setSubmittingChatDisableInput(false);
        }
        return;
      }

      // ── Mode 2: — route through IntentRouter SSE ────────────
      const sessionId =
        planData?.plan?.session_id || closedSessionId || routeSessionId || '';
      const userMsg: AgentMessageData = {
        agent: 'human',
        agent_type: AgentMessageType.HUMAN_AGENT,
        timestamp: Date.now(),
        steps: [],
        next_steps: [],
        content: chatInput,
        raw_data: chatInput,
      };
      setAgentMessages((prev) => [...prev, userMsg]);
      setSubmittingChatDisableInput(true);
      setShowProcessingPlanSpinner(true);
      scrollToBottom();

      // ── Interruptor in Plan position: formal lane, explicitly ──────
      // No router guessing: the plan is created via /process_request and the
      // page flips to plan mode when the planId lands in the URL.
      if (planLane && (!planId || planClosed)) {
        try {
          // session_id must always be PRESENT (pydantic requires the field);
          // the backend mints a uuid when it arrives empty.
          const resp = await apiService.createPlan({
            session_id: sessionId || '',
            description: chatInput,
          });
          setPlanLane(false);
          setWaitingForPlan(true);
          if (routeSessionId) {
            setSearchParams({ planId: resp.plan_id });
          } else {
            navigate(`/plan/${resp.plan_id}`);
          }
        } catch (e: any) {
          showToast(e?.message || 'Failed to create plan', 'error');
          setShowProcessingPlanSpinner(false);
          setSubmittingChatDisableInput(false);
        }
        return;
      }

      let accumulated = '';
      let placeholderAdded = false;
      let respondingAgent = AgentType.GROUP_CHAT_MANAGER;
      // Once the plan is closed in place, follow-ups are plain chat: no
      // in-plan flag, or the backend would treat them as plan follow-ups.
      const activePlanId = planClosed
        ? ''
        : planData?.plan?.plan_id || planData?.plan?.id || planId || '';
      const fileIds = attachedFiles.map((f) => f.file_id);
      const collectedFiles: Array<{
        file_id: string;
        filename: string;
        download_url: string;
      }> = [];
      try {
        for await (const token of ChatService.streamAsAsyncIterable(
          chatInput,
          sessionId,
          {
            onSessionId: (newSessionId) => {
              // Al crear nueva sesión, navegar a /session/:sessionId
              if (!routeSessionId && !planId) {
                navigate(`/session/${newSessionId}`, { replace: true });
              }
            },
            onPlanCreated: (newPlanId) => {
              // Mantener la ruta de sesión y agregar planId como query param
              if (routeSessionId) {
                setSearchParams({ planId: newPlanId });
              } else {
                navigate(`/plan/${newPlanId}`);
              }
            },
            onDone: (data) => {
              if (!data.agent) return;
              respondingAgent = data.agent as AgentType;
              if (placeholderAdded) {
                setAgentMessages((prev) =>
                  prev.map((m, i) =>
                    i === prev.length - 1 ? { ...m, agent: respondingAgent } : m
                  )
                );
              }
            },
            planId: activePlanId,
            onGeneratedFile: (f) => {
              collectedFiles.push(f);
            },
            fileIds,
            // Chat position: the Model Router may escalate to a formal Plan
            // (run_plan) when the request genuinely needs multi-agent
            // orchestration — and it composes the roster in that same
            // decision (team by intent, no manual selection required). The
            // selector branch above remains the explicit path.
            allowPlan: true,
            onOAuthConsentRequest: (consentLink) => {
              // NOTE: no 'noopener' — with it window.open() returns null (per
              // spec), so popup.closed polling never runs and the auto-retry
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
                    handleOnchatSubmit(chatInput);
                  }
                }, 500);
              }
            },
          }
        )) {
          accumulated += token;
          const snap = accumulated;

          if (!placeholderAdded && snap.trim()) {
            // Agregar placeholder solo cuando hay contenido real
            const placeholder: AgentMessageData = {
              agent: respondingAgent,
              agent_type: AgentMessageType.AI_AGENT,
              timestamp: Date.now(),
              steps: [],
              next_steps: [],
              content: snap,
              raw_data: '',
            };
            setAgentMessages((prev) => [...prev, placeholder]);
            placeholderAdded = true;
          } else if (placeholderAdded) {
            // Actualizar el placeholder con el contenido acumulado
            setAgentMessages((prev) =>
              prev.map((m, i) =>
                i === prev.length - 1 ? { ...m, content: snap } : m
              )
            );
          }
          scrollToBottom();
        }
      } catch (e: any) {
        showToast(e?.message || 'Failed to send message', 'error');
        // Solo eliminar el último mensaje si se agregó el placeholder
        if (placeholderAdded) {
          setAgentMessages((prev) =>
            prev.filter((_, i) => i !== prev.length - 1)
          );
        }
      } finally {
        setAttachedFiles([]);
        if (collectedFiles.length > 0) {
          setGeneratedFiles((prev) => [...prev, ...collectedFiles]);
        }
        setShowProcessingPlanSpinner(false);
        setSubmittingChatDisableInput(false);
      }
    },
    [
      clarificationMessage,
      planData,
      routeSessionId,
      planId,
      planLane,
      planClosed,
      closedSessionId,
      planApprovalRequest,
      showToast,
      dismissToast,
      navigate,
      setSearchParams,
      scrollToBottom,
      attachedFiles,
    ]
  );

  // ✅ Handlers for PlanPanelLeft with plan cancellation protection
  const handleNewTaskButton = useCallback(() => {
    handleNavigationWithAlert(() => {
      navigate('/', { state: { focusInput: true } });
    });
  }, [navigate, handleNavigationWithAlert]);

  const resetReload = useCallback(() => {
    setReloadLeftList(false);
  }, []);

  useEffect(() => {
    const initializePlanLoading = async () => {
      // If a plan is present, loadPlanData already merges the related
      // chat session history. Loading /session first creates duplicate
      // GETs and races the plan state.
      if (routeSessionId && !planId) {
        await loadSessionHistory(routeSessionId);
      }

      if (planId) {
        try {
          await loadPlanData(true);
        } catch (err) {
          console.error('Failed to initialize plan loading:', err);
        }
        return;
      }

      // Caso 3: new chat, no planId or sessionId
      if (!planId && !routeSessionId) {
        resetPlanVariables();
        setLoading(false);
        setWaitingForPlan(false);
        setSubmittingChatDisableInput(false);
        return;
      }
    };

    initializePlanLoading();
  }, [
    planId,
    routeSessionId,
    loadPlanData,
    loadSessionHistory,
    resetPlanVariables,
    setErrorLoading,
  ]);

  useEffect(() => {
    if (planData?.team) {
      setSelectedTeam(planData.team);
    }
  }, [planData, setSelectedTeam]);

  if (errorLoading) {
    return (
      <CoralShellColumn>
        <CoralShellRow>
          <PlanPanelLeft
            reloadTasks={reloadLeftList}
            onNewTaskButton={handleNewTaskButton}
            restReload={resetReload}
            onTeamSelect={() => {}}
            onTeamUpload={async () => {}}
            isHomePage={false}
            selectedTeam={selectedTeam}
            onNavigationWithAlert={handleNavigationWithAlert}
          />
          <Content>
            <div className="plan-error-message">
              <Text size={500}>
                {'An error occurred while loading the plan'}
              </Text>
            </div>
          </Content>
        </CoralShellRow>
      </CoralShellColumn>
    );
  }

  const activeWorkspaceId =
    activeWorkspaceOverride ||
    planData?.plan?.session_id ||
    closedSessionId ||
    routeSessionId ||
    '';

  return (
    <HtmlPreviewProvider workspaceId={activeWorkspaceId}>
      <CoralShellColumn>
        <CoralShellRow>
          {/* ✅ RESTORED: PlanPanelLeft for navigation */}
          <PlanPanelLeft
            reloadTasks={reloadLeftList}
            onNewTaskButton={handleNewTaskButton}
            restReload={resetReload}
            onTeamSelect={setSelectedTeam}
            onTeamUpload={async () => {}}
            isHomePage={!planId && !routeSessionId}
            selectedTeam={selectedTeam}
            onNavigationWithAlert={handleNavigationWithAlert}
            onWorkspaceChange={setActiveWorkspaceOverride}
          />

          <Content>
            {/* isPlanMode, NOT planId: once the plan closes in place the URL
              still carries /plan/:id while planData is cleared — keyed on
              planId this guard swapped the whole chat for the plan spinner
              (no input, canvas gone). Chat mode never shows it. */}
            {isPlanMode && (loading || !planData) ? (
              <>
                <div className="plan-loading-spinner">
                  <Spinner size="medium" />
                  <Text>Loading plan data...</Text>
                </div>
                <LoadingMessage
                  loadingMessage={loadingMessage}
                  iconSrc={Octo}
                />
              </>
            ) : (
              <>
                <ContentToolbar
                  panelTitle={
                    isPlanMode ? 'Multi-Agent Planner' : 'Multi-Agent Chat'
                  }
                >
                  <InspectorLink />
                </ContentToolbar>

                <PlanChat
                  key={chatKey}
                  planData={planData}
                  loading={loading}
                  messagesContainerRef={messagesContainerRef}
                  input={input}
                  setInput={setInput}
                  agentMessages={agentMessages}
                  streamingMessageBuffer={streamingMessageBuffer}
                  showBufferingText={showBufferingText}
                  submittingChatDisableInput={submittingChatDisableInput}
                  waitingForPlan={waitingForPlan}
                  showProcessingPlanSpinner={showProcessingPlanSpinner}
                  showApprovalButtons={showApprovalButtons}
                  processingApproval={processingApproval}
                  planApprovalRequest={planApprovalRequest}
                  OnChatSubmit={handleOnchatSubmit}
                  handleApprovePlan={handleApprovePlan}
                  handleRejectPlan={handleRejectPlan}
                  attachedFiles={attachedFiles}
                  generatedFiles={generatedFiles}
                  onFileSelect={handleFileSelect}
                  onRemoveFile={removeAttachedFile}
                  onRemoveGeneratedFile={removeGeneratedFile}
                  planLane={planLane}
                  onPlanLaneChange={setPlanLane}
                  showPlanLaneToggle={!isPlanMode}
                  bufferAt={bufferAt}
                />
              </>
            )}
          </Content>

          {/* Right slot: an open HTML preview takes it; otherwise the plan
            panel (plan mode) or nothing (chat mode) — the preview is the
            canvas seed and must never inflate the composer above ChatInput. */}
          <PreviewRightSlot
            fallback={
              isPlanMode ? (
                <React.Suspense fallback={null}>
                  <PlanPanelRight
                    planData={planData}
                    loading={loading}
                    planApprovalRequest={planApprovalRequest}
                  />
                </React.Suspense>
              ) : null
            }
          />
        </CoralShellRow>

        {/* Plan Cancellation Confirmation Dialog */}
        <PlanCancellationDialog
          isOpen={showCancellationDialog}
          onConfirm={handleConfirmCancellation}
          onCancel={handleCancelDialog}
          loading={cancellingPlan}
        />
      </CoralShellColumn>
    </HtmlPreviewProvider>
  );
};

export default PlanPage;
