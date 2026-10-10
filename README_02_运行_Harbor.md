# 02 运行 Harbor

[上一步：安装并启动代理](README_01_安装并启动代理.md) · [返回总览](README.md) · [下一步：整理最终数据集](README_03_整理最终数据集.md)

正常启动 Harbor，不需要给 LLM URL 增加 task 前缀。建议每个 Harbor run 使用独立的代理 `--log-dir`，便于隔离运行边界。

运行期间不要执行整理脚本。等单个 task 或整个 Harbor run 完成，并确认客户端原生会话已保存、`/healthz` 的 `capture.active = 0`，再进入下一步。
`capture.failures` 或 `capture.skipped` 非零时，检查是否有采集不完整；代理仍可服务不等于采集完整。

## 运行中的采集目录

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

`response.body` 保存实际收到的原始字节：推理接口非流式时通常是 JSON，流式时是
原始 SSE。`response.json.message` 保留协议对应的 Anthropic Message、OpenAI
`choices` 或 Responses `output`，是否完整由 state 与聚合状态判定。

运行结束后保留原始证据；可通过[自动归档](README_08_自动归档.md)压缩保存。
已开启归档原件清理时，恢复完整批次后再整理，在线 `raw/` 可能只有近期轮次。

## Kimi / Hermes 的会话归档

代理无法仅从 HTTP 推断 task/agent，运行结束后需要客户端原生会话一起整理：

- Kimi Code 2.x：实际 `KIMI_CODE_HOME` 下的 sessions/Wire journal；旧版为
  wire/context JSONL。将其归档到每个任务的日志目录。
- Hermes：实际 profile 的 `state.db`。仍在 WAL 模式运行时通过 SQLite backup
  取一致快照，或关闭后保留完整数据库/WAL；不要只复制尚未合并 WAL 的数据库。
- Harbor jobs/node/proc 的归档布局和 `--client` 参数见
  [整理数据](README_03_整理最终数据集.md#kimi--hermes-会话整理)。

本项目提供会话格式适配和整理入口，不替 Harbor 注册新的 agent 实现。
Kimi/Hermes 的独立客户端配置及复现测试见[接入文档](README_07_Kimi_Code与Hermes.md)。
