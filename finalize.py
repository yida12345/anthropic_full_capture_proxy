from __future__ import annotations

"""把代理的 HTTP 采集结果与 Harbor/Claude Code session 日志关联起来。

这个脚本是离线后处理器，不参与在线请求转发。它有两个输入：

1. ``capture_root``：proxy.py 的 ``--log-dir``，或该目录下面的 ``raw`` 目录；
2. ``harbor_root``：Harbor run 根目录、tasks 目录、单个 task 目录，或一个 session JSONL。

关联时不使用请求时间、客户端 IP、prompt hash 等模糊信息。唯一的主要关联键是：

    capture 的 response.json.message_id == session 中 assistant.message.id

这样即使多个 Claude Code/agent 并发请求，也不会靠时间邻近关系误配。
"""

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from capture_core import SCHEMA_VERSION, body_representation, utc_timestamp, write_json


# request.json 完整支持的顶层字段只有下面 6 个。此列表同时是实际输出白名单：
# 删除某项，该字段就不输出；调整顺序会改变输出顺序；添加未知项或重复项会报错。
FINAL_REQUEST_PARTS = [
    "schema_version",  # 数据格式版本
    "capture_id",  # 代理为本次 HTTP 请求生成的唯一采集 ID
    "association",  # task、session、主/子 agent、round 的关联信息
    "transport",  # 请求方法、URL、header、时间、客户端等 HTTP 元数据
    "body",  # 请求原始 body 的 JSON/UTF-8/Base64 表示、大小和 SHA-256
    "provenance",  # 本记录对应的原始 capture 目录和 body 文件
]

# response.json 完整支持的顶层字段只有下面 9 个。此列表同时是实际输出白名单：
# 删除某项，该字段就不输出；调整顺序会改变输出顺序；添加未知项或重复项会报错。
# SSE 原文在 body，解析事件和聚合 Message 分开保存。
FINAL_RESPONSE_PARTS = [
    "schema_version",  # 数据格式版本
    "capture_id",  # 与 request.json 相同的唯一采集 ID
    "association",  # task、session、主/子 agent、round 的关联信息
    "transport",  # 状态码、header、耗时、流式状态、聚合状态等响应元数据
    "message",  # 非流式 JSON 或由 Anthropic SSE 聚合得到的完整 Message
    "sse_events",  # 按接收顺序解析出的 SSE 事件；非流式响应为空列表
    "body",  # 原始响应 body（流式时是原始 SSE）的可逆表示和 SHA-256
    "state",  # complete/partial、传输错误、客户端断开等采集状态
    "provenance",  # 本记录对应的原始 capture 目录和 body 文件
]

# 用不可变集合保存代码当前能够构造的完整顶层字段。它们不是输出配置；修改实际
# 输出请编辑上面的 FINAL_REQUEST_PARTS / FINAL_RESPONSE_PARTS。
SUPPORTED_FINAL_REQUEST_PARTS = frozenset(
    {
        "schema_version", 
        "capture_id", 
        "association", 
        "transport", 
        "body", 
        "provenance"
    }
)
SUPPORTED_FINAL_RESPONSE_PARTS = frozenset(
    {
        "schema_version",
        "capture_id",
        "association",
        "transport",
        "message",
        "sse_events",
        "body",
        "state",
        "provenance",
    }
)


def validate_output_parts(
    parts: list[str], supported_parts: frozenset[str], output_name: str
) -> None:
    """验证输出白名单，未知字段或重复字段都会立即报错。"""

    unknown_parts = [part for part in parts if part not in supported_parts]
    duplicate_parts = sorted({part for part in parts if parts.count(part) > 1})
    if unknown_parts or duplicate_parts:
        problems: list[str] = []
        if unknown_parts:
            problems.append(f"不支持的顶层字段: {unknown_parts}")
        if duplicate_parts:
            problems.append(f"重复的顶层字段: {duplicate_parts}")
        raise ValueError(f"{output_name} 输出字段配置错误；" + "；".join(problems))


def select_output_parts(
    complete_value: dict[str, Any], parts: list[str], output_name: str
) -> dict[str, Any]:
    """按照白名单及其顺序，从完整记录中选择真正写入 JSON 的顶层字段。"""

    supported_parts = frozenset(complete_value)
    validate_output_parts(parts, supported_parts, output_name)
    return {part: complete_value[part] for part in parts}


@dataclass
class SessionLocation:
    message_id: str
    task_id: str
    task_dir: str
    harbor_agent_id: Optional[str]
    session_id: str
    actor_type: str
    agent_id: Optional[str]
    agent_type: Optional[str]
    agent_description: Optional[str]
    first_timestamp: Optional[str]
    first_line: int
    session_file: str
    source_files: list[str] = field(default_factory=list)
    fragment_count: int = 1
    # Claude Code 的自动压缩会把同一个 assistant 事件同时写入主 session 和
    # agent-acompact-*.jsonl。该内部字段只用于识别这种镜像，不写入最终 association。
    fragment_identities: set[str] = field(default_factory=set, repr=False)

    def semantic_key(self) -> tuple[str, str, str, str]:
        return (
            self.task_id,
            self.session_id,
            self.actor_type,
            self.agent_id or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "task_id": self.task_id,
            "task_dir": self.task_dir,
            "harbor_agent_id": self.harbor_agent_id,
            "session_id": self.session_id,
            "actor_type": self.actor_type,
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "agent_description": self.agent_description,
            "first_timestamp": self.first_timestamp,
            "first_line": self.first_line,
            "session_file": self.session_file,
            "source_files": sorted(set(self.source_files or [self.session_file])),
            "fragment_count": self.fragment_count,
        }


@dataclass(frozen=True)
class CaptureRecord:
    capture_dir: Path
    request: dict[str, Any]
    response: dict[str, Any]
    state: dict[str, Any]

    @property
    def capture_id(self) -> str:
        value = self.request.get("capture_id") or self.capture_dir.name
        return str(value)

    @property
    def message_id(self) -> Optional[str]:
        value = self.response.get("message_id")
        return value if isinstance(value, str) and value else None


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def jsonl_records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield line_number, value


def is_probable_session_file(path: Path) -> bool:
    """限制 JSONL 搜索范围，避免把 Harbor 中其他用途的 JSONL 当成 session。

    Claude Code 的主 session 通常位于 ``.claude/projects/.../*.jsonl``，子 agent
    session 通常位于 ``.../<session-id>/subagents/agent-*.jsonl``。因此，只接受
    路径组件中出现 ``projects`` 或 ``subagents`` 的 JSONL。
    """
    lower_parts = {part.lower() for part in path.parts}
    return "projects" in lower_parts or "subagents" in lower_parts


def discover_session_files(harbor_root: Path) -> list[Path]:
    """根据 --harbor-run-dir 的形态发现 session 文件。

    * 参数本身是文件：直接把它当作唯一 session 文件，不检查目录名；
    * 参数是目录：递归查找 ``*.jsonl``，再由 is_probable_session_file 过滤。

    所以传入 Harbor run、tasks、单 task 三种目录时都不要求固定的中间层级，
    只要 session 最终位于 Claude Code 常见的 projects/subagents 路径下即可。
    """
    if harbor_root.is_file():
        return [harbor_root]
    return sorted(
        path
        for path in harbor_root.rglob("*.jsonl")
        if is_probable_session_file(path)
    )


def task_context(path: Path, harbor_root: Path) -> tuple[str, Path]:
    """从 session 路径提取 ``task_id`` 和 task 根目录。

    支持的入口形态及结果：

    * ``<run>/``：若相对路径含 ``tasks/<id>/...``，取 ``<id>``；
    * ``<run>/tasks/``：相对路径第一段就是 task id；
    * ``<run>/tasks/<id>/``：用目录名作为 task id，要求该目录有
      ``final_status.json``；
    * 单独的 JSONL 或无法识别的目录：task id 退化为 ``unknown_task``。

    task 不是从代理 URL、请求时间或 prompt 推断的，而是从 Harbor 目录结构恢复。
    """
    try:
        relative = path.resolve().relative_to(harbor_root.resolve())
    except ValueError:
        relative = path
    parts = list(relative.parts)
    lowered = [part.lower() for part in parts]

    if "tasks" in lowered:
        index = lowered.index("tasks")
        if index + 1 < len(parts):
            task_dir = harbor_root.joinpath(*parts[: index + 2])
            return parts[index + 1], task_dir

    if harbor_root.name.lower() == "tasks" and parts:
        return parts[0], harbor_root / parts[0]

    if (harbor_root / "final_status.json").exists():
        return harbor_root.name, harbor_root

    return "unknown_task", harbor_root


def find_harbor_agent_id(task_dir: Path) -> Optional[str]:
    """查找 Harbor 为整个 task 启动的 agent id（不是 Claude 子 agent id）。

    优先读取 ``final_status.json.agent_id``；如果没有，则尝试扫描
    ``persisted_workspaces/*/agent_id.txt``。后者必须只有一个唯一值才采用，
    多个候选值时返回 None，避免猜测。
    """
    final_status = read_json(task_dir / "final_status.json")
    value = final_status.get("agent_id")
    if isinstance(value, str) and value:
        return value

    candidates: set[str] = set()
    persisted_root = task_dir / "persisted_workspaces"
    if persisted_root.is_dir():
        for path in persisted_root.glob("*/agent_id.txt"):
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                candidates.add(text)
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def read_subagent_metadata(path: Path) -> dict[str, Any]:
    """读取与 agent-*.jsonl 同名的可选 .meta.json（agentType/description）。"""
    metadata_path = path.with_suffix(".meta.json")
    return read_json(metadata_path) if metadata_path.exists() else {}


def subagent_id_from_path(path: Path) -> Optional[str]:
    """从 ``subagents/agent-<id>.jsonl`` 提取 ``<id>``。"""
    if path.parent.name.lower() != "subagents":
        return None
    stem = path.stem
    return stem[6:] if stem.startswith("agent-") else stem


def assistant_fragment_identity(
    record: dict[str, Any], message: dict[str, Any]
) -> Optional[str]:
    """返回足以确认两个 JSONL assistant fragment 完全相同的稳定标识。

    message.id 只标识一次模型响应，同一响应的 thinking/tool_use 等 fragment 会共用
    message.id。因此这里同时要求 JSONL 事件 uuid 和 fragment message 内容相同，避免
    把真正来自不同 agent 的同 ID 记录误判成自动压缩镜像。
    """

    event_uuid = record.get("uuid")
    if not isinstance(event_uuid, str) or not event_uuid:
        return None
    canonical_message = json.dumps(
        message,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(canonical_message).hexdigest()
    return f"{event_uuid}:{digest}"


def is_acompact_location(location: SessionLocation) -> bool:
    return bool(
        location.actor_type == "subagent"
        and location.agent_id
        and location.agent_id.startswith("acompact-")
    )


def is_acompact_mirror(
    previous: SessionLocation, current: SessionLocation
) -> bool:
    """判断两个不同 actor 位置是否是主 session 与自动压缩文件的同一事件。"""

    if previous.task_id != current.task_id or previous.session_id != current.session_id:
        return False
    is_main_acompact_pair = (
        previous.actor_type == "main" and is_acompact_location(current)
    ) or (current.actor_type == "main" and is_acompact_location(previous))
    if not is_main_acompact_pair:
        return False
    return bool(previous.fragment_identities & current.fragment_identities)


def build_session_index(
    harbor_root: Path,
    *,
    session_files: Optional[list[Path]] = None,
    task_context_resolver: Optional[Callable[[Path, Path], tuple[str, Path]]] = None,
) -> tuple[dict[str, SessionLocation], dict[str, list[SessionLocation]], dict[str, int]]:
    """扫描 session，建立 ``message.id -> 语义位置`` 索引。

    只索引同时满足以下条件的 JSONL 行：

    * 行本身是合法 JSON object；
    * ``type == "assistant"``；
    * ``message`` 是 object；
    * ``message.id`` 是非空字符串。

    主/子 agent 的判断来源按可靠性组合：文件是否位于 subagents、记录中的
    ``agentId``/``agent_id``、以及 ``isSidechain``/``is_sidechain``。位于
    subagents 目录本身就足以判定为子 agent。

    ``session_files`` 和 ``task_context_resolver`` 是目录布局适配点。默认使用本文件
    的旧 Harbor/Claude Code 目录规则；其他布局可以显式传入已经发现的 session
    文件和 task 解析函数，而不用复制 message.id 索引逻辑。

    返回值分别是：正常索引、同一 message id 的语义冲突候选、扫描统计。
    """
    index: dict[str, SessionLocation] = {}
    conflicts: dict[str, list[SessionLocation]] = {}
    stats = {
        "session_files": 0,
        "records": 0,
        "assistant_fragments": 0,
        "acompact_mirror_message_ids": 0,
    }
    acompact_mirror_message_ids: set[str] = set()
    agent_id_cache: dict[Path, Optional[str]] = {}

    files_to_scan = (
        sorted(session_files)
        if session_files is not None
        else discover_session_files(harbor_root)
    )
    resolve_task_context = task_context_resolver or task_context

    for session_file in files_to_scan:
        stats["session_files"] += 1
        task_id, task_dir = resolve_task_context(session_file, harbor_root)
        if task_dir not in agent_id_cache:
            agent_id_cache[task_dir] = find_harbor_agent_id(task_dir)
        harbor_agent_id = agent_id_cache[task_dir]
        path_agent_id = subagent_id_from_path(session_file)
        metadata = read_subagent_metadata(session_file)

        for line_number, record in jsonl_records(session_file):
            stats["records"] += 1
            if record.get("type") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            message_id = message.get("id")
            if not isinstance(message_id, str) or not message_id:
                continue
            stats["assistant_fragments"] += 1

            record_agent_id = record.get("agentId") or record.get("agent_id")
            # 行内 agentId 优先；旧版/部分 session 没写时，再从 agent-*.jsonl 文件名取。
            agent_id = (
                record_agent_id
                if isinstance(record_agent_id, str) and record_agent_id
                else path_agent_id
            )
            is_subagent = bool(
                path_agent_id
                or agent_id
                or record.get("isSidechain")
                or record.get("is_sidechain")
            )
            session_id_value = record.get("sessionId") or record.get("session_id")
            # sessionId 缺失时使用文件名，保证仍有稳定的分组键。
            session_id = (
                session_id_value
                if isinstance(session_id_value, str) and session_id_value
                else session_file.stem
            )
            location = SessionLocation(
                message_id=message_id,
                task_id=task_id,
                task_dir=str(task_dir.resolve()),
                harbor_agent_id=harbor_agent_id,
                session_id=session_id,
                actor_type="subagent" if is_subagent else "main",
                agent_id=agent_id if is_subagent else None,
                agent_type=metadata.get("agentType")
                if isinstance(metadata.get("agentType"), str)
                else None,
                agent_description=metadata.get("description")
                if isinstance(metadata.get("description"), str)
                else None,
                first_timestamp=record.get("timestamp")
                if isinstance(record.get("timestamp"), str)
                else None,
                first_line=line_number,
                session_file=str(session_file.resolve()),
                source_files=[str(session_file.resolve())],
                fragment_identities={
                    identity
                    for identity in (assistant_fragment_identity(record, message),)
                    if identity is not None
                },
            )

            previous = index.get(message_id)
            if previous is None:
                index[message_id] = location
                continue
            if previous.semantic_key() == location.semantic_key():
                # CC 可能把同一模型响应拆成多条 assistant 事件；这里只增加碎片计数，不增加轮次。
                previous.fragment_count += 1
                previous.source_files.append(location.session_file)
                previous.fragment_identities.update(location.fragment_identities)
                if location.first_timestamp and (
                    not previous.first_timestamp
                    or location.first_timestamp < previous.first_timestamp
                ):
                    previous.first_timestamp = location.first_timestamp
                    previous.first_line = location.first_line
                    previous.session_file = location.session_file
                elif (
                    location.session_file == previous.session_file
                    and location.first_line < previous.first_line
                ):
                    previous.first_line = location.first_line
                continue

            if is_acompact_mirror(previous, location):
                # 自动压缩 transcript 是主 session 的镜像，不是独立执行轨迹。同一个
                # JSONL 事件同时出现时优先保留 main；只存在于 acompact 的最终摘要响应
                # 不会进入此分支，仍会作为压缩轮正常导出。
                acompact_mirror_message_ids.add(message_id)
                if location.actor_type == "main":
                    index[message_id] = location
                continue

            candidates = conflicts.setdefault(message_id, [previous])
            # 同一 message id 落在不同 task/session/agent 时绝不覆盖，交给 conflicts 输出。
            if location.semantic_key() not in {item.semantic_key() for item in candidates}:
                candidates.append(location)

    stats["acompact_mirror_message_ids"] = len(acompact_mirror_message_ids)
    return index, conflicts, stats


def resolve_raw_root(capture_root: Path) -> Path:
    """兼容传入 ``--log-dir`` 和直接传入其 ``raw`` 子目录两种用法。"""
    nested = capture_root / "raw"
    return nested if nested.is_dir() else capture_root


def load_captures(capture_root: Path) -> tuple[list[CaptureRecord], int]:
    """读取完成的 capture，并统计尚未结束的 inflight 请求。

    只有 ``completed/<capture-id>/`` 会进入最终分类；inflight 不会被伪装成完成
    round，仅将数量写入 finalization_report.json，供操作者检查代理是否异常中断。
    """
    raw_root = resolve_raw_root(capture_root)
    records: list[CaptureRecord] = []
    completed_root = raw_root / "completed"
    if completed_root.is_dir():
        for capture_dir in sorted(path for path in completed_root.iterdir() if path.is_dir()):
            records.append(
                CaptureRecord(
                    capture_dir=capture_dir,
                    request=read_json(capture_dir / "request.json"),
                    response=read_json(capture_dir / "response.json"),
                    state=read_json(capture_dir / "state.json"),
                )
            )
    inflight_root = raw_root / "inflight"
    inflight_count = (
        len([path for path in inflight_root.iterdir() if path.is_dir()])
        if inflight_root.is_dir()
        else 0
    )
    return records, inflight_count


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def safe_component(value: str) -> str:
    result = re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip(" ._") or "unknown"
    if result.upper() in WINDOWS_RESERVED_NAMES:
        result = "_" + result
    return result[:180]


def load_sse_events(capture_dir: Path) -> list[dict[str, Any]]:
    path = capture_dir / "sse_events.jsonl"
    if not path.exists():
        return []
    return [record for _, record in jsonl_records(path)]


def association_dict(
    location: Optional[SessionLocation], round_number: Optional[int] = None
) -> Optional[dict[str, Any]]:
    if location is None:
        return None
    value = location.to_dict()
    value["round"] = round_number
    return value


def final_request(
    record: CaptureRecord,
    location: Optional[SessionLocation],
    round_number: Optional[int] = None,
    output_parts: Optional[list[str]] = None,
) -> dict[str, Any]:
    raw_body_path = record.capture_dir / "request.body"
    raw_body = raw_body_path.read_bytes() if raw_body_path.exists() else b""
    transport = {
        key: value for key, value in record.request.items() if key != "body_json"
    }
    complete_value = {
        "schema_version": SCHEMA_VERSION,
        "capture_id": record.capture_id,
        "association": association_dict(location, round_number),
        "transport": transport,
        "body": body_representation(raw_body),
        "provenance": {
            "raw_capture_dir": str(record.capture_dir.resolve()),
            "raw_body_file": "request.body",
        },
    }
    selected_parts = FINAL_REQUEST_PARTS if output_parts is None else output_parts
    return select_output_parts(complete_value, selected_parts, "request.json")


def final_response(
    record: CaptureRecord,
    location: Optional[SessionLocation],
    round_number: Optional[int] = None,
    output_parts: Optional[list[str]] = None,
) -> dict[str, Any]:
    raw_body_path = record.capture_dir / "response.body"
    raw_body = raw_body_path.read_bytes() if raw_body_path.exists() else b""
    transport = {
        key: value
        for key, value in record.response.items()
        if key not in {"body_json", "message"}
    }
    complete_value = {
        "schema_version": SCHEMA_VERSION,
        "capture_id": record.capture_id,
        "association": association_dict(location, round_number),
        "transport": transport,
        "message": record.response.get("message"),
        "sse_events": load_sse_events(record.capture_dir),
        "body": body_representation(raw_body),
        "state": record.state,
        "provenance": {
            "raw_capture_dir": str(record.capture_dir.resolve()),
            "raw_body_file": "response.body",
        },
    }
    selected_parts = FINAL_RESPONSE_PARTS if output_parts is None else output_parts
    return select_output_parts(complete_value, selected_parts, "response.json")


def write_capture_pair(
    destination: Path,
    record: CaptureRecord,
    location: Optional[SessionLocation],
    round_number: Optional[int] = None,
    request_output_parts: Optional[list[str]] = None,
    response_output_parts: Optional[list[str]] = None,
) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    write_json(
        destination / "request.json",
        final_request(record, location, round_number, request_output_parts),
    )
    write_json(
        destination / "response.json",
        final_response(record, location, round_number, response_output_parts),
    )


def round_sort_key(
    item: tuple[CaptureRecord, SessionLocation],
) -> tuple[str, str, int, str, str]:
    capture, location = item
    timestamp = location.first_timestamp or "9999-12-31T23:59:59Z"
    return (
        timestamp,
        location.session_file,
        location.first_line,
        str(capture.request.get("captured_at") or ""),
        capture.capture_id,
    )


def ensure_empty_output(output_root: Path) -> None:
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"输出目录不是空目录: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)


def finalize_dataset(
    capture_root: Path,
    harbor_root: Path,
    output_root: Path,
    *,
    session_files: Optional[list[Path]] = None,
    task_context_resolver: Optional[Callable[[Path, Path], tuple[str, Path]]] = None,
    location_filter: Optional[Callable[[SessionLocation], bool]] = None,
    task_output_group_resolver: Optional[Callable[[SessionLocation], str]] = None,
    request_output_parts: Optional[list[str]] = None,
    response_output_parts: Optional[list[str]] = None,
) -> dict[str, Any]:
    """执行关联、分类、round 排序和最终文件写出。

    分类规则：

    * 有唯一 message id 且在 session 索引中唯一命中 -> tasks；
    * Messages 请求缺少/找不到 message id -> unmatched；
    * 非 Messages 辅助请求缺少匹配 -> auxiliary；
    * session 语义冲突或多个 capture 共用 message id -> conflicts。

    ``session_files``/``task_context_resolver`` 用于适配不同的 Harbor 目录布局。
    ``location_filter`` 在 capture 已通过 message.id 精确关联到 task 后执行；返回
    False 的轨迹不会写入 tasks、unmatched 或 conflicts。这一点适合实现“只保留
    reward=1 的成功轨迹”，且不会把已知的失败轨迹伪装成 unmatched。

    ``task_output_group_resolver`` 可把已匹配的 task 写入输出根目录下的
    指定分组目录，例如 ``successful/<task_id>`` 和 ``failed/<task_id>``。
    分组名会经过与 task ID 相同的文件名安全处理。未传入时保持
    原有的 ``tasks/<task_id>`` 布局。

    ``request_output_parts`` 和 ``response_output_parts`` 是实际输出白名单。未传时
    使用文件开头的 FINAL_REQUEST_PARTS / FINAL_RESPONSE_PARTS；未知或重复字段
    会在创建输出目录之前报错。

    输出目录必须为空，这是为了防止不同 Harbor run 的旧 round 被静默混入。
    """
    selected_request_parts = list(
        FINAL_REQUEST_PARTS if request_output_parts is None else request_output_parts
    )
    selected_response_parts = list(
        FINAL_RESPONSE_PARTS if response_output_parts is None else response_output_parts
    )
    # 提前验证，避免配置写错后仍创建一个空的 output 目录或写出部分数据。
    validate_output_parts(
        selected_request_parts,
        SUPPORTED_FINAL_REQUEST_PARTS,
        "request.json",
    )
    validate_output_parts(
        selected_response_parts,
        SUPPORTED_FINAL_RESPONSE_PARTS,
        "response.json",
    )
    ensure_empty_output(output_root)
    session_index, session_conflicts, session_stats = build_session_index(
        harbor_root,
        session_files=session_files,
        task_context_resolver=task_context_resolver,
    )
    captures, inflight_count = load_captures(capture_root)

    captures_by_message: dict[str, list[CaptureRecord]] = {}
    for capture in captures:
        if capture.message_id:
            captures_by_message.setdefault(capture.message_id, []).append(capture)
    duplicate_capture_ids = {
        message_id
        for message_id, values in captures_by_message.items()
        if len(values) > 1
    }

    matched: dict[tuple[str, str, str], list[tuple[CaptureRecord, SessionLocation]]] = {}
    unmatched: list[CaptureRecord] = []
    auxiliary: list[CaptureRecord] = []
    conflict_records: list[tuple[CaptureRecord, list[SessionLocation]]] = []
    filtered_count = 0

    for capture in captures:
        message_id = capture.message_id
        is_messages = bool(capture.request.get("is_messages_request"))
        if not message_id:
            (unmatched if is_messages else auxiliary).append(capture)
            continue
        normal_location = session_index.get(message_id)
        candidates = session_conflicts.get(message_id, [])
        if candidates and location_filter is not None:
            candidates = [candidate for candidate in candidates if location_filter(candidate)]
            if not candidates:
                filtered_count += 1
                continue
        elif (
            not candidates
            and normal_location is not None
            and location_filter is not None
            and not location_filter(normal_location)
        ):
            # 在检查 duplicate capture 之前过滤失败 task，保证失败轨迹不会因为重复
            # message id 被写到 conflicts/。
            filtered_count += 1
            continue

        if message_id in duplicate_capture_ids or len(candidates) > 1:
            conflict_records.append((capture, candidates))
            continue

        # 如果原 session 索引有冲突，但过滤后只剩一个合格 task，可以安全使用该候选；
        # 否则使用正常的唯一索引项。
        location = candidates[0] if len(candidates) == 1 else normal_location
        if location is None:
            (unmatched if is_messages else auxiliary).append(capture)
            continue
        if location_filter is not None and not location_filter(location):
            filtered_count += 1
            continue
        actor_id = location.agent_id or "main"
        # 先按 task + actor 类型 + agent id 分组，再在每个 agent 内部编号 round。
        key = (location.task_id, location.actor_type, actor_id)
        matched.setdefault(key, []).append((capture, location))

    task_summary: dict[str, dict[str, Any]] = {}
    task_folders: dict[str, Path] = {}
    output_group_counts: dict[str, int] = {}
    matched_count = 0

    for (task_id, actor_type, actor_id), items in sorted(matched.items()):
        # round 首选 session assistant 记录时间排序，缺失时再使用文件/行号和采集时间。
        items.sort(key=round_sort_key)
        if task_output_group_resolver is None:
            output_group = "tasks"
        else:
            raw_output_group = task_output_group_resolver(items[0][1])
            output_group_parts = Path(raw_output_group).parts
            if not output_group_parts or any(
                part in ("", ".", "..") for part in output_group_parts
            ):
                raise ValueError(f"输出分组路径不合法: {raw_output_group!r}")
            output_group = "/".join(
                safe_component(part) for part in output_group_parts
            )
        task_folder = output_root.joinpath(*output_group.split("/"), safe_component(task_id))
        previous_task_folder = task_folders.setdefault(task_id, task_folder)
        if previous_task_folder != task_folder:
            raise ValueError(f"同一 task 被分配到不同输出分组: {task_id}")
        actor_folder_name = (
            "main_agent"
            if actor_type == "main"
            else "subagent_" + safe_component(actor_id)
        )
        actor_folder = task_folder / actor_folder_name
        first_location = items[0][1]
        actor_folder.mkdir(parents=True, exist_ok=True)
        write_json(
            actor_folder / "agent.json",
            {
                "schema_version": SCHEMA_VERSION,
                "task_id": task_id,
                "actor_type": actor_type,
                "agent_id": None if actor_type == "main" else actor_id,
                "harbor_agent_id": first_location.harbor_agent_id,
                "session_id": first_location.session_id,
                "agent_type": first_location.agent_type,
                "agent_description": first_location.agent_description,
                "round_count": len(items),
                "session_files": sorted(
                    {
                        source
                        for _, location in items
                        for source in location.source_files
                    }
                ),
            },
        )
        for round_number, (capture, location) in enumerate(items, 1):
            round_folder = actor_folder / f"round_{round_number:06d}"
            write_capture_pair(
                round_folder,
                capture,
                location,
                round_number,
                selected_request_parts,
                selected_response_parts,
            )
            matched_count += 1

        initial_task_info = {
            "task_id": task_id,
            "task_dir": first_location.task_dir,
            "harbor_agent_id": first_location.harbor_agent_id,
            "agents": [],
            "round_count": 0,
        }
        if task_output_group_resolver is not None:
            initial_task_info["output_group"] = output_group
        task_info = task_summary.setdefault(task_id, initial_task_info)
        task_info["agents"].append(
            {
                "actor_type": actor_type,
                "agent_id": None if actor_type == "main" else actor_id,
                "folder": actor_folder_name,
                "round_count": len(items),
            }
        )
        task_info["round_count"] += len(items)

    for task_id, task_info in task_summary.items():
        write_json(task_folders[task_id] / "task.json", task_info)
        if task_output_group_resolver is not None:
            group = task_info["output_group"]
            output_group_counts[group] = output_group_counts.get(group, 0) + 1

    for capture in unmatched:
        write_capture_pair(
            output_root / "unmatched" / capture.capture_id,
            capture,
            None,
            request_output_parts=selected_request_parts,
            response_output_parts=selected_response_parts,
        )
    for capture in auxiliary:
        write_capture_pair(
            output_root / "auxiliary" / capture.capture_id,
            capture,
            None,
            request_output_parts=selected_request_parts,
            response_output_parts=selected_response_parts,
        )
    for capture, candidates in conflict_records:
        destination = output_root / "conflicts" / capture.capture_id
        write_capture_pair(
            destination,
            capture,
            None,
            request_output_parts=selected_request_parts,
            response_output_parts=selected_response_parts,
        )
        write_json(
            destination / "conflict.json",
            {
                "message_id": capture.message_id,
                "session_candidates": [candidate.to_dict() for candidate in candidates],
                "duplicate_capture_count": len(
                    captures_by_message.get(capture.message_id or "", [])
                ),
            },
        )

    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_timestamp(),
        "capture_root": str(capture_root.resolve()),
        "harbor_root": str(harbor_root.resolve()),
        "output_root": str(output_root.resolve()),
        "captures": len(captures),
        "matched": matched_count,
        "unmatched": len(unmatched),
        "auxiliary": len(auxiliary),
        "conflicts": len(conflict_records),
        "filtered_captures": filtered_count,
        "inflight": inflight_count,
        "tasks": len(task_summary),
        "session_message_ids": len(session_index),
        "session_conflict_ids": len(session_conflicts),
        "duplicate_capture_message_ids": len(duplicate_capture_ids),
        "session_scan": session_stats,
    }
    if task_output_group_resolver is not None:
        report["task_output_groups"] = output_group_counts
    write_json(output_root / "finalization_report.json", report)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把代理原始数据与 Harbor/Claude Code session 关联并生成最终数据集"
    )
    parser.add_argument(
        "--capture-dir",
        type=Path,
        required=True,
        help="proxy.py 的 --log-dir，或其 raw 子目录",
    )
    parser.add_argument(
        "--harbor-run-dir",
        type=Path,
        required=True,
        help="Harbor run 目录、tasks 目录或单个 task 目录",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="最终数据集目录；必须不存在或为空",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    report = finalize_dataset(
        capture_root=args.capture_dir,
        harbor_root=args.harbor_run_dir,
        output_root=args.output_dir,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
