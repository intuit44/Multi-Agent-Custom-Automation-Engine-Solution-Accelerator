#!/usr/bin/env python3
"""
MCP Server: Command Validator
Validates and executes shell commands safely via Model Context Protocol.
"""

import subprocess
import re
import sys
import os
from typing import Tuple, Dict, Any
from mcp.server.fastmcp import FastMCP

# Inicializamos el servidor FastMCP para la Foundry Toolkit
mcp = FastMCP("command-validator")

# Lee la configuración de auto-fix desde las variables de entorno de tu mcp.json
AUTO_FIX_ENV = os.environ.get("AUTO_FIX", "true").lower() == "true"

# ─────────────────────────────────────────────────────────────
# Patrones de Validación (Se mantienen exactamente tus reglas)
# ─────────────────────────────────────────────────────────────
FOREGROUND_DANGER_PATTERNS = {
    r"docker logs(?!\s+--tail)": {
        "danger": "docker logs without --tail will stream indefinitely",
        "fix": "docker logs --tail 100 <container>",
        "suggestion": "Add '--tail 100' or '--tail 50' to limit output",
    },
    r"tail -f": {
        "danger": "tail -f will follow file indefinitely",
        "fix": "tail -n 100 <file>",
        "suggestion": "Use 'tail -n 100' to show last 100 lines, or 'tail -f' with timeout: timeout 30 tail -f <file>",
    },
    r"journalctl(?!\s+(-n|--lines))": {
        "danger": "journalctl without -n/--lines will page through all logs",
        "fix": "journalctl -n 100",
        "suggestion": "Add '-n 100' or '--lines=100' to limit output",
    },
    r"kubectl logs(?!\s+--tail)": {
        "danger": "kubectl logs without --tail will stream indefinitely",
        "fix": "kubectl logs --tail=100 <pod>",
        "suggestion": "Add '--tail=100' to limit output",
    },
    r"curl\s+(?!.*-m|.*--max-time|.*-w)": {
        "danger": "curl without timeout may hang indefinitely",
        "fix": "curl -m 10 <url>",
        "suggestion": "Add '-m 10' (10s timeout) or use '-w' to write progress",
    },
}

SERVICE_PATTERNS = {
    r"docker run(?!\s+-d)": {
        "danger": "docker run without -d will block foreground",
        "fix": "docker run -d <image>",
        "suggestion": "Add '-d' flag to run detached",
    },
    r"docker-compose up(?!\s+-d)": {
        "danger": "docker-compose up without -d will block foreground",
        "fix": "docker-compose up -d",
        "suggestion": "Add '-d' flag to run in background",
    },
    r"uvicorn\s+.*(?!--reload)(?!&)": {
        "danger": "uvicorn without & will block foreground",
        "fix": "uvicorn app:app &",
        "suggestion": "Append ' &' to run in background, or use detached mode",
    },
    r"npm run dev(?!&)": {
        "danger": "npm run dev without & will block foreground",
        "fix": "npm run dev &",
        "suggestion": "Append ' &' to run in background",
    },
    r"python\s+-m\s+pytest\s+.*--watch": {
        "danger": "pytest --watch will run indefinitely",
        "fix": "pytest <file> (single run)",
        "suggestion": "Remove --watch flag or run single test: pytest <file>::<test>",
    },
}


def validate_command(command: str) -> Tuple[bool, Dict[str, Any]]:
    result = {
        "safe": True,
        "command": command,
        "warnings": [],
        "suggestions": [],
        "recommended_command": command,
    }

    if command.strip().startswith("#") or not command.strip():
        return True, result

    for pattern, info in FOREGROUND_DANGER_PATTERNS.items():
        if re.search(pattern, command, re.IGNORECASE):
            result["safe"] = False
            result["warnings"].append(info["danger"])
            result["suggestions"].append(info["suggestion"])
            result["recommended_command"] = info["fix"]
            break

    if result["safe"]:
        for pattern, info in SERVICE_PATTERNS.items():
            if re.search(pattern, command, re.IGNORECASE):
                result["safe"] = False
                result["warnings"].append(info["danger"])
                result["suggestions"].append(info["suggestion"])
                result["recommended_command"] = info["fix"]
                break

    return result["safe"], result


# ─────────────────────────────────────────────────────────────
# EXPOSICIÓN COMO HERRAMIENTA MCP NATIVA
# ─────────────────────────────────────────────────────────────
@mcp.tool()
def execute_safe_command(command: str) -> str:
    """
    Valida y ejecuta un comando de shell de forma segura en el workspace.
    Aplica auto-fix automático si el comando bloquea el foreground.
    """
    is_safe, validation = validate_command(command)
    auto_fix = AUTO_FIX_ENV

    if not is_safe:
        # Enviamos los logs informativos a stderr para NO romper el transporte stdio JSON-RPC
        print(f"⚠️ Command unsafe: {validation['warnings'][0]}", file=sys.stderr)
        if auto_fix:
            print(
                f"📝 Auto-fixing to instead use: {validation['recommended_command']}",
                file=sys.stderr,
            )
            command = validation["recommended_command"]
        else:
            return f"ERROR: Command rejected due to being UNSAFE. Reason: {validation['warnings'][0]}. Suggestion: {validation['suggestions'][0]}"

    try:
        # Fijar directorio de ejecución por defecto del Solution Accelerator
        workspace_cwd = (
            "/workspaces/Multi-Agent-Custom-Automation-Engine-Solution-Accelerator"
        )

        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            timeout=300,  # 5 min max
            text=True,
            cwd=workspace_cwd,
            encoding="utf-8",
            errors="replace",
        )

        output_summary = f"--- STDOUT ---\n{result.stdout[:5000]}\n"
        if result.stderr:
            output_summary += f"\n--- STDERR ---\n{result.stderr[:5000]}"

        return f"Execution Success: {result.returncode == 0}\nExit Code: {result.returncode}\n\n{output_summary}"

    except subprocess.TimeoutExpired:
        return "ERROR: El comando ha superado el timeout límite de 300 segundos."
    except Exception as e:
        return f"ERROR durante la ejecución: {str(e)}"


if __name__ == "__main__":
    # Arranca el servidor MCP real interactuando mediante canales stdio limpios
    mcp.run(transport="stdio")
