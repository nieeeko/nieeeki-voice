"""Verbindung zum nieeeki-Host (Werkzeuge am Windows-PC) per MCP ueber stdio.

Der Voice-Core startet host/nieeeki_host.py als Unterprozess. Dadurch ist der Host nicht im Netz
erreichbar, und der Unterprozess erbt nur die Standard-Umgebung von MCP (PATH, USERPROFILE, ...),
keine API-Schluessel aus .env. Risikoklassen und Freigaben prueft der Host selbst.

NIEEEKI_PC_HOST=off schaltet die PC-Werkzeuge ab.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from loguru import logger
from mcp import StdioServerParameters

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.mcp_service import MCPClient

HOST_DIR = Path(__file__).resolve().parent.parent / "host"
CONNECT_TIMEOUT_SECS = 20


def host_client() -> MCPClient | None:
    if os.getenv("NIEEEKI_PC_HOST", "on").lower() in ("off", "0", "false", "aus"):
        return None
    script = str(HOST_DIR / "nieeeki_host.py")
    venv_python = HOST_DIR / ".venv" / "Scripts" / "python.exe"
    if venv_python.is_file():
        # Direkt das Python des Hosts: startet in ~1 s statt ~6 s ueber "uv run"
        command, args = str(venv_python), [script, "--stdio"]
    else:
        uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv.exe")
        command, args = uv, ["run", "--project", str(HOST_DIR), "python", script, "--stdio"]
    params = StdioServerParameters(command=command, args=args, cwd=str(HOST_DIR))
    return MCPClient(server_params=params)


async def host_tools(client: MCPClient | None) -> ToolsSchema | None:
    """Werkzeuge des Hosts; None, wenn er aus ist oder nicht startet (Gespraech laeuft trotzdem)."""
    if client is None:
        return None
    try:
        tools = await asyncio.wait_for(client.tools(), CONNECT_TIMEOUT_SECS)
        logger.info("PC-Host verbunden: {}", [t.name for t in tools.standard_tools])
        return tools
    except Exception as e:  # PC-Werkzeuge sind optional
        logger.warning("PC-Host nicht verfuegbar: {}", e)
        return None
