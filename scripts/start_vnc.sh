#!/bin/bash
# Virtual desktop helper: Xvfb + fluxbox + x11vnc + noVNC on port 6080.
# Usage:
#   ./scripts/start_vnc.sh start   — start the virtual desktop
#   ./scripts/start_vnc.sh stop    — stop everything and clean up
#   ./scripts/start_vnc.sh status  — show running processes

DISPLAY_NUM=99
VNC_PORT=5900
NOVNC_PORT=6080
PIDFILE=/tmp/.vnc_stack.pids
APP_URL=${APP_URL:-http://localhost:3001}

_stop() {
    echo "▶ Stopping virtual desktop..."

    # 1) Los PIDs que arrancó este script.
    if [[ -f "$PIDFILE" ]]; then
        while read -r pid; do
            kill "$pid" 2>/dev/null
        done < "$PIDFILE"
        rm -f "$PIDFILE"
    fi

    # 2) Restos de corridas anteriores, o procesos arrancados a mano que
    #    nunca entraron al PIDFILE. Patrones anclados al binario: un
    #    `pkill -f websockify` suelto también mata al shell que lo invoca
    #    cuando su propia línea de comando menciona esa palabra.
    pkill -f "^Xvfb :${DISPLAY_NUM}\b"     2>/dev/null
    pkill -f "^x11vnc .*:${DISPLAY_NUM}\b" 2>/dev/null
    pkill -x websockify                    2>/dev/null
    pkill -x fluxbox                       2>/dev/null
    sleep 1

    # 3) Escalada por PUERTO — lo único que no depende de la línea de comando.
    #    x11vnc puede quedarse en bucle cerrado (STAT R, CPU al tope) sin
    #    llegar a atender SIGTERM, y sigue ocupando el 5900. Entonces el
    #    x11vnc nuevo no puede bindear y muere, mientras el zombi acepta la
    #    conexión TCP sin servirla: noVNC se queda en "Connecting..." para
    #    siempre. Visible en `ss -ltn` como Recv-Q creciendo en el 5900.
    for port in "$VNC_PORT" "$NOVNC_PORT"; do
        if fuser -s "${port}/tcp" 2>/dev/null; then
            fuser -k -TERM "${port}/tcp" &>/dev/null
            sleep 1
            fuser -k -KILL "${port}/tcp" &>/dev/null
        fi
    done

    sudo rm -f "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null

    # 4) Comprobar antes de afirmar. El "✅ Stopped." anterior salía siempre,
    #    incluso con los puertos todavía tomados.
    local busy=""
    for port in "$VNC_PORT" "$NOVNC_PORT"; do
        fuser -s "${port}/tcp" 2>/dev/null && busy+=" ${port}"
    done
    if [[ -n "$busy" ]]; then
        echo "⚠️  Puertos aún ocupados:${busy} — revisa 'fuser -v ${VNC_PORT}/tcp'"
        return 1
    fi
    echo "✅ Stopped."
}

_status() {
    echo "=== Virtual desktop processes ==="
    pgrep -a -f "Xvfb|x11vnc|websockify|fluxbox" 2>/dev/null || echo "(none running)"
    echo "=== X11 sockets ==="
    ls -la /tmp/.X11-unix/ 2>/dev/null || echo "(none)"
}

_start() {
    # Stop any previous instance first
    _stop

    echo "▶ Starting Xvfb on display :${DISPLAY_NUM}..."
    Xvfb :${DISPLAY_NUM} -screen 0 1440x900x24 &
    echo $! > "$PIDFILE"
    sleep 1

    echo "▶ Starting fluxbox..."
    DISPLAY=:${DISPLAY_NUM} fluxbox &>/tmp/fluxbox.log &
    echo $! >> "$PIDFILE"
    sleep 1

    echo "▶ Starting x11vnc on port ${VNC_PORT}..."
    # Unset WAYLAND_DISPLAY so x11vnc doesn't auto-detect a Wayland session
    # (VS Code sets it) and exit instead of grabbing the Xvfb X11 display.
    env -u WAYLAND_DISPLAY DISPLAY=:${DISPLAY_NUM} x11vnc -display :${DISPLAY_NUM} -nopw -forever -shared \
      -listen localhost -rfbport ${VNC_PORT} &>/tmp/x11vnc.log &
    echo $! >> "$PIDFILE"
    sleep 1

    echo "▶ Starting noVNC on http://localhost:${NOVNC_PORT}/vnc.html..."
    websockify --web /usr/share/novnc ${NOVNC_PORT} localhost:${VNC_PORT} &>/tmp/novnc.log &
    echo $! >> "$PIDFILE"
    sleep 1

    # Un escritorio recién creado no tiene nada encima y se ve gris, que es
    # indistinguible de "noVNC no conecta". El navegador se abre aquí para que
    # el script se baste solo. Su PID va al PIDFILE; además muere junto con
    # Xvfb, así que `stop` lo limpia por las dos vías.
    echo "▶ Starting Chrome on the desktop (${APP_URL})..."
    env DISPLAY=:${DISPLAY_NUM} google-chrome \
        --user-data-dir="$HOME/.config/google-chrome" \
        --no-sandbox --no-first-run --no-default-browser-check \
        --start-maximized "${APP_URL}" &>/tmp/chrome-vnc.log &
    echo $! >> "$PIDFILE"
    sleep 8

    # No anunciar "ready" sin comprobarlo: el ✅ salía aunque x11vnc no
    # hubiera podido bindear su puerto, y el síntoma aparecía mucho después
    # como un noVNC que carga la página y se queda en "Connecting...".
    local failed=""
    pgrep -f "^Xvfb :${DISPLAY_NUM}\b" >/dev/null 2>&1 || failed+=" Xvfb"
    fuser -s "${VNC_PORT}/tcp"   2>/dev/null || failed+=" x11vnc(:${VNC_PORT})"
    fuser -s "${NOVNC_PORT}/tcp" 2>/dev/null || failed+=" noVNC(:${NOVNC_PORT})"
    pgrep -f "google-chrome .*--start-maximized" >/dev/null 2>&1 || failed+=" Chrome"
    if [[ -n "$failed" ]]; then
        echo ""
        echo "❌ No arrancó:${failed}"
        echo "   Logs: /tmp/x11vnc.log  /tmp/novnc.log  /tmp/fluxbox.log"
        return 1
    fi

    echo ""
    echo "✅ Virtual desktop ready."
    echo "   Browser (VS Code port forwarding): http://localhost:${NOVNC_PORT}/vnc.html"
    echo ""
    echo "Run playwright codegen:"
    echo "   cd src/frontend && DISPLAY=:${DISPLAY_NUM} uv run playwright install chromium"
    echo "   DISPLAY=:${DISPLAY_NUM} uv run playwright codegen http://localhost:3001"
    echo ""
    echo "Stop when done:"
    echo "   ./scripts/start_vnc.sh stop"
}

case "${1:-start}" in
    start)  _start  ;;
    stop)   _stop   ;;
    status) _status ;;
    *)      echo "Usage: $0 {start|stop|status}"; exit 1 ;;
esac
