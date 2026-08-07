# 01 安装并启动代理

[返回总览](README.md) · [下一步：运行 Harbor](README_02_运行_Harbor.md)

## 1. 安装依赖

```bash
pip install -r requirements.txt
```

如果机器上没有 Conda 环境，也可以使用独立 venv。

Linux：

```bash
cd anthropic_full_capture_proxy
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows PowerShell：

```powershell
cd anthropic_full_capture_proxy
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2. 启动代理

```bash
python proxy.py \
  --listen-host 0.0.0.0 \
  --listen-port 30303 \
  --upstream-url http://36.138.156.38:35991 \
  --log-dir ./capture_logs/run_20260806_1 \
  --timeout-seconds 300
```

`--upstream-url` 是 base URL，不要包含 `/v1/messages`。代理会保留原请求 path 和 query string。

也可以使用环境变量：

```bash
export PROXY_LISTEN_HOST=0.0.0.0
export PROXY_LISTEN_PORT=30303
export UPSTREAM_URL=http://10.17.10.67:31542
export CAPTURE_LOG_DIR=./capture_logs/run_20260806
export UPSTREAM_TIMEOUT_SECONDS=300
python proxy.py
```

配置示例见 `config.example.env`。程序不会自动加载该文件，需要由 shell、Docker 或任务调度器注入。

## 3. 健康检查

```bash
curl http://10.0.0.1:30303/healthz
```

## 4. 配置 Harbor / Claude Code

代理没有以下参数：

```text
--upstream-api-key
--upstream-auth-mode
```

原因是 Harbor/Claude Code 已经负责发送模型名和认证头。只需让 Harbor 启动 Claude Code 时使用代理地址：

```bash
export ANTHROPIC_BASE_URL=http://代理IP:30303
```

Harbor 原有的 `ANTHROPIC_API_KEY`、`ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_MODEL` 等配置保持不变。代理会把相关请求头和 body 原样转发。

## 多个 Claude Code 并发

一个代理可以同时服务多个 Claude Code。每个请求使用独立的：

- `capture_id`
- 原始数据目录
- response 文件
- SSE decoder
- Message aggregator

因此不同客户端的网络 chunk 不会写到同一个文件。不要根据 client IP、请求时间或 prompt hash 推断 task；这些字段只用于审计。
