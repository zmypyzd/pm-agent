# pm-agent · bug 复现脚本集

为 `BUGS.md` 里 34 条发现各写一份独立可跑的 repro。每个脚本：

- **独立可跑**：不依赖其它 repro 文件
- **零 API 成本**：需要 `claude` 子进程的，用 `_harness.claude_shim()` 注入假二进制
- **二态退出**：exit 0 = 缺陷仍存在；exit 1 = 缺陷已修复 / 未触发
- **白盒友好**：直接 import `pm_agent` 模块或读源码字符串

## 跑全套

```bash
bash reproductions/run_all.sh
```

按 `BUGS.md` 的"推荐修复顺序"逐条执行，最后输出汇总：

```
Reproduced (bug still present): N
Not reproduced (likely fixed):  M
Script error / missing:         K
```

## 跑单条

```bash
# 单个 tier
bash reproductions/run_all.sh critical
bash reproductions/run_all.sh high

# 单个 bug
bash reproductions/run_all.sh BUG-001

# 直接调用
uv run python reproductions/critical/bug_001_stderr_deadlock.py
```

## 目录结构

```
reproductions/
├── README.md                ← 本文件
├── run_all.sh               ← 一键跑 + 汇总
├── _harness.py              ← 共享工具：temp_git_repo / claude_shim / report
│
├── critical/                ← 进程级 / 资源死锁
│   ├── bug_001_stderr_deadlock.py        stderr PIPE 永不被读 → 死锁
│   └── bug_002_test_cmd_orphans.py       test_cmd 不杀进程组 → 孤儿
│
├── high/                    ← 数据正确性 / 安全 / 主流程崩
│   ├── bug_003_run_id_collision.py       秒精度 run_id 同秒覆盖 artifacts
│   ├── bug_004_011_027_parse_tasks_type_checks.py  parse_tasks str() 强转
│   ├── bug_005_glob_disjoint.py          glob 重叠未检测
│   ├── bug_006_coder_no_allowed_paths.py Coder 不知 allowed_paths
│   ├── bug_007_detached_head.py          detached HEAD → base='HEAD'
│   ├── bug_008_default_repo_race.py      default repo 并发 race
│   ├── bug_009_n_gt_2_tasks.py           N>2 task UI 缺失
│   ├── bug_010_cancel_doesnt_kill.py     worker cancel 不杀 claude
│   ├── bug_025_git_author_env.py         无 git identity 静默失败
│   └── bug_026_filename_prompt_injection.py 文件名注入 prompt
│
├── medium/                  ← UX / 边界 / 一致性
│   ├── bug_012_yaml_bomb.py
│   ├── bug_013_extract_yaml_newline.py
│   ├── bug_014_diff_stats.py
│   ├── bug_016_test_timeout_unconfigurable.py
│   ├── bug_018_progress_not_updated.py
│   ├── bug_019_cleanup_silent.py
│   ├── bug_020_day8_cleanup_overlap.py
│   ├── bug_024_demo_path_mismatch.py
│   ├── bug_029_is_error_not_checked.py
│   ├── bug_031_default_branch.py
│   ├── bug_032_arg_max.py
│   └── bug_035_no_tests.py
│
└── low/                     ← hygiene / dead code
    ├── bug_015_ev_null_comment.py
    ├── bug_017_max_retries_hardcoded.py
    ├── bug_021_idle_range.py
    ├── bug_022_reviewer_dead.py
    ├── bug_023_main_orphan.py
    ├── bug_028_empty_path.py
    ├── bug_033_log_throttle.py
    └── bug_034_pyproject_scripts.py
```

## 总数

- Critical 2 + High 11 + Medium 12 + Low 8 = **33 个文件**（BUG-004/011/027 合并为一个文件）

## 如何当回归测试用

修完一个 bug 之后跑对应 repro：

```bash
$ uv run python reproductions/critical/bug_001_stderr_deadlock.py
[BUG-001] NOT-REPRODUCED — runner returned in 1.2s (≤ 4s budget)
$ echo $?
1
```

`exit 1` 在 `run_all.sh` 里被归入 "Not reproduced (likely fixed)"。

把 `run_all.sh` 接到 CI 里：当 "Reproduced" 计数从 N 减到 N-1 时就证明这次提交修了一条 bug。**反之，如果"Reproduced"计数突然升回 N**，说明回归了。

## 注意事项

1. **macOS 文件系统**：`bug_026_filename_prompt_injection.py` 优先尝试创建含 `\n` 的文件名；APFS 允许，但若你的 FS 拒绝，脚本自动回退到 `\t`，仍能演示同一类注入。
2. **claude shim** 只是 bash 脚本，shim 进程本身受 `PATH` 优先级影响。每个脚本在 `prepend_path()` 上下文里执行 —— 不污染你的 PATH。
3. **临时目录** 默认在 `$TMPDIR`，每条脚本 finally 里 `shutil.rmtree(ignore_errors=True)`。脚本异常崩溃时少数情况下会留下 `bug-repro-*` / `claude-shim-*` 目录 —— 可手动清理。
4. **不修源码**：所有脚本只 import / 读取 / 模拟，绝不写入 `pm_agent/*.py`。

## 与 `BUGS.md` 的映射

每个 repro 的 docstring 第一行直接对应 `BUGS.md` 中同号条目的标题。修复时建议：

1. 读 `BUGS.md` 拿到 expected behavior
2. 跑对应 repro，确认能复现
3. 改 `pm_agent/*.py`
4. 再跑 repro，期望 exit 1
5. 提交：`fix: BUG-XXX <一句话>` + commit message 引用 BUGS.md 行号
