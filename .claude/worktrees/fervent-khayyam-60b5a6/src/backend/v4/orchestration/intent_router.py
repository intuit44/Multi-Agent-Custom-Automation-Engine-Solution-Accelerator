"""
IntentRouter — Classifies user messages into intent categories.

Uses LLM-based classification (AzureOpenAIChatClient) for accurate intent
detection across languages and phrasings.  The LLM is the *sole* decision
maker; there are no hardcoded keyword/regex fallbacks.

Routes messages to:
  - "task"           → Full plan workflow (existing process_request flow)
  - "conversational" → Direct agent response without plan creation
  - "mcp_query"      → MCP Inspector / bridge query via TechnicalSupportAgent
"""

import logging
from enum import Enum
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class Intent(str, Enum):
    TASK = "task"
    CONVERSATIONAL = "conversational"
    MCP_QUERY = "mcp_query"


class IntentResult(BaseModel):
    intent: Intent
    confidence: float
    reasoning: str


_SYSTEM_PROMPT = """You are the intent classifier for a multi-agent automation \
platform that orchestrates real business workflows across HR, IT support, \
marketing, customer success, sales operations, and contract management. The \
platform can EXECUTE multi-step plans (provisioning accounts, onboarding people, \
generating documents, configuring systems) AND can ANSWER questions, AND can \
operate external tools through MCP servers (filesystem, GitHub, third-party \
APIs, etc.).

Every user message MUST be routed through the multi-step plan \

Classify the user's message into exactly ONE of these three lanes.

CONVERSATIONAL — Lane for messages that can be resolved with a textual answer, \
explanation, analysis, summary, recommendation, or a clarification. The \
user wants information, opinion, or insight \
Greetings, farewells, "what is X", "how does Y work", "compare A vs B", \
"summarize this", "explain that", "analyze customer Z's behavior", "give me \
recommendations" — all conversational. Asking ABOUT a process is \
conversational, even if the process itself would be a task.

TASK — Lane for messages where the user wants the platform to PERFORM a \
multi-step business workflow that changes state in real systems: provisioning, \
onboarding, offboarding, generating and publishing artifacts, scheduling, \
configuring, sending, processing, registering, assigning. The user is issuing \
an order, not asking for information. The verb is operative ("onboard", \
"create the account", "send the welcome email", "process the return", \
"configure the laptop", "generate and publish the press release"). If the \
message names a person, product, or entity AS THE OBJECT of an action verb, \
it's almost certainly a task.

MCP_QUERY — Lane for messages that interact with the MCP Inspector subsystem: \
listing connected servers, connecting/disconnecting MCP servers, discovering \
tool capabilities, calling tools on external MCP servers, browsing the \
filesystem MCP, GitHub MCP operations, or anything where the user references \
MCP/inspector/server/tool/capability concepts directly.

Decision heuristic, in order:
1. Does the message reference MCP, inspector, servers, tools, or external \
platforms via MCP? → MCP_QUERY.
2. Is the user issuing an operative command to change state, not asking a \
question? → TASK.
3. Otherwise → MCP_QUERY.

Session continuity: if PREVIOUS_INTENT is provided and the new message is a \
short confirmation, denial, follow-up, or clarification ("yes", "do it", \
"why?", "the second one"), keep the previous lane. Switch lanes only when the \
user clearly opens a new topic.

Respond with EXACTLY one word: task, or mcp_query."""


class IntentRouter:
    """
    LLM-based intent classifier with keyword fallback.

    Uses AzureOpenAIChatClient.get_response() for accurate classification
    across languages. Falls back to keyword heuristics on failure.
    """

    @staticmethod
    async def classify_async(
        message: str,
        previous_intent: Optional[str] = None,
        agent_response: Optional[str] = None,
    ) -> IntentResult:
        """Classify using LLM as the sole decision maker.

        Args:
            message: The user message to classify.
            previous_intent: The intent of the last assistant message in this
                session.  Passed as structured context so the LLM can
                maintain session lane continuity.
            agent_response: The response already produced by ChatMCPAgent
                (which queried the KB).  When provided, the classifier sees
                the grounded context — full history, customer data, contracts,
                etc. — and can decide correctly whether the request warrants
                a multi-agent plan.
        """
        if not message or not message.strip():
            return IntentResult(
                intent=Intent.CONVERSATIONAL,
                confidence=1.0,
                reasoning="Empty message",
            )

        try:
            from agent_framework import ChatOptions, Message
            from agent_framework.azure import AzureOpenAIChatClient
            from azure.identity.aio import DefaultAzureCredential

            from common.config.app_config import config

            client = AzureOpenAIChatClient(
                endpoint=config.AZURE_OPENAI_ENDPOINT,
                deployment_name=config.AZURE_OPENAI_DEPLOYMENT_NAME,
                credential=DefaultAzureCredential(),
            )

            # Build the user payload with agent context + session continuity
            user_text = message.strip()
            if agent_response:
                user_text = (
                    f"AGENT_RESPONSE (from KB-backed ChatMCPAgent):\n{agent_response.strip()}\n\n"
                    f"USER_MESSAGE: {user_text}"
                )
            if previous_intent:
                user_text = f"PREVIOUS_INTENT: {previous_intent}\n{user_text}"

            messages = [
                Message(role="system", text=_SYSTEM_PROMPT),
                Message(role="user", text=user_text),
            ]
            options = ChatOptions(max_tokens=20, temperature=0.3)

            response = await client.get_response(messages, options=options)
            raw = (response.text or "").strip().lower().rstrip(".")

            intent_map = {
                "mcp_query": Intent.MCP_QUERY,
                "task": Intent.TASK,
                "conversational": Intent.CONVERSATIONAL,
            }

            # Exact match first
            if raw in intent_map:
                return IntentResult(
                    intent=intent_map[raw],
                    confidence=0.95,
                    reasoning=f"LLM exact: {raw}",
                )

            # Partial match as fallback
            for key, intent in intent_map.items():
                if key in raw:
                    return IntentResult(
                        intent=intent,
                        confidence=0.85,
                        reasoning=f"LLM partial: {raw}",
                    )

            # LLM returned unexpected output — use previous_intent if available
            logger.warning(
                "IntentRouter LLM returned unexpected: '%s', falling back", raw
            )

        except Exception as e:
            logger.warning("IntentRouter LLM call failed (%s), falling back", e)

        # ── Fallback: preserve session lane, no keyword heuristics ────
        if previous_intent:
            try:
                kept = Intent(previous_intent)
                logger.info(
                    "IntentRouter fallback: preserving previous_intent '%s'",
                    previous_intent,
                )
                return IntentResult(
                    intent=kept,
                    confidence=0.7,
                    reasoning=f"fallback: preserved previous_intent ({previous_intent})",
                )
            except ValueError:
                pass

        return IntentResult(
            intent=Intent.CONVERSATIONAL,
            confidence=0.5,
            reasoning="fallback: no previous_intent, defaulting to conversational",
        )
