/**
 * ChatPage — Una sola página, un solo hilo
 *
 * Rutas : /chat/:sessionId  (conversación + plan inline)
 *         /plan/:planId     (mismo componente — deep link a un plan)
 *
 * PRINCIPIO: Chat.tsx NUNCA se desmonta.
 *   - Los mensajes de agentes (WebSocket) se pushean al chatRef como burbujas normales.
 *   - El plan (aprobación, thinking, ejecución) se renderiza en el slot {children}
 *     de Chat.tsx — inline, por encima del input, sin reemplazar nada.
 *   - Al terminar/cancelar el plan el widget desaparece y la conversación continúa
 *     con todo el historial intacto.
 */
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";

import { PlanDataService } from "../services/PlanDataService";
import {
    ProcessedPlanData, WebsocketMessageType, MPlanData, AgentMessageData,
    AgentMessageType, ParsedUserClarification, AgentType, PlanStatus,
    TeamConfig,
} from "../models";

import Chat, { ChatHandle } from "../coral/modules/Chat";
import PlanPanelRight from "../components/content/PlanPanelRight";
import PlanPanelLeft from "../components/content/PlanPanelLeft";
import CoralShellColumn from "../coral/components/Layout/CoralShellColumn";
import CoralShellRow from "../coral/components/Layout/CoralShellRow";
import Content from "../coral/components/Content/Content";
import ContentToolbar from "../coral/components/Content/ContentToolbar";
import InspectorLink from "@/components/inspector/InspectorLink";
import InlineToaster, { useInlineToaster } from "../components/toast/InlineToaster";
import webSocketService from "../services/WebSocketService";
import { APIService } from "../api/apiService";
import { ChatService } from "../services/ChatService";
import { TeamService } from "../services/TeamService";
import { getUserId } from "../api/config";
import { usePlanCancellationAlert } from "../hooks/usePlanCancellationAlert";
import PlanCancellationDialog from "../components/common/PlanCancellationDialog";

// Plan inline widgets (usados en el slot {children} de Chat.tsx)
import RenderPlanResponse from "../components/content/streaming/StreamingPlanResponse";
import { renderPlanExecutionMessage, renderThinkingState } from "../components/content/streaming/StreamingPlanState";
import StreamingBufferMessage from "../components/content/streaming/StreamingBufferMessage";

import "../styles/PlanPage.css";

const apiService = new APIService();

const ChatPage: React.FC = () => {
    const { sessionId } = useParams<{ sessionId?: string }>();
    const { planId: planIdParam } = useParams<{ planId?: string }>();
    const navigate = useNavigate();
    const location = useLocation();
    const navigateRef = useRef(navigate);
    navigateRef.current = navigate;

    const userId = getUserId();
    const { showToast, dismissToast } = useInlineToaster();

    // ── Chat.tsx handle — ÚNICA fuente de verdad de mensajes ──────────────────
    const chatRef = useRef<ChatHandle>(null);

    // ── Estado del plan (widget inline) — NO controla qué se renderiza ────────
    const [activePlanId, setActivePlanId]     = useState<string | null>(null);
    const [planData, setPlanData]             = useState<ProcessedPlanData | any>(null);
    const [planApprovalRequest, setPlanApprovalRequest] = useState<MPlanData | null>(null);
    const [waitingForPlan, setWaitingForPlan] = useState<boolean>(false);
    const [showProcessingPlanSpinner, setShowProcessingPlanSpinner] = useState<boolean>(false);
    const [showApprovalButtons, setShowApprovalButtons] = useState<boolean>(false);
    const [processingApproval, setProcessingApproval] = useState<boolean>(false);
    const [streamingMessageBuffer, setStreamingMessageBuffer] = useState<string>("");
    const [showBufferingText, setShowBufferingText] = useState<boolean>(false);
    const [continueWithWebsocketFlow, setContinueWithWebsocketFlow] = useState<boolean>(false);
    const [selectedTeam, setSelectedTeam]     = useState<TeamConfig | null>(null);
    const [reloadLeftList, setReloadLeftList] = useState<boolean>(false);
    const [isLoadingTeam, setIsLoadingTeam] = useState<boolean>(true);
    const initCalledRef = useRef(false);
    const [clarificationMessage, setClarificationMessage] = useState<ParsedUserClarification | null>(null);
    const wsStreamingBufferRef = useRef<string>("");

    // ── Diálogo de cancelación ────────────────────────────────────────────────
    const [showCancellationDialog, setShowCancellationDialog] = useState<boolean>(false);
    const [pendingNavigation, setPendingNavigation] = useState<(() => void) | null>(null);
    const [cancellingPlan, setCancellingPlan] = useState<boolean>(false);

    const { isPlanActive } = usePlanCancellationAlert({
        planData, planApprovalRequest,
        onNavigate: pendingNavigation || (() => {}),
    });

    const handleNavigationWithAlert = useCallback((fn: () => void | Promise<void>) => {
        if (!isPlanActive()) { void Promise.resolve(fn()); return; }
        setPendingNavigation(() => () => { void Promise.resolve(fn()); });
        setShowCancellationDialog(true);
    }, [isPlanActive]);

    const handleConfirmCancellation = useCallback(async () => {
        setCancellingPlan(true);
        try {
            if (planApprovalRequest?.id) {
                await apiService.approvePlan({
                    m_plan_id: planApprovalRequest.id,
                    plan_id: planData?.plan?.id,
                    approved: false,
                    feedback: "Plan cancelled by user navigation",
                });
            }
            if (pendingNavigation) pendingNavigation();
            webSocketService.disconnect();
        } catch {
            if (pendingNavigation) pendingNavigation();
        } finally {
            setCancellingPlan(false);
            setShowCancellationDialog(false);
            setPendingNavigation(null);
        }
    }, [planApprovalRequest, planData, pendingNavigation]);

    const handleCancelDialog = useCallback(() => {
        setShowCancellationDialog(false);
        setPendingNavigation(null);
    }, []);

    // ── Helpers ───────────────────────────────────────────────────────────────
    const formatErrorMessage = useCallback((content: string): string => {
        return content.split("\n").map((line, i) => {
            if (i === 0) return `⚠️ ${line}`;
            if (line.trim() === "") return "";
            return `      ${line}`;
        }).join("\n");
    }, []);

    // ── processAgentMessage — persiste en backend ─────────────────────────────
    const processAgentMessage = useCallback(
        (agentMessageData: AgentMessageData, currentPlanData: ProcessedPlanData, is_final = false, streaming_message = "") => {
            const resp = PlanDataService.createAgentMessageResponse(agentMessageData, currentPlanData, is_final, streaming_message);
            return apiService.sendAgentMessage(resp)
                .then(() => { if (is_final) setTimeout(() => setReloadLeftList(true), 1000); })
                .catch(() => { if (is_final) setTimeout(() => setReloadLeftList(true), 1000); });
        }, []
    );

    // ── Limpiar estado del plan (el chat conserva su historial) ───────────────
    const clearPlanState = useCallback(() => {
        setActivePlanId(null);
        setPlanData(null);
        setPlanApprovalRequest(null);
        setWaitingForPlan(false);
        setShowProcessingPlanSpinner(false);
        setShowApprovalButtons(false);
        setContinueWithWebsocketFlow(false);
        setStreamingMessageBuffer("");
        setShowBufferingText(false);
        setClarificationMessage(null);
        wsStreamingBufferRef.current = "";
        // NO se limpia chatRef — el historial de mensajes permanece
    }, []);

    // ── activatePlanMode — carga plan y activa widget inline ──────────────────
    const activatePlanMode = useCallback(async (newPlanId: string) => {
        clearPlanState();
        setActivePlanId(newPlanId);
        setWaitingForPlan(true);
        setReloadLeftList(true);
        try {
            const planResult = await PlanDataService.fetchPlanData(newPlanId, false);
            const isInProgress = planResult?.plan?.overall_status === PlanStatus.IN_PROGRESS;

            if (isInProgress) {
                setShowApprovalButtons(true);
                setContinueWithWebsocketFlow(true);
                if (!planResult?.mplan) {
                    try { await apiService.triggerPlanOrchestration(newPlanId); }
                    catch (e) { console.warn("⚠️ Could not re-trigger orchestration:", e); }
                }
            } else {
                setShowApprovalButtons(false);
            }

            if (planResult?.mplan) {
                setPlanApprovalRequest(planResult.mplan);
                setWaitingForPlan(false);
                setShowApprovalButtons(false);
            }

            // Mensajes históricos del plan → pushear al chat como burbujas
            if (planResult?.messages?.length) {
                for (const msg of planResult.messages) {
                    chatRef.current?.pushMessage({
                        role: msg.agent_type === AgentMessageType.HUMAN_AGENT ? "user" : "assistant",
                        content: msg.content || "",
                    });
                }
            }

            if (planResult?.streaming_message?.trim()) {
                wsStreamingBufferRef.current = planResult.streaming_message;
                setStreamingMessageBuffer(planResult.streaming_message);
                setShowBufferingText(true);
            }

            if (planResult?.plan?.overall_status === PlanStatus.COMPLETED) {
                setWaitingForPlan(false);
            }

            if (planResult?.team) setSelectedTeam(planResult.team);
            setPlanData(planResult);
        } catch (err) {
            console.error("Failed to load plan:", err);
            showToast("Failed to load plan data", "error");
        }
    }, [clearPlanState, showToast]);
    // ── Team initialization (igual que HomePage) ─────────────────────────────
    useEffect(() => {
    if (initCalledRef.current) return;
    initCalledRef.current = true;
    const initTeam = async () => {
        setIsLoadingTeam(true);
        try {
        const initResponse = await TeamService.initializeTeam();
        if (initResponse.data?.status === 'Request started successfully' && initResponse.data?.team_id) {
            const teams = await TeamService.getUserTeams();
            const initializedTeam = teams.find(team => team.team_id === initResponse.data?.team_id);
            if (initializedTeam) {
            setSelectedTeam(initializedTeam);
            TeamService.storageTeam(initializedTeam);
            } else if (teams.length > 0) {
            setSelectedTeam(teams[0]);
            TeamService.storageTeam(teams[0]);
            }
        }
        } catch (error) {
        console.error('Error initializing team:', error);
        } finally {
        setIsLoadingTeam(false);
        }
    };
    initTeam();
    }, []);
    // ── Mount: ruta /plan/:planId ─────────────────────────────────────────────
    // eslint-disable-next-line react-hooks/exhaustive-deps
    useEffect(() => { if (planIdParam) activatePlanMode(planIdParam); }, [planIdParam]);

    // ── Mount: HomeInput pasó initialPlanId ───────────────────────────────────
    useEffect(() => {
        const initialPlanId = (location.state as any)?.initialPlanId;
        if (initialPlanId) activatePlanMode(initialPlanId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // ── WebSocket: conectar cuando plan lo requiera ───────────────────────────
    useEffect(() => {
        if (activePlanId && continueWithWebsocketFlow) {
            webSocketService.connect(activePlanId).catch(err =>
                console.error("❌ WebSocket connection failed:", err)
            );
            return () => { webSocketService.disconnect(); };
        }
    }, [activePlanId, continueWithWebsocketFlow]);

    // ── WS: PLAN_APPROVAL_REQUEST ─────────────────────────────────────────────
    useEffect(() => {
        const unsub = webSocketService.on(WebsocketMessageType.PLAN_APPROVAL_REQUEST, (req: any) => {
            let mPlanData: MPlanData | null = null;
            if (req.parsedData) mPlanData = req.parsedData;
            else if (req.data?.parsedData) mPlanData = req.data.parsedData;
            else if (req.data && typeof req.data === "object") mPlanData = req.data;
            else if (req.rawData) mPlanData = PlanDataService.parsePlanApprovalRequest(req.rawData);
            else mPlanData = PlanDataService.parsePlanApprovalRequest(req);
            if (mPlanData) {
                setPlanApprovalRequest(mPlanData);
                setWaitingForPlan(false);
                setShowProcessingPlanSpinner(false);
                setShowApprovalButtons(true);
            }
        });
        return () => unsub();
    }, []);

    // ── WS: AGENT_MESSAGE_STREAMING ───────────────────────────────────────────
    useEffect(() => {
        const unsub = webSocketService.on(WebsocketMessageType.AGENT_MESSAGE_STREAMING, (msg: any) => {
            const line = PlanDataService.simplifyHumanClarification(msg.data.content);
            wsStreamingBufferRef.current += line;
            setShowBufferingText(true);
            setStreamingMessageBuffer(prev => prev + line);
        });
        return () => unsub();
    }, []);

    // ── WS: USER_CLARIFICATION_REQUEST ────────────────────────────────────────
    useEffect(() => {
        const unsub = webSocketService.on(WebsocketMessageType.USER_CLARIFICATION_REQUEST, (msg: any) => {
            if (!msg) return;
            // Pushear la pregunta de clarificación como burbuja al chat
            const question = msg.data?.question || "";
            chatRef.current?.pushMessage({ role: "assistant", content: `💬 **Clarification needed:** ${question}` });
            setClarificationMessage(msg.data as ParsedUserClarification | null);
            setShowBufferingText(false);
            setStreamingMessageBuffer("");
            wsStreamingBufferRef.current = "";
            setShowProcessingPlanSpinner(false);
            // Persistir
            const agentMessageData: AgentMessageData = {
                agent: AgentType.GROUP_CHAT_MANAGER,
                agent_type: AgentMessageType.AI_AGENT,
                timestamp: msg.timestamp || Date.now(),
                steps: [], next_steps: [],
                content: question,
                raw_data: msg.data || "",
            };
            processAgentMessage(agentMessageData, planData);
        });
        return () => unsub();
    }, [planData, processAgentMessage]);

    // ── WS: AGENT_MESSAGE — pushear al chatRef como burbuja ──────────────────
    useEffect(() => {
        const unsub = webSocketService.on(WebsocketMessageType.AGENT_MESSAGE, (agentMessage: any) => {
            const agentMessageData = agentMessage.data as AgentMessageData;
            if (agentMessageData) {
                agentMessageData.content = PlanDataService.simplifyHumanClarification(agentMessageData.content);
                // El mensaje del agente va directo al chat como burbuja
                chatRef.current?.pushMessage({ role: "assistant", content: agentMessageData.content });
                setShowProcessingPlanSpinner(true);
                processAgentMessage(agentMessageData, planData);
            }
        });
        return () => unsub();
    }, [planData, processAgentMessage]);

    // ── WS: FINAL_RESULT_MESSAGE ──────────────────────────────────────────────
    useEffect(() => {
        const unsub = webSocketService.on(WebsocketMessageType.FINAL_RESULT_MESSAGE, (finalMessage: any) => {
            if (!finalMessage) return;
            const content = "🎉🎉 " + (finalMessage.data?.content || "");
            if (finalMessage?.data?.status === PlanStatus.COMPLETED) {
                // Pushear resultado final al chat como burbuja
                chatRef.current?.pushMessage({ role: "assistant", content });
                setShowBufferingText(false);
                setShowProcessingPlanSpinner(false);
                if (planData?.plan) planData.plan.overall_status = PlanStatus.COMPLETED;
                setSelectedTeam(planData?.team || null);
                webSocketService.disconnect();
                const capturedBuffer = wsStreamingBufferRef.current;
                wsStreamingBufferRef.current = "";
                const agentMessageData: AgentMessageData = {
                    agent: AgentType.GROUP_CHAT_MANAGER,
                    agent_type: AgentMessageType.AI_AGENT,
                    timestamp: Date.now(), steps: [], next_steps: [],
                    content, raw_data: finalMessage,
                };
                processAgentMessage(agentMessageData, planData, true, capturedBuffer);
                // Plan terminó — limpiar widget inline pero el chat conserva el historial
                setTimeout(() => {
                    setWaitingForPlan(false);
                    setShowApprovalButtons(false);
                    setPlanApprovalRequest(null);
                    setActivePlanId(null);
                }, 500);
            }
        });
        return () => unsub();
    }, [planData, processAgentMessage]);

    // ── WS: ERROR_MESSAGE ─────────────────────────────────────────────────────
    useEffect(() => {
        const unsub = webSocketService.on(WebsocketMessageType.ERROR_MESSAGE, (errorMessage: any) => {
            let errorContent = "An unexpected error occurred. Please try again later.";
            if (errorMessage?.data?.data?.content?.trim()) errorContent = errorMessage.data.data.content.trim();
            else if (errorMessage?.data?.content?.trim()) errorContent = errorMessage.data.content.trim();
            else if (errorMessage?.content?.trim()) errorContent = errorMessage.content.trim();
            else if (typeof errorMessage === "string" && errorMessage.trim()) errorContent = errorMessage.trim();
            chatRef.current?.pushMessage({ role: "assistant", content: formatErrorMessage(errorContent) });
            setShowProcessingPlanSpinner(false);
            setShowBufferingText(false);
            showToast(errorContent, "error");
        });
        return () => unsub();
    }, [showToast, formatErrorMessage]);

    // ── WS: AGENT_TOOL_MESSAGE ────────────────────────────────────────────────
    useEffect(() => {
        const unsub = webSocketService.on(WebsocketMessageType.AGENT_TOOL_MESSAGE, () => {});
        return () => unsub();
    }, []);

    // ── Aprobación / Rechazo ──────────────────────────────────────────────────
    const handleApprovePlan = useCallback(async () => {
        if (!planApprovalRequest) return;
        setProcessingApproval(true);
        const id = showToast("Submitting Approval", "progress");
        try {
            await apiService.approvePlan({
                m_plan_id: planApprovalRequest.id,
                plan_id: planData?.plan?.id,
                approved: true,
                feedback: "Plan approved by user",
            });
            dismissToast(id);
            setShowProcessingPlanSpinner(true);
            setShowApprovalButtons(false);
            setPlanApprovalRequest(null);   // ocultar la tarjeta completa tras aprobación
        } catch {
            dismissToast(id);
            showToast("Failed to submit approval", "error");
        } finally {
            setProcessingApproval(false);
        }
    }, [dismissToast, planApprovalRequest, planData?.plan?.id, showToast]);

    const handleRejectPlan = useCallback(async () => {
        if (!planApprovalRequest) return;
        setProcessingApproval(true);
        const id = showToast("Submitting cancellation", "progress");
        try {
            await apiService.approvePlan({
                m_plan_id: planApprovalRequest.id,
                plan_id: planData?.plan?.id,
                approved: false,
                feedback: "Plan rejected by user",
            });
            dismissToast(id);
            webSocketService.disconnect();
            // El plan se cancela — el widget desaparece, el chat continúa
            clearPlanState();
            chatRef.current?.pushMessage({ role: "assistant", content: "Plan cancelled. You can continue the conversation or start a new task." });
        } catch {
            dismissToast(id);
            showToast("Failed to submit cancellation", "error");
        } finally {
            setProcessingApproval(false);
        }
    }, [planApprovalRequest, planData?.plan?.id, showToast, dismissToast, clearPlanState]);

    // ── onSendMessage — maneja chat normal + clarificaciones ─────────────────
    // Chat.tsx consume esto como AsyncIterable<string>
    const handleSendMessage = useCallback(
        async function* (userInput: string, _history: any[]) {
            if (!userInput.trim()) return;

            // Si hay clarificación pendiente → interceptar
            if (clarificationMessage) {
                if (!planData?.plan) return;
                const toastId = showToast("Submitting clarification", "progress");
                try {
                    await PlanDataService.submitClarification({
                        request_id: clarificationMessage.request_id || "",
                        answer: userInput,
                        plan_id: planData.plan.id,
                        m_plan_id: planApprovalRequest?.id || "",
                    });
                    dismissToast(toastId);
                    showToast("Clarification submitted", "success");
                    setClarificationMessage(null);
                    setShowProcessingPlanSpinner(true);
                    yield "✅ Clarification submitted. Processing…";
                } catch {
                    dismissToast(toastId);
                    showToast("Failed to submit clarification", "error");
                }
                return;
            }

            // Conversación normal / nuevo plan — siempre el mismo sessionId
            const sid = planData?.plan?.session_id || sessionId || "";
            yield* ChatService.streamAsAsyncIterable(
                userInput,
                sid,
                // plan_created → activar widget inline SIN salir del chat
                (newPlanId) => activatePlanMode(newPlanId),
                // session actualizado → reemplazar URL sin navegar
                (newSid) => {
                    if (newSid && newSid !== sessionId)
                        navigateRef.current(`/chat/${newSid}`, { replace: true });
                    // Refresh panel after first message so session appears in Recent Chats
                    setReloadLeftList(true);
                },
            );
        },
        [clarificationMessage, planData, planApprovalRequest, sessionId, showToast, dismissToast, activatePlanMode],
    );

    // ── Historia y sesión ─────────────────────────────────────────────────────
    const handleLoadHistory = useCallback(
        (_uid: string) => sessionId ? ChatService.loadSession(sessionId) : Promise.resolve([]),
        [sessionId],
    );

    const handleClearHistory = useCallback((_uid: string) => {
        const newId = crypto.randomUUID();
        clearPlanState();
        chatRef.current?.clearMessages();
        navigateRef.current(`/chat/${newId}`);
    }, [clearPlanState]);

    // ── Navegación ────────────────────────────────────────────────────────────
    const resetReload = useCallback(() => setReloadLeftList(false), []);

    const handleNewTaskButton = useCallback(() => {
        handleNavigationWithAlert(() => {
            clearPlanState();
            chatRef.current?.clearMessages();
            navigateRef.current("/chat", { state: { focusInput: true } });
        });
    }, [handleNavigationWithAlert, clearPlanState]);

    const handleTeamSelect = useCallback(async (team: TeamConfig | null) => {
        setSelectedTeam(team);
        setReloadLeftList(true);
        if (team) {
            try {
                setIsLoadingTeam(true);
                const initResponse = await TeamService.initializeTeam(true);
                if (initResponse.data?.status === 'Request started successfully' && initResponse.data?.team_id) {
                    const teams = await TeamService.getUserTeams();
                    const initializedTeam = teams.find(t => t.team_id === initResponse.data?.team_id);
                    if (initializedTeam) {
                        setSelectedTeam(initializedTeam);
                        TeamService.storageTeam(initializedTeam);
                        setReloadLeftList(true);
                    }
                }
            } catch (error) {
                console.error('Error switching team:', error);
                showToast('Error switching team. Please try again.', 'warning');
            } finally {
                setIsLoadingTeam(false);
            }
        }
    }, [showToast]);

    const handleTeamUpload = useCallback(async () => {
        try {
            const teams = await TeamService.getUserTeams();
            if (teams.length > 0) {
                const hrTeam = teams.find(t => t.name === 'Human Resources Team');
                const defaultTeam = hrTeam || teams[0];
                setSelectedTeam(defaultTeam);
                TeamService.storageTeam(defaultTeam);
            }
        } catch (error) {
            console.error('Error refreshing teams after upload:', error);
        }
    }, []);

    useEffect(() => {
        if (planData?.team) setSelectedTeam(planData.team);
    }, [planData]);

    // ── Plan inline widget — renderizado en el slot {children} de Chat.tsx ────
    const planWidget = activePlanId ? (
        <div style={{ padding: "0 0 8px 0" }}>
            {/* "Creating your plan..." */}
            {renderThinkingState(waitingForPlan)}

            {/* Plan approval card */}
            {planApprovalRequest && (
                <RenderPlanResponse
                    planApprovalRequest={planApprovalRequest}
                    handleApprovePlan={handleApprovePlan}
                    handleRejectPlan={handleRejectPlan}
                    processingApproval={processingApproval}
                    showApprovalButtons={showApprovalButtons}
                />
            )}

            {/* "Processing your plan and coordinating with AI agents..." */}
            {showProcessingPlanSpinner && renderPlanExecutionMessage()}

            {/* Streaming buffer */}
            {showBufferingText && (
                <StreamingBufferMessage
                    streamingMessageBuffer={streamingMessageBuffer}
                    isStreaming={true}
                />
            )}
        </div>
    ) : null;

    // ── Render ────────────────────────────────────────────────────────────────
    return (
        <>
            <InlineToaster />
            <CoralShellColumn>
                <CoralShellRow>
                    <PlanPanelLeft
                        reloadTasks={reloadLeftList}
                        onNewTaskButton={handleNewTaskButton}
                        restReload={resetReload}
                        onTeamSelect={handleTeamSelect}
                        onTeamUpload={handleTeamUpload}
                        isHomePage={true}
                        selectedTeam={selectedTeam}
                        isLoadingTeam={isLoadingTeam}
                        onNavigationWithAlert={activePlanId ? handleNavigationWithAlert : undefined}
                    />

                    <Content>
                        <ContentToolbar panelTitle={activePlanId ? "Multi-Agent Planner" : "Chat"}>
                            <InspectorLink />
                        </ContentToolbar>

                        {/* Chat.tsx SIEMPRE montado — nunca se reemplaza */}
                        <Chat
                            ref={chatRef}
                            userId={userId}
                            onSendMessage={handleSendMessage}
                            onLoadHistory={handleLoadHistory}
                            onClearHistory={handleClearHistory}
                        >
                            {/* Widget del plan renderizado inline, sin desmontar el chat */}
                            {planWidget}
                        </Chat>
                    </Content>

                    {/* Panel derecho sólo cuando hay plan activo */}
                    {activePlanId && (
                        <PlanPanelRight
                            planData={planData}
                            loading={false}
                            planApprovalRequest={planApprovalRequest}
                        />
                    )}
                </CoralShellRow>

                <PlanCancellationDialog
                    isOpen={showCancellationDialog}
                    onConfirm={handleConfirmCancellation}
                    onCancel={handleCancelDialog}
                    loading={cancellingPlan}
                />
            </CoralShellColumn>
        </>
    );
};

export default ChatPage;
