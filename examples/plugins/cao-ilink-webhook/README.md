# cao-ilink-webhook

`cao-ilink-webhook` is a CAO plugin that forwards agent status transitions and terminal creations to a WeChat user via a local [WeChat iLink Webhook Service](https://github.com/JessieKaa/wechat-ilink-webhook). Use it to get a push notification in WeChat every time a worker agent finishes a phase of work.

## Install

From the repository root, inside the CAO development virtual environment:

```bash
uv pip install -e examples/plugins/cao-ilink-webhook
```

## Setup

1. **Deploy the iLink Webhook Service** — clone [JessieKaa/wechat-ilink-webhook](https://github.com/JessieKaa/wechat-ilink-webhook), scan the auth QR code, and start the service. Note the service base URL, its `API_TOKEN`, and your WeChat user ID (`xxx@im.wechat`).

2. **Have the WeChat user send the bot a message** — the service caches a `context_token` per user on first inbound message; outbound `/messages/send` needs that token.

3. **Install the plugin** (from the CAO repo root, inside the CAO venv):
   ```bash
   uv pip install -e examples/plugins/cao-ilink-webhook
   ```

4. **Configure** — create a `.env` file in the directory you'll run `cao-server` from:
   ```dotenv
   CAO_ILINK_WEBHOOK_URL=http://127.0.0.1:8080
   CAO_ILINK_API_TOKEN=change-me
   CAO_ILINK_TO_USER_ID=your_user@im.wechat
   ```

5. **Start the server**:
   ```bash
   cao-server
   ```
   Confirm you see `Loaded CAO plugin: ilink_webhook` in the logs.

6. **Run a workflow** — e.g. `cao launch ...` and trigger a handoff or assign. Watch WeChat for Markdown-formatted messages like:

   ```markdown
   ## CAO Agent 已启动

   - **Session**: `cao-shattered`
   - **角色**: `feature_worker`
   - **状态**: `Started`
   - **终端类型**: `claude_code`
   - **终端 ID**: `287c7699`
   ```

   ```markdown
   ## CAO Agent 状态更新

   - **Session**: `cao-shattered`
   - **角色**: `feature_worker`
   - **状态**: `Processing` → `Completed`
   - **终端类型**: `claude_code`
   - **终端 ID**: `287c7699`

   ### Last Response
   ```text
   Finished the requested implementation.
   ```
   ```

   `Last Response` uses the same extraction path as the Web UI's **Terminal Output → Last Response** tab (`terminal_service.get_output(..., OutputMode.LAST)`). If extraction fails, the notification is still sent without that section.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `CAO_ILINK_WEBHOOK_URL` | Yes | Base URL of the running iLink Webhook Service (e.g. `http://127.0.0.1:8080`). |
| `CAO_ILINK_API_TOKEN` | Yes | Bearer token matching the service's `API_TOKEN`. |
| `CAO_ILINK_TO_USER_ID` | Yes | Target WeChat user ID, e.g. `xxx@im.wechat`. |
| `CAO_ILINK_NOTIFY_STATUSES` | No | Comma-separated status filter. Defaults to `idle,completed,error,waiting_user_answer`. |
| `CAO_ILINK_TIMEOUT_SECONDS` | No | HTTP timeout in seconds. Default `5.0`. |
| `CAO_ILINK_CONTEXT_TOKEN` | No | Explicit iLink `context_token`; if unset, the service resolves one per `to_user_id`. |

## Troubleshooting

- **Plugin logs `WARNING` at startup and is skipped** — one of the three required env vars is missing. Set it and restart `cao-server`.
- **`502` from the service** — the iLink gateway rejected the send. Re-check that the target user has sent the bot an inbound message (refreshes `context_token`), or set `CAO_ILINK_CONTEXT_TOKEN` explicitly.
- **No notification fired but agent finished** — confirm the terminal's status landed in `CAO_ILINK_NOTIFY_STATUSES`. `processing`/`unknown` are intentionally excluded as transient.
- **`cao-server` keeps running after a failed POST** — by design. All HTTP failures are swallowed and logged as warnings; losing one WeChat notification must not break orchestration.

## Alternative: global tool install

```bash
uv tool install --reinstall . \
  --with-editable ./examples/plugins/cao-ilink-webhook
```

## Note

This plugin is provided as an example. It is not expected to be actively maintained — take it as a starting point and adapt for your own use cases.
