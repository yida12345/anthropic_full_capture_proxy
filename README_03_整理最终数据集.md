# 03 整理最终数据集

[上一步：运行 Harbor](README_02_运行_Harbor.md) · [返回总览](README.md) · [下一步：导出 ShareGPT](README_04_导出_ShareGPT.md)

等单个 task 或整个 Harbor run 完成并保存客户端原生会话、等待后台采集完成后，根据输入目录布局选择下面四个脚本之一。输出目录必须不存在或为空，防止旧轮次残留导致数据混合。

若启用了自动归档原件清理，先将所需批次恢复到独立采集目录，再补齐未归档轮次；
不能直接把仅剩近期文件的在线目录当完整数据。操作见[归档恢复](README_08_自动归档.md#恢复与重新导出)。

## 选择整理脚本

| 输入目录布局 | 使用脚本 |
| --- | --- |
| 标准 Harbor run、`tasks/`、单个 task 或 session JSONL | `finalize.py` |
| 无 `tasks/` 中间层的 Harbor jobs 目录 | `finalize-harbor.py` |
| `node/worker` 目录 | `finalize-node.py` |
| 单个 `proc_n/<group>/<task_id>` 目录，每个任务内有 `result.json` | `finalize-proc-split.py` |

## Kimi / Hermes 会话整理

所有整理入口默认 `--client cc`，原有 Claude Code 行为保持一致。
Kimi 和 Hermes 显式指定会话格式；独立客户端使用 `finalize.py --session-dir`：

```bash
python finalize.py --client kimi \
  --capture-dir ./capture_logs/kimi_run \
  --session-dir "$HOME/.kimi-code/sessions" \
  --output-dir ./dataset_output/kimi_run

python finalize.py --client hermes \
  --capture-dir ./capture_logs/hermes_run \
  --session-dir "$HOME/.hermes/state.db" \
  --output-dir ./dataset_output/hermes_run
```

会话目录需按实际版本/profile 配置传入。Kimi 用 Wire 响应 ID 或完整历史，Hermes
以只读 SQLite 事务匹配历史与响应，均拒绝有歧义的候选。独立 sessions 数据没有任务
目录时以 session/lineage 根 ID 作为 task ID；不会根据 prompt 猜任务名。

`finalize-harbor.py`、`finalize-node.py`、`finalize-node-split.py`、
`finalize-proc-split.py` 也接受 `--client kimi|hermes`，仍用 `--harbor-run-dir`
指定原有布局根目录，成功/失败筛选规则不变。会话文件须归档到对应任务日志路径，
具体布局见 [客户端接入说明](README_07_Kimi_Code与Hermes.md#harbor--node--proc-布局)。

原生压缩摘要归到 `subagent_acompact-kimi-*` / `subagent_acompact-hermes-*`。
这是压缩操作的导出分组；Hermes 因压缩创建的后继 session 仍归主 agent，
同一 message_uid 的复制不会伪造额外模型轮次。未知改写、缺失或冲突保留在
unmatched/conflicts，详见[压缩关联规则](README_07_Kimi_Code与Hermes.md#原生-compact-与恢复导出)。

以下各方案的 Claude Code session 搜索说明适用于默认 `--client cc`。

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

如果 Claude Code 自动压缩时没有生成 `agent-acompact-*.jsonl`，finalizer 会用 HTTP compact 请求、session header、主 session 的 `compact_boundary`、时间和完整摘要正文做严格的一对一恢复。成功恢复的轨迹输出到 `subagent_acompact-recovered-<boundary_uuid>/`；源 session 不会被改写。证据不完整或不唯一时仍留在 `unmatched/`，详情见[采集与关联机制](README_05_采集与关联机制.md)。

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

## 方案 D：proc_n 分组任务目录

适用于输入路径直接指向单个 `proc_n`，任务先按 `kernel`、`user` 等 group 分组，每个任务目录内分别保存 session 和判定结果：

```text
<proc_n>/
├── <group>/
│   └── <task_id>/
│       ├── logs/
│       │   └── projects/**/*.jsonl
│       └── result.json
└── run.log
```

运行：

```bash
python finalize-proc-split.py \
  --capture-dir ./capture_logs/run_20260821 \
  --proc-dir /path/to/run/proc_0 \
  --output-dir ./dataset_output/proc_0
```

`--proc-dir` 必须是最后一级的单个 `proc_n`，不能传它们共同的上级目录。为了兼容其他整理脚本的调用方式，也可以用 `--harbor-run-dir` 作为 `--proc-dir` 的别名。

脚本只处理完整任务目录。完整任务必须同时满足：

- `logs/projects/` 下至少存在一个 `*.jsonl` session 文件
- `result.json` 是合法 JSON，且包含非空的 `checks` 数组
- 每个 `checks[*].score` 都是数值 `1.0` 或 `0.0`

所有 check 的 `score` 都为 `1.0` 时，任务输出到 `successful/tasks/`；任一 check 的 `score` 为 `0.0` 时，任务输出到 `failed/tasks/`。缺少 session、缺少 `result.json`、JSON 损坏或 score 非法的未完成任务会被跳过，不会混入 `failed/tasks/`。

最终输出的 task ID 使用 `<task_id>` 目录名，例如 `kernel_kernelctf_CVE-2023-3776_lts`。`result.json.task_id` 仅是结果元数据，示例值 `kernel:kernelctf/CVE-2023-3776_lts` 含路径分隔符或平台非法字符，因此不用于生成输出目录。

如果只需要成功轨迹，增加 `--only-successful`。无论某一类是否有轨迹，脚本都会创建稳定的 `successful/tasks/` 和 `failed/tasks/` 目录。跳过数量、跳过原因、评分分类数量及实际导出的成功/失败任务数量会写入 `finalization_report.json`。

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
