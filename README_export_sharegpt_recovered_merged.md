# 合并压缩轨迹的 ShareGPT 导出器

`export_sharegpt_recovered_merged.py` 从 `export_sharegpt_recovered.py` 复制扩展。
它保持单组 `tasks/<task>/<agent>/round_*` 输入、ShareGPT 消息格式、reasoning
模式和工具格式选项，并保留原来的重试替换、后续请求历史恢复及结构校验。

```bash
python export_sharegpt_recovered_merged.py \
  --input-dir /path/to/dataset/successful \
  --output-dir /path/to/sharegpt/successful \
  --reasoning-mode separate

python export_sharegpt_recovered_merged.py \
  --input-dir /path/to/dataset/failed \
  --output-dir /path/to/sharegpt/failed \
  --task-group failed
```

输入目录下必须直接有 `tasks/`，输出目录必须不存在或为空。成功和失败两组分别运行。
输入目录改名后，可用 `--task-group successful` 或 `--task-group failed` 明确任务组。
默认 `auto` 根据输入目录名 `successful`/`failed` 或 `task.json.output_group`
识别；无法识别的任务在报告中标记 `unknown`，不执行失败分类。

## 压缩合并

同一 task 内，仅对 `main_agent` 和正常、恢复出的 `subagent_acompact-*` 配对：

- 两者必须有相同的非空 session ID；可从 round association 或 agent.json 获取。
- model 和规范化 system 相同；压缩请求保留 main 已有工具及其定义，可以新增工具。
- main 样本的完整消息序列，必须是压缩请求历史的完整规范化前缀。
- 有时间信息时，压缩不能早于被覆盖的 main；最长匹配必须唯一，每个 main 只合并一次。

成功配对后，使用压缩样本的完整对话和工具集合，保留 main 的 ID 与文件名，
省去独立压缩文件，历史 assistant 参与训练。原 main 和 compact 的来源写入
`optimization_report.json`。不同 system、模型、工具不兼容、历史不覆盖、session
不明或匹配多义时，仍分别输出；压缩历史默认保持 `masked_assistant`。

`--no-merge-compact-trajectories` 关闭合并；
`--no-mask-compact-assistant-history` 保留未合并压缩样本的原始角色。
后一个选项会恢复原有重复历史监督行为，适用于需要旧角色格式的下游。

## 输出上限处理

在完成原有恢复后，对 `stop_reason=max_tokens` 额外处理：

| 响应 | 处理 |
| --- | --- |
| 无正文、无工具调用的截断思考 | 删除该轮独立监督目标；此前有效历史保留 |
| 截断思考出现在后续真实请求历史中 | 保留历史内容，将对应 assistant 标为 masked_assistant |
| 有正文或合法工具调用，被后续请求完整回放 | 保留，按原有连续性规则与续写合并 |
| 有正文或工具调用，缺少后续接受证据 | 删除该轮监督目标，保留此前有效部分 |

接受证据限于同 agent 的下一条有效请求，或同 session 的压缩请求；model、system、
tools 和历史必须兼容。普通 `tool_use`、正常结束的思考不会仅因正文为空被删除。
截断历史的 mask 在压缩合并后应用，合并不会重新启用这些目标的 loss。

`masked_assistant` 需要下游训练模板正确屏蔽 loss。历史作为输入仍参与模型计算；
通过合并覆盖样本减少历史的重复输入。合并后的长度需与训练端长度上限匹配。

## 超限失败分类

已知属于 failed 组的任务，读取 main 的原始 CC session，检查最后一个主线 user/
assistant 事件。最终 API 错误为 `max_output_tokens`，或输入超长错误
`context_length_exceeded`/`Prompt is too long` 时，归入 `limit_exceeded/`。
中途超限后继续正常执行，不会仅因出现过超限就移动整个任务。

按本次约定，源 session 缺失、无法读取、损坏或没有可用主线事件时，也放入
`limit_exceeded/`，在报告中记录 `reason=session_unavailable`、`confirmed=false`。
这表示隔离无法核实的失败任务，而不是证明它因超限退出。

```text
<output>/
├── <普通任务>/<agent>_<segment>.json
├── sharegpt.jsonl                    # 仅普通任务
├── limit_exceeded/
│   ├── <超限或无法核实任务>/<agent>_<segment>.json
│   └── sharegpt.jsonl                # 仅此分类；存在该类任务时生成
├── export_errors.json
├── recovery_report.json
├── optimization_report.json
└── export_report.json
```

即使任务的全部响应都因截断被排除，仍保留任务分类目录和分类报告。
原有恢复报告格式保留；优化报告另外记录合并、未合并原因、截断处理、历史 mask、
输出来源和任务分类。`sharegpt_files` 是两组文件总数，两个 JSONL 的记录数分别报告。

此脚本未接入 online/v2；在线流程的入口、缓存和增量发布将在后续单独处理。
