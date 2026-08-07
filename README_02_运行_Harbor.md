# 02 运行 Harbor

[上一步：安装并启动代理](README_01_安装并启动代理.md) · [返回总览](README.md) · [下一步：整理最终数据集](README_03_整理最终数据集.md)

正常启动 Harbor，不需要给 LLM URL 增加 task 前缀。建议每个 Harbor run 使用独立的代理 `--log-dir`，便于隔离运行边界。

运行期间不要执行整理脚本。等单个 task 或整个 Harbor run 完成，并确认 Claude Code session 已保存后，再进入下一步。

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

`response.body` 是事实源：非流式时为原始 JSON，流式时为原始 SSE。`response.json.message` 是聚合后的完整 Message。

运行结束后不要删除 `raw/`；整理后的数据集可以从这些原始记录重新生成。
