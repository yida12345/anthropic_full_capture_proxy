# 完整缓存与自动重试代理

`proxy_retry.py` 是独立入口，复用原有采集和 HTTP 工具。`proxy.py`、
`proxy_lb.py`、`capture_core.py` 保持原样。

代理收到请求后，完整接收上游响应，落盘并校验，再向客户端发送响应头和
原始响应字节。成功的 Anthropic Messages SSE 必须收到 `message_start` 和
`message_stop`，且不能有 SSE 错误事件、聚合错误或压缩解码错误。回放按
64 KiB 分块读取文件，不模拟原来的生成速度，也不改写文字或工具调用。

连接、读写、协议错误、单次尝试超时、不完整 SSE，以及 HTTP
408/429/500/502/503/504/529 会触发重试。永久 HTTP 错误（如 400/401/403）和
永久 SSE API 错误不会在 proxy 内重试。仅 Messages、count_tokens 和
GET/HEAD/OPTIONS 支持重试，其他写入端点保留单次尝试。

默认首次发送后最多重试 20 次，采用随机指数退避，退避上限 30 秒；上游
`Retry-After` 可要求更长等待。连接超时 30 秒，写入/连接池等待 300 秒，
读取空闲超时 600 秒，单次尝试最多 3600 秒，整个请求含所有重试最多
86400 秒。收到客户端断开或请求被取消后停止上游操作及重试。

## 单独启动

```bash
/root/miniconda3/envs/arvo/bin/python proxy_retry.py \
  --listen-host 0.0.0.0 \
  --listen-port 30304 \
  --upstream-url http://8.130.156.14:8000 \
  --log-dir ./capture_logs/retry_run \
  --max-retries 20 \
  --request-timeout-seconds 86400
```

依赖仍使用 `requirements.txt`。环境变量示例见 `config.retry.example.env`，
程序不会自动加载 env 文件。

## ExploitGym 联动

`/data/exploitgym/scripts/local_full_image/00_config.sh` 统一设置：

| 配置 | 默认值 |
| --- | --- |
| `CAPTURE_PROXY_ENTRYPOINT` | `${CAPTURE_PROXY_DIR}/proxy_retry.py` |
| `CAPTURE_MAX_RETRIES` | 20（首次之外的重试次数） |
| `CAPTURE_CONNECT_TIMEOUT_SECONDS` | 30 |
| `CAPTURE_TIMEOUT_SECONDS` | 300（写入/连接池等待） |
| `CAPTURE_READ_TIMEOUT_SECONDS` | 600（读取空闲，不是响应总时长） |
| `CAPTURE_ATTEMPT_TIMEOUT_SECONDS` | 3600 |
| `CAPTURE_REQUEST_TIMEOUT_SECONDS` | 86400 |
| `CAPTURE_RETRY_DELAY_SECONDS` | 1 |
| `CAPTURE_RETRY_MAX_DELAY_SECONDS` | 30 |
| `CAPTURE_SMOKE_TIMEOUT_SECONDS` | 请求总预算 + 300 |
| `AGENT_API_TIMEOUT_MS` | 90000000（25 小时） |
| `AGENT_API_MAX_RETRIES` | 3（agent 层兜底） |
| `FIREWALL_LLM_TIMEOUT_SECONDS` | agent API 超时秒数 + 300 |
| `AGENT_TIMEOUT_SECONDS` | 604800（7 天） |
| `ATTEMPT_TIMEOUT_SECONDS` | agent 总预算 + 1800 |

Claude Code 容器环境通过 runner 读取 `AGENT_API_*`。默认关闭独立的
stream/byte watchdog 和 body idle timeout，由 proxy 管理有界超时。
对应开关是 `AGENT_API_STREAM_WATCHDOG`、`AGENT_API_BYTE_WATCHDOG` 和
`AGENT_API_FORCE_IDLE_TIMEOUT`，均可配置为 `0` 或 `1`。

`03_start_proxy.sh` 和 `05_start_evaluation.sh` 会核对 `/healthz` 的
`buffer_response` 及完整 `retry_policy`，避免复用旧程序或不同重试配置。
修改代码或配置不会热更新运行中的 proxy/agent，需在下一轮启动或受控重启时
生效；这里不自动终止已有评测任务。Docker 超时配置保持原值。
运行阶段 Squid 的 read/forward/client lifetime 超时也使用
`FIREWALL_LLM_TIMEOUT_SECONDS`，并在 reconfigure 前通过 `squid -k parse`
检查配置。访问控制规则保持原样。

## 采集

最终选中的响应仍在 `raw/completed/<capture-id>/`，保持原始 body、认证头
脱敏、重复 header、path/query 和消息聚合格式。网络分块边界可以改变，
SSE 字节、事件顺序及内容保持原样。回放被中断时会记录客户端断开。

失败尝试保存在 `raw/retry_attempts/<proxy-request-id>/<capture-id>/`，
不会进入默认轨迹导出扫描的 `raw/completed`。每次尝试的 `request.json`
都有 `proxy_request_id` 和 `proxy_attempt`，方便关联审计。全部失败时，正常
采集目录保存最终返回客户端的 proxy 错误；重试耗尽的 HTTP 错误保留上游
原状态和 body。

读取或写入失败时，上游可能已经开始推理，重试可能产生额外计算费用。
客户端需等待完整生成和重试结束后才看到输出、执行工具。

## 验证

```bash
/root/miniconda3/envs/arvo/bin/python -m unittest discover -s tests -v
```

重试测试使用本地模拟上游，覆盖断流、不完整响应、SSE 错误、压缩响应、
超时、并发、重试耗尽，以及发送响应头之前的客户端取消。
