# 04 导出 ShareGPT SFT 数据

[上一步：整理最终数据集](README_03_整理最终数据集.md) · [返回总览](README.md) · [下一篇：采集与关联机制](README_05_采集与关联机制.md)

`export_sharegpt.py` 接收任一整理脚本的最终 `--output-dir`，读取其中的 `tasks/<task_id>/<agent>/round_*`：

```bash
python export_sharegpt.py \
  --input-dir ./dataset_output/run_20260803 \
  --output-dir ./sharegpt_output/run_20260803 \
  --reasoning-mode separate \
  --standard-structure
```

输出目录必须不存在或为空。

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
