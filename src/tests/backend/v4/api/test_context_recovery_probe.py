"""Probe determinístico del seam persist→recover.

Turno 1 ejecutó una herramienta y su resultado real (el árbol del workspace)
quedó persistido en la sesión. Turno 2 (`function=<none>`) debe recuperar ese
resultado dentro de `history`. Si NO está, el contexto de una ejecución real no
sobrevive al turno siguiente — que es exactamente el "margen de error" observado
en prod (01:10 ejecuta OK → 01:11:47 responde "sin contexto").

Aísla el lado RECOVER: si este probe queda VERDE, la recuperación preserva el
resultado y la pérdida está del lado PERSIST (el turno de ejecución no guardó su
resultado, o cambió el session_id). Si queda ROJO, la recuperación lo tira.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from v4.api.router import _recover_session_context

# Formato REAL con que _turn_ledger persiste una ejecución (router.py:4319-4323):
# full_text + "\n\n[turn-log]\n" + "\n".join(ledger). Validamos el contrato real,
# NO una aproximación de cómo desearíamos que se viera.
TOOL_RESULT = (
    "Respuesta basada en la ejecución real."
    "\n\n[turn-log]\n"
    "MacaeMcpServer.workspace_list_entries(...)"
    " -> 20 directories, 37 files in '/' of workspace "
    "'multi-agent-custom-automation-engine-solution-accelerator'."
)


@pytest.mark.asyncio
async def test_recovered_history_preserves_prior_tool_result():
    """PROBE A — RECOVER. Si Cosmos tiene el ledger, recover lo conserva.
    (NO prueba que producción persista: eso es Probe B.)"""
    session_id = "sess-probe"
    user_id = "user-probe"

    # Turno 1 quedó persistido: assistant con el [turn-log] real.
    chat_svc = MagicMock()
    chat_svc.get_session = AsyncMock(
        return_value={
            "messages": [
                {"role": "user", "content": "valida el workspace"},
                {"role": "assistant", "content": TOOL_RESULT},
            ]
        }
    )

    # Azure AI Search aún no indexó el turno (lag async) → memoria larga vacía.
    search_stub = MagicMock()
    search_stub.search_chat_history = AsyncMock(return_value=[])

    with patch(
        "common.services.search_index_service.get_search_index_service",
        AsyncMock(return_value=search_stub),
    ):
        history = await _recover_session_context(
            chat_svc,
            session_id,
            user_id,
            current_message="¿qué directorios listaste?",
        )

    assert any(
        "[turn-log]" in m.get("content", "")
        and "workspace_list_entries" in m.get("content", "")
        and "20 directories, 37 files" in m.get("content", "")
        for m in history
    ), (
        "El [turn-log] de la ejecución del turno 1 NO sobrevivió al history del "
        "turno 2 — recover lo tira."
    )
