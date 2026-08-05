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

_stop() {
    echo "▶ Stopping virtual desktop..."
    if [[ -f "$PIDFILE" ]]; then
        while read -r pid; do
            kill "$pid" 2>/dev/null
        done < "$PIDFILE"
        rm -f "$PIDFILE"
    fi
    pkill -f "Xvfb :${DISPLAY_NUM}"   2>/dev/null
    pkill -f "x11vnc.*display :${DISPLAY_NUM}" 2>/dev/null
    pkill -f "websockify.*${NOVNC_PORT}" 2>/dev/null
    pkill -f "fluxbox" 2>/dev/null
    sleep 1
    # Clean residual X socket if it exists
    sudo rm -f "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null
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
