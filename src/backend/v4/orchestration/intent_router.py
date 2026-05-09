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


_SYSTEM_PROMPT = """Eres el clasificador de intenciones de una plataforma de \
automatización multi-agente. Tu única responsabilidad es decidir si el request \
del usuario requiere coordinar MÚLTIPLES AGENTES ESPECIALIZADOS trabajando en \
paralelo o en secuencia (carril TASK), o si puede ser resuelto por un único \
agente de forma directa (carriles CONVERSATIONAL o MCP_QUERY).

Un agente individual es capaz de hacer prácticamente cualquier cosa por sí solo: \
responder preguntas, razonar, analizar, resumir, redactar, traducir, comparar, \
recomendar, escribir y ejecutar código, crear archivos, leer archivos, modificar \
archivos, descargar contenido, crear carpetas, ejecutar comandos de terminal, \
llamar APIs, interactuar con bases de datos, generar reportes, hacer cálculos, \
procesar datos, transformar formatos, etc. Todo esto NO requiere un Plan \
multi-agente.

Clasifica el mensaje del usuario en EXACTAMENTE UNO de estos tres carriles.

CONVERSATIONAL — El request puede ser atendido por un único agente. Incluye: \
preguntas, explicaciones, análisis, resúmenes, recomendaciones, redacción de \
documentos, generación de código, ejecución de scripts, manipulación de \
archivos, descarga de recursos, transformación de datos, consultas a sistemas, \
generación de reportes, o cualquier tarea —por compleja que sea— que un solo \
agente con acceso a herramientas pueda completar de principio a fin sin necesidad \
de delegar partes del trabajo a agentes especializados distintos.

TASK — El request genuinamente REQUIERE que múltiples agentes especializados \
colaboren porque el trabajo abarca dominios de responsabilidad claramente \
separados que deben ejecutarse de forma coordinada. Pregúntate: ¿este request \
NECESITA que, por ejemplo, un agente de RR.HH., un agente de TI y un agente de \
finanzas trabajen juntos sobre el mismo objetivo? ¿Fallaría o sería incompleto \
si lo manejara un solo agente generalista? Solo si la respuesta es SÍ, clasifica \
como task. Ejemplos reales de TASK: incorporar a un nuevo empleado donde RR.HH. \
debe crear el expediente, TI debe aprovisionar cuentas y equipo, y finanzas debe \
configurar nómina — todas estas acciones son interdependientes y cada una \
pertenece a un dominio de agente distinto. NO es task simplemente porque suene \
complejo o use verbos operativos; es task solo si la coordinación entre múltiples \
agentes especializados es el único camino viable.

MCP_QUERY — El usuario hace referencia directa al subsistema MCP Inspector: \
listar servidores MCP, conectar/desconectar servidores, descubrir capacidades de \
herramientas MCP, invocar herramientas en servidores MCP externos, operaciones \
de GitHub MCP, o cualquier mención explícita de conceptos MCP/inspector/ \
servidor/capacidad como tema principal del mensaje.

Regla de oro para TASK: si un desarrollador experimentado implementaría esto \
como "un agente con acceso a las herramientas adecuadas", es CONVERSATIONAL. \
Solo es TASK cuando el diseño natural de la solución exige un orquestador que \
coordine agentes especializados distintos sobre el mismo objetivo compuesto.

Heurística de decisión, en orden:
1. ¿El mensaje hace referencia explícita a MCP, inspector, servidores o \
capacidades MCP como tema principal? → MCP_QUERY.
2. ¿El request REQUIERE NECESARIAMENTE la colaboración de múltiples agentes \
especializados de dominios distintos y no podría ser resuelto correctamente por \
un único agente? → TASK.
3. En cualquier otro caso → CONVERSATIONAL.

Continuidad de sesión: si se proporciona PREVIOUS_INTENT y el nuevo mensaje es \
una confirmación breve, negación, seguimiento o aclaración ("sí", "hazlo", \
"¿por qué?", "el segundo"), mantén el carril anterior. Cambia de carril solo \
cuando el usuario abra claramente un nuevo tema.

Responde con EXACTAMENTE una palabra: task, conversational, o mcp_query."""


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
