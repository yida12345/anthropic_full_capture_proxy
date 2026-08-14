# 03 整理最终数据集

[上一步：运行 Harbor](README_02_运行_Harbor.md) · [返回总览](README.md) · [下一步：导出 ShareGPT](README_04_导出_ShareGPT.md)

等单个 task 或整个 Harbor run 完成并保存 Claude Code session 后，根据输入目录布局选择下面三个脚本之一。输出目录必须不存在或为空，防止旧轮次残留导致数据混合。

## 选择整理脚本

| 输入目录布局 | 使用脚本 |
| --- | --- |
| 标准 Harbor run、`tasks/`、单个 task 或 session JSONL | `finalize.py` |
| 无 `tasks/` 中间层的 Harbor jobs 目录 | `finalize-harbor.py` |
| `node/worker` 目录 | `finalize-node.py` |

## 方案 A：标准 Harbor 目录

```bash
python finalize.py \
  --capture-dir ./capture_logs/run_20260806 \
  --harbor-run-dir /data1/nfs/ztr/run/jobs/2026-08-06__11-18-33/cve-2010-5312__WitBjmd/ \
  --output-dir ./dataset_output/run_20260806/test3
```

`--harbor-run-dir` 支持：

- Harbor run 根目录，下面包含 `tasks/`
- `tasks/` 目录
- 单个 task 目录
- 单个 session JSONL 文件

### session 搜索和 task ID 解析

传入目录时，脚本会从该目录开始递归搜索 `*.jsonl`，但只保留路径组件中包含 `projects` 或 `subagents` 的文件，以避开 Harbor 中其他用途的 JSONL。例如：

```text
<run>/tasks/task_a/logs/run/cc_session/.claude/projects/-workspace/session-1.jsonl
<run>/tasks/task_a/logs/run/cc_session/.claude/projects/-workspace/
  session-1/subagents/agent-sub1.jsonl
```

第一类通常是主 agent session；位于 `subagents/` 的第二类会被识别为子 agent。如果直接传入一个 JSONL 文件，脚本会直接读取，不再应用 `projects/subagents` 路径过滤。

```text
传 <run>/
  -> 从相对路径 tasks/<task_id>/... 提取 task_id

传 <run>/tasks/
  -> 用 session 相对路径的第一层目录名作为 task_id

传 <run>/tasks/<task_id>/
  -> 当目录内存在 final_status.json 时，用当前目录名作为 task_id

传单个 session JSONL
  -> 能关联 message.id，但因没有上层 task 路径，task_id 为 unknown_task
```

如果希望最终数据保留真实 task ID，建议至少传单个 task 目录，而不是单独传 session 文件。

扫描 session 时，只索引合法 JSON object 中 `type == "assistant"`、`message` 为 object 且 `message.id` 为非空字符串的行。主/子 agent 由文件位置、`agentId`/`agent_id` 和 `isSidechain`/`is_sidechain` 联合判断。子 agent ID 优先取记录内的 `agentId`，缺失时从 `subagents/agent-<id>.jsonl` 文件名提取。

## 方案 B：Harbor jobs 目录

适用于没有 `tasks/` 中间层的布局：

```text
<job-root>/
├── <task_id>/
│   ├── agent/sessions/projects/**/*.jsonl
│   └── verifier/reward.txt
├── config.json
└── result.json
```

运行：

```bash
python finalize-harbor.py \
  --capture-dir /data1/nfs/ztr/anthropic_full_capture_proxy/capture_logs/run_20260806_1 \
  --harbor-run-dir /data1/nfs/ztr/run/jobs/2026-08-06__16-10-23/ \
  --output-dir ./dataset_output/2026-08-06__16-10-23
```

job 根目录的每个直接子目录名会被完整用作 task ID，例如 `cve-2010-5312__oGPeD8H`。脚本只在每个 task 的 `agent/sessions/projects/` 下递归寻找主 session 和 subagent JSONL。

成功判定使用 job 根目录 `result.json` 中 `stats.evals.*.reward_stats.reward`：只要 task ID 在 `"1.0"` 数组中就是成功，输出到 `successful/tasks/`；已关联 session 但不在 `"1.0"` 中的 task（包括 `"0.0"` 和运行异常）输出到 `failed/tasks/`。不再依赖 `<task_id>/verifier/reward.txt`。

`result.json` 不会用来凭 task ID 生成轨迹。异常 task 如果不存在 session，就无法与 capture 按 `message.id` 关联，因此不会导出，也不计入成功或失败轨迹数。

如果只需要成功轨迹，增加 `--only-successful`；启用后只保留 `result.json` 中 reward 为 `1.0` 的 task。

两个分组目录内都保留 `export_sharegpt.py` 要求的 `tasks/` 层级，因此可分别导出：

```bash
python export_sharegpt.py --input-dir <output-dir>/successful --output-dir <sharegpt-successful> --reasoning-mode separate
python export_sharegpt.py --input-dir <output-dir>/failed --output-dir <sharegpt-failed> --reasoning-mode separate
```

## 方案 C：node/worker 目录

适用于：

```text
<node>/
└── worker*/
    ├── logs/
    │   └── <task_id>/logs/projects/**/*.jsonl
    └── result/
        └── <task-prefix>_<run-hash>.log
```

运行：

```bash
python finalize-node.py \
  --capture-dir ./capture_logs/run_20260805 \
  --harbor-run-dir /path/to/node0 \
  --output-dir ./dataset_output/node0
```

例如任务目录 `arvo_10013-b4800b5cb2eb4314bff374befc95bd55` 对应 result 日志 `arvo_10013_b4800b5cb2eb4314bff374befc95bd55.log`。脚本与 `shili/print_result.py` 一样，从 result 文件名最后一个下划线处分隔并用连字符还原任务目录名。

只转换正确轨迹时增加 `--only-successful`。正误判定严格使用 `print_result.py` 的状态码逻辑：忽略 `vul_exit_code == 0` 的记录后取最后一次提交；漏洞版退出码不在 `{0, 71, 300}` 且修复版退出码为 `0` 时才是 `correct`。result 缺失、状态码缺失及其他分类都不会作为成功轨迹输出。

## 输出结构

```text
dataset_output/run_20260806/
├── finalization_report.json
├── tasks/
│   └── <task_id>/
│       ├── task.json
│       ├── main_agent/
│       │   ├── agent.json
│       │   └── round_000001/
│       │       ├── request.json
│       │       └── response.json
│       └── subagent_<agent_id>/
│           ├── agent.json
│           └── round_000001/
│               ├── request.json
│               └── response.json
├── unmatched/      # 无 message.id 或找不到 session 的 Messages 请求
├── auxiliary/      # count_tokens 等无法按 message.id 归属的非推理请求
└── conflicts/      # message.id 冲突，绝不自动猜测
```

每个最终 `request.json` 和 `response.json` 都包含 task/session/agent/round 关联信息、HTTP transport 元数据、解析后的 JSON、原始 body 及其 SHA-256，以及原始 capture 目录。原始 body 为 UTF-8 文本，非 UTF-8 时使用 Base64。
