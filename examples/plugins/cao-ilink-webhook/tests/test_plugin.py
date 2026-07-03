"""Tests for the iLink webhook plugin — config loading, hook dispatch, failure isolation."""

import json

import httpx
import pytest
from cao_ilink_webhook.plugin import ILinkWebhookPlugin

from cli_agent_orchestrator.plugins import (
    PostCreateTerminalEvent,
    PostStatusChangeEvent,
)


def _required_env(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Set the three required env vars, with optional overrides.

    Optional vars not present in ``overrides`` are explicitly cleared so each
    test starts from defaults rather than inheriting the host shell.
    """

    base = {
        "CAO_ILINK_WEBHOOK_URL": "http://ilink.example.local:8080",
        "CAO_ILINK_API_TOKEN": "secret-token",
        "CAO_ILINK_TO_USER_ID": "user-42@im.wechat",
    }
    base.update(overrides)

    for optional in (
        "CAO_ILINK_NOTIFY_STATUSES",
        "CAO_ILINK_TIMEOUT_SECONDS",
        "CAO_ILINK_CONTEXT_TOKEN",
    ):
        if optional not in overrides:
            monkeypatch.delenv(optional, raising=False)
    for key, value in base.items():
        monkeypatch.setenv(key, value)


async def _replace_client_with_mock_transport(
    plugin: ILinkWebhookPlugin, handler: httpx.MockTransport
) -> None:
    """Swap in a mock transport-backed client and close the setup client first."""

    await plugin._client.aclose()
    plugin._client = httpx.AsyncClient(transport=handler)


@pytest.mark.asyncio
async def test_setup_raises_when_required_config_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Missing any of the three required vars should raise RuntimeError."""

    for var in (
        "CAO_ILINK_WEBHOOK_URL",
        "CAO_ILINK_API_TOKEN",
        "CAO_ILINK_TO_USER_ID",
        "CAO_ILINK_NOTIFY_STATUSES",
        "CAO_ILINK_TIMEOUT_SECONDS",
        "CAO_ILINK_CONTEXT_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_ilink_webhook.plugin.find_dotenv", lambda usecwd=True: "")

    plugin = ILinkWebhookPlugin()

    with pytest.raises(RuntimeError, match="CAO_ILINK_WEBHOOK_URL"):
        await plugin.setup()


@pytest.mark.asyncio
async def test_setup_loads_required_config_and_default_filter(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Setup populates URL/token/recipient and the default notify set."""

    _required_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_ilink_webhook.plugin.find_dotenv", lambda usecwd=True: "")

    plugin = ILinkWebhookPlugin()
    await plugin.setup()

    assert plugin._base_url == "http://ilink.example.local:8080"
    assert plugin._api_token == "secret-token"
    assert plugin._to_user_id == "user-42@im.wechat"
    assert plugin._context_token is None
    assert plugin._notify_statuses == frozenset(
        {"idle", "completed", "error", "waiting_user_answer"}
    )
    await plugin.teardown()


@pytest.mark.asyncio
async def test_setup_parses_custom_notify_statuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """CAO_ILINK_NOTIFY_STATUSES overrides the default filter (case-insensitive, trimmed)."""

    _required_env(monkeypatch, CAO_ILINK_NOTIFY_STATUSES="COMPLETED, ERROR")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_ilink_webhook.plugin.find_dotenv", lambda usecwd=True: "")

    plugin = ILinkWebhookPlugin()
    await plugin.setup()

    assert plugin._notify_statuses == frozenset({"completed", "error"})
    await plugin.teardown()


@pytest.mark.asyncio
async def test_on_post_status_change_filters_out_processing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """PROCESSING is not in the default notify set and must not POST."""

    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"chunks": 1})

    _required_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_ilink_webhook.plugin.find_dotenv", lambda usecwd=True: "")

    plugin = ILinkWebhookPlugin()
    await plugin.setup()
    await _replace_client_with_mock_transport(plugin, httpx.MockTransport(handler))

    await plugin.on_post_status_change(
        PostStatusChangeEvent(
            terminal_id="abc12345",
            old_status="idle",
            new_status="processing",
        )
    )

    assert requests == []
    await plugin.teardown()


@pytest.mark.asyncio
async def test_on_post_status_change_posts_arrow_format_on_transition(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A real transition posts URL/headers/body per the iLink API contract."""

    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "authorization": request.headers.get("authorization"),
                "json": json.loads(request.content.decode("utf-8")),
            }
        )
        return httpx.Response(200, json={"chunks": 1})

    _required_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_ilink_webhook.plugin.find_dotenv", lambda usecwd=True: "")
    monkeypatch.setattr(
        "cao_ilink_webhook.plugin.get_terminal_metadata",
        lambda terminal_id: {
            "tmux_session": "cao-shattered",
            "tmux_window": "feature_worker-a1b2",
            "agent_profile": "feature_worker",
            "provider": "claude_code",
            "id": terminal_id,
        },
    )

    plugin = ILinkWebhookPlugin()
    await plugin.setup()
    async def last_response(_terminal_id: str) -> str:
        return "Implemented feature.\n```python\nprint('ok')\n```"

    monkeypatch.setattr(plugin, "_last_response", last_response)
    await _replace_client_with_mock_transport(plugin, httpx.MockTransport(handler))

    await plugin.on_post_status_change(
        PostStatusChangeEvent(
            terminal_id="abc12345",
            old_status="processing",
            new_status="completed",
        )
    )

    assert requests == [
        {
            "method": "POST",
            "url": "http://ilink.example.local:8080/api/v1/messages/send",
            "authorization": "Bearer secret-token",
            "json": {
                "to_user_id": "user-42@im.wechat",
                "text": (
                    "## CAO Agent 状态更新\n\n"
                    "- **Session**: `cao-shattered`\n"
                    "- **角色**: `feature_worker`\n"
                    "- **状态**: `Processing` → `Completed`\n"
                    "- **终端类型**: `claude_code`\n"
                    "- **终端 ID**: `abc12345`\n\n"
                    "### Last Response\n"
                    "```text\n"
                    "Implemented feature.\n``\\`python\nprint('ok')\n``\\`\n"
                    "```"
                ),
            },
        }
    ]
    await plugin.teardown()


@pytest.mark.asyncio
async def test_on_post_status_change_first_detection_emits_only_new_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An empty old_status renders as just the new status (no arrow)."""

    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"chunks": 1})

    _required_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_ilink_webhook.plugin.find_dotenv", lambda usecwd=True: "")
    monkeypatch.setattr(
        "cao_ilink_webhook.plugin.get_terminal_metadata",
        lambda terminal_id: None,
    )

    plugin = ILinkWebhookPlugin()
    await plugin.setup()

    async def empty_last_response(_terminal_id: str) -> str:
        return ""

    monkeypatch.setattr(plugin, "_last_response", empty_last_response)
    await _replace_client_with_mock_transport(plugin, httpx.MockTransport(handler))

    await plugin.on_post_status_change(
        PostStatusChangeEvent(
            terminal_id="abc12345",
            old_status="",
            new_status="idle",
        )
    )

    assert requests == [
        {
            "to_user_id": "user-42@im.wechat",
            "text": (
                "## CAO Agent 状态更新\n\n"
                "- **Session**: `unknown`\n"
                "- **角色**: `abc12345`\n"
                "- **状态**: `-` → `Idle`\n"
                "- **终端类型**: `unknown`\n"
                "- **终端 ID**: `abc12345`"
            ),
        }
    ]
    await plugin.teardown()


@pytest.mark.asyncio
async def test_on_post_status_change_includes_context_token_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An explicit context_token is forwarded in the request body."""

    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"chunks": 1})

    _required_env(monkeypatch, CAO_ILINK_CONTEXT_TOKEN="explicit-token")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_ilink_webhook.plugin.find_dotenv", lambda usecwd=True: "")
    monkeypatch.setattr(
        "cao_ilink_webhook.plugin.get_terminal_metadata",
        lambda terminal_id: {"tmux_window": "coder-a1b2"},
    )

    plugin = ILinkWebhookPlugin()
    await plugin.setup()

    async def empty_last_response(_terminal_id: str) -> str:
        return ""

    monkeypatch.setattr(plugin, "_last_response", empty_last_response)
    await _replace_client_with_mock_transport(plugin, httpx.MockTransport(handler))

    await plugin.on_post_status_change(
        PostStatusChangeEvent(
            terminal_id="abc12345",
            old_status="processing",
            new_status="error",
        )
    )

    assert requests[0]["context_token"] == "explicit-token"
    await plugin.teardown()


@pytest.mark.asyncio
async def test_on_post_create_terminal_posts_agent_started_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """post_create_terminal emits a single "agent started" notification."""

    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"chunks": 1})

    _required_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_ilink_webhook.plugin.find_dotenv", lambda usecwd=True: "")

    plugin = ILinkWebhookPlugin()
    await plugin.setup()
    await _replace_client_with_mock_transport(plugin, httpx.MockTransport(handler))

    await plugin.on_post_create_terminal(
        PostCreateTerminalEvent(
            session_id="cao-demo",
            terminal_id="abc12345",
            agent_name="coder",
            provider="claude_code",
        )
    )

    assert requests == [
        {
            "to_user_id": "user-42@im.wechat",
            "text": (
                "## CAO Agent 已启动\n\n"
                "- **Session**: `cao-demo`\n"
                "- **角色**: `coder`\n"
                "- **状态**: `Started`\n"
                "- **终端类型**: `claude_code`\n"
                "- **终端 ID**: `abc12345`"
            ),
        }
    ]
    await plugin.teardown()


@pytest.mark.asyncio
async def test_http_failures_are_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """HTTP errors must not propagate out of the hook."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "ilink gateway down"})

    _required_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cao_ilink_webhook.plugin.find_dotenv", lambda usecwd=True: "")
    monkeypatch.setattr(
        "cao_ilink_webhook.plugin.get_terminal_metadata",
        lambda terminal_id: {"tmux_window": "coder-a1b2"},
    )

    plugin = ILinkWebhookPlugin()
    await plugin.setup()

    async def empty_last_response(_terminal_id: str) -> str:
        return ""

    monkeypatch.setattr(plugin, "_last_response", empty_last_response)
    await _replace_client_with_mock_transport(plugin, httpx.MockTransport(handler))

    # Must not raise.
    await plugin.on_post_status_change(
        PostStatusChangeEvent(
            terminal_id="abc12345",
            old_status="processing",
            new_status="completed",
        )
    )
    await plugin.teardown()
