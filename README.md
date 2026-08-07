# Anthropic Full Capture Proxy

透明转发 Anthropic Messages API，完整保存每次 HTTP 请求、非流式响应、原始 SSE、SSE 事件和聚合后的完整 Message。Harbor/Claude Code 运行结束后，再使用 session JSONL 按 `message.id` 整理成 `task / main agent / subagent / round` 数据集。

本项目不会修改请求 body、模型名称或认证方式。Harbor/Claude Code 发来的 `Authorization`、`X-Api-Key` 等认证头会原样转发给上游；采集文件中的密钥值会脱敏并只保存 SHA-256。

## 工作流程

```text
多个 Harbor / Claude Code
        │
        │ POST /v1/messages
        ▼
proxy.py
  ├─ 为每个请求生成独立 capture_id
  ├─ 原样转发请求和响应
  ├─ 保存原始 request/response
  └─ 流式响应同时保存 SSE 并聚合完整 Message
        │
        ▼
capture_logs/<run>/raw/{inflight,completed}
        │
        │ Harbor task 或整个 run 结束
        ▼
finalize.py
  ├─ 扫描主 session JSONL
  ├─ 扫描 subagents/agent-*.jsonl
  ├─ response.message.id == session message.id
  └─ 生成最终 task/agent/round 数据集
```

代理只负责保证每个 HTTP 请求和它自己的响应不串。task、主 agent、子 agent 的语义归属由 `finalize.py` 在运行结束后从 Harbor session 精确恢复，因此不需要修改带任务前缀的 LLM 地址。

## 安装

使用现有 Conda 环境 `unsw`：

```bash
conda activate unsw
cd anthropic_full_capture_proxy
python -m pip install -r requirements.txt
```

如果机器上没有该 Conda 环境，也可以使用独立 venv。

Linux：

```bash
cd anthropic_full_capture_proxy
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell：

```powershell
cd anthropic_full_capture_proxy
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 第一步：启动代理

```bash
python proxy.py \
  --listen-host 0.0.0.0 \
  --listen-port 30303 \
  --upstream-url http://36.138.156.38:35991 \
  --log-dir ./capture_logs/run_20260806_1 \
  --timeout-seconds 300
```

`--upstream-url` 是 base URL，不要包含 `/v1/messages`。代理会保留原请求 path 和 query string。

也可以使用环境变量：

```bash
export PROXY_LISTEN_HOST=0.0.0.0
export PROXY_LISTEN_PORT=30303
export UPSTREAM_URL=http://10.17.10.67:31542
export CAPTURE_LOG_DIR=./capture_logs/run_20260806
export UPSTREAM_TIMEOUT_SECONDS=300
python proxy.py
```

配置示例见 `config.example.env`。程序不会自动加载该文件，需要由 shell、Docker 或任务调度器注入。

健康检查：

```bash
curl http://10.0.0.1:30303/healthz
```

### 认证和模型配置

代理没有以下参数：

```text
--upstream-api-key
--upstream-auth-mode
```

原因是 Harbor/Claude Code 已经负责发送模型名和认证头。只需让 Harbor 启动 CC 时使用代理地址：

```bash
export ANTHROPIC_BASE_URL=http://代理IP:30303
```

Harbor 原有的 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_MODEL` 等配置保持不变。代理会把相关请求头和 body 原样转发。

### 多个 CC 并发

一个代理可以同时服务多个 CC。每个请求使用独立的：

- `capture_id`
- 原始数据目录
- response 文件
- SSE decoder
- Message aggregator

因此不同 CC 的网络 chunk 不会写到同一个文件。不要根据 client IP、请求时间或 prompt hash 推断 task；这些字段只用于审计。

## 第二步：运行 Harbor

正常启动 Harbor，不需要给 LLM URL 增加 task 前缀。建议每个 Harbor run 使用独立的代理 `--log-dir`，便于隔离运行边界。

运行中数据保存在：

```text
capture_logs/run_20260806/raw/
├── inflight/       # 尚未完成或代理中断的请求
└── completed/
    └── cap_<uuid>/
        ├── request.json
        ├── request.body
        ├── response.json
        ├── response.body
        ├── sse_events.jsonl
        └── state.json
```

`response.body` 是事实源：非流式时为原始 JSON，流式时为原始 SSE。`response.json.message` 是聚合后的完整 Message。

## 第三步：运行结束后整理数据集

等单个 task 或整个 Harbor run 完成并保存 CC session 后执行：

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

### `finalize.py` 如何寻找 session

传入目录时，脚本会从该目录开始递归搜索 `*.jsonl`，但只保留路径组件中包含
`projects` 或 `subagents` 的文件，以避开 Harbor 中其他用途的 JSONL。例如：

```text
<run>/tasks/task_a/logs/run/cc_session/.claude/projects/-workspace/session-1.jsonl
<run>/tasks/task_a/logs/run/cc_session/.claude/projects/-workspace/
  session-1/subagents/agent-sub1.jsonl
```

第一类通常是主 agent session；位于 `subagents/` 的第二类会被识别为子 agent。
如果直接把一个 JSONL 文件传给 `--harbor-run-dir`，脚本会直接读取该文件，
不会再应用 `projects/subagents` 路径过滤。

目录入口的解析方式如下：

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

因此，如果希望最终数据保留真实 task ID，建议至少传单个 task 目录，而不是单独
传 session 文件。

扫描每个 session JSONL 时，只索引满足以下条件的行：

- 该行是合法 JSON object
- `type` 等于 `assistant`
- `message` 是 object
- `message.id` 是非空字符串

主/子 agent 由 session 文件位置、`agentId`/`agent_id` 和
`isSidechain`/`is_sidechain` 联合判断。子 agent ID 优先取记录内的 `agentId`，
缺失时从 `subagents/agent-<id>.jsonl` 文件名提取。相邻时间、客户端 IP 和
prompt 内容都不参与关联。

### Harbor jobs 目录使用 `finalize-harbor.py`

如果 Harbor 输出没有 `tasks/` 中间层，而是以下结构：

```text
<job-root>/
├── <task_id>/
│   ├── agent/sessions/projects/**/*.jsonl
│   └── verifier/reward.txt
├── config.json
└── result.json
```

使用专用脚本：

```bash
python finalize-harbor.py \
  --capture-dir /data1/nfs/ztr/anthropic_full_capture_proxy/capture_logs/run_20260806_1 \
  --harbor-run-dir /data1/nfs/ztr/run/jobs/2026-08-06__16-10-23/ \
  --output-dir ./dataset_output/2026-08-06__16-10-23
```

job 根目录的每个直接子目录名会被完整用作 task ID，例如
`cve-2010-5312__oGPeD8H`。脚本只在每个 task 的
`agent/sessions/projects/` 下递归寻找主 session 和 subagent JSONL。

如果只需要 verifier 成功的轨迹，增加：

```bash
--only-successful
```

启用后，仅保留 `<task_id>/verifier/reward.txt` 去除首尾空白后等于 `1` 的 task；
内容为 `0`、文件缺失、无法读取或其他内容时均不转换该 task 的轨迹。

## 导出混合兼容 ShareGPT SFT 文件

`export_sharegpt.py` 接收 `finalize.py`、`finalize-harbor.py` 或
`finalize-node.py` 的最终 `--output-dir`，读取其中的 `tasks/<task_id>/<agent>/round_*`：

```bash
python export_sharegpt.py \
  --input-dir ./dataset_output/run_20260803 \
  --output-dir ./sharegpt_output/run_20260803 \
  --reasoning-mode separate
```

输出结构：

```text
sharegpt_output/run_20260803/
└── <task_id>/
    ├── main_agent_1.json
    ├── main_agent_2.json
    └── subagent_<agent_id>_1.json
```

同一个 agent 的相邻 round 在 model、实质 system、tools 和消息历史连续时合并；
billing cch、`cache_control` 和 thinking `signature` 的变化不会误触发切分。发生真正
的上下文压缩、历史替换或 system/tools/model 变化时，从该 round 开始生成下一个文件。

工具调用同时包含两套等价字段：

- Hugging Face/TRL 标准的 `type/function` 嵌套结构；
- `shili/sharegpt.json` 使用的扁平 `name/arguments` 兼容别名。

标准嵌套字段是事实源，程序在写出前检查两套字段完全一致。工具定义同样同时包含
标准 `function.name/description/parameters` 和扁平别名。

`--reasoning-mode` 支持：

- `separate`（默认）：thinking 写入 assistant 的 `reasoning_content`，最终文本写入
  `content`；适合明确读取该字段的 chat template。
- `inline`：thinking 写成 `<think>...</think>` 并拼到 `content` 前面，不再输出
  `reasoning_content`；适合使用 think token 的模型模板。

输出目录必须不存在或为空。遇到 partial 响应、缺少 `body.json`/`message`、无法
关联的 tool result、未知或非文本 content block 时会报错，不会静默生成有损 SFT 数据。

### Node/worker 目录使用 `finalize-node.py`

如果采集任务采用下面的 node/worker 布局：

```text
<node>/
└── worker*/
    ├── logs/
    │   └── <task_id>/logs/projects/**/*.jsonl
    └── result/
        └── <task-prefix>_<run-hash>.log
```

把单个 node 目录传给专用脚本：

```bash
python finalize-node.py \
  --capture-dir ./capture_logs/run_20260805 \
  --harbor-run-dir /path/to/node0 \
  --output-dir ./dataset_output/node0
```

例如任务目录 `arvo_10013-b4800b5cb2eb4314bff374befc95bd55` 对应 result 日志
`arvo_10013_b4800b5cb2eb4314bff374befc95bd55.log`。脚本与
`shili/print_result.py` 一样，从 result 文件名最后一个下划线处分隔并用连字符
还原任务目录名。

只转换正确轨迹时增加：

```bash
--only-successful
```

正误判定严格使用 `print_result.py` 的状态码逻辑：忽略 `vul_exit_code == 0`
的记录后取最后一次提交；漏洞版退出码不在 `{0, 71, 300}` 且修复版退出码为
`0` 时才是 `correct`。result 缺失、状态码缺失及其他分类都不会作为成功轨迹输出。

输出目录必须不存在或为空，防止旧轮次残留导致数据混合。

最终结构：

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

每个最终 `request.json` 和 `response.json` 都包含：

- task/session/agent/round 关联信息
- HTTP transport 元数据
- 解析后的 JSON
- 原始 UTF-8 body，非 UTF-8 时使用 Base64
- SHA-256
- 原始 capture 目录

## SSE 聚合规则

聚合器处理：

- `message_start`
- `content_block_start`
- `text_delta`
- `thinking_delta`
- `signature_delta`
- `input_json_delta`
- `citations_delta`
- `content_block_stop`
- `message_delta`
- `message_stop`

`input_json_delta.partial_json` 会先按 block index 拼接，等 block 结束后再解析，避免把中间的不完整 JSON 当成错误。

如果流中断或缺少 `message_stop`：

- 原始 SSE 仍完整保存到实际收到的位置
- 聚合结果标记 `aggregation_complete: false`
- `state.json` 标记 `partial`
- 后处理不会把它伪装成完整响应

## 关联规则和保证范围

主要关联键：

```text
代理捕获的 response.message.id
                  ==
Harbor/Claude Code session 中 assistant.message.id
```

同一个 `message.id` 在 session 中出现多条 assistant 记录时，会合并为一个模型 round。主/子 agent 由 session 文件路径、`agentId` 和 `isSidechain` 共同判断，其中 `subagents/agent-*.jsonl` 文件路径优先。

能够保证：

- 已捕获的每个请求与其 HTTP 响应不会串位
- 标准完整 SSE 可以聚合成完整 Message
- 有唯一 `message.id` 且 session 已保存时，可精确归到 task 和具体 agent
- 冲突不会覆盖，统一进入 `conflicts`

不能绝对保证：

- 在生成 `message.id` 前就失败的请求属于哪个 CC
- Harbor 崩溃且没有保存 session 时的 task/agent 归属
- 上游服务重复使用同一个 `message.id` 时的自动归属
- Anthropic 服务端没有通过 API 返回的隐藏信息

无法精确关联的数据只进入 `unmatched`，不会根据时间或 IP 自动猜测。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖：

- SSE 跨任意网络 chunk 聚合
- thinking、signature、tool input JSON 聚合
- 两个流交错写入时的文件隔离
- Harbor 主 agent 和子 agent 后处理
- 认证参数已移除、采集日志中密钥已脱敏

## 运维注意事项

- 每次 Harbor run 使用独立 `--log-dir`。
- 原始 `raw/` 是事实源，生成最终数据集后也不要删除。
- 定期检查 `finalization_report.json` 中的 `unmatched`、`conflicts` 和 `inflight`。
- system、messages、tool 参数和工具结果可能包含内部路径或敏感数据，应限制日志目录权限。
- 代理保存的是 HTTP 应用层可见的 header/body，不是 TLS 包、TCP 包或 HTTP/2 帧级抓包。
