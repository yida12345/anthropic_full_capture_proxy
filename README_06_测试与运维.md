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

## 运维注意事项

- 每次 Harbor run 使用独立 `--log-dir`。
- 原始 `raw/` 是事实源，生成最终数据集后也不要删除。
- 定期检查 `finalization_report.json` 中的 `unmatched`、`conflicts` 和 `inflight`。
- system、messages、tool 参数和工具结果可能包含内部路径或敏感数据，应限制日志目录权限。
- 代理保存的是 HTTP 应用层可见的 header/body，不是 TLS 包、TCP 包或 HTTP/2 帧级抓包。
