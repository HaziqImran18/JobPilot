"""MCP client runtime: spawns the stdio servers and discovers their tools."""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import sys
import threading
import time
from collections import OrderedDict
from contextlib import AsyncExitStack, suppress
from datetime import timedelta
from typing import Any

from jobpilot.core.config import PROJECT_ROOT, Settings, get_settings, user_settings
from jobpilot.core.models import PermissionTier, ToolResult, ToolSpec
from jobpilot.core.user_context import current_user_id
from jobpilot.core.utils import json_dumps, truncate
from jobpilot.harness.permissions import TOOL_TIERS

SERVER_MODULES: OrderedDict[str, str] = OrderedDict(
    [
        ("jobs", "jobpilot.servers.jobs_server"),
        ("profile", "jobpilot.servers.profile_server"),
        ("memory", "jobpilot.servers.memory_server"),
        ("match", "jobpilot.servers.match_server"),
        ("tailor", "jobpilot.servers.tailor_server"),
        ("tracker", "jobpilot.servers.tracker_server"),
    ]
)


class MCPManager:
    """Long-lived MCP sessions on a private asyncio loop.

    The agent loop is synchronous (Flask request threads), so each tool call is
    submitted with ``run_coroutine_threadsafe``. Servers stay alive for the whole
    process, which keeps discovery and call latency low.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="jobpilot-mcp", daemon=True)
        self._ready = threading.Event()
        self._start_lock = threading.RLock()
        self._started = False
        self._sessions: dict[str, Any] = {}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._tool_map: dict[str, tuple[str, ToolSpec]] = {}
        self._server_tools: dict[str, list[ToolSpec]] = {}
        self._errors: dict[str, str] = {}

    # ---------------------------------------------------------------- lifecycle
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            if not self._thread.is_alive():
                self._thread.start()
            if not self._ready.wait(timeout=10):
                raise RuntimeError("MCP event loop failed to start.")
            future = asyncio.run_coroutine_threadsafe(self._connect_all(), self._loop)
            try:
                future.result(timeout=120)
            except Exception as exc:  # pragma: no cover - catastrophic startup failure
                raise RuntimeError(f"MCP startup failed: {exc}") from exc
            self._started = True

    async def _connect_all(self) -> None:
        await asyncio.gather(
            *(self._connect_one(server, module) for server, module in SERVER_MODULES.items())
        )
        if not self._tool_map:
            self._errors["all"] = "No MCP tools could be discovered."

    async def _connect_one(self, server: str, module: str) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        stack = AsyncExitStack()
        try:
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", module],
                env=self._server_env(),
                cwd=str(PROJECT_ROOT),
                encoding="utf-8",
            )
            read, write = await asyncio.wait_for(
                stack.enter_async_context(stdio_client(params)), timeout=30
            )
            session = await asyncio.wait_for(
                stack.enter_async_context(ClientSession(read, write)), timeout=15
            )
            await asyncio.wait_for(session.initialize(), timeout=30)
            listed = await asyncio.wait_for(session.list_tools(), timeout=20)
            specs: list[ToolSpec] = []
            for tool in listed.tools:
                name = tool.name
                if name in self._tool_map:
                    self._errors[name] = f"Duplicate tool name from {server}; kept the first."
                    continue
                spec = ToolSpec(
                    name=name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema or {},
                    tier=TOOL_TIERS.get(name, PermissionTier.external),
                    server=server,
                )
                specs.append(spec)
                self._tool_map[name] = (server, spec)
            self._server_tools[server] = specs
            self._sessions[server] = session
            self._stacks[server] = stack
        except Exception as exc:  # noqa: BLE001 - one server failure must not kill the rest
            self._errors[server] = truncate(str(exc), 300)
            with suppress(Exception):
                await stack.aclose()

    def stop(self) -> None:
        with self._start_lock:
            if not self._started:
                return
            try:
                future = asyncio.run_coroutine_threadsafe(self._close_all(), self._loop)
                future.result(timeout=15)
            except Exception:
                pass
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._started = False

    async def _close_all(self) -> None:
        for server in list(self._stacks):
            stack = self._stacks.pop(server)
            with suppress(Exception):
                await stack.aclose()
        self._sessions.clear()

    # ---------------------------------------------------------------------- info
    @property
    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    @property
    def servers(self) -> list[str]:
        return list(self._sessions)

    def tools(self) -> list[ToolSpec]:
        return [spec for _, spec in self._tool_map.values()]

    def tool(self, name: str) -> ToolSpec | None:
        entry = self._tool_map.get(name)
        return entry[1] if entry else None

    # --------------------------------------------------------------------- calls
    def call(self, name: str, arguments: dict[str, Any], *, timeout: float = 180) -> ToolResult:
        if not self._started:
            self.start()
        entry = self._tool_map.get(name)
        if entry is None:
            return ToolResult(
                call_id="",
                name=name,
                ok=False,
                error=f"Unknown tool `{name}`. Discovered tools: {', '.join(sorted(self._tool_map))}",
                tier=PermissionTier.external,
            )
        server, spec = entry
        started = time.perf_counter()
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._call_tool(server, name, arguments), self._loop
            )
            result = future.result(timeout=timeout)
            duration = int((time.perf_counter() - started) * 1000)
            return self._convert_result(name, spec, result, duration)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id="",
                name=name,
                ok=False,
                error=truncate(str(exc), 500),
                duration_ms=int((time.perf_counter() - started) * 1000),
                tier=spec.tier,
            )

    async def _call_tool(self, server: str, name: str, arguments: dict[str, Any]) -> Any:
        session = self._sessions[server]
        return await session.call_tool(
            name,
            arguments,
            read_timeout_seconds=timedelta(seconds=180),
        )

    @staticmethod
    def _convert_result(name: str, spec: ToolSpec, result: Any, duration: int) -> ToolResult:
        texts = [text for item in (result.content or []) if (text := getattr(item, "text", ""))]
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict) and set(structured.keys()) == {"result"}:
            structured = structured["result"]
        data: Any = structured
        if data is None:
            joined = "\n".join(texts).strip()
            if joined:
                try:
                    data = json.loads(joined)
                except json.JSONDecodeError:
                    data = joined
        content = json_dumps(data) if data is not None else "\n".join(texts)
        return ToolResult(
            call_id="",
            name=name,
            ok=not bool(getattr(result, "isError", False)),
            content=truncate(content, 20000),
            data=data,
            error="" if not getattr(result, "isError", False) else truncate(content, 500),
            duration_ms=duration,
            tier=spec.tier,
        )

    def _server_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "JOBPILOT_DB_PATH": str(self.settings.db_path),
                "JOBPILOT_DATA_DIR": str(self.settings.data_dir),
                "JOBPILOT_TRACES_DIR": str(self.settings.traces_dir),
                "JOBPILOT_UPLOADS_DIR": str(self.settings.uploads_dir),
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            }
        )
        return env


_managers: dict[str, MCPManager] = {}
_manager_lock = threading.Lock()


def get_mcp_manager(user_id: str = "") -> MCPManager:
    """Return the MCP process group for one account.

    Each user needs separate subprocesses because the servers read the user's
    SQLite path from their environment. Managers start lazily on first chat.
    """
    active_user = user_id or current_user_id()
    key = active_user or "__shared__"
    with _manager_lock:
        manager = _managers.get(key)
        if manager is None:
            settings = user_settings(active_user) if active_user else get_settings()
            manager = MCPManager(settings)
            _managers[key] = manager
            atexit.register(manager.stop)
        return manager


__all__ = ["MCPManager", "SERVER_MODULES", "get_mcp_manager"]
