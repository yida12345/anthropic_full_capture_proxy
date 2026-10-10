"""Kimi Code / Hermes 的只读会话适配与严格关联，不用 IP 或时间猜测归属。"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from api_protocols import ANTHROPIC, CHAT, chat_message, infer_protocol, normalize_request, normalize_response, responses_messages


@dataclass
class Transcript:
    session_id: str
    task_id: str
    task_dir: Path
    path: Path
    actor_id: str | None
    messages: list[tuple[int, dict[str, Any], str | None, str | None]]
    system: Any = None
    model: str | None = None
    compactions: list[tuple[dict[str, Any], dict[str, Any], int, str | None]] = field(default_factory=list)
    lineage_root: str | None = None


def timestamp(value: Any, *, milliseconds: bool = False) -> str | None:
    if isinstance(value, (float, int)):
        if milliseconds:
            value /= 1000
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return value if isinstance(value, str) else None


def jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"会话 JSONL 损坏: {path}:{number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"会话记录不是对象: {path}:{number}")
            yield number, value


def task_for(path: Path, root: Path, fallback: str) -> tuple[str, Path]:
    from finalize import task_context

    task, directory = task_context(path, root)
    if task != "unknown_task":
        return task, directory
    # Harbor job 与 worker 日志没有 tasks 中间层。
    parts = path.resolve().parts
    for marker in ("agent", "logs"):
        if marker in parts:
            index = max(i for i, part in enumerate(parts) if part == marker)
            if index > 0:
                directory = Path(*parts[:index])
                if directory.name not in {".kimi", ".kimi-code", ".hermes", "sessions", "main"}:
                    return directory.name, directory
    return fallback, path.parent


def kimi_identity(path: Path) -> tuple[str, Path, str | None]:
    parent = path.parent
    if parent.parent.name in {"subagents", "agents"}:
        session = parent.parent.parent
        return session.name, session, None if parent.name == "main" else parent.name
    if path.name not in {"wire.jsonl", "context.jsonl"}:
        return path.stem, parent, None
    return parent.name, parent, None


def location(transcript: Transcript, message_id: str | None, number: int, time: str | None, client: str, method: str):
    from finalize import SessionLocation

    return SessionLocation(
        message_id=message_id, task_id=transcript.task_id, task_dir=str(transcript.task_dir.resolve()),
        harbor_agent_id=None, session_id=transcript.session_id,
        actor_type="subagent" if transcript.actor_id else "main", agent_id=transcript.actor_id,
        agent_type=client, agent_description=None, first_timestamp=time, first_line=number,
        session_file=str(transcript.path.resolve()), source_files=[str(transcript.path.resolve())],
        client=client, association_method=method,
    )


def discover_session_sources(root: Path, client: str) -> list[Path]:
    if root.is_file():
        return [root]
    names = ("context.jsonl", "wire.jsonl") if client == "kimi" else ("state.db",)
    return sorted({path for name in names for path in root.rglob(name)})


def client_task_context(path: Path, root: Path, layout: str) -> tuple[str, Path]:
    parts = path.resolve().relative_to(root.resolve()).parts
    if layout == "jobs" and len(parts) >= 3 and parts[1] == "agent":
        return parts[0], root / parts[0]
    if layout == "node" and len(parts) >= 5 and parts[0].startswith("worker") and parts[1] == "logs" and parts[3] == "logs":
        return parts[2], root / parts[0] / "logs" / parts[2]
    if layout == "proc" and len(parts) >= 4 and parts[2] == "logs":
        return parts[1], root / parts[0] / parts[1]
    raise ValueError(f"{layout} 布局的会话路径不合法: {path}")


def load_kimi(root: Path, files=None, resolver=None):
    explicit_files = files is not None
    files = discover_session_sources(root, "kimi") if files is None else sorted(files)
    if not files and not explicit_files:
        raise ValueError(f"找不到 Kimi context.jsonl / wire.jsonl: {root}")
    transcripts = []
    ids: dict[str, list[Any]] = defaultdict(list)
    count = 0
    for path in files:
        session_id, session_dir, actor_id = kimi_identity(path)
        task_id, task_dir = resolver(path, root) if resolver else task_for(path, root, session_id)
        transcript = Transcript(session_id, task_id, task_dir, path, actor_id, [])
        compact_requests = {}
        compact_applied = {}
        for number, record in jsonl(path):
            count += 1
            if path.name == "wire.jsonl":
                event_actor = record.get("agentId", actor_id or "main")
                if record.get("type") == "full_compaction.begin":
                    compact_requests.pop(event_actor, None)
                    compact_applied.pop(event_actor, None)
                elif record.get("type") == "llm.request" and record.get("kind") == "compaction":
                    compact_requests[event_actor] = record
                elif record.get("type") == "context.apply_compaction" and event_actor in compact_requests:
                    compact_applied[event_actor] = (record, number)
                elif record.get("type") == "full_compaction.complete" and event_actor in compact_applied:
                    applied, boundary_line = compact_applied.pop(event_actor)
                    transcript.compactions.append((compact_requests.pop(event_actor), applied, boundary_line,
                        timestamp(applied.get("time"), milliseconds=True)))
                elif record.get("type") == "full_compaction.cancel":
                    compact_requests.pop(event_actor, None)
                    compact_applied.pop(event_actor, None)
                # Kimi Code 2.x 的 Wire journal 与旧 Python kimi-cli 的 envelope 不同。
                # step.end / agent.message.appended 的 messageId 是模型响应 id；
                # context.append_message.message.id 是客户端内部消息 id，不能拿来关联。
                message_id = None
                if record.get("type") == "context.append_loop_event":
                    event = record.get("event", {})
                    if event.get("type") == "step.end":
                        message_id = event.get("messageId")
                elif record.get("type") == "agent.message.appended":
                    message_id = record.get("message", {}).get("meta", {}).get("messageId")
                if isinstance(message_id, str) and message_id:
                    ids[message_id].append(location(transcript, message_id, number,
                        timestamp(record.get("time"), milliseconds=True), "kimi", "wire_message_id"))
                    continue
                envelope = record.get("message", record)
                if envelope.get("type") == "SubagentEvent":
                    payload = envelope.get("payload", {})
                    envelope = payload.get("event", {})
                    event_actor = payload.get("agent_id")
                    # 旧格式没有 agent_id 时，tool_call id 不能冒充持久 agent id。
                    if not isinstance(event_actor, str) or not event_actor:
                        continue
                else:
                    event_actor = actor_id
                if envelope.get("type") == "StatusUpdate":
                    message_id = envelope.get("payload", {}).get("message_id")
                    if isinstance(message_id, str) and message_id:
                        event_transcript = copy.copy(transcript)
                        event_transcript.actor_id = event_actor
                        ids[message_id].append(location(event_transcript, message_id, number,
                            timestamp(record.get("timestamp")), "kimi", "wire_message_id"))
                continue
            role = record.get("role")
            if role == "_system_prompt":
                transcript.system = record.get("content")
            elif role in {"user", "assistant", "tool", "system"}:
                transcript.messages.append((number, chat_message(record), timestamp(record.get("timestamp")), None))
        if transcript.messages or transcript.compactions:
            transcripts.append(transcript)
    return transcripts, ids, {"session_files": len(files), "records": count, "wire_message_ids": len(ids)}


def load_hermes(root: Path, files=None, resolver=None):
    explicit_files = files is not None
    files = discover_session_sources(root, "hermes") if files is None else sorted(files)
    if not files and not explicit_files:
        raise ValueError(f"找不到 Hermes state.db: {root}")
    transcripts = []
    count = 0
    for path in files:
        # mode=ro + query_only + 同一读事务；不会执行迁移、修改数据库或丢掉 WAL。
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            sessions = {row["id"]: dict(row) for row in connection.execute("SELECT * FROM sessions")}
            for session_id, session in sessions.items():
                root_id = session_id
                seen = set()
                while root_id in sessions and sessions[root_id].get("parent_session_id"):
                    if root_id in seen:
                        raise ValueError(f"Hermes session 父链存在环: {path}:{session_id}")
                    seen.add(root_id)
                    root_id = sessions[root_id]["parent_session_id"]
                config = session.get("model_config")
                config = json.loads(config) if isinstance(config, str) and config else {}
                if not isinstance(config, dict):
                    raise ValueError(f"Hermes model_config 不是对象: {path}:{session_id}")
                is_child = session.get("source") == "subagent" or bool(config.get("_delegate_from"))
                task_id, task_dir = resolver(path, root) if resolver else task_for(path, root, root_id)
                transcript = Transcript(session_id, task_id, task_dir, path,
                    session_id if is_child else None, [], session.get("system_prompt"), session.get("model"))
                transcript.lineage_root = root_id
                # Rotation children store only the handoff; original source turns remain
                # in the parent. Keep native user witnesses from the whole lineage.
                lineage = {session_id}
                ancestor = session_id
                while ancestor in sessions and sessions[ancestor].get("parent_session_id"):
                    ancestor = sessions[ancestor]["parent_session_id"]
                    lineage.add(ancestor)
                source_users = [r["content"] for sid in lineage for r in connection.execute(
                    "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id", (sid,))
                    if isinstance(r["content"], str)]
                rows = connection.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,))
                for row in rows:
                    value = dict(row)
                    count += 1
                    if value.get("role") not in {"user", "assistant", "tool", "system"}:
                        continue
                    message = {"role": value["role"], "content": value.get("api_content")
                               if value.get("api_content") is not None else value.get("content")}
                    if isinstance(message["content"], str) and message["content"].startswith("\x00json:"):
                        try:
                            message["content"] = json.loads(message["content"][6:])
                        except json.JSONDecodeError:
                            pass  # 与 Hermes 的 fail-closed 解码规则一致，保留原文。
                    for key in ("tool_call_id", "tool_name", "reasoning_content", "reasoning"):
                        if value.get(key) is not None:
                            message[key] = value[key]
                    if value.get("tool_calls"):
                        message["tool_calls"] = json.loads(value["tool_calls"])
                    if not message["content"] and value.get("codex_message_items"):
                        items = json.loads(value["codex_message_items"])
                        canonical = responses_messages(items)
                        if any(m["role"] != "assistant" for m in canonical):
                            raise ValueError(f"非法 Hermes codex_message_items: {path}:{value['id']}")
                        converted = {"role": "assistant", "content": [b for m in canonical for b in m["content"]]}
                    else:
                        converted = chat_message(message)
                    transcript.messages.append((value["id"], converted, timestamp(value.get("timestamp")), value.get("message_uid")))
                    if value.get("_compressed_summary") and isinstance(message["content"], str):
                        transcript.compactions.append(({"kind": "hermes_compaction", "source_users": source_users},
                            {"summary": message["content"]}, value["id"], timestamp(value.get("timestamp"))))
                if transcript.messages:
                    transcripts.append(transcript)
    return transcripts, {}, {"session_files": len(files), "records": count}


def canonical_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只规范化等价文本表示和签名；文本、thinking、tool id/name/参数必须完整一致。"""
    result = []
    for message in messages:
        role = message.get("role")
        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        pending = []

        def flush():
            if pending:
                result.append({"role": role, "content": copy.deepcopy(pending)})
                pending.clear()

        for block in content:
            kind = block.get("type")
            if kind == "tool_result":
                flush()
                result.append({"role": "tool", "tool_call_id": block.get("tool_use_id"),
                    "content": canonical_messages([{"role": "tool", "content": block.get("content", "")}])})
            else:
                cleaned = {k: copy.deepcopy(v) for k, v in block.items() if k not in {"signature", "cache_control"}}
                if kind == "text" and not cleaned.get("text"):
                    continue
                if kind == "text" and pending and pending[-1].get("type") == "text":
                    pending[-1]["text"] += cleaned["text"]
                else:
                    pending.append(cleaned)
        flush()
    return result


def history_matches(stored: list[dict[str, Any]], requested: list[dict[str, Any]], client: str) -> bool:
    if len(stored) != len(requested):
        return False
    for actual, expected in zip(stored, requested):
        # Hermes 会持久保存 reasoning，但对不支持重放 reasoning 的 Chat provider
        # 不会把它发回模型。仅比较历史时投影到本次请求的可见字段；响应本身仍要求
        # thinking 全量一致，文本和工具信息也绝不省略。
        if client == "hermes" and actual.get("role") == expected.get("role") == "assistant":
            # DeepSeek/Kimi echo-back inserts exactly one space when the original
            # response has no reasoning. This is a replay pad, not a model thought.
            if not any(b.get("type") == "thinking" for b in actual.get("content", [])):
                expected = {**expected, "content": [b for b in expected.get("content", [])
                    if not (b.get("type") == "thinking" and b.get("thinking") == " ")]}
            if not any(b.get("type") == "thinking" for b in expected.get("content", [])):
                actual = {**actual, "content": [b for b in actual.get("content", []) if b.get("type") != "thinking"]}
        if actual != expected:
            return False
    return True


def hermes_storage_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hermes 持久化时 strip 文本；此投影只用于证据比较，不修改捕获或导出。"""
    if sum(b.get("type") == "text" for b in content) > 1:
        return content  # 多段非连续正文不能猜测其 flatten 顺序。
    result = []
    for block in content:
        if block.get("type") == "text":
            text = block.get("text", "").strip()
            if text:
                result.append({**block, "text": text})
        else:
            result.append(block)
    return result


def response_matches(stored: list[dict[str, Any]], captured: list[dict[str, Any]], client: str) -> bool:
    if stored == captured:
        return True
    if client != "hermes":
        return False
    def project(messages):
        return [{**m, "content": hermes_storage_content(m["content"])}
                if m.get("role") == "assistant" else m for m in messages]
    if not any(b.get("type") == "thinking" for m in captured for b in m.get("content", [])):
        # Hermes may persist its synthesized echo pad even when the provider
        # returned no reasoning. Only the native single-space pad is removed;
        # captured content remains authoritative and is never rewritten.
        stored = [{**m, "content": [b for b in m.get("content", [])
            if not (b.get("type") == "thinking" and b.get("thinking") == " ")]} for m in stored]
    return project(stored) == project(captured)


def kimi_compaction_matches(body, request, answer, native_request, boundary):
    """摘要没有普通 step.end id；专用 prompt、摘要全文及原生请求指纹共同证明归属。"""
    if body.get("model") != native_request.get("model") or native_request.get("kind") != "compaction":
        return False
    content = answer[0].get("content", []) if len(answer) == 1 else []
    summary = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
    if not summary or summary != boundary.get("summary"):
        return False
    messages = request.get("messages", [])
    last = messages[-1] if messages else {}
    last_content = last.get("content", [])
    if isinstance(last_content, str):
        last_content = [{"type": "text", "text": last_content}]
    prefix = "You are about to run out of context. Create a handoff summary for the\nmodel that will resume this task after the earlier conversation is cleared."
    if last.get("role") != "user" or not any(b.get("type") == "text" and b.get("text", "").startswith(prefix)
        for b in last_content):
        return False
    system = request.get("system", [])
    system_text = system if isinstance(system, str) else "".join(b.get("text", "") for b in system if b.get("type") == "text")
    if hashlib.sha256(system_text.encode()).hexdigest() != native_request.get("systemPromptHash"):
        return False
    tools = [{"name": t.get("name"), "description": t.get("description", ""), "parameters": t.get("input_schema", {})}
             for t in request.get("tools", [])]
    tools_hash = hashlib.sha256(json.dumps(tools, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return tools_hash == native_request.get("toolsHash")


def hermes_compaction_matches(request, answer, native_request, boundary):
    """支持 Hermes 的原生 wrapper 和确定性 task-snapshot 替换；其它改写拒绝猜测。"""
    messages = request.get("messages", [])
    prompt_prefix = "You are a summarization agent creating a context checkpoint."
    if len(messages) != 1 or messages[0].get("role") != "user" or request.get("tools"):
        return False
    if not any(b.get("type") == "text" and b.get("text", "").startswith(prompt_prefix)
        for b in messages[0].get("content", [])):
        return False
    native = boundary.get("summary", "")
    end_marker = "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"
    if not native.startswith("[CONTEXT COMPACTION — REFERENCE ONLY] ") or native.count(end_marker) != 1 or "\n" not in native:
        return False
    stored_body, _, native_tail = native.split("\n", 1)[1].partition(end_marker)
    # Automatic compression may merge a preserved actionable user into the
    # summary row. Accept the suffix only when it is an exact native user turn.
    if native_tail.strip() and native_tail.strip() not in native_request.get("source_users", []):
        return False
    stored_body = stored_body.rstrip()
    content = answer[0].get("content", []) if len(answer) == 1 else []
    captured_body = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
    if not captured_body:
        return False
    if captured_body == stored_body:
        return True
    # Exact legacy transform: replace only the first task section with a
    # deterministic quote of a native user turn; every other byte must match.
    section = re.compile(r"(?m)^## Historical Task Snapshot\n[\s\S]*?(?=^## |\Z)")
    stored_match = section.search(stored_body)
    captured_match = section.search(captured_body)
    if not stored_match or not captured_match:
        return False
    grounded = stored_match.group().rstrip()
    valid_grounding = any(grounded == "## Historical Task Snapshot\nUser asked (deterministic, from compacted turns): "
        + repr(re.sub(r"\s+", " ", text.strip()))
        + "\nHistorical only; newer protected-tail messages after this summary win."
        for text in native_request.get("source_users", []))
    if not valid_grounding:
        return False
    projected = captured_body[:captured_match.start()] + stored_match.group() + captured_body[captured_match.end():]
    return projected == stored_body


def build_client_associations(captures: list[Any], root: Path, client: str, *, session_files=None, task_context_resolver=None):
    from finalize import capture_request_body

    loader = load_kimi if client == "kimi" else load_hermes
    transcripts, direct_ids, stats = loader(root, session_files, task_context_resolver)
    associations: dict[str, list[Any]] = {}
    ambiguity = {}
    stats.update(assistant_fragments=0, id_matched=0, history_matched=0, compact_matched=0, unsupported_captures=0)
    # 预先规范化完整历史，避免针对每个 capture 重复解析。
    histories = []
    for transcript in transcripts:
        history = []
        for number, message, time, uid in transcript.messages:
            canonical = canonical_messages([message])
            if message["role"] == "assistant":
                stats["assistant_fragments"] += 1
                # 共享规范化历史并记录本轮结束位置，避免每轮复制全部前缀造成平方级内存。
                histories.append((transcript, history, len(history), canonical, number, time, uid))
            history.extend(canonical)

    def history_candidates(body, request, wanted, answer, message_id):
        matches = {}
        for transcript, history, end, canonical, number, time, uid in histories:
            if not response_matches(canonical, answer, client) or end < len(wanted) or not history_matches(history[end - len(wanted):end], wanted, client):
                continue
            # 对已知 system/model 再做一致性检查，不忽略会改变语义的配置。
            if client == "kimi" and transcript.system is not None:
                system = normalize_request({"messages": [], "system": transcript.system}, ANTHROPIC)["system"]
                if isinstance(system, str):
                    system = [{"type": "text", "text": system}] if system else []
                if canonical_messages([{"role": "system", "content": system}]) != canonical_messages([
                    {"role": "system", "content": request.get("system", [])}]):
                    continue
            # Hermes 的 session model/system 是会话级快照，切换模型后不是旧轮次的事实源。
            if client == "kimi" and body.get("prompt_cache_key") and body["prompt_cache_key"] != transcript.session_id:
                continue
            match = location(transcript, message_id, number, time, client, "exact_history")
            # Hermes compaction 的同一 message_uid 可出现在多个物理行，不伪造额外轮次。
            key = ((transcript.task_id, transcript.lineage_root, transcript.actor_id, uid)
                if client == "hermes" and uid else (*match.semantic_key(), uid or number))
            previous = matches.get(key)
            # A rotation can copy one logical response into a child. Its UID
            # remains the same: retain the original global DB row as witness.
            if previous is None or number < previous.first_line:
                matches[key] = match
        return list(matches.values())

    def normalized(capture):
        body = capture_request_body(capture)
        response = capture.response.get("message")
        if not body or not isinstance(response, dict):
            return None
        protocol = capture.request.get("api_protocol") or infer_protocol(body, response)
        request = normalize_request(body, protocol)
        answer = canonical_messages([normalize_response(response, protocol)])
        wanted = canonical_messages(request.get("messages", []))
        return (body, request, wanted, answer) if wanted and answer else None

    def witness(candidate):
        return (candidate.session_file, candidate.first_line, candidate.semantic_key())

    # Wire 与 context 的物理行不同。先用 ID 已确认响应的完整请求/答案，
    # 严格映射同一 actor 的 context 轮次，避免扫描顺序决定强/弱证据的所有权。
    claimed = defaultdict(set)
    for capture in captures:
        if client != "kimi" or not request_protocol_for_capture(capture):
            continue
        direct = direct_ids.get(capture.message_id, [])
        if not direct:
            continue
        try:
            values = normalized(capture)
        except (ValueError, TypeError, KeyError):
            continue
        if values:
            semantics = {candidate.semantic_key() for candidate in direct}
            for candidate in history_candidates(*values, capture.message_id):
                if candidate.semantic_key() in semantics:
                    claimed[witness(candidate)].add(capture.capture_id)

    for capture in captures:
        if not request_protocol_for_capture(capture):
            continue
        candidates = direct_ids.get(capture.message_id, [])
        if candidates:
            # 主 wire 中嵌套的子事件与子 wire 是镜像，同一个 actor 去重。
            unique = {candidate.semantic_key(): candidate for candidate in candidates}
            associations[capture.capture_id] = list(unique.values())
            stats["id_matched"] += 1
            continue
        if capture.state.get("state") != "complete" or capture.response.get("aggregation_errors"):
            continue
        try:
            values = normalized(capture)
        except (ValueError, TypeError, KeyError):
            stats["unsupported_captures"] += 1
            continue
        if values is None:
            continue
        body, request, wanted, answer = values
        if client in {"kimi", "hermes"}:
            compact_candidates = []
            for transcript in transcripts:
                for native_request, boundary, number, time in transcript.compactions:
                    matches_compact = (kimi_compaction_matches(body, request, answer, native_request, boundary)
                        if client == "kimi" else hermes_compaction_matches(request, answer, native_request, boundary))
                    if not matches_compact:
                        continue
                    compact_transcript = copy.copy(transcript)
                    compact_transcript.actor_id = "acompact-" + client + "-" + str(boundary.get("agentId") or transcript.actor_id or "main")
                    match = location(compact_transcript, capture.message_id, number, time, client, client + "_compaction_boundary")
                    match.agent_description = client + " native compaction operation"
                    compact_candidates.append(match)
            if compact_candidates:
                associations[capture.capture_id] = compact_candidates
                stats["compact_matched"] += 1
                continue
        matches = history_candidates(body, request, wanted, answer, capture.message_id)
        for match in matches:
            if claimed[witness(match)] - {capture.capture_id}:
                ambiguity[capture.capture_id] = "session_round_claimed_by_wire_message_id"
        if matches:
            associations[capture.capture_id] = matches
            stats["history_matched"] += 1
    # 一个 transcript round 不能同时证明多个不同 HTTP 响应属于它。
    witnesses: dict[tuple, list[str]] = defaultdict(list)
    for capture_id, candidates in associations.items():
        for candidate in candidates:
            if candidate.association_method in {"exact_history", "kimi_compaction_boundary", "hermes_compaction_boundary"}:
                key = witness(candidate)
                witnesses[key].append(capture_id)
    for capture_ids in witnesses.values():
        if len(set(capture_ids)) > 1:
            for capture_id in capture_ids:
                ambiguity[capture_id] = "multiple_captures_for_one_session_round"
    stats["ambiguous_captures"] = len(ambiguity)
    return associations, stats, ambiguity


def request_protocol_for_capture(capture: Any) -> bool:
    from api_protocols import request_protocol
    return request_protocol(capture.request.get("method"), capture.request.get("path")) is not None
