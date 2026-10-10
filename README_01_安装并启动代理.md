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
  --upstream-url https://llmapi.datatang.com \
  --log-dir /data/ztr/a_log/830test/capture_logs \
  --timeout-seconds 300
```

`--upstream-url` 是固定上游的根地址/网关前缀，不包含推理 endpoint。代理保留客户端
请求的 path 和 query。例如 OpenAI 客户端 base URL 为 `http://127.0.0.1:30303/v1`，
上游设为 `https://api.openai.com`；原 base URL 为 `https://gateway.example/api/v1` 时，
上游设为 `https://gateway.example/api`，避免重复 `/v1`。Anthropic 仍按原配置使用。

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

需要长期采集时可加 `--archive-dir`，并配置 `--archive-interval-seconds`、
`--archive-min-age-seconds` 和 `--archive-batch-size`。默认只备份；使用
`--archive-delete-source` 才会在归档校验后清理原件。配置及恢复见[自动归档](README_08_自动归档.md)。

## 3. 健康检查

```bash
curl http://0.0.0.0:30303/healthz
```

返回值中的 `capture` 独立报告采集状态。`status = degraded` 表示出现过采集失败或
超限跳过；`failures`、`skipped`、`last_failure` 给出数量和故障阶段，不包含认证或正文。
顶层 `status = ok` 表示代理仍可服务，不代表所有请求都采集完整。

写盘与解析在后台执行。默认每条采集的积压上限为 8 MiB，最多保留 64 个后台采集
任务，另外限制单条采集最多积压 1024 个操作。达到上限时继续转发模型响应，停止
受影响的采集并报告故障，避免无限占用内存。可通过 `--capture-buffer-bytes` /
`CAPTURE_BUFFER_BYTES` 与 `--capture-max-pending` / `CAPTURE_MAX_PENDING` 调整字节和任务上限。

客户端结束后，先确认 `capture.active = 0` 再整理数据；同时核对失败和跳过计数。
正常停止代理会等待最多 5 秒让后台采集收尾。超时会报告 `shutdown_timeout`，未完成
数据不应作为完整轨迹使用；无法写盘时只能在健康状态和进程日志中报告故障。

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

## 5. 配置 Kimi / Hermes

客户端保留原 provider、模型和认证配置，只把 provider base URL 指向代理。
新版 Kimi Code 修改 `config.toml` 中 provider 的 `base_url`；Hermes 修改 custom
provider 的 base URL。原生安装和会话目录的具体例子见
[Kimi Code 与 Hermes 接入](README_07_Kimi_Code与Hermes.md)。

不同客户端需要访问不同上游时各自启动代理实例。代理只透明转发，不在运行时转换
Anthropic / OpenAI 协议，也不负责客户端安装、模型选择或登录。

## 多个客户端并发

一个固定上游的代理可以同时服务多个 Claude Code、Kimi Code 或 Hermes 客户端。每个请求使用独立的：

- `capture_id`
- 原始数据目录
- response 文件
- SSE decoder
- Message aggregator

因此不同客户端的网络 chunk 不会写到同一个文件。不要根据 client IP、请求时间或 prompt hash 推断 task；这些字段只用于审计。
