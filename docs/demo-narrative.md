# Demo Narrative — pm-agent

Live walk-through script for the 5/22 demo. 9 beats, ≈ 10-12 minutes.

Pitch (one-liner first, then beats):

> "Claude Code 频繁打断让你分心，所以我做了一个 PM-agent：你给一个目标，它自己拆任务、
>  并行起多个 Coder、合 diff、跑测试、把结果摆给你看——全程不打扰。"

Order: foundation → core capability → robustness → polish → demo packaging.
Each beat = 1-2 commits + 1 SVG + ≤ 90s of talk. Skip the failure demo if running
short — beats 1-6 + 8-9 still tell the story.

---

## Beat 1 — Foundation: 60-line `claude -p` runner (Day 1)

- **Commits**: `431dc64` (runner) + `ab73cfd` (settings isolation)
- **SVG**: none — show terminal output instead
- **Talk** (60s):
  > "起手不用 SDK，直接 spawn `claude -p` 当 building block——60 行 stdlib only。
  >  关键 trick：spawned child 默认会继承父 session 的 hook 和 40k token 用户级
  >  system prompt，导致每次调用 $0.17。我加了 `--setting-sources project,local`
  >  把它隔离掉，cost 降到 $0.05，砍 68%。这一条决定了整个项目的单位成本。"
- **Show**:
  ```bash
  uv run python -m pm_agent.runner "what is 2+2"
  ```

## Beat 2 — TUI 5-panel skeleton (Day 2)

- **Commits**: `e3ec409`
- **SVG**: `docs/tui-day2-snapshot.svg`
- **Talk** (45s):
  > "Textual 5-panel layout：goal bar、task table、agent cards、live log、footer。
  >  这一版还是 mock 数据 + 0.8s ticker，但 layout 决定后面所有东西的视觉锚点。
  >  为什么 TUI 不是 web UI？单人 macOS 本地跑，TUI 不用起服务、5 分钟见效。"

## Beat 3 — Real `claude -p` stream into TUI (Day 3)

- **Commits**: `cfc1b70`
- **SVG**: `docs/tui-day3-real-snapshot.svg` (单 Coder, 4 events, $0.057)
- **Talk** (45s):
  > "把 mock 拔掉，real `run_claude_async` 的 stream-json 事件直接灌进 RichLog。
  >  这是第一次看到真 LLM 在 TUI 里活的。单 Coder, $0.057 / 4 events。"

## Beat 4 — 2 parallel Coders via `git worktree` (Day 5)

- **Commits**: `dc366fb`
- **SVG**: `docs/tui-day5-multi-snapshot.svg` (2 coders, 8 events, $0.112)
- **Talk** (90s):
  > "并发由 `asyncio.gather` 跑两个 Coder，隔离由 git worktree——每个 task 一个
  >  `.pm-agent-worktrees/<task_id>` 目录 + `ai/<task_id>` 分支，session 退出全清理。
  >  这是整个项目最核心的架构决定：worktree 是物理沙箱，不是 file lock 或 mutex，
  >  Coders 各自 commit 各自的 branch，完全不冲突。"
- **Show**: side-by-side panels updating, 两边的 RichLog 在交替滚

## Beat 5 — Real Planner emits validated YAML task DAG (Day 6)

- **Commits**: `79983ad`
- **SVG**: `docs/tui-day6-planner-snapshot.svg`
- **Talk** (60s):
  > "Planner 是另一个 `claude -p` 调用，吃 goal 吐 YAML。System prompt 禁 backtick
  >  和 markdown 防呆，parser 还有 backtick-strip 二次重试。互斥的 allowed_paths
  >  是 Planner 自己负责输出——不让两个 Coder 撞同一个文件。"

## Beat 6 — End-to-end: diff + run summary (Day 7)

- **Commits**: `0a0ca51`
- **SVG**: `docs/tui-day7-e2e-snapshot.svg`
- **Talk** (90s):
  > "第一次端到端跑通：Coders 真在 worktree 里 commit，diff 收集到
  >  `~/.pm-agent/runs/<run-id>/T-X.diff`，summary.md 是 PR 风格的报告。
  >  现在用户可以 `git apply` 这个 diff 接到自己的 branch。"
- **Show**: `cat ~/.pm-agent/runs/<latest>/summary.md`

## Beat 7 — Integration merge + tests + robustness (Day 8)

- **Commits**: `0f98823`, `a79877d`, `a577eb7`, `c5934b2` (4 step bundle)
- **SVG**: `docs/tui-day8-integration-snapshot.svg`
- **Talk** (120s):
  > "Day 8 是 4 step 一气呵成的 robustness 包：(1) Integration merge — 把所有
  >  Coder branch 合到 `ai/integration/<run-id>` 跑测试；(2) Coder timeout 熔断
  >  — 每个 Coder 进程 180s 上限，超时 kill 并报 timeout；(3) Planner self-correcting
  >  retry — YAML parse 失败带错误信息 retry，最多 3 attempts；(4) API failure
  >  检测 + rate-limit 表面化。这天之后 orchestrator 才能宣称'生产级 robust'。"

## Beat 8 — TUI polish (Day 10)

- **Commits**: `c230a09`
- **SVG**: `docs/tui-day10-polish-snapshot.svg` + `docs/tui-day10-e2e-snapshot.svg`
- **Talk** (60s):
  > "纯视觉打磨：X/Y task counter、▶/✓/✗/⏸ 状态图标、每行 HH:MM:SS 时间戳、
  >  Status 列颜色（绿/红/黄/橙）、`r` 键 re-run。不改业务逻辑。视觉冲击直接
  >  上去一档。"

## Beat 9 — Failure recovery + second positive demo (Day 11)

- **Commits**: `dd35b5f` (inject-fault), `cbf7936` (/stats run)
- **SVG**:
  - `docs/tui-day11-fault-planner-yaml-snapshot.svg` (Planner retry + mock fallback)
  - `docs/tui-day11-fault-coder-timeout-snapshot.svg` (双 Coder 熔断)
  - `docs/tui-day11-fault-api-error-snapshot.svg` (rate-limit + is_error)
  - `docs/tui-day11-stats-snapshot.svg` (正向 /stats demo, $0.44, 102s, integration PASS)
- **Talk** (180s = 90s recovery + 90s 正向):
  > "**Recovery 演示** — `--inject-fault {planner-yaml,coder-timeout,api-error}` 三模式
  >  确定性触发三条错误路径。Planner-yaml：每次 attempt 都 fail parse → fallback
  >  到 mock_planner_decompose，演示继续跑。Coder-timeout：双 Coder 都熔断，cards
  >  变红 ✗，integration 还是 merge 两个空 branch + 写 summary（失败也有 PR 风格
  >  报告）。API-error：synthetic rate-limit + is_error result，看到 ❌ API ERROR
  >  headers。
  >
  >  **正向 demo 二号** — `/stats` request counter（一个 module-level int 加 /stats
  >  endpoint）。比 /health 复杂一档——counter 名是 T-1 / T-2 之间的隐式契约，
  >  踩了'契约漂移'风险区。Planner 把 `request_count` 写进 acceptance criteria，
  >  两个 Coder 独立工作得出完全一致的 shape，integration test PASS。102s, $0.44。"

---

## Known limits — answer prep cheat-sheet

| 问题 | 一句话答 |
|---|---|
| Coder 契约漂移怎么处理？ | Planner 在 acceptance criteria 里 pin 名字 + integration test 兜底；漂移会在合并那一层亮红灯，是把分歧暴露而不是装作成功。Future: shared_contract 字段 + Reviewer agent。 |
| Cost 怎么样？ | 完整 e2e $0.30-0.60；inject-fault 三模式接近免费；mock-planner 模式几乎免费。Day 1 isolation fix 把单 call 从 $0.17 砍到 $0.05。 |
| 为什么不用 Cursor/Aider/Devin？ | 这是评估"实习生能从零写多 agent 编排"的命题，不是"能用现成工具"。但 orchestrator 模式本身和那些工具不冲突——Coder 完全可以换成它们。 |
| 为什么 TUI 不是 web UI？ | 单人 macOS 本地 demo，TUI 5 分钟见效 + 不用起服务 + Textual 渲染干净。Web 化是 future work，不是 PoC 范围。 |
| 扩展到 N 个 Coder？ | `asyncio.gather` 已经 N-way，只是 Planner system prompt 默认 limit 2-3 task。改 prompt 即可。Worktree 没有并发上限。 |
| Rate limit 怎么办？ | Day 8 step 4 把 rate_limit_event 表面化（看 day8 SVG）。non-allowed 计数 + summary.md 标 ❌ API ERROR。Retry 由用户决定，orchestrator 不自动 backoff（避免烧钱）。 |
| 中途 abort 会留垃圾吗？ | `q` 退出 → `finally` 块清理所有 worktree + non-base branch。`~/.pm-agent/runs/<id>/` 产物保留。 |
| 怎么集成进真实开发流？ | PoC 是本地 branch。生产化路径：(a) git push 到远程；(b) PR 自动开到 Github；(c) Reviewer agent 做 contract 检查；(d) 跨 session run 持久化。都是 future work。 |
| 为什么 Coder 要 `--dangerously-skip-permissions`？ | `claude -p` 默认 deny-all 工具调用，不开 Coder 没法 Edit/Write/Bash。安全边界是 worktree 沙箱本身——Coder 改的 branch 不会自动合到 base，integration test 拦截坏 diff。 |
| Mock planner 是降级路径吗？ | 不是。Mock 是 fallback for unrecoverable Planner failure（3 retry 都失败），主选永远是 real Planner。`--mock-planner` 只用于不烧钱的 layout 调试。 |

---

## Recording prep checklist (Day 13 用)

- [ ] 终端字体大小确认（Fira Code 18pt 起跳）
- [ ] iTerm 主题对比度够录屏（深色背景 + 高对比文本）
- [ ] `/tmp/pm-agent-day7-target` 干净 seed (`git reset --hard master` + 删 worktrees)
- [ ] 预热 claude session（避免第一次 call 慢）
- [ ] 网络稳，5G/wifi 切到稳定档（避免 rate limit 录到一半）
- [ ] 录屏软件：QuickTime 或 OBS，分辨率 ≥ 1920x1080
- [ ] 准备 fallback：如果 live e2e 撞 rate limit，切到 `--inject-fault` 三模式继续演

## Dry-run sequence (Day 14)

至少 3 次完整 demo flow 零事故才能上场。每次记录：

| Run # | 日期 | 卡点（如有） | 总时长 | 总 cost |
|:-:|---|---|---|---|
| 1 | _ | _ | _ | _ |
| 2 | _ | _ | _ | _ |
| 3 | _ | _ | _ | _ |
