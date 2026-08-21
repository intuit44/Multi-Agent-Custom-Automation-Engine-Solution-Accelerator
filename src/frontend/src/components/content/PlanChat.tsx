import React, { useEffect } from 'react';
import { PlanChatProps, MPlanData } from '../../models/plan';
import InlineToaster from '../toast/InlineToaster';
import { AgentMessageData, AgentMessageType } from '@/models';
import renderUserPlanMessage from './streaming/StreamingUserPlanMessage';
import RenderPlanResponse from './streaming/StreamingPlanResponse';
import {
  renderPlanExecutionMessage,
  renderThinkingState,
} from './streaming/StreamingPlanState';
import ContentNotFound from '../NotFound/ContentNotFound';
import PlanChatBody from './PlanChatBody';
import RenderAgentMessages from './streaming/StreamingAgentMessage';
import StreamingBufferMessage from './streaming/StreamingBufferMessage';

interface SimplifiedPlanChatProps extends PlanChatProps {
  onPlanReceived?: (planData: MPlanData) => void;
  initialTask?: string;
  planApprovalRequest: MPlanData | null;
  waitingForPlan: boolean;
  messagesContainerRef: React.RefObject<HTMLDivElement>;
  streamingMessageBuffer: string;
  showBufferingText: boolean;
  agentMessages: AgentMessageData[];
  showProcessingPlanSpinner: boolean;
  showApprovalButtons: boolean;
  handleApprovePlan: () => Promise<void>;
  handleRejectPlan: () => Promise<void>;
  processingApproval: boolean;
  /** True when parent attempted to load a plan and failed (404 case). */
  notFound?: boolean;
  attachedFiles?: Array<{ name: string; file_id: string }>;
  generatedFiles?: Array<{
    file_id: string;
    filename: string;
    download_url: string;
  }>;
  onFileSelect?: (file: File) => void;
  onRemoveFile?: (file_id: string) => void;
  onRemoveGeneratedFile?: (file_id: string) => void;
  /** Chat|Plan selector state + change handler (rendered by PlanChatBody). */
  planLane?: boolean;
  onPlanLaneChange?: (checked: boolean) => void;
  showPlanLaneToggle?: boolean;
  /** When the plan ended (ms). Anchors the result summary in time so later
   *  chat turns render BELOW it instead of pushing it to the bottom. */
  bufferAt?: number;
}

const PlanChat: React.FC<SimplifiedPlanChatProps> = ({
  planData,
  input,
  setInput,
  submittingChatDisableInput,
  OnChatSubmit,
  onPlanApproval,
  onPlanReceived,
  initialTask,
  planApprovalRequest,
  waitingForPlan,
  messagesContainerRef,
  streamingMessageBuffer,
  showBufferingText,
  agentMessages,
  showProcessingPlanSpinner,
  showApprovalButtons,
  handleApprovePlan,
  handleRejectPlan,
  processingApproval,
  notFound,
  attachedFiles,
  generatedFiles,
  onFileSelect,
  onRemoveFile,
  onRemoveGeneratedFile,
  planLane,
  onPlanLaneChange,
  showPlanLaneToggle,
  bufferAt = 0,
}) => {
  // Notify parent when an MPlan arrives via planData.
  useEffect(() => {
    if (planData?.mplan) onPlanReceived?.(planData.mplan);
  }, [planData?.mplan, onPlanReceived]);

  // Bridge approval/rejection callbacks through onPlanApproval if provided.
  const onApprove = async () => {
    await handleApprovePlan();
    onPlanApproval?.(true);
  };
  const onReject = async () => {
    await handleRejectPlan();
    onPlanApproval?.(false);
  };

  // The plan card belongs WHERE IT HAPPENED: after the conversation that
  // already existed when the plan was created, before the messages its own
  // execution produced. Any fixed position is wrong in one direction —
  // bottom put it under its own clarifications, top put it above the prior
  // chat. Split by the plan's creation timestamp instead.
  const planStartedAt = planData?.plan?.timestamp
    ? new Date(planData.plan.timestamp).getTime()
    : 0;
  const priorMessages = planStartedAt
    ? agentMessages.filter((m) => m.timestamp < planStartedAt)
    : [];
  const planMessages = planStartedAt
    ? agentMessages.filter((m) => m.timestamp >= planStartedAt)
    : agentMessages;

  // The synthetic "user task" bubble existed only because the request that
  // created the plan was never persisted. Now that the backend stores it, the
  // real message is in the history — render the synthetic one ONLY when the
  // request is absent (older plans), or the task shows twice.
  // Result summary anchored in time: once the plan ends (bufferAt > 0) the
  // messages split around it. While the plan streams (bufferAt === 0) it is
  // the live tail, so everything stays before it.
  const beforeBuffer =
    bufferAt > 0
      ? agentMessages.filter((m) => m.timestamp <= bufferAt)
      : agentMessages;
  const afterBuffer =
    bufferAt > 0 ? agentMessages.filter((m) => m.timestamp > bufferAt) : [];

  const requestAlreadyInHistory = agentMessages.some(
    (m) => m.agent_type === AgentMessageType.HUMAN_AGENT
  );

  if (notFound) {
    return (
      <ContentNotFound subtitle="The requested page could not be found." />
    );
  }

  if (!planData)
    return (
      <div
        style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}
      >
        <InlineToaster />
        <div
          ref={messagesContainerRef}
          style={{
            flex: 1,
            overflow: 'auto',
            padding: '32px 0',
            maxWidth: '800px',
            margin: '0 auto',
            width: '100%',
          }}
        >
          {renderThinkingState(waitingForPlan)}
          {/* The result summary keeps the position where the plan ENDED.
              Anything that came after (the Hosted Agent continuing the
              conversation in chat) renders below it, not above. */}
          <RenderAgentMessages agentMessages={beforeBuffer} />
          {showBufferingText && (
            <StreamingBufferMessage
              streamingMessageBuffer={streamingMessageBuffer}
              isStreaming={bufferAt === 0}
            />
          )}
          {afterBuffer.length > 0 && (
            <RenderAgentMessages agentMessages={afterBuffer} />
          )}
        </div>
        <PlanChatBody
          planData={null as any}
          input={input}
          setInput={setInput}
          submittingChatDisableInput={submittingChatDisableInput}
          OnChatSubmit={OnChatSubmit}
          waitingForPlan={waitingForPlan}
          loading={false}
          attachedFiles={attachedFiles}
          generatedFiles={generatedFiles}
          onFileSelect={onFileSelect}
          onRemoveFile={onRemoveFile}
          onRemoveGeneratedFile={onRemoveGeneratedFile}
          planLane={planLane}
          onPlanLaneChange={onPlanLaneChange}
          showPlanLaneToggle={showPlanLaneToggle}
        />
      </div>
    );
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100vh',
      }}
    >
      {/* Messages Container */}
      <InlineToaster />
      <div
        ref={messagesContainerRef}
        style={{
          flex: 1,
          overflow: 'auto',
          padding: '32px 0',
          maxWidth: '800px',
          margin: '0 auto',
          width: '100%',
        }}
      >
        {/* Conversation that already existed when the plan started */}
        <RenderAgentMessages agentMessages={priorMessages} />

        {/* The task and its proposed plan, in place */}
        {!requestAlreadyInHistory &&
          renderUserPlanMessage(planApprovalRequest, initialTask, planData)}

        <RenderPlanResponse
          planApprovalRequest={planApprovalRequest}
          handleApprovePlan={onApprove}
          handleRejectPlan={onReject}
          processingApproval={processingApproval}
          showApprovalButtons={showApprovalButtons}
        />

        {/* Everything the plan execution produced afterwards */}
        <RenderAgentMessages agentMessages={planMessages} />

        {/* AI thinking state */}
        {renderThinkingState(waitingForPlan)}

        {showProcessingPlanSpinner && renderPlanExecutionMessage()}
        {/* Streaming plan updates */}
        {showBufferingText && (
          <StreamingBufferMessage
            streamingMessageBuffer={streamingMessageBuffer}
            isStreaming={true}
          />
        )}
      </div>

      {/* Chat Input - only show if no plan is waiting for approval */}
      <PlanChatBody
        planData={planData}
        input={input}
        setInput={setInput}
        submittingChatDisableInput={submittingChatDisableInput}
        OnChatSubmit={OnChatSubmit}
        waitingForPlan={waitingForPlan}
        loading={false}
        attachedFiles={attachedFiles}
        generatedFiles={generatedFiles}
        onFileSelect={onFileSelect}
        onRemoveFile={onRemoveFile}
        onRemoveGeneratedFile={onRemoveGeneratedFile}
        planLane={planLane}
        onPlanLaneChange={onPlanLaneChange}
        showPlanLaneToggle={showPlanLaneToggle}
      />
    </div>
  );
};

export default PlanChat;
