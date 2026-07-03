"""WeChat iLink webhook plugin lifecycle, hook handling, and HTTP dispatch.

Subscribes to two CAO plugin events:

- ``post_status_change`` — every latched status transition of a terminal.
  Filtered by ``CAO_ILINK_NOTIFY_STATUSES`` so that intermediate flap states
  don't generate spam; defaults to the four "phase-finished" signals
  (idle, completed, error, waiting_user_answer).
- ``post_create_terminal`` — emits a single "agent started" notification
  per terminal, so a supervisor spawning workers surfaces in WeChat
  immediately rather than only on first completion.

Both paths funnel through :meth:`_post`, which POSTs to the local iLink
Webhook Service's ``/api/v1/messages/send`` endpoint. All HTTP failures are
swallowed and logged — losing a WeChat notification must never break the
orchestrator.
"""

import asyncio
import logging
import os

import httpx
from dotenv import find_dotenv, load_dotenv

from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.plugins import (
    PostCreateTerminalEvent,
    PostStatusChangeEvent,
    hook,
)
from cli_agent_orchestrator.plugins.base import CaoPlugin

logger = logging.getLogger(__name__)

# Defaults are deliberately the four "phase-finished" signals — agents that
# need human intervention (waiting_user_answer) or terminated abnormally
# (error) are surfaced alongside the normal completion states. PROCESSING and
# UNKNOWN are excluded because they are transient and would generate noise.
_DEFAULT_NOTIFY_STATUSES = frozenset(
    {"idle", "completed", "error", "waiting_user_answer"}
)


class ILinkWebhookPlugin(CaoPlugin):
    """WeChat iLink webhook plugin for CAO status and lifecycle events."""

    _base_url: str
    _api_token: str
    _to_user_id: str
    _context_token: str | None
    _notify_statuses: frozenset[str]
    _client: httpx.AsyncClient

    async def setup(self) -> None:
        """Load configuration and initialize the HTTP client."""

        load_dotenv(find_dotenv(usecwd=True))

        base_url = os.environ.get("CAO_ILINK_WEBHOOK_URL", "").rstrip("/")
        api_token = os.environ.get("CAO_ILINK_API_TOKEN", "")
        to_user_id = os.environ.get("CAO_ILINK_TO_USER_ID", "")

        missing = [
            name
            for name, value in (
                ("CAO_ILINK_WEBHOOK_URL", base_url),
                ("CAO_ILINK_API_TOKEN", api_token),
                ("CAO_ILINK_TO_USER_ID", to_user_id),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Required CAO iLink webhook config is missing: "
                + ", ".join(missing)
                + ". Set them in the environment or in a .env file before "
                "starting cao-server."
            )

        raw_statuses = os.environ.get("CAO_ILINK_NOTIFY_STATUSES")
        if raw_statuses and raw_statuses.strip():
            notify_statuses = frozenset(
                s.strip().lower() for s in raw_statuses.split(",") if s.strip()
            )
        else:
            notify_statuses = _DEFAULT_NOTIFY_STATUSES

        self._base_url = base_url
        self._api_token = api_token
        self._to_user_id = to_user_id
        self._context_token = os.environ.get("CAO_ILINK_CONTEXT_TOKEN") or None
        self._notify_statuses = notify_statuses
        timeout = float(os.environ.get("CAO_ILINK_TIMEOUT_SECONDS", "5.0"))
        self._client = httpx.AsyncClient(timeout=timeout)

    async def teardown(self) -> None:
        """Close the HTTP client when setup completed successfully."""

        if hasattr(self, "_client"):
            await self._client.aclose()

    @hook("post_status_change")
    async def on_post_status_change(self, event: PostStatusChangeEvent) -> None:
        """Forward filtered status transitions to the configured webhook."""

        if event.new_status not in self._notify_statuses:
            return

        metadata = self._metadata(event.terminal_id)
        text = await self._format_status_change_message(event, metadata)
        await self._post(text)

    @hook("post_create_terminal")
    async def on_post_create_terminal(self, event: PostCreateTerminalEvent) -> None:
        """Notify when a new terminal (agent) is spawned."""

        text = self._format_terminal_started_message(event)
        await self._post(text)

    def _metadata(self, terminal_id: str) -> dict[str, object] | None:
        """Return terminal metadata if it is still available."""

        return get_terminal_metadata(terminal_id)

    def _resolve_display_name(self, terminal_id: str, metadata: dict[str, object] | None = None) -> str:
        """Resolve a human-friendly agent name from terminal metadata."""

        if metadata is None:
            metadata = self._metadata(terminal_id)
        if metadata is None:
            return terminal_id
        return str(metadata.get("tmux_window") or terminal_id)

    async def _format_status_change_message(
        self,
        event: PostStatusChangeEvent,
        metadata: dict[str, object] | None,
    ) -> str:
        """Render a status-change notification as Markdown text."""

        agent = self._resolve_display_name(event.terminal_id, metadata)
        session = self._value(metadata, "tmux_session", "unknown")
        role = self._role(agent, metadata)
        provider = self._value(metadata, "provider", event.provider or "unknown")
        old_status = self._pretty_status(event.old_status) if event.old_status else "-"
        new_status = self._pretty_status(event.new_status)

        lines = [
            "## CAO Agent 状态更新",
            "",
            f"- **Session**: `{session}`",
            f"- **角色**: `{role}`",
            f"- **状态**: `{old_status}` → `{new_status}`",
            f"- **终端类型**: `{provider}`",
            f"- **终端 ID**: `{event.terminal_id}`",
        ]

        last_response = await self._last_response(event.terminal_id)
        if last_response:
            lines.extend(
                [
                    "",
                    "### Last Response",
                    "```text",
                    self._truncate_code_block(last_response),
                    "```",
                ]
            )

        return "\n".join(lines)

    def _format_terminal_started_message(self, event: PostCreateTerminalEvent) -> str:
        """Render a terminal-created notification as Markdown text."""

        role = event.agent_name or event.terminal_id
        provider = event.provider or "unknown"
        return "\n".join(
            [
                "## CAO Agent 已启动",
                "",
                f"- **Session**: `{event.session_id or 'unknown'}`",
                f"- **角色**: `{role}`",
                "- **状态**: `Started`",
                f"- **终端类型**: `{provider}`",
                f"- **终端 ID**: `{event.terminal_id}`",
            ]
        )

    async def _last_response(self, terminal_id: str) -> str:
        """Return the same extracted text as Web UI's Terminal Output > Last Response."""

        try:
            from cli_agent_orchestrator.services.terminal_service import (
                OutputMode,
                get_output,
            )

            return await asyncio.to_thread(get_output, terminal_id, OutputMode.LAST)
        except Exception as exc:
            logger.debug("Failed to get last response for %s: %s", terminal_id, exc)
            return ""

    @staticmethod
    def _value(metadata: dict[str, object] | None, key: str, fallback: str) -> str:
        if metadata is None:
            return fallback
        value = metadata.get(key)
        return str(value) if value else fallback

    @staticmethod
    def _role(agent: str, metadata: dict[str, object] | None) -> str:
        if metadata is not None and metadata.get("agent_profile"):
            return str(metadata["agent_profile"])
        if "-" in agent:
            return agent.rsplit("-", 1)[0]
        return agent

    @staticmethod
    def _pretty_status(status: str) -> str:
        return status.replace("_", " ").title() if status else ""

    @staticmethod
    def _truncate_code_block(text: str, limit: int = 1800) -> str:
        cleaned = text.strip().replace("```", "``\\`")
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 20].rstrip() + "\n... <truncated>"

    async def _post(self, text: str) -> None:
        """Send a text message via iLink Webhook Service, swallowing failures."""

        payload: dict[str, str] = {"to_user_id": self._to_user_id, "text": text}
        if self._context_token:
            payload["context_token"] = self._context_token

        try:
            response = await self._client.post(
                f"{self._base_url}/api/v1/messages/send",
                headers={"Authorization": f"Bearer {self._api_token}"},
                json=payload,
            )
            if response.status_code >= 400:
                logger.warning(
                    "iLink webhook POST failed: %s %s",
                    response.status_code,
                    response.text[:200],
                )
        except httpx.HTTPError as exc:
            logger.warning("iLink webhook POST raised: %s", exc)
