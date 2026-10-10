# 06 测试与运维

[上一步：采集与关联机制](README_05_采集与关联机制.md) · [返回总览](README.md)

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
- OpenAI Chat/Responses 流式聚合、分片工具参数、并发透明转发和断流判断
- Kimi Wire 主/子 agent、上下文关联及重复候选拒绝
- Hermes SQLite 只读关联、thinking/工具导出、跨后继会话压缩副本和一对一证据约束
- Kimi 原生 compact 请求指纹、摘要与完成边界，缺失/不匹配/多 capture 拒绝
- Hermes 原生摘要任务说明、合并用户消息、单空格占位及压缩历史 mask
- 新客户端在 Harbor、node、proc 布局中的成功任务筛选
- 采集初始化、请求/响应写盘、解析和收尾故障时仍保持正文、认证、状态码与重复响应头
- 真实 TCP 流式转发与健康检查不等待被阻塞的采集线程
- 采集积压与并发上限、关闭超时、短暂故障后的下一请求恢复采集
- 自动归档调度、去重、批量/年龄限制、校验/索引失败保护、清理重试、恢复后关联导出
- 归档慢盘/失败不阻塞转发，恢复拒绝不安全 tar 成员、损坏和重复 capture

## 真实客户端和服务验收

`tests/run_live_clients.py` 使用真实客户端和本地确定性上游；
`tests/run_vendor_acceptance.py` 显式接入指定真实模型服务，核对同一交易的字节，
或执行真实文件工具；`tests/run_native_compact.py` 驱动真实原生压缩；
`tests/verify_native_compact.py` 对保留证据离线整理、导出并验证内容和文件哈希。

这些 opt-in 脚本不属于普通 unittest，真实上游测试需要显式提供私有凭据，
不会默认消耗模型额度。版本、成功/失败范围和复现说明分别见
[验收说明](tests/ACCEPTANCE.md)和[接入文档](README_07_Kimi_Code与Hermes.md#复现验收)。

## 运维注意事项

- 每次 Harbor run 使用独立 `--log-dir`。
- 原始 `raw/` 是事实源；需要释放本地空间时使用[自动归档](README_08_自动归档.md)，校验后清理原件，保留归档包和客户端会话。
- 定期检查 `finalization_report.json` 中的 `unmatched`、`conflicts` 和 `inflight`。
- 采集在后台完成，整理前等待 `/healthz` 的 `capture.active = 0`，并核对 `failures` 与 `skipped`；两者非零时不能声称该次运行采集完整。
- 采集故障继续转发，尽力保留 `partial` / `inflight` 和 `capture_error`。磁盘完全不可写时查看进程日志及健康接口，不能要求故障标记一定落盘。
- system、messages、tool 参数和工具结果可能包含内部路径或敏感数据，应限制日志目录权限。
- 代理保存的是 HTTP 应用层可见的 header/body，不是 TLS 包、TCP 包或 HTTP/2 帧级抓包。
