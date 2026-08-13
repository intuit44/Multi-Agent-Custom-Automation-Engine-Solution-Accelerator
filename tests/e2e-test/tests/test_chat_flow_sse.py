"""Captura el SSE del turno real y valida la lógica, no el renderizado.

La grabación de codegen (test_chat_flow_generated.py) sirve como evidencia de
lo que se ve, pero no como test: sus locators son clases generadas de FluentUI
(`.___1osfnhx_0000000`) y nombres de archivo únicos por corrida
(`cfile_6a7ad49…`), así que no reproducen.

Lo que sí necesitás validar viaja por el mismo navegador, en el stream de
`/api/v4/chat/message/stream`: qué tools se llamaron y contra qué servidor,
cada `generated_file` con su `file_id`/`container_id`, los tokens (donde
aparece el link `sandbox:`) y el evento de error que produce
`classify_tool_error`. Playwright puede leer ese cuerpo entero.

Requiere frontend en :3001 y backend en :8000 corriendo.

    cd tests/e2e-test
    DISPLAY=:99 .venv/bin/pytest tests/test_chat_flow_sse.py --headed
"""

import json

import pytest
from playwright.sync_api import Page

APP = "http://localhost:3001/"
PROMPT = "Genera un grafico de barras con categorias A, B, C, D y valores 20, 30, 60, 80"

# Nombres accesibles, no hashes CSS: sobreviven a los rebuilds del frontend.
BOX = ("textbox", "Describe your task...")
SEND = ("button", "Send message")


def _send_and_capture(page: Page, prompt: str) -> list[dict]:
    """Manda un mensaje y devuelve los eventos SSE decodificados del turno."""
    page.goto(APP)
    page.get_by_role(BOX[0], name=BOX[1]).fill(prompt)

    with page.expect_response(
        lambda r: "/chat/message/stream" in r.url, timeout=180_000
    ) as got:
        page.get_by_role(SEND[0], name=SEND[1]).click()

    body = got.value.text()
    return [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def _of(events: list[dict], etype: str) -> list[dict]:
    return [e for e in events if e.get("type") == etype]


@pytest.fixture(scope="module")
def chart_turn(browser):
    """Un solo turno real; todos los tests de abajo leen la misma captura.

    Cada turno invoca Foundry y escribe en Cosmos de verdad, así que se hace
    una vez por módulo y no uno por aserción.
    """
    page = browser.new_page()
    try:
        yield _send_and_capture(page, PROMPT)
    finally:
        page.close()


def test_el_turno_produjo_eventos(chart_turn):
    """Guardia: si el stream vino vacío, lo de abajo no significa nada."""
    assert chart_turn, "el SSE no trajo ningún evento"


def test_un_solo_artefacto_por_grafico(chart_turn):
    """Un gráfico pedido = un artefacto.

    Foundry manda dos archivos por figura (la autocapturada, sin `filename`, y
    la del `savefig`), con file_id y tamaños distintos. Dos eventos = la imagen
    y el chip de descarga duplicados en pantalla.
    """
    files = _of(chart_turn, "generated_file")
    assert len(files) == 1, [
        (f.get("file_id"), f.get("filename")) for f in files
    ]


def test_ningun_link_sandbox_en_el_turno_vivo(chart_turn):
    """`sandbox:/mnt/data/...` no es alcanzable desde el navegador.

    `ChatService.loadSession` los reescribe al reabrir la sesión, pero eso no
    corre mientras se streamea: en el turno vivo el link queda muerto.
    """
    texto = "".join(e.get("content", "") for e in _of(chart_turn, "token"))
    assert "sandbox:" not in texto, texto


def test_sin_errores_de_herramienta(chart_turn):
    """Un 5xx transitorio no debe terminar el turno.

    `tool_errors.run_with_backoff` existe y clasifica esto como TRANSIENT,
    pero no está aplicado a este punto de llamada.
    """
    errores = _of(chart_turn, "error")
    assert not errores, errores


def test_metadata_de_herramientas(chart_turn, record_property):
    """No asevera: deja registrada la metadata del turno en el reporte.

    Qué tool se llamó, contra qué servidor (ahí se distingue el MCP remoto del
    registrado en Cosmos) y con qué resultado.
    """
    for act in _of(chart_turn, "tool_activity"):
        record_property(
            f"tool:{act.get('tool')}",
            f"{act.get('activity')} server={act.get('server')} "
            f"ok={act.get('success')} {str(act.get('message') or act.get('args') or '')[:200]}",
        )
    for f in _of(chart_turn, "generated_file"):
        record_property(
            f"file:{f.get('file_id')}",
            f"name={f.get('filename')} container={f.get('container_id')}",
        )
