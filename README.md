# Anthropic Full Capture Proxy

透明转发 Anthropic Messages API，完整保存每次 HTTP 请求、非流式响应、原始 SSE、SSE 事件和聚合后的完整 Message。Harbor/Claude Code 运行结束后，再使用 session JSONL 按 `message.id` 整理成 `task / main agent / subagent / round` 数据集。

本项目不会修改请求 body、模型名称或认证方式。Harbor/Claude Code 发来的 `Authorization`、`X-Api-Key` 等认证头会原样转发给上游；采集文件中的密钥值会脱敏并只保存 SHA-256。

## 按运行顺序阅读

1. [安装并启动代理](README_01_安装并启动代理.md)
2. [运行 Harbor](README_02_运行_Harbor.md)
3. [整理最终数据集](README_03_整理最终数据集.md)
4. [导出 ShareGPT SFT 数据](README_04_导出_ShareGPT.md)
5. [采集与关联机制](README_05_采集与关联机制.md)（参考）
6. [测试与运维](README_06_测试与运维.md)（参考）

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
finalize.py / finalize-harbor.py / finalize-node.py
  ├─ 扫描主 session JSONL
  ├─ 扫描 subagents/agent-*.jsonl
  ├─ response.message.id == session message.id
  └─ 生成最终 task/agent/round 数据集
        │
        │ 可选
        ▼
export_sharegpt.py
  └─ 生成混合兼容 ShareGPT SFT 文件
```

代理只负责保证每个 HTTP 请求和它自己的响应不串。task、主 agent、子 agent 的语义归属由整理脚本在运行结束后从 Harbor session 精确恢复，因此不需要修改带任务前缀的 LLM 地址。
