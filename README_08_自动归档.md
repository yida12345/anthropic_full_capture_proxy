# 08 自动归档与恢复

[返回总览](README.md) · [安装并启动代理](README_01_安装并启动代理.md)

自动归档由代理进程内的后台任务执行，不需要额外部署定时服务或安装新依赖。
配置归档目录即可开启；启动时检查一次，此后每次检查完成后等待指定间隔再执行。
压缩、校验和索引操作在线程中运行，归档失败会在 `/healthz` 报告，HTTP 转发继续。

## 配置

下面示例每小时检查一次，归档已经收尾至少 24 小时的 capture，每次最多处理 1000 条。
归档目录可以是本地磁盘或已经挂载的归档盘。每个 run 使用独立子目录便于恢复。

```bash
python proxy.py \
  --listen-host 127.0.0.1 --listen-port 30303 \
  --upstream-url https://your-existing-service \
  --log-dir ./capture_logs/run_001 \
  --archive-dir /mnt/trajectory_archive/run_001 \
  --archive-interval-seconds 3600 \
  --archive-min-age-seconds 86400 \
  --archive-batch-size 1000 \
  --archive-delete-source
```

| 命令行参数 | 环境变量 | 默认值 / 含义 |
| --- | --- | --- |
| `--archive-dir` | `CAPTURE_ARCHIVE_DIR` | 未配置即关闭；归档目录不能位于该 run 的 `raw/` 内 |
| `--archive-interval-seconds` | `CAPTURE_ARCHIVE_INTERVAL_SECONDS` | `3600`；检查间隔，必须大于零 |
| `--archive-min-age-seconds` | `CAPTURE_ARCHIVE_MIN_AGE_SECONDS` | `3600`；收尾后至少保留多久才允许归档，非负 |
| `--archive-batch-size` | `CAPTURE_ARCHIVE_BATCH_SIZE` | `100`；每次最多归档或清理多少条，必须大于零 |
| `--archive-delete-source` | `CAPTURE_ARCHIVE_DELETE_SOURCE` | `false`；归档校验并提交索引后清理本地原件 |

`--no-archive-delete-source` 可以覆盖环境变量，强制保留原件。未开启清理时是压缩备份，
不会释放本地原始日志占用。开启清理后，旧 capture 会移出在线目录；要处理完整历史，
需按下面步骤恢复后再整理。最小年龄应覆盖计划中的在线整理时间。

每次处理条数受批量限制，积压会在后续检查继续处理；吞吐不够时增大批量或缩短间隔。
每包清单最多 4 MiB，较大批次会自动分成多个包。单条清单超限会保留原件并报告故障，
继续处理其它记录。只做备份时，已登记记录只查询包路径，不逐条读取整包清单。
这里配置的是执行间隔，不是每天某一时刻的 cron 日历。归档包没有自动过期删除策略。

环境配置例子见 [config.example.env](config.example.env)。程序不会自动读取 `.env`，
需要 shell 或部署工具注入。路径在代理运行的机器上解释；对象存储需要先挂载或另行同步。

## 归档内容与完整性

只处理 `raw/completed/<capture_id>/` 中状态为 `complete` 或 `partial` 的采集，
且跳过当前后台采集仍在使用的 ID。`partial` 保留原状态，归档不会将其变成完整数据。
`raw/inflight/`、客户端会话、整理结果和 ShareGPT 输出不由本任务清理。

```text
/mnt/trajectory_archive/run_001/
  ├─ index.sqlite3
  └─ YYYY-MM-DD/                    # UTC 日期
       └─ captures-<unique_id>.tar.gz
            ├─ manifest.json
            └─ completed/<capture_id>/
                 ├─ request.body
                 ├─ request.json
                 ├─ response.body
                 ├─ response.json
                 ├─ sse_events.jsonl # 流式时存在
                 └─ state.json
```

归档清单记录来源目录、时间、每个文件的长度和 SHA-256。流程为：写临时压缩包、
逐文件解压读回校验、文件及目录落盘、原子发布、提交 SQLite 索引，然后按配置清理。
删除前还会核对源文件与清单一致，再在同一文件系统内原子移到 `raw/.archive-cleanup/`
清理区。删除中断后，下一轮重新校验归档和残余文件，无需依赖已删除的 `state.json`。
旧版留在 `completed/` 的半删目录也可继续清理；只有残余文件完全属于已校验清单且哈希一致
才允许续删。新增文件、文件内容变化、缺少索引或没有可完整校验的归档时保留残余并报警。

单条状态损坏、文件读取或清理失败会单独报告，继续处理其它 capture。旧包损坏或丢失时，
只有完整原件与持久化清单一致才重新归档；残缺原件不据此删除。损坏旧包保留供排查，
恢复时需剔除已确认损坏的包并选择经校验的新包；工具不会忽略损坏包。

SQLite 索引用于重启后避免重复归档、重试已归档但未清理的 capture。保留索引；丢失索引
或更改源目录绝对路径可能重复生成归档，但恢复只依赖压缩包和包内清单。中断时可能留下
已发布但尚未登记的包或临时文件，可在确认没有任务运行后核对清理。
重试产生的完全相同 capture 可以跨包去重恢复，真实内容冲突仍拒绝。

归档目录不可用、压缩包校验失败或归档任务关闭超时时，`/healthz.archive.status` 为
`degraded`，`failures`、`last_failure` 给出累计故障数量、阶段、异常类型，以及单条故障的
capture ID，不输出正文、异常消息或密钥。故障修复后累计记录仍保留到进程重启。
同时提供 `enabled`、`running`、目录、调度参数、`runs`、`archived_captures`、
`removed_captures`、`archive_bytes` 和 `last_run`。这些运行计数在重启后重新累计；
`archive_bytes` 仅计压缩包，不包含索引体积。顶层代理 `status = ok` 不代表归档成功。

单 worker 启动代理；不要让多个代理/整理器同时修改同一个 capture 根目录。
归档操作增加压缩 CPU、磁盘读写和临时包空间，压缩率取决于实际内容，不保证固定比例。
源日志、归档包和客户端会话均可能包含敏感正文，按运行环境限制目录访问权限。

## 恢复与重新导出

使用恢复工具逐文件校验后恢复到一个不存在或为空的 **raw 根目录**：

```bash
python capture_archive.py \
  --archives /mnt/trajectory_archive/run_001/2026-10-10/*.tar.gz \
  --output-dir ./restored/run_001/raw
```

多个日期的包可一起传给 `--archives`；按 run 选齐需要的批次。工具拒绝路径越界、
链接、包内重复/缺失成员和哈希不符，且不覆盖已有输出。跨包同名 capture 的完整文件集合、
长度及哈希全部一致时只恢复一份；内容或成员集合冲突则拒绝。重复包也逐文件校验。
若原在线目录还有尚未归档的轮次，应先停止代理，再把这些 `completed/` 轮次复制到
恢复目录中。同名轮次需核对哈希一致，不能直接覆盖。不要在自动清理运行时从在线目录
整理完整数据，否则可能漏掉已归档的早期轮次。

客户端原生会话仍须单独保存：CC 的 projects JSONL、Kimi 的 sessions/Wire、Hermes 的
一致 `state.db` 快照。HTTP 归档无法替代这些关联证据。以 Kimi 为例：

```bash
python finalize.py --client kimi \
  --capture-dir ./restored/run_001 \
  --session-dir /path/to/retained-kimi-sessions \
  --output-dir ./dataset_output/run_001

python export_sharegpt_recovered.py \
  --input-dir ./dataset_output/run_001 \
  --output-dir ./sharegpt_output/run_001 \
  --reasoning-mode separate
```

归档不改变 body、SSE、关联信息或 compact mask；这些由恢复后的原始文件和原有
整理/导出流程决定。归档包中的来源路径可能指向旧机器，实际恢复使用包内相对路径。

## 验证

自动化用例覆盖默认关闭、定时执行、年龄/批量限制、清单按字节分包、重启后去重、
清理中断与旧版半删目录恢复、单条故障隔离、损坏/源文件变化/索引失败保护、
活动采集隔离、重复包恢复及冲突拒绝、不安全成员校验和慢盘/故障不阻塞转发。
另包含恢复后重新关联 Kimi 会话并导出的完整链路。

对已验收的五组真实 Kimi/Hermes compact 证据副本归档、清理、恢复后重新导出，
33 条样本逐条 JSON 一致，全部原始文件哈希一致；既有测试证据未修改。
全部测试及实测范围见 [验收说明](tests/ACCEPTANCE.md)。
