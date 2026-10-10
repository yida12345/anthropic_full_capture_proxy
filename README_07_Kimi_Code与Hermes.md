# 07 Kimi Code CLI 与 Hermes Agent

[返回总览](README.md)

代理现在识别三种推理 API：Anthropic Messages、OpenAI Chat Completions 和
OpenAI Responses。HTTP 请求 body、认证头和响应字节继续原样转发；协议转换只在
离线导出时进行。请求路径可以带网关前缀。

| 客户端 | 整理参数 | 会话来源 | 关联证据 |
| --- | --- | --- | --- |
| Claude Code | `--client cc`（默认） | `projects/**/*.jsonl` | assistant `message.id` |
| Kimi Code CLI | `--client kimi` | 新版 Wire journal，旧版 `wire.jsonl` / `context.jsonl` | 新版 `step.end.messageId` / message metadata；旧版 `StatusUpdate.message_id`，完整历史作为补充 |
| Hermes Agent（NousResearch） | `--client hermes` | `state.db` | 请求消息历史与 assistant 响应完整、唯一、一对一匹配 |

安装依赖仍使用 `pip install -r requirements.txt`，没有新增运行依赖。

## Kimi Code CLI

下面示例连接 Kimi Coding 的 OpenAI 兼容接口。代理的上游 URL **不包含末尾
`/v1`**，客户端 base URL **包含 `/v1`**，避免拼接出重复路径。

```bash
python proxy.py \
  --listen-host 127.0.0.1 \
  --listen-port 30303 \
  --upstream-url https://api.kimi.com/coding \
  --log-dir ./capture_logs/kimi_run
```

### Kimi Code 2.x（新版）

新版默认使用 `~/.kimi-code/config.toml`，数据根目录通过 `KIMI_CODE_HOME` 指定。
请在原 provider 的配置中将 `base_url` 改为代理，保留原有 type、密钥/OAuth 和模型。
以下是独立 OpenAI 兼容 provider 的例子（密钥为占位值）：

```toml
default_model = "capture-model"

[providers.capture]
type = "openai"
base_url = "http://127.0.0.1:30303/v1"
api_key = "your-existing-api-key"

[models.capture-model]
provider = "capture"
model = "your-existing-model"
max_context_size = 262144
capabilities = ["tool_use", "thinking"]
```

新版 provider type 为 `kimi` / `openai` / `openai_responses` / `anthropic` 等。
仅设置旧版的 `KIMI_BASE_URL` 不足以重定向新版配置的 provider。
本机 Kimi Code 2.0.0 实测不识别此处的 `api_key_env`，会在发出请求前报
`provider ... has no credential configured`。保留现有有效的认证配置，或使用该版本
支持的 `api_key`。独立验收中的临时配置权限为 0600，运行结束后移除密钥。

```bash
python finalize.py \
  --client kimi \
  --capture-dir ./capture_logs/kimi_run \
  --session-dir "$HOME/.kimi-code/sessions" \
  --output-dir ./dataset_output/kimi_run
```

新版 Wire protocol 1.5 的 `context.append_loop_event` 中 `step.end.messageId`，以及
`agent.message.appended` 中 `message.meta.messageId` 是可关联的模型响应 ID。
`context.append_message.message.id` 是客户端内部消息 ID，不能拿来关联响应。
新版记录的 `time` 是毫秒时间戳。

### Python kimi-cli（旧版）

在另一个终端运行，保留原有登录、API key 和模型配置：

```bash
export KIMI_BASE_URL=http://127.0.0.1:30303/v1
kimi
```

旧版 `KIMI_BASE_URL` 适用于 Kimi 的 `type = "kimi"` provider。如果使用
`openai_legacy` / `openai_responses`，改用 `OPENAI_BASE_URL`；Anthropic provider
应把其配置的 `base_url` 指向代理根地址，并把代理上游设为原 Anthropic base URL。
代理不把一种 API 请求转换成另一种 API。

运行结束后整理会话：

```bash
python finalize.py \
  --client kimi \
  --capture-dir ./capture_logs/kimi_run \
  --session-dir "$HOME/.kimi/sessions" \
  --output-dir ./dataset_output/kimi_run
```

支持传入 sessions 根目录、单个 session 目录或单个 JSONL。若安装版本把数据放在
`~/.kimi-code/sessions` 或自定义目录，传实际路径即可，不依赖固定 home 名称。
支持主会话直接保存的 wire/context，以及 `subagents/<id>/` 和 `agents/<id>/`
中的文件；`agents/main` 识别为主 agent。主 Wire 中携带持久 `agent_id` 的
`SubagentEvent` 与子 agent 文件中的镜像事件会去重。

`context.jsonl` 补充关联会保留工具调用、thinking 和全部请求消息历史进行比较；
已知的冻结 system prompt 必须一致。请求存在 `prompt_cache_key` 时还要求它与
session ID 一致。只依赖主 Wire 的旧子事件如果没有持久 agent ID，不会用
tool call ID 冒充 agent ID。

同一原生轮次只允许证明一个 HTTP 响应。Wire 已按响应 ID 确认的 capture 会通过严格
请求/答案匹配占用对应 context 轮次；另一条响应即使历史和文本相同，也会进入 conflicts，
不会因改用历史回退而成为额外轮次。其它未被占用的轮次仍可按唯一完整历史关联。

## Hermes Agent

以 OpenAI 兼容上游为例，在独立端口启动代理：

```bash
python proxy.py \
  --listen-host 127.0.0.1 \
  --listen-port 30304 \
  --upstream-url https://api.openai.com \
  --log-dir ./capture_logs/hermes_run
```

使用 Hermes 的 custom endpoint 配置。在实际使用的 `config.yaml` 中保留原模型
名称，把 base URL 指向代理；密钥仍由 Hermes 提供。例如：

```yaml
model:
  default: your-existing-model
  provider: custom
  base_url: http://127.0.0.1:30304/v1
  key_env: OPENAI_API_KEY
```

也可以运行 `hermes model`，选择 Custom endpoint 后输入代理地址、原模型和原密钥。
如果原上游不是 OpenAI，请把代理 `--upstream-url` 改为对应服务的根地址/网关前缀。
例如原 base URL 为 `https://openrouter.ai/api/v1`，上游参数应为
`https://openrouter.ai/api`。

运行结束后整理：

```bash
python finalize.py \
  --client hermes \
  --capture-dir ./capture_logs/hermes_run \
  --session-dir "$HOME/.hermes/state.db" \
  --output-dir ./dataset_output/hermes_run
```

可以传入单个数据库、Hermes home 或包含多个 `state.db` 的目录；命名 profile
应传入该 profile 实际使用的数据库。适配器以只读 SQLite 连接加载数据库，不执行
迁移。复制处于 WAL 模式且仍在运行的数据库时，应使用 SQLite backup 得到一致快照，
或在结束运行后复制完整数据库，不能只复制尚未合并 WAL 的 `state.db`。

Hermes 的 messages.id 是数据库行号，不能当作模型 API 响应 ID。关联必须满足：

- 捕获响应完整、聚合无错误。
- 请求中的全部非 system 消息与 session 中 assistant 前的连续历史一致。
- 响应的文本、thinking、工具 ID、名称和参数完整一致。
- 候选 session round 唯一，且同一个 round 不被多个 capture 共用。

`api_content` 优先用于恢复客户端实际发送的历史；结构化 content 的原生
`\0json:` 存储格式会解码。支持旧 schema 缺少可选字段的数据库。会话级
model/system 快照不用于否定切换模型之前的历史轮次，实际导出以捕获请求为准。

`source = subagent` 或 `model_config._delegate_from` 判定为子 agent。
`parent_session_id` 只负责会话 lineage，压缩产生的 continuation 不会被误判为
子 agent。压缩后同一 lineage 内同一个 `message_uid` 的多代副本按同一证据处理；
无法唯一归属的副本仍保留为冲突。

## Harbor / node / proc 布局

原有四个布局整理入口均支持 `--client kimi|hermes`，成功/失败判定规则保持原有含义：

```bash
python finalize-harbor.py --client kimi \
  --capture-dir ./capture_logs/run \
  --harbor-run-dir /path/to/job \
  --output-dir ./dataset_output/job

python finalize-node-split.py --client hermes \
  --capture-dir ./capture_logs/run \
  --harbor-run-dir /path/to/node0 \
  --output-dir ./dataset_output/node0
```

`finalize-node.py` 与 `finalize-proc-split.py` 同样支持 `--client`。
Kimi 的 context/wire 或 Hermes 的 state.db 必须保存到对应任务的日志位置：

- Harbor jobs：`<job>/<task_id>/agent/**`
- node/worker：`<node>/worker*/logs/<task_id>/logs/**`
- proc：`<proc>/<group>/<task_id>/logs/**`

这些入口从任务目录恢复 task ID。直接使用 `finalize.py` 整理客户端 home 时，
没有任务目录的 Kimi 默认以 session ID 为 task ID；Hermes 默认以 lineage 根 session
ID 为 task ID。整理器不会自行从 prompt 推断任务名或判定任务成功。

## 导出与保证范围

最终仍生成 `tasks/<task>/<agent>/round_*/request.json`、`response.json`，并保留
原始 HTTP body、原生响应结构和 SSE 事件。OpenAI 的 `response.json.message`
是原生 `choices` / `output` 结构，不会把修改后的数据伪装成原始响应。

标准导出入口可以直接使用：

```bash
python export_sharegpt.py \
  --input-dir ./dataset_output/kimi_run \
  --output-dir ./sharegpt_output/kimi_run \
  --reasoning-mode separate
```

`export_sharegpt_recovered.py` 同样支持三种协议，并输出合并的 `sharegpt.jsonl`。
工具调用与工具结果保持 ID 关联；thinking 支持 `separate` / `inline` 两种模式。
OpenAI 格式仅在内存中转为公共导出格式，不修改捕获文件。

Chat Completions SSE 要同时收到所有 choice 的 `finish_reason` 和 `[DONE]`；
Responses SSE 要收到有效的 `response.completed`。任何协议的解析/聚合错误都会使
采集标记 `partial`，严格导出不会仅凭停止事件放行。
客户端在已收齐有效协议结束标记后关闭 HTTP 连接，不会将完整捕获误标为残缺。
`client_disconnected`、`transport_error` 和 `disconnect_after_complete` 仍保留用于审计；
结束标记之前断开、或存在聚合错误的响应继续标记为 `partial`。

当前导出支持文本、可见 thinking/reasoning 和 function 工具调用。不支持的情况会
拒绝或保持未关联，不静默生成有损数据：

- Gemini 等其他协议尚未添加聚合与导出适配，代理仍会保存原始 HTTP。
- 多 choice completion、图片/音频等非文本训练内容和非 function 内置工具。
- 使用 `previous_response_id` 隐藏历史的 stateful Responses 请求。
- 只有加密 reasoning、没有可见内容的 Responses reasoning item。
- Hermes 辅助模型或瞬时子任务没有保存可验证的原生历史/边界。
- 日志缺失、历史已被不可逆替换、重复响应导致无法建立一对一关联。

这类数据保留在原始 capture 中；推理请求进入 `unmatched` 或 `conflicts`，
不再把 OpenAI 推理请求误分类为 `auxiliary`。最终报告包含 client、ID/历史关联统计、
不支持的 capture 数和多 capture 共用证据的歧义数。每条关联记录注明
`client` 与 `association_method`。

一个代理实例对应一个固定上游。不同客户端使用不同上游时，应各自启动一个代理实例。

## 原生 compact 与恢复导出

Kimi 普通响应优先按 Wire 响应 ID 关联。原生摘要没有普通 `step.end` ID 时，
要求专用压缩 prompt、摘要全文、模型、system/tools 指纹和已完成的原生压缩边界共同
匹配；失败/取消的压缩不产生已应用边界。重复候选和多个 capture 共用边界保留为冲突。

Hermes 原生压缩支持摘要包装、确定性的 Historical Task Snapshot 替换及摘要行
合并保留用户消息。摘要其它内容和保留用户消息必须精确匹配；未知改写保持 unmatched。
同一 message_uid 跨后继会话复制时保留原始逻辑轮次。

Hermes 会修剪保存的文本首尾空白，省略部分 provider 的历史 reasoning，或在
无 reasoning 时补单空格 echo 占位。这些已知投影只用于关联证据比较。
完整捕获是恢复导出的事实源；历史变换时切分样本，不用重放历史覆盖原始模型输出。

```bash
python export_sharegpt_recovered.py \
  --input-dir ./dataset_output/kimi_run \
  --output-dir ./sharegpt_output/kimi_run \
  --reasoning-mode separate
```

恢复版默认将压缩样本中最后一条之前的 assistant 改成 `masked_assistant`，最后
一条摘要保留 `assistant`。system/user/tool 保持原角色。Hermes 把历史序列化进 user
压缩提示时，没有独立的历史 assistant 需要改角色。训练管道需识别该角色并屏蔽
历史回复的 loss。可用 `--no-mask-compact-assistant-history` 关闭。

## 复现验收

自动化测试不需要模型凭据：

```bash
python -m unittest discover -s tests -v
```

真实客户端本地验收使用确定性 Chat Completions 上游，不调用付费模型：

```bash
python tests/run_live_clients.py --client kimi --case tool \
  --kimi-bin /path/to/actual-kimi-binary --proxy-python /path/to/python \
  --output-dir /path/to/empty-evidence-dir
```

Hermes 另传 `--hermes-python` 与 `--hermes-source`。脚本使用独立客户端 home，不会
自动安装客户端。请传入实际 Kimi 程序，避免 shell 包装器额外加载用户配置。

真实服务验收需显式指定上游、模型和权限为 0600 的凭据文件：

```bash
python tests/run_vendor_acceptance.py --phase wire \
  --upstream https://your-existing-service \
  --token-file /path/to/private-token-file --models your-existing-model \
  --output-dir /path/to/empty-evidence-dir

python tests/run_vendor_acceptance.py --phase clients --client kimi \
  --kimi-bin /path/to/actual-kimi-binary \
  --upstream https://your-existing-service \
  --token-file /path/to/private-token-file --models your-existing-model \
  --output-dir /path/to/empty-evidence-dir

python tests/run_native_compact.py --client kimi \
  --kimi-bin /path/to/actual-kimi-binary \
  --upstream https://your-existing-service \
  --token-file /path/to/private-token-file --model your-existing-model \
  --output-dir /path/to/empty-evidence-dir

python tests/verify_native_compact.py --client kimi \
  --root /path/to/compact-evidence-dir
```

离线验证会在证据目录生成 `verified_exports`；该子目录须不存在或为空，避免混入旧导出。

客户端/compact 验收同样支持 Hermes runtime 路径。Hermes compact 默认在原会话内，
`--hermes-rotate` 验证后继会话；`--hermes-auto --hermes-threshold N` 准备历史后
调整阈值，由下一轮原生 preflight 自动触发。Kimi 可用 `--kimi-context-size` 配置
测试上下文大小；自动压缩是否发生必须核对原生日志的 `source = auto`。

测试保留原生会话、HTTP capture 和导出数据；完成后应移除临时安装及凭据。
公开仓库不包含私有服务认证信息和原始验收会话。受测版本、结果及实测范围见
[验收说明](tests/ACCEPTANCE.md)。

## 格式参考

实现核对了 [Kimi Code 的会话位置说明](https://moonshotai.github.io/kimi-cli/en/configuration/data-locations.html)、
[Kimi 官方 Wire 定义](https://github.com/MoonshotAI/kimi-cli/blob/main/src/kimi_cli/wire/types.py)、
[Hermes 官方会话存储说明](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/session-storage.md)
和 [Hermes custom endpoint 配置](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/integrations/providers.md)。
另使用 Kimi 官方 Kosong Message 实际序列化与 Hermes 官方 SCHEMA_SQL 数据库完成兼容验证。
新版格式另依据 [Kimi Code 数据说明](https://moonshotai.github.io/kimi-code/en/configuration/data-locations.html)
及实际 2.0.0 Wire journal 验证。
