# 多后端采集代理

`proxy_lb.py` 保留原代理的原始 HTTP/SSE 采集、认证头透传及 path/query 透传，按请求选择一个后端。原 `proxy.py` 保持不变，依赖仍为 `requirements.txt`。

## 配置和启动

复制 `backends.example.txt` 为 `backends.txt`，替换为实际地址。每行只写一个 `IP:端口`，可以有空行，不要写 URL 协议、路径或 API key。IPv6 用 `[IP]:端口`。重复地址会报错，避免意外给某个后端增加权重。所有后端使用 HTTP，必须支持相同模型、认证头和请求路径。

```bash
python proxy_lb.py \
  --backends-file /data/ztr/anthropic_full_capture_proxy/backends.txt \
  --balance round-robin \
  --listen-host 0.0.0.0 \
  --listen-port 30304 \
  --log-dir ./capture_logs/example_run
```

`--balance random` 使用等概率随机选择，短期请求数不保证相同。`round-robin` 按配置顺序循环，各后端请求数最多相差一。每个请求只选择一次，整条 SSE 都来自同一后端；健康检查不消耗轮询位置。

也可设置 `UPSTREAM_BACKENDS_FILE`、`PROXY_BALANCE`；监听地址、端口、日志目录和超时环境变量与原代理相同。配置在启动时读取，修改后需要重启。保持单 worker 运行。

```bash
curl http://127.0.0.1:30304/healthz
```

返回 `upstream_urls`、`balance` 和 `capture_root`。这是代理存活检查，不会探测后端。每个采集目录的 `request.json` 中，`upstream_url` 是本次实际选中后端的完整请求 URL。

本版不自动重试、剔除故障节点或限制后端并发。连接失败沿用原代理的 502 行为，上游 HTTP 错误原样返回。轮询/随机使每个 API 平等，不保证实际 token 量或生成耗时相等。

## 对接 ExploitGym run_all.sh

客户端仍使用原采集代理地址，不需要修改 Agent。`run_all.sh` 调用的脚本需适配新启动参数及健康检查；本次只新增本仓库文件，没有修改 ExploitGym 脚本。

1. `00_config.sh` 增加并导出 `CAPTURE_BACKENDS_FILE`（绝对路径）和 `CAPTURE_BALANCE`（默认 `round-robin`）。
2. `03_start_proxy.sh` 的启动入口及 PID 命令校验从 `proxy.py` 改为 `proxy_lb.py`；把 `--upstream-url` 替换为 `--backends-file "${CAPTURE_BACKENDS_FILE}" --balance "${CAPTURE_BALANCE}"`，其余参数保留。
3. `03_start_proxy.sh` 和 `05_start_evaluation.sh` 的健康检查保留 `capture_root` 校验，将单个 `upstream_url` 比较改为比较配置文件解析得到的完整、有序 `upstream_urls` 及 `balance`。可以用 `proxy_lb.load_backends(Path(...))` 得到规范化地址列表。避免复用配置不同的旧进程。
4. `CAPTURE_ROUTE_PREFIX` 会原样转发，例如 `/glm53/v1/messages`，列表中的每个后端都需支持该路由。
