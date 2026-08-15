# 04 导出 ShareGPT SFT 数据

[上一步：整理最终数据集](README_03_整理最终数据集.md) · [返回总览](README.md) · [下一篇：采集与关联机制](README_05_采集与关联机制.md)

`export_sharegpt.py` 接收任一整理脚本的最终 `--output-dir`，读取其中的 `tasks/<task_id>/<agent>/round_*`：

```bash
python export_sharegpt.py \
  --input-dir ./dataset_output/2026-08-06__16-10-23 \
  --output-dir ./sharegpt_output/2026-08-06__16-10-23-shili-jincou \
  --reasoning-mode separate \
  --no-standard-structure
```

输出目录必须不存在或为空。

默认优先使用 `export_sharegpt.py`。它是严格导出器：只接受自身已经完整、结构合法的响应，适合正常采集的数据。

如果上游流式 SSE 存在缺少 `content_block_start`、中途断流、空 content block 或幽灵 `tool_use` 等聚合异常，但客户端随后成功重试，或者后续请求历史保存了客户端实际接受的 assistant 消息，可以改用 `export_sharegpt_recovered.py`：

```bash
python export_sharegpt_recovered.py \
  --input-dir ./dataset_output/2026-08-06__16-10-23 \
  --output-dir ./sharegpt_output/2026-08-06__16-10-23-recovered \
  --reasoning-mode separate
```

恢复版复用严格导出器的 ShareGPT 格式、上下文切分、reasoning 模式和工具调用校验，并额外生成 `recovery_report.json`。该报告逐条记录 task、agent、被处理的 round、恢复动作以及作为证据的下一 round；无法安全恢复的不完整响应仍按严格导出器的方式写入 `export_errors.json`，其他结构错误仍会直接终止导出。

## 恢复版的证据和边界

恢复版不是根据残缺 SSE 猜测缺失内容，只执行以下有事实证据支持的操作：

- 相邻两个请求除 `stream` 和已知非语义元数据外完全相同，且后一个响应完整、可转换时，将前一个视为失败重试并丢弃，保留后一个成功响应。
- 下一请求在相同 model、实质 system 和 tools 配置下完整保留了当前请求的消息前缀，并紧接着保存了 assistant 消息时，以这份客户端实际接受的 assistant 历史替换错误的流式聚合结果。
- 响应已经收到 `message_stop`、`aggregation_complete=true`、`stop_reason=end_turn`，且包含非空正文时，可以删除与 `end_turn` 明确矛盾的空块或幽灵 `tool_use`，只保留合法的 text/thinking 块。

以下情况不会恢复，仍按错误处理：

- 残缺响应之后没有相邻成功重试，也没有后续请求历史可以证明其真实内容。
- 相邻请求的模型、system、tools 或已有消息历史发生实质变化，无法证明它们属于同一次重试或同一上下文。
- 后续重试本身仍不完整，或者证据中的 assistant content 不能通过严格导出器的结构校验。
- `tool_use` 结束、没有非空正文或没有收到完整停止事件的末轮；恢复版不会自行补造工具名、参数、文本或停止原因。

因此，恢复版恢复的是客户端最终接受并继续执行的有效轨迹，不保证还原已断流响应中从未到达代理或从未进入后续请求历史的原始内容。

每个 ShareGPT 文件使用 UTF-8 紧凑单行 JSON，与 `shili/sharegpt.json` 一致；
文件末尾保留一个换行符。缩进和换行只影响文件展示，不改变 JSON 数据结构。
修改位置在export_sharegpt的652 compact=True/False

## 输出结构

```text
sharegpt_output/run_20260803/
└── <task_id>/
    ├── main_agent_1.json
    ├── main_agent_2.json
    └── subagent_<agent_id>_1.json
```

同一个 agent 的相邻 round 在 model、实质 system、tools 和消息历史连续时合并；billing cch、`cache_control` 和 thinking `signature` 的变化不会误触发切分。发生真正的上下文压缩、历史替换或 system/tools/model 变化时，从该 round 开始生成下一个文件。

## 工具调用兼容格式

工具调用同时包含两套等价字段：

- Hugging Face/TRL 标准的 `type/function` 嵌套结构
- `shili/sharegpt.json` 使用的扁平 `name/arguments` 兼容别名

标准嵌套字段是事实源，程序在写出前检查两套字段完全一致。工具定义同样同时包含标准 `function.name/description/parameters` 和扁平别名。

标准结构默认开启，也可以明确传入 `--standard-structure`。如果下游只接受
`shili/sharegpt.json` 的示例扁平格式，使用：

```bash
--no-standard-structure
```

关闭后，`tool_calls` 只包含扁平 `name/arguments`，tools 只包含扁平
`name/description/parameters`，tool 消息只包含 `role/content`。Reasoning 格式仍由
独立的 `--reasoning-mode` 控制。

## reasoning 模式

- `separate`（默认）：thinking 写入 assistant 的 `reasoning_content`，最终文本写入 `content`；适合明确读取该字段的 chat template。
- `inline`：thinking 写成 `<think>...</think>` 并拼到 `content` 前面，不再输出 `reasoning_content`；适合使用 think token 的模型模板。

遇到 partial 响应、缺少 `body.json`/`message`、无法关联的 tool result、未知或非文本 content block 时会报错，不会静默生成有损 SFT 数据。
