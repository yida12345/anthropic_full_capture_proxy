# 错误报告：上游 Anthropic 流式 SSE 结构不完整，Harbor 成功但轨迹无法严格导出

## 报告范围

本报告只使用以下完整运行结果：

```text
/data1/nfs/ztr/a_log/20260816-test
```

对应 Harbor job：

```text
/data1/nfs/ztr/a_log/20260816-test/2026-08-16__10-20-39
```

运行时间为 2026-08-16。模型为 `openai/glm52fp8gj`，Claude Code 版本参数为 `2.1.89`，代理上游地址为 `http://dlrrrrbs.tcp01.cn:13007`。

## 结论

这次运行中 Harbor 的 4 个 trial 全部成功，4 个 reward 均为 `1.0`，没有 Harbor exception；但上游返回的部分 Anthropic SSE 存在结构错误或提前断流，导致 117 个已关联 round 中有 12 个响应不完整。

严格导出器 `export_sharegpt.py` 按 agent 遇到第一个不完整响应即停止该 agent，因此最终：

- 数据集包含 4 个成功任务、9 个 agent、117 个已关联 round。
- 12 个已关联 round 不完整。
- 其中 11 个记录 `CancelledError`，1 个记录 `RemoteProtocolError: incomplete chunked read`。
- 8 个 round 明确记录 `content_block_delta` 指向从未开始的 index。
- 严格导出报告 6 个失败 agent，只成功写出 3 个 agent 的 ShareGPT 文件。

问题不影响 Harbor 最终得分，是因为客户端随后对不完整请求进行了成功重试；但失败的原始 round 仍被 capture 和 finalize 保留下来，所以严格 SFT 导出会拒绝它们。

## 错误最明显的位置

### 1. 严格导出的总错误报告

```text
/data1/nfs/ztr/a_log/20260816-test/sharegpt_output/successful/export_errors.json
```

该文件开头记录：

```json
{
  "error_count": 6
}
```

最明显的错误条目指向：

```text
/data1/nfs/ztr/a_log/20260816-test/dataset_output/successful/tasks/cve-2017-15198__3FUgfEH/main_agent/round_000006/response.json
```

这个响应记录了 25 次：

```text
content_block_delta 指向不存在的 index=1
```

同时记录：

```json
{
  "stream": true,
  "aggregation_complete": false,
  "sse_event_count": 28,
  "transport_error": "CancelledError: ...",
  "client_disconnected": true,
  "stop_reason": null
}
```

### 2. 对应的原始 capture

上述 dataset round 对应：

```text
capture_id = cap_057791438e4c42beb3cacdaf76bb4fb5
```

最关键的原始文件地址：

```text
/data1/nfs/ztr/a_log/20260816-test/capture_logs/raw/completed/cap_057791438e4c42beb3cacdaf76bb4fb5/sse_events.jsonl
/data1/nfs/ztr/a_log/20260816-test/capture_logs/raw/completed/cap_057791438e4c42beb3cacdaf76bb4fb5/response.json
/data1/nfs/ztr/a_log/20260816-test/capture_logs/raw/completed/cap_057791438e4c42beb3cacdaf76bb4fb5/state.json
/data1/nfs/ztr/a_log/20260816-test/capture_logs/raw/completed/cap_057791438e4c42beb3cacdaf76bb4fb5/response.body
```

`sse_events.jsonl` 的实际事件序列为：

```text
sequence 1: message_start
sequence 2: content_block_start index=0 type=text
sequence 3: content_block_stop  index=0
sequence 4-28: content_block_delta index=1 type=input_json_delta
```

整个文件中不存在：

```text
content_block_start index=1
content_block_stop index=1
message_delta
message_stop
```

其中 sequence 4 已经开始发送工具参数：

```json
{
  "event": "content_block_delta",
  "data": {
    "type": "content_block_delta",
    "index": 1,
    "delta": {
      "type": "input_json_delta",
      "partial_json": ""
    }
  }
}
```

但此前从未声明 index 1 是哪个 content block。代理只能将 25 个 index 1 增量全部判定为无法聚合。

原始 `response.json` 还包含响应头：

```text
x-litellm-version: 1.81.3
content-type: text/event-stream; charset=utf-8
```

对应 `state.json` 为：

```json
{
  "state": "partial",
  "client_disconnected": true
}
```

`raw/completed` 中的 `completed` 仅表示 capture 已经结束并移出 `inflight`，不表示响应内容完整；是否完整必须看目录内的 `state.json`。

### 3. 紧邻的成功非流式重试

失败的 dataset round 后面紧邻：

```text
/data1/nfs/ztr/a_log/20260816-test/dataset_output/successful/tasks/cve-2017-15198__3FUgfEH/main_agent/round_000007
```

对应成功 capture：

```text
capture_id = cap_344127d03c334ad1a09e387a21a9075f
```

关键文件：

```text
/data1/nfs/ztr/a_log/20260816-test/capture_logs/raw/completed/cap_344127d03c334ad1a09e387a21a9075f/request.body
/data1/nfs/ztr/a_log/20260816-test/capture_logs/raw/completed/cap_344127d03c334ad1a09e387a21a9075f/response.json
/data1/nfs/ztr/a_log/20260816-test/capture_logs/raw/completed/cap_344127d03c334ad1a09e387a21a9075f/state.json
```

成功重试记录：

```json
{
  "content_type": "application/json",
  "stream": false,
  "aggregation_errors": [],
  "transport_error": null,
  "client_disconnected": false,
  "stop_reason": "tool_use"
}
```

`state.json` 为：

```json
{
  "state": "complete"
}
```

失败的 round 6 和成功的 round 7 在去掉 `stream`、billing header、`cache_control` 和 thinking `signature` 等非语义字段后，请求完全相同。因此 round 7 是同一语义请求的非流式重试，不是新的任务步骤。

## 完整运行结果统计

### Harbor 结果

地址：

```text
/data1/nfs/ztr/a_log/20260816-test/2026-08-16__10-20-39/result.json
```

关键结果：

```text
n_total_trials     = 4
n_completed_trials = 4
n_errored_trials   = 0
mean               = 1.0
reward 1.0         = 4
```

这证明业务任务全部正常结束，SSE 问题不是 Harbor trial exception。

### Finalize 结果

地址：

```text
/data1/nfs/ztr/a_log/20260816-test/dataset_output/finalization_report.json
```

关键结果：

```text
captures  = 128
matched   = 117
unmatched = 7
auxiliary = 4
inflight  = 0
tasks     = 4
```

`inflight=0` 说明 finalize 时没有遗留进行中的 capture。这次错误不能归因于 Harbor 完成后强制停止代理。

### 原始 capture 状态

`capture_logs/raw/completed` 中共有 128 个 capture：

```text
complete = 109
partial  = 19
```

19 个 partial capture 中：

```text
CancelledError      = 17
RemoteProtocolError = 2
包含缺失 block index 聚合错误 = 14
client_disconnected = 17
```

其中 12 个 partial capture 成功关联到最终 dataset；其余属于 unmatched 或 auxiliary 范围。

### 严格 ShareGPT 导出结果

严格导出输入中共有 9 个 agent。`export_sharegpt.py` 遇到不完整响应时按 agent 跳过，因此：

```text
失败 agent = 6
成功导出 agent 文件 = 3
```

成功写出的只有：

```text
cve-2017-15198__3FUgfEH/subagent_aa5649070e77aac50_1.json
cve-2017-15199__S3YTzW4/subagent_a19ac8cc5f819dcdc_1.json
cve-2017-15200__SGhaEnF/main_agent_1.json
```

`export_errors.json` 只记录每个失败 agent 遇到的第一个不完整 round，因此 `error_count=6` 不代表只有 6 个不完整 round。扫描全部 117 个 dataset round 后，实际共有 12 个不完整 round。

## 严格导出报告中的 6 个失败 agent

| Task | Agent | 首个失败 round | Capture ID | 主要错误 |
|---|---|---:|---|---|
| `cve-2017-15197__cGSmYHu` | `main_agent` | `round_000002` | `cap_aa9badb17a5342b6837c100635764f79` | 缺少 index 2 start，随后断连 |
| `cve-2017-15197__cGSmYHu` | `subagent_a27150dab33aa98d8` | `round_000006` | `cap_c66b030f9c88475bb724e630402929cf` | 缺少 index 3、2 start，随后断连 |
| `cve-2017-15197__cGSmYHu` | `subagent_a4c41d8ed405b2331` | `round_000001` | `cap_448739d750f94187b9f7662e32f940b5` | 缺少 index 2 start，随后断连 |
| `cve-2017-15198__3FUgfEH` | `main_agent` | `round_000006` | `cap_057791438e4c42beb3cacdaf76bb4fb5` | 连续 25 个 index 1 delta 无 start |
| `cve-2017-15199__S3YTzW4` | `main_agent` | `round_000001` | `cap_00b612dc629840739d116595807b373c` | upstream incomplete chunked read |
| `cve-2017-15200__SGhaEnF` | `subagent_afe3e6c2dca821afd` | `round_000003` | `cap_e83fbdc255794550a1bffafaedc4d586` | 缺少 index 4 start，随后断连 |

除表中的首个失败 round 外，相同 agent 后面还可能存在其他不完整 round。

## 问题定位

### 已确认事实

1. 缺失的 `content_block_start` 在代理保存的原始 `response.body` 和 `sse_events.jsonl` 中均不存在，不是 ShareGPT 转换时丢失。
2. 原始响应已经先发送 index 1 的 delta，因此不能用“客户端断开后，上游还没来得及发送 start”解释；Anthropic SSE 中 start 必须先于相同 index 的 delta。
3. 代理对请求和响应做透明转发，聚合逻辑只是旁路记录。`aggregation_complete=false` 是检测结果，不是 SSE 被修改的原因。
4. 同语义请求切换为非流式后能完整返回，Harbor 继续执行并最终通过验证。
5. 最明显样例的上游响应头包含 `x-litellm-version: 1.81.3`。
6. Finalize 报告为 `inflight=0`，错误在正常运行期间已经发生，与结束后按 `Ctrl+C` 无关。

### 判断

主要问题位于上游 Anthropic 流式响应链路：`http://dlrrrrbs.tcp01.cn:13007` 返回的部分 SSE 不符合 Anthropic content block 事件顺序。根据响应头，问题可能位于 LiteLLM 1.81.3 的 Anthropic 流式适配层，或者 LiteLLM 后端模型到 Anthropic SSE 的转换层。

本报告不能仅凭客户端侧 capture 进一步断定是哪一个上游内部组件产生了错误；需要结合上游对应 call ID 的服务端日志确认。但可以排除 ShareGPT 导出器是原始错误来源。

此外还存在少量独立的提前断流：`RemoteProtocolError: peer closed connection without sending complete message body (incomplete chunked read)`。这同样来自代理与上游之间的响应体未完整传输，不属于 SSE 聚合器生成的错误。

## 完整复现步骤

### 1. 启动代理

在服务器终端 A 中运行：

```bash
cd /data1/nfs/ztr/anthropic_full_capture_proxy

python proxy.py \
  --listen-host 0.0.0.0 \
  --listen-port 30303 \
  --upstream-url http://dlrrrrbs.tcp01.cn:13007 \
  --log-dir /data1/nfs/ztr/a_log/20260816-test/capture_logs \
  --timeout-seconds 1200
```

确保 `/data1/nfs/ztr/configs/.env` 中 Claude Code 使用的 Anthropic API 地址指向该代理的 `30303` 端口。不要在 Harbor 运行期间停止代理。

### 2. 运行 Harbor

在服务器终端 B 中运行：

```bash
cd /data1/nfs/ztr/run

harbor run \
  -p /data1/nfs/ztr/a_log/20260815-test/tasks \
  -o /data1/nfs/ztr/a_log/20260816-test \
  -a claude-code \
  -m openai/glm52fp8gj \
  --env-file /data1/nfs/ztr/configs/.env \
  -k 1 \
  -n 15 \
  --n-concurrent-agents 6 \
  --max-retries 2 \
  --timeout-multiplier 3 \
  --ak 'version=2.1.89' \
  --ak 'max_turns=400' \
  --export-traces \
  --export-sharegpt \
  --export-episodes last \
  --no-force-build \
  --delete
```

该问题不是每个请求必现。包含工具调用的长轨迹会产生多个 content block index；并发和较多轮次可以增加观测样本，但现有证据不能证明并发本身是根因。

### 3. 确认 Harbor 正常完成

检查：

```bash
jq '.stats | {n_completed_trials, n_errored_trials, evals}' \
  /data1/nfs/ztr/a_log/20260816-test/2026-08-16__10-20-39/result.json
```

本次复现应看到 4 个 completed、0 个 errored、mean 1.0。

### 4. 查找 partial capture

```bash
grep -RIl '"state": "partial"' \
  /data1/nfs/ztr/a_log/20260816-test/capture_logs/raw/completed/*/state.json
```

检查最明显样例：

```bash
jq '{stream, content_type, aggregation_complete, aggregation_errors, sse_event_count, transport_error, client_disconnected}' \
  /data1/nfs/ztr/a_log/20260816-test/capture_logs/raw/completed/cap_057791438e4c42beb3cacdaf76bb4fb5/response.json
```

### 5. 检查原始 SSE 顺序

```bash
jq -r '[.sequence, .event, (.data.index // "-"), (.data.content_block.type // .data.delta.type // "-")] | @tsv' \
  /data1/nfs/ztr/a_log/20260816-test/capture_logs/raw/completed/cap_057791438e4c42beb3cacdaf76bb4fb5/sse_events.jsonl
```

预期复现输出的核心部分：

```text
1  message_start        -  -
2  content_block_start  0  text
3  content_block_stop   0  -
4  content_block_delta  1  input_json_delta
5  content_block_delta  1  input_json_delta
...
28 content_block_delta  1  input_json_delta
```

如果某个 index 已出现 `content_block_delta`，此前却没有同 index 的 `content_block_start`，则 SSE 结构错误已经复现。

### 6. 整理最终数据集

```bash
cd /data1/nfs/ztr/anthropic_full_capture_proxy

python finalize-harbor.py \
  --capture-dir /data1/nfs/ztr/a_log/20260816-test/capture_logs \
  --harbor-run-dir /data1/nfs/ztr/a_log/20260816-test/2026-08-16__10-20-39 \
  --output-dir /data1/nfs/ztr/a_log/20260816-test/dataset_output
```

检查：

```bash
jq '{captures, matched, unmatched, auxiliary, inflight, tasks}' \
  /data1/nfs/ztr/a_log/20260816-test/dataset_output/finalization_report.json
```

### 7. 使用严格导出器复现导出失败

输出目录必须不存在或为空：

```bash
python export_sharegpt.py \
  --input-dir /data1/nfs/ztr/a_log/20260816-test/dataset_output/successful \
  --output-dir /data1/nfs/ztr/a_log/20260816-test/sharegpt_output/successful \
  --reasoning-mode separate
```

检查错误：

```bash
jq '.error_count, (.errors[] | {task, agent, response_path, details})' \
  /data1/nfs/ztr/a_log/20260816-test/sharegpt_output/successful/export_errors.json
```

本次运行应看到 `error_count=6`，其中最明显条目指向 `cve-2017-15198__3FUgfEH/main_agent/round_000006/response.json`。

## 验证失败请求确实被成功重试

可以在项目目录运行：

```bash
python - <<'PY'
from pathlib import Path
from export_sharegpt_recovered import (
    is_semantically_identical_retry,
    load_source_round,
)

root = Path(
    "/data1/nfs/ztr/a_log/20260816-test/dataset_output/successful/tasks/"
    "cve-2017-15198__3FUgfEH/main_agent"
)
failed = load_source_round(root / "round_000006", 6)
retry = load_source_round(root / "round_000007", 7)
print(is_semantically_identical_retry(failed, retry))
PY
```

预期输出：

```text
True
```

这项比较只忽略 `stream` 和已知非语义元数据，不忽略 model、system、tools 或消息历史的实质差异。

对全部 117 个已关联 round 执行同一检查后，12 个不完整 round 都能找到紧邻的完整等价重试。这解释了为什么 Harbor 能完成任务，也说明失败响应本身没有作为有效 assistant 回合进入后续轨迹。

## 安全恢复导出

严格导出的失败是预期保护行为。如果需要导出客户端最终实际采用的轨迹，应使用恢复版并写入新的空目录：

```bash
python export_sharegpt_recovered.py \
  --input-dir /data1/nfs/ztr/a_log/20260816-test/dataset_output/successful \
  --output-dir /data1/nfs/ztr/a_log/20260816-test/sharegpt_output_recovered/successful \
  --reasoning-mode separate
```

然后检查：

```bash
jq '.error_count' \
  /data1/nfs/ztr/a_log/20260816-test/sharegpt_output_recovered/successful/export_errors.json

jq '.recovered_round_count, (.recoveries | group_by(.action) | map({action: .[0].action, count: length}))' \
  /data1/nfs/ztr/a_log/20260816-test/sharegpt_output_recovered/successful/recovery_report.json
```

恢复版只使用相邻完整等价重试或后续请求中保存的 assistant 历史作为证据，不会根据残缺 SSE 猜测丢失内容。它是数据导出的安全绕过方案，不是上游 SSE 问题的修复。

## 与代理退出的关系

本次 `finalization_report.json` 明确记录 `inflight=0`。所有 Harbor trial 也已经完成，错误 capture 后面存在成功重试。因此本次错误不是 Harbor 完成后按 `Ctrl+C` 停止代理造成的。

代理退出时等待连接属于独立的生命周期问题；强制停止可能在其他运行中额外留下 partial/inflight capture，但不能解释本次原始 SSE 中已经出现的“delta 先于 start”。

## 上游修复验收标准

上游修复后应满足：

1. 每个 `content_block_delta index=N` 之前都存在对应的 `content_block_start index=N`。
2. 每个正常流式响应都以 `message_delta` 和 `message_stop` 结束。
3. 不再出现上游提前关闭 chunked body 的 `RemoteProtocolError`。
4. 代理记录 `aggregation_complete=true`、`aggregation_errors=[]`。
5. 不再触发相同请求的紧邻非流式降级重试。
6. `export_sharegpt.py` 可以直接导出全部 agent，`export_errors.json` 为 0。
