# HANDOFF — pm-agent

新 Claude Code 会话从这里开始。读完这一份文档你就能无损接上 Day 10。

## TL;DR

- **what**: 多 Coder 编排器（接 `claude -p` 子进程，git worktree 隔离并行）
- **who**: 实习生 zmy 给 mentor 安子岩做的 demo 项目；14 天 deadline，今天 Day 9 收工
- **why this file**: 上一会话即将爆 token，新会话冷启动用此文档接力
- **next**: Day 10 — TUI polish（5 个具体子项，见下文 §8）

---

## 1. 启动序列（新会话第一件事）

```bash
# 进项目根
cd /Users/zmy/intership/5/agenter/pm-agent

# 验环境
git log --oneline | head -10        # 最上面应该是 c5934b2 day-8 step 4
git branch --show-current             # main
ls pm_agent/                         # tasks.py runner.py worktree.py planner.py tui.py
which claude && claude --version     # 2.1.137
which uv && uv --version             # 0.11.x
```

如果 `git log` 第一行不是 `c5934b2 feat: API failure detection ...`，说明上次会话之后又有 commit
进来了 — 看 commit message 决定要不要从 Day 11 开始而不是 Day 10。

---

## 2. 项目背景

### 真实业务情况（这段必读）

- 这不是市场 PMF 项目，是 **实习生评估**。安子岩半个月给的硬 deadline，不交活就走人。
- "用户" = 安子岩**一个人**。所有设计选择围绕"打动他"，不围绕通用性。
- Demo 形式：他在自己 macOS 笔记本上跑，给真实开发任务。质量 ≥ 他手动调度多 agent 的水平。
- 设计文档（含 14 天时间表 / 风险 / 验收）在：
  `~/.gstack/projects/agenter/zmy-no-git-design-20260509-110211.md`

### 安子岩在乎什么

原话："Claude Code 频繁询问，不能自主朝目标推进"。所以核心价值锚是**自主性 + 不打扰**。
辅助锚是**视觉冲击**（用户判断 demo 视觉对单人评估很关键，所以选了 TUI 而不是 CLI）。

---

## 3. 文件系统

```
代码主仓:    /Users/zmy/intership/5/agenter/pm-agent
原始 PRD:    /Users/zmy/intership/5/agenter/multi_agent_product_orchestrator_prd.md
            （2196 行，已 deprecated；保留是过程证据，不再当蓝图）
设计文档:    ~/.gstack/projects/agenter/zmy-no-git-design-20260509-110211.md
demo 目标:   /tmp/pm-agent-day7-target
            （seed Python repo: server.py + tests/test_server.py + README.md）
运行产物:    ~/.pm-agent/runs/<YYYYMMDD-HHMMSS>/
            ├── summary.md            ← PR-style 报告
            ├── T-1.diff / T-2.diff   ← 每个 Coder 单独的 diff
            ├── integration.diff      ← 合并后的整体 diff
            └── （可能有 planner-error.log / planner-crash.log）
SVG 快照:    pm-agent/docs/tui-day{2,3,5,6,7,8}-*-snapshot.svg
```

---

## 4. 架构图

```
[user CLI: pm-agent.tui "goal" --repo $TGT --test-cmd "..." --coder-timeout 180]
                ↓
        [PMAgentTUI (Textual 5-panel)]
                ↓
        @work _run_session():
            1. _run_planner    →  pm_agent.planner.plan(goal, repo, on_retry)
                                 ├─ _call_planner_once (LLM call, returns text+cost)
                                 ├─ extract_yaml + parse_tasks + validate_disjoint
                                 └─ retry up to 2x with error feedback
            2. asyncio.gather(_stream_one(t) for t in tasks)
                                 ├─ wm.acreate(t.id)            ← worktree on ai/T-X
                                 ├─ run_claude_async(unrestricted=True, timeout=180)
                                 ├─ capture diff vs base
                                 └─ wm.acleanup_worktree(t.id)  ← keeps branch
            3. _run_integration → wm.aintegrate(run_id, task_ids, test_cmd)
                                 ├─ create ai/integration/<run-id> from base
                                 ├─ git merge each ai/T-X (capture conflicts)
                                 ├─ run test_cmd (capture exit/stdout/stderr)
                                 └─ capture diff vs base
            4. _write_run_summary  →  ~/.pm-agent/runs/<id>/summary.md
            finally: delete all task branches + integration worktree/branch
```

模块责任：

| 文件 | 职责 |
|---|---|
| `pm_agent/tasks.py` | `CoderTask` dataclass: id/title/prompt/allowed_paths/acceptance |
| `pm_agent/runner.py` | `run_claude_async(prompt, role, isolate, cwd, unrestricted, timeout)` async generator + sync wrapper |
| `pm_agent/worktree.py` | `WorktreeManager`: create/cleanup_worktree/delete_branch/integrate/(async wrappers) |
| `pm_agent/planner.py` | `plan(goal, repo, on_retry)` 真 LLM YAML 输出 + 重试 |
| `pm_agent/tui.py` | Textual app；3 模式（mock / single / multi-coder real）；@work 协程 |

---

## 5. 9 天进度（commit 串）

每个 commit 自身的 message 写得很详细，新会话需要细节直接 `git show <hash>`。

| 天 | commit | 简述 |
|:--:|---|---|
| 1 | `97e39f1` | init: pm-agent 骨架（README + docs/stream-json-events.md + uv 项目） |
| - | `cd68894` | （teamagent 自动 sync，无视即可） |
| 1+ | `ab73cfd` | fix: `--setting-sources project,local` 隔离 spawned claude，避开 parent 的 stop-hook + cost 降 68% |
| 1+ | `431dc64` | feat: 最小 `claude -p` runner（60 行 stdlib only） |
| 2 | `e3ec409` | feat: TUI 5-panel skeleton + 0.8s mock ticker |
| 3-4 | `cfc1b70` | feat: TUI worker 接 `run_claude_async`，stream 进 RichLog |
| 5 | `dc366fb` | feat: git worktree + 2 Coder 并行（asyncio.gather） |
| 6 | `79983ad` | feat: 真 Planner agent，YAML 输出 + 互斥 path + backtick 防呆 |
| 7 | `0a0ca51` | feat: 端到端，Coders 真 commit、diff 收集、`summary.md` 写产物 |
| 8 | `0f98823` | feat: integration merge + 集成测试（step 1/4） |
| 8 | `a79877d` | feat: Coder timeout 熔断（step 2/4） |
| 8 | `a577eb7` | feat: Planner self-correcting retry（step 3/4） |
| 9 | `c5934b2` | feat: API failure 检测 + rate-limit 表面化（step 4/4） |

---

## 6. 关键设计决策（不要重新讨论）

1. **`--setting-sources project,local` 是 Coder spawn 的标配**，不是优化项。
   原因：parent session 的 hook（laziness-self-report 等）会在 spawned child 也触发，
   污染 stream-json 输出 + 推高 cost ~$0.17/call → ~$0.05/call。
2. **Coder 必须 `--dangerously-skip-permissions`**。`claude -p` 默认 deny-all 工具调用，
   不开 Coder 没法 Edit/Write/Bash。安全边界是 worktree 沙箱本身。Planner 不需要。
3. **Worktree 层级**：每个 task 一个 `<repo>/.pm-agent-worktrees/<task_id>` 目录 +
   `ai/<task_id>` 分支。Integration 用 `ai/integration/<run_id>`。session 退出全清理。
4. **YAML 防呆**：Planner system prompt 禁 `` ` ``、`{}`、`[]`、markdown。Parser 还有
   一层 backtick-strip 重试。3 attempt retry loop 兜底。
5. **`mock_planner_decompose`** 永远是 fallback，不是降级路径主选。`--mock-planner` 仅
   用于不烧钱的 layout 调试。
6. **Day 5 起 cleanup 拆成两半**：`cleanup_worktree`（删目录）和 `delete_branch`（删分支）。
   Branch 必须活到 integration 之后。

---

## 7. 已知 demo 风险

### Coder 契约漂移（高优先级，demo 答辩必讲）

实测 `/version` demo：T-1 返回字符串 `"v1"`，T-2 断言 `{"status": 200, "body": "v1"}`。
两个 Coder 在隔离 worktree 各自做假设，integration 测试捕获 exit=1。

**这不是 bug，是产品 limit**。Demo 答辩时要主动讲：
> "Planner 没强制接口契约。Coders 各干各的会出现假设漂移。Integration 测试会
> 在合并那一层把红灯亮起来 — 这是把分歧暴露在你看得见的地方，不是装作成功。"

Day 11+ 缓解方向（不在 day 10 范围）：让 Planner 输出额外的 "shared_contract" 字段，
两个 Coder prompt 都注入这一段。或加 Reviewer agent 在 integration 后跑契约检查。

### 成本

每次完整 demo run（real Planner + 2 Coders + integration test）≈ $0.20 - $0.40。
快速验证用 `--mock-planner` 几乎免费。Demo dry-run 别一天跑 50 次。

### TUI 在 SSH/录屏

实测 SVG snapshot 渲染干净。但 live SSH session 可能闪屏 — Day 13 录屏前要在
真实终端验证一次。

---

## 8. Day 10 任务（**这就是新会话要做的事**）

**目标**：TUI polish，纯视觉/体验，不改业务逻辑。低风险高 demo 收益。

按顺序做，做完一项 smoke 验视觉一项。Day 10 应该 1-2 个 commit 收尾，不是 5 个。

- [ ] **10.1** 顶部 progress bar 右侧加 `X/Y tasks` 文字（`tasks_done/len(_tasks)`）
- [ ] **10.2** Agent card 状态加 unicode 图标：`▶ running` / `✓ done` / `✗ failed` / `⏸ idle`。
       改 `_set_agent_status` 让它在 name 前加图标。
- [ ] **10.3** RichLog 每行左侧加 `HH:MM:SS` 时间戳。封装一个 `_log(msg)` helper 替换
       所有 `log.write(...)`。
- [ ] **10.4** Task table Status 列用颜色（done=绿 / failed=红 / running=黄 / timeout=橙）。
       Textual 用 Rich Text style 实现。
- [ ] **10.5** 底部 footer 加 `r=re-run` keybind。`BINDINGS` 加 `("r", "rerun", "Re-run")`，
       实现 `action_rerun` 重启 `_run_session`。

每条做完跑：
```bash
uv run python -m pm_agent.tui                       # mock 模式看视觉
# 最终 polish 完抓一张 SVG
# (在 smoke test 里 pilot.pause(3) 后 app.export_screenshot() 写到
#  docs/tui-day10-polish-snapshot.svg)
```

### Day 10 完成的验证

- mock mode 能 q 退出 + r 重跑
- SVG snapshot 含 ▶/✓/✗ 图标 + 时间戳列 + 颜色 status
- 不破 Day 9 端到端：
  ```bash
  TGT=/tmp/pm-agent-day7-target
  git -C "$TGT" reset --hard master && rm -rf "$TGT/.pm-agent-worktrees"
  uv run python -m pm_agent.tui --repo "$TGT" --coder-timeout 120 \
    --test-cmd 'python3 -c "import tests.test_server as t; [getattr(t,n)() for n in dir(t) if n.startswith(\"test_\")]; print(\"PASS\")"' \
    "Add /health endpoint returning a dict with key status equal to ok, plus a test"
  ```
  这条命令应该全 PASS。

---

## 9. Day 11-14 outline（不要现在做）

- **Day 11-12**: 第二个复杂 demo task。**选 task 时保守**，5-10 分钟双 Coder 能跑完。
                 Demo 思路：可以演示一次"故意制造的失败 → recovery 流程"——比如关掉
                 网络让 Planner 触发 retry，或 timeout 强制熔断，把错误处理面板展示出来。
- **Day 13**: 录屏 + answer 准备。把 9 commit 串成 9 段叙事。每段配一张 SVG snapshot。
              对每个 known limit 准备一句话回复。
- **Day 14**: buffer / dry-run。至少跑 3 次零事故 demo 才能上场。

---

## 10. 协作偏好（zmy 风格）

- **直说，别恭维**。我反推时多说一句"我反推因为 X"，让我看见你的推理。
- **不写多余文档**。代码 comments 只在 WHY 不显然时写。
- **进度用紧凑的 status table 报**，不要长篇散文。
- **每条 sub-task 完成后 git commit**。Day 10 1-2 个 commit 即可。
- **别再写第 N 份 PRD**。时间紧，直接动手。
- **测试代价心里有数**：每次完整 e2e ~$0.30。验证 logic 优先 mock + unit test，
  最后一次 real e2e 出 SVG。

---

## 11. 环境特殊性（避坑）

- **Parent session 的 stop-hook**（laziness-self-report）会强制每条助手回复带
  `<laziness-self-report>` 块。不要去关或修。Spawned `claude -p` 已用
  `--setting-sources project,local` 隔离掉了。
- **teamagent 自动 commit**：`.teamagent/` 和 `.githooks/` 的偶发 `[teamagent-sync]`
  commit 不要管，这是 user 主动选择保留的。
- **target repo `/tmp/pm-agent-day7-target` 默认 branch 是 `master` 不是 `main`**。
  WorktreeManager 已经会动态检测 base_branch，不需要修。
- **SVG snapshot 的 grep 限制**：Textual export_screenshot 把每个词渲染成独立的
  `<text>` 元素，所以 grep 多词字符串会 miss。grep 单词没事。

---

## 12. Quickref 命令

```bash
# 跑 mock 模式（cheap，验视觉）
uv run python -m pm_agent.tui

# 跑单 Coder 真模式
uv run python -m pm_agent.tui --single "what is 2+2"

# 跑多 Coder 真模式（默认 real Planner）
uv run python -m pm_agent.tui --repo /tmp/pm-agent-day7-target "your goal"

# 完整 e2e 含集成测试
uv run python -m pm_agent.tui \
    --repo /tmp/pm-agent-day7-target \
    --coder-timeout 180 \
    --test-cmd 'pytest tests/' \
    "your goal"

# 重置 demo target
TGT=/tmp/pm-agent-day7-target
git -C "$TGT" reset --hard master
rm -rf "$TGT/.pm-agent-worktrees"
git -C "$TGT" branch | grep -v master | xargs -I {} git -C "$TGT" branch -D {}

# 看最近 run 的 summary
ls -t ~/.pm-agent/runs | head -1 | xargs -I {} cat ~/.pm-agent/runs/{}/summary.md
```

---

## 13. gstack skills（新会话可用）

如果新会话识别 gstack：

- `/gstack-investigate` — 真 bug 调查（root cause first）
- `/gstack-qa` — 测 demo 找 bug + 修
- `/gstack-review` — 看 diff 做 PR review
- `/gstack-ship` — 走完整 ship 流程
- 不要再跑 `/gstack-office-hours` 或 `/gstack-autoplan`，那是 day 0 的事，方向已定

---

## 14. 一句话给新 Claude

读完这份 HANDOFF 你就有上下文了。从 Day 10 §8 开始，1→2→3→4→5 顺序做完，
1-2 个 commit 收尾。中间不要重新讨论方向 / 不要写新 PRD / 不要建议 refactor。
有真问题（command 不通、API 错误等）就报，没就直接干。结束按惯例给一个紧凑
status table，标 STATUS: DONE。
