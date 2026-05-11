# pm-agent — Quickstart

5 分钟从零到看到结果。一页文档，照走。

## 0 · 前置

| 工具 | 版本 | 检查 | 安装 |
|---|---|---|---|
| macOS | 13+ | — | — |
| Claude CLI | 2.1+ | `claude --version` | 见 https://docs.claude.com/claude-code |
| uv | 0.11+ | `uv --version` | `brew install uv` |
| git | 任意 | `git --version` | — |

Claude CLI 需要已登录（`claude` 命令能进交互模式即可）。

## 1 · 启动（30 秒）

```bash
cd /Users/zmy/intership/5/agenter/pm-agent
uv sync                                 # 装依赖，一次性
```

## 2 · 第一眼：看视觉（零成本）

```bash
uv run python -m pm_agent.tui
```

会看到 5 panel 的 Textual TUI：Goal bar + 进度条、Tasks 表、Active Agents
卡片、Live Log、底部 footer。**按 `q` 退出**。这一步用 mock 数据，没花任何
API 钱。

## 3 · 跑一个真实任务（约 1 分钟，~$0.35）

准备一个 demo target repo（一次性，已脚本化）：

```bash
bash docs/demo-commands.sh reset_target
```

跑完整 e2e：

```bash
bash docs/demo-commands.sh full_e2e_dry_run
```

或者展开成原生命令：

```bash
uv run python -m pm_agent.tui \
    --repo /tmp/pm-agent-day7-target \
    --coder-timeout 180 \
    --test-cmd "python3 -c 'import tests.test_server as t; [getattr(t,n)() for n in dir(t) if n.startswith(\"test_\")]; print(\"PASS\")'" \
    "Add a /health endpoint to handle_request returning a dict with key status equal to ok, plus a test"
```

**预期**: 约 50-100 秒后 TUI 显示 `2/2 tasks ✓ done`，integration test `PASS`，
`q` 退出。

## 4 · 看产出

```bash
ls -t ~/.pm-agent/runs | head -1 | xargs -I {} cat ~/.pm-agent/runs/{}/summary.md
```

得到一份 PR 风格报告：

- goal / total cost / duration / tasks completed
- 每个 task 的 acceptance criteria + 单独 diff
- Integration merge 状态 + 测试输出
- 合并后的整体 diff（可 `git apply` 接到任意 branch）

## 5 · 把成果接到你自己的 repo

```bash
# 把 demo 跑出的改动应用到任何 git repo
git -C /your/repo apply ~/.pm-agent/runs/<latest>/integration.diff
```

或者直接把 `--repo` 指向你的项目：

```bash
uv run python -m pm_agent.tui \
    --repo /path/to/your/own/repo \
    --test-cmd "pytest" \
    "你的目标，自然语言一句"
```

## 6 · 特殊模式（不烧钱地看错误处理）

```bash
# 演示 Planner 失败 → 自动 fallback 流程（~$0.12）
uv run python -m pm_agent.tui --repo /tmp/pm-agent-day7-target \
    --inject-fault planner-yaml "anything"

# 演示 Coder 超时熔断（~$0.08）
--inject-fault coder-timeout

# 演示 rate-limit / API failure（~$0.03）
--inject-fault api-error
```

三种 fault 模式都是**确定性触发**，不靠运气，每次都演给你看。

## 7 · 翻车自救

| 现象 | 处理 |
|---|---|
| TUI 卡死 | `q` 退出；`finally` 会清 worktree + ai/T-* branch |
| 强杀后留垃圾 | `rm -rf /tmp/pm-agent-day7-target/.pm-agent-worktrees` |
| API rate limit | `summary.md` 标 ❌ API ERROR；换时段重跑，或用 `--inject-fault` 三模式继续演 |
| Planner YAML parse 失败 | 自动重试 3 次；最终 fallback 到 mock；详细 log 在 `~/.pm-agent/runs/<id>/planner-error.log` |
| 第一次 call 很慢 | 跑一次 `bash docs/demo-commands.sh prewarm_claude` 暖 session |

## 8 · 想看更多

- **完整剧本（9 段 demo 叙事 + 答辩 cheat-sheet）**: `docs/demo-narrative.md`
- **18 个录屏入口函数**: `docs/demo-commands.sh help`
- **架构 + 设计决策**: `README.md`
- **历史 + 14 天进度**: `HANDOFF.md`
- **视觉证据（12 张 SVG）**: `docs/tui-*.svg`

---

**一句话**：给它一个 git repo + 一句话目标 + 一条测试命令，它自动拆任务、并
行起 Coder、合 diff、跑测试，结果以 PR 报告摆给你。整个过程 TUI 实时显示，
不需要你介入。
