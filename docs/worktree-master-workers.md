# 场景:1 个 Master Claude + N 个 Worktree Worker Claude

> 目标:用一个 Claude 作为总控(Supervisor),并发派发多个 Claude Worker,每个 Worker 在独立的 git worktree 上工作;Supervisor 能感知每个 Worker 的进度,用户随时可以 `tmux attach` 进入任一 Worker 干预。

## 0. 适用版本与前置条件

| 项 | 要求 |
|----|------|
| CAO | main 分支最新(或 ≥ v2.1) |
| tmux | ≥ 3.3 |
| Claude Code CLI | 已安装且 `claude --version` 可用 |
| Anthropic API | 已配置(API key 或 Bedrock/Vertex) |
| Python | ≥ 3.10 |

核心依赖能力:
- `claude_code` provider —— 一等公民,见 `src/cli_agent_orchestrator/models/provider.py:9`
- `working_directory` 参数 —— **默认关闭**,需通过环境变量开启
- `assign` MCP 工具 —— 异步派发 worker
- `send_message` 回调路由 —— 通过 `caller_id` 自动寻回 Supervisor

## 1. 一次性环境准备

### 1.1 安装 CAO

```bash
uv tool install git+https://github.com/awslabs/cli-agent-orchestrator.git@main --upgrade
```

### 1.2 开启 `working_directory` 参数(关键)

`handoff` / `assign` 默认不暴露 `working_directory`,需在启动 server **之前**导出:

```bash
export CAO_ENABLE_WORKING_DIRECTORY=true
```

> 原因:避免 agent 幻觉路径(见 `docs/working-directory.md`)。本场景必须开。

可选加入 `~/.bashrc`:
```bash
echo 'export CAO_ENABLE_WORKING_DIRECTORY=true' >> ~/.bashrc
```

## 2. 准备 Worktrees

把 N 个分支 checkout 到独立目录:

```bash
cd /path/to/your/repo
git worktree add ../repo-wt-a feature/a
git worktree add ../repo-wt-b feature/b
git worktree add ../repo-wt-c feature/c

# 验证
git worktree list
# /path/to/repo           # 主工作区(Supervisor 用)
# /path/to/repo-wt-a      # Worker A
# /path/to/repo-wt-b      # Worker B
# /path/to/repo-wt-c      # Worker C
```

记下每个 worktree 的**绝对路径**,后面要用。

## 3. 准备 Agent Profile

### 3.1 Supervisor profile

创建 `~/cao-profiles/code_supervisor.md`:

```markdown
---
name: code_supervisor
provider: claude_code
role: supervisor
permissionMode: auto              # ← 走 --permission-mode auto(等价 claude --auto)
allowedTools:
  - execute_bash
  - fs_read
  - fs_write
  - "@cao-mcp-server"
mcpServers:
  cao-mcp-server:
    command: uvx
    args:
      - --from
      - git+https://github.com/awslabs/cli-agent-orchestrator.git@main
      - cao-mcp-server
---

你是 Supervisor,职责是把一个大任务拆解成 N 个独立的子任务,
通过 `assign` 工具并发派发给多个 developer worker,每个 worker
在独立的 worktree 上工作。

派发规则:
1. 每个 assign 调用必须传 `working_directory`,值为对应 worktree 的绝对路径
2. 不要等待单个 worker 完成再派下一个 —— 连续 assign 完所有 worker
3. worker 完成后会通过 `send_message` 自动回报结果,你在下一轮 IDLE 时收到
4. 所有 worker 回报完毕后,汇总结果输出给用户,不要再次 handoff

不要自己写代码,你的角色是协调。
```

> `permissionMode` 可选值:`default` / `acceptEdits` / `plan` / `auto` / `bypassPermissions`。
> Supervisor 推荐用 `auto`(自动接受所有非破坏性操作),详见 §3.4。

### 3.2 Worker profile

创建 `~/cao-profiles/developer.md`:

```markdown
---
name: developer
provider: claude_code
role: developer
# 不写 permissionMode → CAO 自动用 --dangerously-skip-permissions
allowedTools:
  - execute_bash
  - fs_read
  - fs_write
  - "@cao-mcp-server"
mcpServers:
  cao-mcp-server:
    command: uvx
    args:
      - --from
      - git+https://github.com/awslabs/cli-agent-orchestrator.git@main
      - cao-mcp-server
---

你是 Worker,在 Supervisor 指定的 worktree 中完成子任务。

工作流程:
1. 收到任务后,先 `git status` 和 `git branch --show-current` 确认所在 worktree
2. 实现任务,跑必要的测试
3. 完成后,调用 `send_message(receiver_id=None, message=<结果摘要>)`
   - receiver_id 留空,会自动路由给创建你的 Supervisor
4. 不要尝试自己启动其他 worker,这是 Supervisor 的职责
```

> **不要**给 worker profile 写 `permissionMode: bypassPermissions` 想达到等价效果。
> 省略字段让 CAO 走 `--dangerously-skip-permissions` 才是推荐路径(避免 hooks 边缘行为差异)。

### 3.3 安装 profile

```bash
cao install ~/cao-profiles/code_supervisor.md
cao install ~/cao-profiles/developer.md

# 验证
cao info
```

### 3.4 权限模式与环境变量配置策略

> 本节解释为什么 profile 这么写、env 变量为什么这么分两路设。

#### 权限模式

CAO 在 `claude_code.py:167-170` 的判定:

```python
if profile and profile.permissionMode and not yolo:
    command_parts = ["claude", "--permission-mode", profile.permissionMode]
else:
    command_parts = ["claude", "--dangerously-skip-permissions"]
```

所以 supervisor / worker 用 **profile 字段** 控制即可,无需 `--yolo`:

| 角色 | profile 配置 | 实际启动命令 |
|------|--------------|--------------|
| Supervisor | `permissionMode: auto` | `claude --permission-mode auto ...` |
| Worker | **省略** `permissionMode` | `claude --dangerously-skip-permissions ...` |

`permissionMode` 可选值(`agent_profile.py:7`):`default` / `acceptEdits` / `plan` / `auto` / `bypassPermissions`。

#### 环境变量:必须分两路

`launch.py:36` 屏蔽了 `CLAUDE*` 前缀(`launch.py:37-46` 白名单只允许 `CLAUDE_CODE_USE_*` / `SKIP_*_AUTH`),所以常见 env 不能全走 `--env`:

| 变量 | 设置方式 | 原因 |
|------|----------|------|
| `DISABLE_AUTOUPDATER=1` | `--env DISABLE_AUTOUPDATER=1` | 无前缀,走 CLI 透传 |
| `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` | 启动 `cao-server` 前 `export` | CLAUDE 前缀被 `--env` 屏蔽;但 server 进程 env 会被 tmux 自动继承 |
| `CAO_ENABLE_WORKING_DIRECTORY=true` | 启动 `cao-server` 前 `export` | server 自身配置(`mcp_server/server.py:27` 在 server 启动时读) |

`--env` 设的变量会**自动透传给 session 内所有后续 spawn 的 worker**(`launch.py:137`),所以 supervisor 启动时设一次,后面 N 个 worker 全继承。

## 4. 启动流程

### 4.1 启动 server(后台常驻)

> **推荐**:用 systemd user service 一次性装好,之后开机自启,跳过本节(见 §10.1.1)。
> 下面是手动启动的方式。

```bash
# === server 端必须设的环境变量 ===
export CAO_ENABLE_WORKING_DIRECTORY=true                # 开启 worktree 支持
export CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1           # CLAUDE 前缀,--env 屏蔽

# 验证
echo $CAO_ENABLE_WORKING_DIRECTORY                      # 应输出 true
echo $CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN              # 应输出 1

# 启动(占一个终端,或用 nohup/tmux 后台跑)
cao-server
```

### 4.2 启动 Supervisor

另开一个终端:

```bash
# 推荐:用 cao-start 短命令(见 §10)
cao-start orchestrator

# 等价原生写法 ↓
cd /path/to/your/repo   # 主工作区
cao launch \
  --agents code_supervisor \
  --provider claude_code \
  --session-name orchestrator \
  --env DISABLE_AUTOUPDATER=1
```

参数说明:
- `--session-name orchestrator`:固定 session 名,后续命令好引用(实际名为 `cao-orchestrator`)
- `--env DISABLE_AUTOUPDATER=1`:无前缀变量,通过 CLI 透传到 supervisor 和所有 worker
- 不加 `--yolo`,保留工具限制确认

启动后自动 `tmux attach` 进入 Supervisor 会话。

### 4.3 给 Supervisor 下达总任务

在 Supervisor 会话里输入(举例):

```
我有一个项目需要并行开发 3 个 feature:
- Feature A: 用户认证模块 → worktree /abs/path/repo-wt-a
- Feature B: 订单服务     → worktree /abs/path/repo-wt-b
- Feature C: 支付集成     → worktree /abs/path/repo-wt-c

请并发派发 3 个 developer worker,每个在自己的 worktree 上工作。
所有 worker 完成后,汇总它们的输出给我。
```

Supervisor 收到后会:
1. 调 `assign("developer", "实现 A…", working_directory="/abs/path/repo-wt-a")`
2. 立即调 `assign("developer", "实现 B…", working_directory="/abs/path/repo-wt-b")`
3. 立即调 `assign("developer", "实现 C…", working_directory="/abs/path/repo-wt-c")`
4. 进入 IDLE 等待 worker 回报

每个 `assign` 调用会在**同一 cao-session 的 tmux session 里创建一个新 window**(不是新 tmux session),跑一个独立的 claude_code 进程,cwd 设为对应 worktree。

## 5. 监控 Worker 进度(三条通道)

### 通道 A:Supervisor 被动接收(自动)

Worker 完成后自动 `send_message` 回报,Supervisor 在下一轮 IDLE 时收到。无需任何操作。

### 通道 B:命令行主动查询

新开一个终端:

```bash
# 推荐:用 cao-st 短命令(见 §10)
cao-st                              # 默认 cao-orchestrator
cao-st mywork                       # 指定 session

# 等价原生写法 ↓
cao session status cao-orchestrator --workers

# 输出示例:
# Session:  cao-orchestrator
# Terminal: t-abc123
# Agent:    code_supervisor
# Provider: claude_code
# Status:   PROCESSING
#
# ID           AGENT       PROVIDER       STATUS
# ---------------------------------------------------------
# t-def-001    developer   claude_code    PROCESSING
# t-def-002    developer   claude_code    IDLE
# t-def-003    developer   claude_code    COMPLETED
```

状态语义:
| Status | 含义 |
|--------|------|
| `IDLE` | 等待输入,刚启动或刚完成一轮 |
| `PROCESSING` | 正在生成 / 执行工具 |
| `COMPLETED` | 当前任务完成,等待下一轮 |
| `ERROR` | 异常退出 |
| `WAITING_USER_ANSWER` | Hermes 专用,Claude 不产生 |

### 通道 C:查看某个 Worker 的实时输出

```bash
# 看最新一段输出
curl "http://localhost:9889/terminals/<worker_id>/output?mode=last"

# 或通过 CLI(等价)
cao session status cao-orchestrator --terminal <worker_id>
```

### 通道 D:Supervisor 主动查询(可选)

让 Supervisor 自己用 Bash 轮询:

```bash
# 在 Supervisor 会话里告诉它:
"用 curl 轮询 http://localhost:9889/sessions/cao-orchestrator/terminals
每 30 秒一次,打印每个 worker 的 status 和最近一行输出"
```

REST API 完整定义见 `docs/api.md`。

## 6. Attach 到任一 Worker 干预

> **架构澄清**:CAO 的实际模型是 **1 个 cao-session = 1 个 tmux session,1 个 cao-terminal = 该 session 内的 1 个 tmux window**。Supervisor 和所有 Worker 共享同一个 tmux session,落在不同 window。

### 6.1 找到 worker 的 window 编号

```bash
# 列出 cao-session 内所有 window
tmux list-windows -t cao-orchestrator
# 0: supervisor* "..."
# 1: developer   "..."   ← worker 通常在 window 1+
# 2: developer   "..."   ← 第二个 worker 在 window 2

# 或者通过 API 拿准确映射
curl -s http://localhost:9889/sessions/cao-orchestrator/terminals | python3 -m json.tool
# 返回的 tmux_window 字段就是 window 编号
```

### 6.2 切换到 worker(不用 detach)

如果你已经 attached 在 cao-orchestrator session 里:

| 快捷键 | 作用 |
|--------|------|
| `Ctrl+b 0` / `Ctrl+b 1` / `Ctrl+b 2` | 直接跳到指定 window |
| `Ctrl+b n` / `Ctrl+b p` | 下一个 / 上一个 window |
| `Ctrl+b w` | 弹出 window 选择列表(最直观) |
| `Ctrl+b &` | 关闭当前 window(慎用,等于杀 terminal) |

### 6.3 从外部 attach 直接进 worker

```bash
# 推荐:用 cao-attach 短命令(见 §10)
cao-attach                    # supervisor(window 0)
cao-attach orchestrator 1     # worker 1
cao-attach orchestrator 2     # worker 2

# 等价原生 tmux 写法 ↓
tmux attach -t cao-orchestrator       # window 0
tmux attach -t cao-orchestrator:1     # window 1
tmux attach -t cao-orchestrator:2     # window 2
```

### 6.4 在 worker 会话里能做什么

- **观察**:看 Claude Code 实时输出,不打扰
- **干预(等 IDLE 后)**:直接输入新指令,Claude Code 把它当下一轮请求
- **拒绝某次工具调用**:Claude Code 弹权限确认时选 No
- **Ctrl+c**:打断当前生成

### 6.5 切换回 Supervisor

```
Ctrl+b 0          # 直接跳回 window 0(supervisor)
# 或
Ctrl+b w          # 弹出 window 列表选择
```

> ⚠️ 重要:不要在 worker 还在 PROCESSING 时随意打字,Claude Code 会在当前任务结束后把你的输入当新指令。要干预,等 IDLE 提示符出现。

## 7. 收尾与清理

### 7.1 等所有 worker 完成

Supervisor 收齐所有 worker 的 `send_message` 回报后,会汇总输出。此时 worker 已自动 `/exit` 退出。

### 7.2 关闭整个编排

```bash
# 推荐:用 cao-stop 短命令(见 §10)
cao-stop                # 关 cao-orchestrator
cao-stop --all          # 关所有 CAO session

# 等价原生写法 ↓
cao shutdown --session cao-orchestrator
cao shutdown --all
```

### 7.3 清理 worktrees(任务全部完成后)

```bash
cd /path/to/your/repo
git worktree remove ../repo-wt-a
git worktree remove ../repo-wt-b
git worktree remove ../repo-wt-c
```

## 8. 常见问题排查

### Q1: Supervisor 调 `assign` 报错 "unknown parameter working_directory"

**原因**:`CAO_ENABLE_WORKING_DIRECTORY` 没开,或 server 启动前没 export。

**解决**:
```bash
pkill -f cao-server
export CAO_ENABLE_WORKING_DIRECTORY=true
cao-server &
```

### Q2: Worker 启动后 cwd 不对,在主仓库而不是 worktree

**原因**:`assign` 调用时没传 `working_directory`,或路径不是绝对路径。

**解决**:让 Supervisor 在 prompt 里**显式列出每个 worktree 的绝对路径**,并强制要求每次 assign 必须传参。

### Q3: `cao session status --workers` 看不到 worker

**原因**:Supervisor 可能用了 `handoff` 而不是 `assign`。`handoff` 是**同步阻塞**的,Supervisor 会卡在第一个 handoff 等完成,无法并发。

**解决**:让 Supervisor 改用 `assign`。

### Q4: Worker 完成了但 Supervisor 没收到回报

**排查清单**:
1. Worker profile 是否声明了 `cao-mcp-server`(没有就装不上 `send_message` 工具)
2. Worker 是否真的调用了 `send_message(receiver_id=None, ...)` —— 让 worker 显式打印调用日志
3. Supervisor 当前状态:如果在 PROCESSING,inbox 消息排队等下一轮 IDLE 才投递
4. 兜底:查看 `~/.cao/logs/` 下的 inbox 投递日志

### Q5: 想给某个 worker 加塞指令

**方法 A(推荐)**:`tmux attach` 进去,等 IDLE,直接输入
**方法 B**:`cao session send cao-orchestrator --terminal <worker_id> "你的指令"`,会写入 worker inbox,等 IDLE 自动投递

### Q6: Worker 卡死,想强制重启

```bash
# 找到 worker 的 window 编号
tmux list-windows -t cao-orchestrator

# kill 那个 window(不影响 supervisor 所在的 window)
tmux kill-window -t cao-orchestrator:<N>

# 再让 Supervisor 重新 assign 一个
```

> 注意:被 kill 的 worker ID 在 CAO DB 里仍标记为活跃,可以用 `cao terminal restore <id>` 恢复 scrollback 调试。

### Q7: API 配额不够,想限制并发

让 Supervisor 改成**串行 assign**:派一个等回报完成,再派下一个。失去并发优势但节省配额。

## 9. 关键文件速查

| 关注点 | 文件 |
|--------|------|
| `working_directory` 开关逻辑 | `src/cli_agent_orchestrator/mcp_server/server.py:27` |
| `assign` 工具实现 | `src/cli_agent_orchestrator/mcp_server/server.py:851` |
| `send_message` 回调路由 | `src/cli_agent_orchestrator/mcp_server/server.py:876` |
| Claude Code provider 启动 | `src/cli_agent_orchestrator/providers/claude_code.py:180` |
| Tmux session 创建 | `src/cli_agent_orchestrator/backends/tmux_backend.py:131` |
| Session status 命令 | `src/cli_agent_orchestrator/cli/commands/session.py:122` |

## 10. 本地辅助脚本(推荐)

把高频操作封装成短命令,免去每次敲 5 行 `cao launch` 和长串 `cao session status`。**这是用户自建的,不是 CAO 自带**——配置一次永久受益。

### 10.1 安装

#### 10.1.1 把 cao-server 装成 systemd user service(开机自启)

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/cao-server.service <<'EOF'
[Unit]
Description=CAO Server
After=network.target

[Service]
Type=simple
ExecStart=/home/$USER/.local/bin/cao-server
Restart=on-failure
RestartSec=3
# 关键:env 写在 service 里,免每次手动 export
Environment=CAO_ENABLE_WORKING_DIRECTORY=true
Environment=CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1

[Install]
WantedBy=default.target
EOF

# 让 service 在退出登录后继续跑(必须)
loginctl enable-linger "$USER"

# 启用 + 立即启动
systemctl --user daemon-reload
systemctl --user enable --now cao-server.service
```

验证:
```bash
systemctl --user status cao-server.service
curl -sf http://localhost:9889/health
```

> 装完后 §4.1 的"启动 server"步骤完全不用执行,开机就在跑。

#### 10.1.2 装 4 个短命令脚本

`~/.local/bin/cao-start`(启 supervisor):
```bash
#!/usr/bin/env bash
# Usage: cao-start [session-name] [working-dir]
set -euo pipefail
SESSION="${1:-orchestrator}"
DIR="${2:-$(pwd)}"

curl -sf http://localhost:9889/health >/dev/null 2>&1 || {
  echo "ERROR: cao-server not running. Start: systemctl --user start cao-server" >&2
  exit 1
}

cd "$DIR"
echo "Starting supervisor: session=cao-$SESSION  cwd=$DIR"
exec cao launch \
  --agents code_supervisor \
  --provider claude_code \
  --session-name "$SESSION" \
  --env DISABLE_AUTOUPDATER=1
```

`~/.local/bin/cao-st`(查状态):
```bash
#!/usr/bin/env bash
# Usage: cao-st [session-name]
set -euo pipefail
SESSION="${1:-orchestrator}"
exec cao session status "cao-$SESSION" --workers
```

`~/.local/bin/cao-stop`(关 session):
```bash
#!/usr/bin/env bash
# Usage: cao-stop [session-name|--all]
set -euo pipefail
if [[ "${1:-}" == "--all" ]]; then
  exec cao shutdown --all
fi
SESSION="${1:-orchestrator}"
exec cao shutdown --session "cao-$SESSION"
```

`~/.local/bin/cao-attach`(切到指定 window):
```bash
#!/usr/bin/env bash
# Usage: cao-attach [session-name] [window-number]
set -euo pipefail
SESSION="${1:-orchestrator}"
WIN="${2:-0}"
TARGET="cao-$SESSION:$WIN"
if [ -n "${TMUX:-}" ]; then
  exec tmux switch-client -t "$TARGET"   # 已在 tmux 内:切换 client
else
  exec tmux attach -t "$TARGET"          # 外部:attach
fi
```

加可执行权限:
```bash
chmod +x ~/.local/bin/cao-{start,st,stop,attach}
```

### 10.2 用法速查

| 操作 | 短命令 | 等价原生命令 |
|------|--------|-------------|
| **启动 supervisor**(默认 session) | `cao-start` | `cao launch --agents code_supervisor --provider claude_code --session-name orchestrator --env DISABLE_AUTOUPDATER=1` |
| 启动指定 session | `cao-start mywork` | 同上,`--session-name mywork` |
| 启动 + 指定 cwd | `cao-start mywork ~/repos/foo` | 同上,先 `cd` |
| **查状态**(默认) | `cao-st` | `cao session status cao-orchestrator --workers` |
| 查指定 session | `cao-st mywork` | `cao session status cao-mywork --workers` |
| **attach supervisor** | `cao-attach` | `tmux attach -t cao-orchestrator:0` |
| attach worker 1 | `cao-attach mywork 1` | `tmux attach -t cao-mywork:1` |
| 已在 tmux 内切到 worker 2 | `cao-attach mywork 2` | `tmux switch-client -t cao-mywork:2` |
| **关默认 session** | `cao-stop` | `cao shutdown --session cao-orchestrator` |
| 关指定 session | `cao-stop mywork` | `cao shutdown --session cao-mywork` |
| 关所有 session | `cao-stop --all` | `cao shutdown --all` |

### 10.3 systemd service 日常运维

```bash
systemctl --user status cao-server         # 看状态
systemctl --user restart cao-server        # 重启
systemctl --user stop cao-server           # 停
journalctl --user -u cao-server -f         # 实时日志
```

### 10.4 完整工作流(从零到 attach worker)

```bash
# 0. server 已通过 systemd 自启,跳过

# 1. 启 supervisor(在主仓库目录)
cd ~/repos/myrepo
cao-start mywork

# 进入 supervisor 会话,在里面派 worker(或在另一个终端 cao-dispatch 派)
# detach 回 shell
Ctrl+b d

# 2. 看进度
cao-st mywork

# 3. attach 到某个 worker
cao-attach mywork 1

# 4. 切回 supervisor
cao-attach mywork           # window 0 是 supervisor

# 5. 收工
cao-stop mywork
```

### 10.5 升级 CAO 后的维护

`uv tool install --upgrade` 之后:
- systemd service 不受影响(ExecStart 指向符号链接,会跟着新版本走)
- **web UI 需要重新 build + 拷贝**(uv install 不跑 npm build):
  ```bash
  cd /path/to/clone/web && npm run build
  cp -r ../src/cli_agent_orchestrator/web_ui/* \
    ~/.local/share/uv/tools/cli-agent-orchestrator/lib/python*/site-packages/cli_agent_orchestrator/web_ui/
  systemctl --user restart cao-server
  ```

## 11. 进阶:并行 feature 交付(3 级编排 + codex 评审)

§1–§10 描述的是**单 supervisor + N worker** 的简单并行场景。本节扩展为**3 级架构 + 跨 provider 评审循环**,适合"多 feature 批量交付 + 每个 feature 都要 plan→review→implement→review"的工程化场景。

### 11.1 架构

```
                 ┌──────────────────────┐
                 │  Feature Dispatcher  │  Claude,只拆任务 + 合并
                 │   (主控 claude)      │
                 └──────────┬───────────┘
                            │ assign × N(并行)
              ┌─────────────┼─────────────┐
              ▼             ▼             ▼
       ┌──────────┐  ┌──────────┐  ┌──────────┐
       │ Feature  │  │ Feature  │  │ Feature  │  Claude,每个自主跑
       │ Worker A │  │ Worker B │  │ Worker C │   plan→codex review→
       │ (wt-2fa) │  │ (wt-pwd) │  │ (wt-log) │   implement→codex review
       └────┬─────┘  └────┬─────┘  └────┬─────┘
            │ handoff ×≤3  │            │ handoff ×≤3
            ▼              ▼             ▼
       ┌──────────┐  ┌──────────┐  ┌──────────┐
       │  Codex   │  │  Codex   │  │  Codex   │  Codex,只读评审
       │ Reviewer │  │ Reviewer │  │ Reviewer │   完即删
       └──────────┘  └──────────┘  └──────────┘
```

层级无硬限制,但实际 3 级足够,超过 4 级调试代价大。

### 11.2 三个 Profile

文件位于 `~/cao-profiles/feature-pipeline/`:

| Profile | 角色 | Provider | 关键设计 |
|---------|------|----------|----------|
| `feature_dispatcher.md` | 主控 | claude_code | 有 `execute_bash`(git 操作)、不允许 fs_write 主仓代码 |
| `feature_worker.md` | 中层开发 | claude_code | PLAN 落 `docs/PLAN-<slug>.md`,每阶段 codex 上限 3 次 |
| `codex_reviewer.md` | 评审 | codex | 只 `fs_read`,输出 APPROVED 或 `## Issues` |

安装:
```bash
cao install ~/cao-profiles/feature-pipeline/feature_dispatcher.md
cao install ~/cao-profiles/feature-pipeline/feature_worker.md
cao install ~/cao-profiles/feature-pipeline/codex_reviewer.md
```

### 11.3 关键设计点

#### (a) Worktree 隔离 + 并行

Dispatcher 对每个 feature 执行:
```bash
git worktree add ../<repo>-wt-<slug> -b feature/<slug>
```
然后 `assign("feature_worker", ..., working_directory=<wt-abs-path>)` 连续派发,所有 worker **真并发**(不是串行 handoff)。

> `assign` 必须开 `CAO_ENABLE_WORKING_DIRECTORY=true`(§1.2 已通过 systemd service 配置)

#### (b) PLAN 文件命名规则

Feature Worker 把方案写到 `docs/PLAN-<slug>.md`(相对 worktree 根)。每个 worktree 在不同分支,**天然无冲突**。合并回 main 后,3 个 PLAN 文件并列存在作为审计记录。

#### (c) Codex 调用硬上限

每个 Feature Worker 的两阶段各有 3 次 codex reviewer 交互上限:
- 阶段 1(规划):最多 3 次评审
- 阶段 2(实施):最多 3 次评审
- 第 3 次仍未 APPROVED → 把 issues 记入 `## Pending Issues`,进入下一阶段

3 个 feature 并发 = 最多 18 次 codex 交互,可按 API 配额调整 profile 里的上限值。

#### (d) 合并策略

Dispatcher 收齐所有 send_message 回报后,**顺序**合并:
```bash
git checkout main
git merge --no-ff feature/<slug>
```
冲突时**绝不自动 resolve**,记入"未合并"列表留人工。这是唯一安全选项 ——
codex 已 review 的代码不能被 silently 改掉。

#### (e) PLAN 路径与 reviewer 复用

Codex Reviewer 继承 Feature Worker 的 cwd(worktree 根),所以第一次创建 reviewer 时传**相对路径**即可:
```
assign("codex_reviewer", "评审方案: docs/PLAN-2fa.md")
```
Feature Worker 记录返回的 `terminal_id` 作为该 feature 对应的 reviewer terminal,后续轮次统一复用:
```
send_message(receiver_id=<same-codex-terminal-id>,
             message="继续评审方案: docs/PLAN-2fa.md")
```
实施阶段同样继续向同一个 reviewer terminal 发 `send_message`,而不是重复 `assign` / `handoff` 创建新 terminal。

reviewer terminal 的清理策略:
- worker 完成所有 review 后可显式 `delete_terminal(<codex-terminal-id>)`
- 如果 worker 没显式清理,当 worker terminal 被关闭/删除时,会沿 `caller_id` ownership 自动级联清理 reviewer child terminal

### 11.5 启动 + 触发

```bash
cd /path/to/your/repo
cao-start featurepipeline

# 在 supervisor 会话里:
"我需要并行交付 3 个独立 feature:1. xxx 2. xxx 3. xxx
按你的流程执行,完成后合并到 main。"
```

`Ctrl+b d` 出来,通过 Web UI 或 `cao-st featurepipeline` 看进度。

### 11.6 端到端 Demo

`~/cao-profiles/feature-pipeline/demo.sh` 提供最小验证场景:

```bash
# 1. 建一个最小 demo 仓(~/cao-demo/calc-demo,只有 calc.py 的 add 函数)
bash ~/cao-profiles/feature-pipeline/demo.sh setup

# 2. 启动 dispatcher(会打印任务模板让你粘进 supervisor 会话)
bash ~/cao-profiles/feature-pipeline/demo.sh launch

# 3. 监控
bash ~/cao-profiles/feature-pipeline/demo.sh status
# 或:cao-st featurepipeline
# 或:Web UI http://localhost:9889

# 4. 完事清理
bash ~/cao-profiles/feature-pipeline/demo.sh teardown
```

demo 任务是给 calc.py 并行加 `subtract` / `multiply` / `divide` 三个函数,3 个 worktree、3 个 PLAN、3 个 feature 分支、3 次 merge,全程跑完大约 5-10 分钟(取决于 codex 响应速度)。

### 11.7 监控建议(3 级场景特别重要)

3 级架构会在 tmux session 内产生较多 windows,推荐组合使用:

| 场景 | 推荐工具 |
|------|----------|
| 一屏看全貌 | **Web UI**(http://localhost:9889) → Home → cao-featurepipeline |
| 命令行查状态 | `cao-st featurepipeline` |
| 进 dispatcher 看决策 | `cao-attach featurepipeline`(window 0) |
| 进某个 worker 看迭代 | `cao-attach featurepipeline 1`(window 1 是第一个 worker) |
| 看实时日志 | `journalctl --user -u cao-server -f` |

### 11.8 常见问题

**Q: codex reviewer 复用后,会不会上下文越滚越大?**

会。每个 `codex_reviewer` 改成持久 terminal 后,单个 feature 的方案评审和实施评审都会复用同一个上下文。好处是省去重复启动开销,并且符合 caller ownership + 级联清理模型;代价是评审独立性略弱、上下文会累积。如果后面发现上下文污染明显,再考虑按阶段拆 reviewer 或显式重建 terminal。

**Q: PLAN 文件污染主仓怎么办?**

PLAN-*.md 默认会跟着 merge 进 main。如果嫌乱:
- 在 Phase 5 之后,dispatcher 把 PLAN 移到 `docs/archive/PLAN-<slug>.md`
- 或在合并后用 `git rm docs/PLAN-*.md` 清掉(失去审计价值)

**Q: 多个 feature 改同一文件,merge 必冲突?**

是的。并行 feature 改同一文件几乎必冲突。预防:
- feature 拆分时尽量让 worker 改不同文件
- 或在 PLAN 阶段就让 codex 检查"是否会和其他并行 feature 冲突"
- 接受冲突 → 人工 resolve → 后续 feature rebase on main 再合并

**Q: Dispatcher 卡在某个 worker 等不到 send_message?**

可能原因:
1. worker 死循环(每阶段 3 次上限已挡住,但仍可能在某步卡住)
2. send_message 失败(receiver_id 路由问题)
3. worker 崩溃

排查:
```bash
cao-st featurepipeline              # 看 worker 状态
cao-attach featurepipeline 1        # 进去看实际情况
journalctl --user -u cao-server -f  # 看 server 日志
```

兜底:`cao-stop featurepipeline`,清理 worktrees,重新跑。

**Q: codex_reviewer 拒绝 APPROVED,导致 worker 浪费轮数?**

profile 里已加约束:"如果是小瑕疵标 nice-to-have 并 APPROVED"。但 codex 实际行为可能偏离。如果发现 codex 过于严格,可以:
- 改 `codex_reviewer.md` 的 system prompt,加"偏向 APPROVED,只在 must-fix 才返回 Issues"
- 或减少 worker 的 handoff 上限(从 3 改成 2)
