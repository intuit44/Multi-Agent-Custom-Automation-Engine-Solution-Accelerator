import React, { useEffect } from "react";
import { PlanChatProps, MPlanData } from "../../models/plan";
import InlineToaster from "../toast/InlineToaster";
import { AgentMessageData } from "@/models";
import renderUserPlanMessage from "./streaming/StreamingUserPlanMessage";
import RenderPlanResponse from "./streaming/StreamingPlanResponse";
import { renderPlanExecutionMessage, renderThinkingState } from "./streaming/StreamingPlanState";
import ContentNotFound from "../NotFound/ContentNotFound";
import PlanChatBody from "./PlanChatBody";
import RenderAgentMessages from "./streaming/StreamingAgentMessage";
import StreamingBufferMessage from "./streaming/StreamingBufferMessage";

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
  generatedFiles?: Array<{ file_id: string; filename: string; download_url: string }>;
  onFileSelect?: (file: File) => void;
  onRemoveFile?: (file_id: string) => void;
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

  if (notFound) {
    return <ContentNotFound subtitle="The requested page could not be found." />;
  }

  if (!planData)
    return (
      <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
        <InlineToaster />
        <div
          ref={messagesContainerRef}
          style={{
            flex: 1,
            overflow: 'auto',
            padding: '32px 0',
            maxWidth: '800px',
            margin: '0 auto',
            width: '100%'
          }}
        >
          {renderThinkingState(waitingForPlan)}
          <RenderAgentMessages agentMessages={agentMessages} />
          {showBufferingText && (
            <StreamingBufferMessage
              streamingMessageBuffer={streamingMessageBuffer}
              isStreaming={true}
            />
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
        />
      </div>
    );
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      height: '100vh',

    }}>
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
          width: '100%'
        }}
      >
        {/* Render agent messages first (includes user messages) */}
        <RenderAgentMessages agentMessages={agentMessages} />

        {/* AI thinking state */}
        {renderThinkingState(waitingForPlan)}

        {/* User plan message */}
        {renderUserPlanMessage(planApprovalRequest, initialTask, planData)}

        {/* Plan response with all information - now renders AFTER agent messages */}
        <RenderPlanResponse
          planApprovalRequest={planApprovalRequest}
          handleApprovePlan={onApprove}
          handleRejectPlan={onReject}
          processingApproval={processingApproval}
          showApprovalButtons={showApprovalButtons}
        />

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
      />

    </div>
  );
};

export default PlanChat;
