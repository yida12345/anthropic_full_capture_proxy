from __future__ import annotations

"""恢复 finalize 数据，筛选截断响应，并合并有完整覆盖证据的压缩轨迹。

本脚本保留 ``export_sharegpt.py`` 的输出格式，但在加载 agent rounds 时增加两种
有事实依据的恢复路径：

* 不完整响应后紧跟语义相同的完整重试时，丢弃不完整响应；
* 下一请求的历史中包含上一 assistant 响应时，使用该历史内容修复聚合结果。

在恢复后按任务处理截断、压缩合并和超限失败分类。无法恢复的 partial 仍会被拒绝。
原始 ``export_sharegpt.py`` 和 ``export_sharegpt_recovered.py`` 保持不变。
"""

import argparse
import copy
import json
from dataclasses import dataclass, field
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Any, Optional

import export_sharegpt as base
from capture_core import write_json

COMBINED_JSONL_NAME = "sharegpt.jsonl"


@dataclass(frozen=True)
class CapturedRound(base.RoundRecord):
    association: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceRound:
    """尚未应用完整性策略的 finalize round。"""

    number: int
    round_dir: Path
    request_body: dict[str, Any]
    response: dict[str, Any]
    response_message: dict[str, Any]
    response_path: Path
    association: dict[str, Any] = field(default_factory=dict)


def discover_numbered_round_dirs(agent_dir: Path) -> list[tuple[int, Path]]:
    """复用原脚本的 round 命名约束并返回数字排序结果。"""

    numbered_dirs: list[tuple[int, Path]] = []
    seen_numbers: set[int] = set()
    for path in agent_dir.iterdir():
        if not path.is_dir():
            continue
        match = base.ROUND_DIRECTORY_PATTERN.fullmatch(path.name)
        if match is None:
            if path.name.startswith("round_"):
                raise ValueError(f"round 目录名称不合法: {path}")
            continue
        number = int(match.group(1))
        if number in seen_numbers:
            raise ValueError(f"agent 内存在重复 round 编号 {number}: {agent_dir}")
        seen_numbers.add(number)
        numbered_dirs.append((number, path))

    if not numbered_dirs:
        raise ValueError(f"agent 目录中没有 round: {agent_dir}")
    return sorted(numbered_dirs)


def load_source_round(round_dir: Path, number: int) -> SourceRound:
    """读取 round，但把响应完整性判断推迟到恢复阶段。"""

    request_path = round_dir / "request.json"
    response_path = round_dir / "response.json"
    request = base.read_json_object(request_path)
    response = base.read_json_object(response_path)

    body = base.require_dict(request.get("body"), f"{request_path} 的 body")
    request_body = base.require_dict(
        body.get("json"), f"{request_path} 的 body.json"
    )
    response_message = base.require_dict(
        response.get("message"), f"{response_path} 的 message"
    )
    base.require_list(
        request_body.get("messages"), f"{request_path} 的 body.json.messages"
    )
    base.require_list(
        response_message.get("content"), f"{response_path} 的 message.content"
    )
    association = request.get("association") or response.get("association") or {}
    base.require_dict(association, f"{request_path} 的 association")
    return SourceRound(
        number=number,
        round_dir=round_dir,
        request_body=request_body,
        response=response,
        response_message=response_message,
        response_path=response_path,
        association=copy.deepcopy(association),
    )


def normalized_retry_request(request_body: dict[str, Any]) -> dict[str, Any]:
    """生成重试比较值，只忽略传输模式和已知非语义元数据。"""

    normalized = base.remove_nonsemantic_fields(copy.deepcopy(request_body))
    normalized["system"] = base.normalized_system(request_body.get("system"))
    normalized.pop("stream", None)
    return normalized


def is_semantically_identical_retry(current: SourceRound, following: SourceRound) -> bool:
    """只把相邻、连续编号且完整请求语义相同的调用视为重试。"""

    return (
        following.number == current.number + 1
        and normalized_retry_request(current.request_body)
        == normalized_retry_request(following.request_body)
    )


def same_context_configuration(
    current_request: dict[str, Any], following_request: dict[str, Any]
) -> bool:
    """判断两个请求是否属于相同模型、system 和工具配置。"""

    return (
        current_request.get("model") == following_request.get("model")
        and base.normalized_system(current_request.get("system"))
        == base.normalized_system(following_request.get("system"))
        and base.remove_nonsemantic_fields(current_request.get("tools", []))
        == base.remove_nonsemantic_fields(following_request.get("tools", []))
    )


def response_content_from_following_history(
    current: SourceRound, following: SourceRound
) -> Optional[Any]:
    """从下一请求增长的历史中提取 current 的 canonical assistant content。"""

    if following.number != current.number + 1:
        return None
    if not same_context_configuration(current.request_body, following.request_body):
        return None

    old_messages = base.normalized_messages(current.request_body.get("messages"))
    new_messages = base.normalized_messages(following.request_body.get("messages"))
    if len(new_messages) <= len(old_messages):
        return None
    if new_messages[: len(old_messages)] != old_messages:
        return None

    source_messages = base.require_list(
        following.request_body.get("messages"), "following request.messages"
    )
    assistant_message = source_messages[len(old_messages)]
    if not isinstance(assistant_message, dict):
        return None
    if assistant_message.get("role") != "assistant":
        return None
    return copy.deepcopy(assistant_message.get("content", ""))


def validate_convertible_assistant_content(content: Any, description: str) -> None:
    """提前执行原导出器的 assistant block 结构校验。"""

    base.convert_assistant_content(
        content,
        reasoning_mode="separate",
        standard_structure=True,
        tool_names_by_id={},
        description=description,
    )


def sanitized_end_turn_content(
    source: SourceRound,
) -> Optional[tuple[list[dict[str, Any]], list[dict[str, Any]]]]:
    """清理与完整 end_turn 明确矛盾的空洞和幽灵 tool_use block。

    该恢复只接受已经收到 message_stop 的聚合结果，并要求至少保留一段非空正文。
    对 tool_use 结束或没有正文的末轮不做猜测。
    """

    transport = source.response.get("transport")
    if not isinstance(transport, dict):
        return None
    if transport.get("aggregation_complete") is not True:
        return None
    if source.response_message.get("stop_reason") != "end_turn":
        return None

    content = source.response_message.get("content")
    if not isinstance(content, list):
        return None
    kept: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    has_nonempty_text = False
    for index, block in enumerate(content):
        if isinstance(block, dict) and block.get("type") in {"text", "thinking"}:
            kept.append(copy.deepcopy(block))
            value = block.get("text") if block.get("type") == "text" else block.get("thinking")
            if isinstance(value, str) and value:
                has_nonempty_text = True
            continue
        discarded.append(
            {
                "index": index,
                "type": block.get("type") if isinstance(block, dict) else None,
            }
        )

    if not discarded or not kept or not has_nonempty_text:
        return None
    validate_convertible_assistant_content(
        kept, f"{source.response_path} 清理后的 end_turn message.content"
    )
    return kept, discarded


def as_round_record(source: SourceRound, content: Any = None) -> CapturedRound:
    response_message = copy.deepcopy(source.response_message)
    if content is not None:
        response_message["content"] = content
    return CapturedRound(
        number=source.number,
        round_dir=source.round_dir,
        request_body=source.request_body,
        response_message=response_message,
        association=source.association,
    )


def recovery_failure(
    error: base.IncompleteResponseError, reason: str
) -> base.IncompleteResponseError:
    details = dict(error.details)
    details["recovery_reason"] = reason
    return base.IncompleteResponseError(
        f"{error}; 无法安全恢复: {reason}",
        error.path,
        details,
    )


def load_agent_rounds_with_recovery(
    agent_dir: Path,
) -> tuple[list[CapturedRound], list[dict[str, Any]]]:
    """加载 agent，恢复有后续事实源证明的响应并报告所有恢复动作。"""

    sources = [
        load_source_round(path, number)
        for number, path in discover_numbered_round_dirs(agent_dir)
    ]
    agent_metadata_path = agent_dir / "agent.json"
    agent_metadata = base.read_json_object(agent_metadata_path) if agent_metadata_path.is_file() else {}
    for source in sources:
        for key in ("session_id", "session_files"):
            if not source.association.get(key) and agent_metadata.get(key):
                source.association[key] = agent_metadata[key]
    rounds: list[CapturedRound] = []
    recoveries: list[dict[str, Any]] = []

    for index, source in enumerate(sources):
        following = sources[index + 1] if index + 1 < len(sources) else None
        completion_error: Optional[base.IncompleteResponseError] = None
        try:
            base.validate_completed_response(source.response, source.response_path)
        except base.IncompleteResponseError as exc:
            completion_error = exc

        if following is not None and is_semantically_identical_retry(
            source, following
        ):
            try:
                base.validate_completed_response(
                    following.response, following.response_path
                )
                validate_convertible_assistant_content(
                    following.response_message.get("content"),
                    f"{following.response_path} 的 message.content",
                )
            except (base.IncompleteResponseError, ValueError) as exc:
                if completion_error is not None:
                    raise recovery_failure(
                        completion_error,
                        f"相邻重试 {following.round_dir.name} 也不完整或不可转换: {exc}",
                    ) from exc
            else:
                recoveries.append(
                    {
                        "action": (
                            "discard_incomplete_retry"
                            if completion_error is not None
                            else "discard_retried_response"
                        ),
                        "discarded_round": source.round_dir.name,
                        "discarded_response_path": str(source.response_path),
                        "replacement_round": following.round_dir.name,
                        "replacement_response_path": str(following.response_path),
                    }
                )
                continue

        if completion_error is not None:

            if following is not None:
                historical_content = response_content_from_following_history(
                    source, following
                )
                if historical_content is not None:
                    try:
                        validate_convertible_assistant_content(
                            historical_content,
                            f"{following.round_dir}/request.messages assistant history",
                        )
                    except ValueError as exc:
                        raise recovery_failure(
                            completion_error,
                            f"后续请求保存的 assistant 历史不可转换: {exc}",
                        ) from exc
                    rounds.append(as_round_record(source, historical_content))
                    recoveries.append(
                        {
                            "action": "reconstruct_from_following_request",
                            "recovered_round": source.round_dir.name,
                            "original_response_path": str(source.response_path),
                            "evidence_round": following.round_dir.name,
                            "evidence_request_path": str(
                                following.round_dir / "request.json"
                            ),
                        }
                    )
                    continue

            raise recovery_failure(
                completion_error,
                "没有找到相邻的完整等价重试或包含该响应的后续请求历史",
            )

        original_content = source.response_message.get("content")
        historical_content = (
            response_content_from_following_history(source, following)
            if following is not None
            else None
        )
        if (
            historical_content is not None
            and base.remove_nonsemantic_fields(original_content)
            != base.remove_nonsemantic_fields(historical_content)
        ):
            validate_convertible_assistant_content(
                historical_content,
                f"{following.round_dir}/request.messages assistant history",
            )
            rounds.append(as_round_record(source, historical_content))
            recoveries.append(
                {
                    "action": "replace_aggregation_from_following_request",
                    "recovered_round": source.round_dir.name,
                    "original_response_path": str(source.response_path),
                    "evidence_round": following.round_dir.name,
                    "evidence_request_path": str(following.round_dir / "request.json"),
                }
            )
            continue

        sanitized = sanitized_end_turn_content(source)
        if sanitized is not None:
            sanitized_content, discarded_blocks = sanitized
            rounds.append(as_round_record(source, sanitized_content))
            recoveries.append(
                {
                    "action": "sanitize_complete_end_turn",
                    "recovered_round": source.round_dir.name,
                    "original_response_path": str(source.response_path),
                    "discarded_blocks": discarded_blocks,
                }
            )
            continue

        validate_convertible_assistant_content(
            original_content, f"{source.response_path} 的 message.content"
        )
        rounds.append(as_round_record(source))

    if not rounds:
        raise ValueError(f"agent 的所有 round 都被不完整重试替代: {agent_dir}")
    return rounds, recoveries


def is_compact_agent_dir(agent_dir: Path) -> bool:
    """判断 finalize agent 目录是否属于正常或恢复出的自动压缩子 agent。"""

    return agent_dir.name.startswith("subagent_acompact-")


def mask_assistant_history(record: dict[str, Any]) -> int:
    """将最后一条之外的 assistant 消息标记为不参与 loss。"""

    messages = base.require_list(record.get("messages"), "ShareGPT record.messages")
    assistant_indexes = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]
    for index in assistant_indexes[:-1]:
        messages[index]["role"] = "masked_assistant"
    return max(0, len(assistant_indexes) - 1)


def write_combined_jsonl(output_dir: Path, record_paths: list[Path]) -> Path:
    """按稳定顺序将独立 ShareGPT JSON 合并为一行一个对象的 JSONL。"""

    destination = output_dir / COMBINED_JSONL_NAME
    temporary = output_dir / f".{COMBINED_JSONL_NAME}.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        for path in sorted(record_paths):
            json.dump(
                base.read_json_object(path),
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            handle.write("\n")
    temporary.replace(destination)
    return destination


@dataclass
class ExportSample:
    agent_name: str
    segment_number: int
    segment: list[CapturedRound]
    record: dict[str, Any]
    source_rounds: list[str]

    @property
    def last_round(self) -> CapturedRound:
        return self.segment[-1]

    @cached_property
    def history(self) -> list[Any]:
        return base.normalized_messages(self.last_round.request_body["messages"])

    @cached_property
    def full_history(self) -> list[Any]:
        return self.history + [base.normalized_message_for_context({
            "role": "assistant", "content": self.last_round.response_message["content"],
        })]


def session_id(round_record: CapturedRound) -> Optional[str]:
    value = round_record.association.get("session_id")
    return value if isinstance(value, str) and value else None


def context_configuration_compatible(previous: CapturedRound, current: CapturedRound) -> bool:
    a, b = previous.request_body, current.request_body
    return (
        isinstance(a.get("model"), str) and bool(a["model"])
        and a.get("model") == b.get("model")
        and base.normalized_system(a.get("system")) == base.normalized_system(b.get("system"))
        and base.tools_context_compatible(a.get("tools", []), b.get("tools", []))
    )


def follows_in_time(previous: CapturedRound, current: CapturedRound) -> bool:
    a = previous.association.get("first_timestamp")
    b = current.association.get("first_timestamp")
    if not isinstance(a, str) or not isinstance(b, str):
        return True
    try:
        return datetime.fromisoformat(a.replace("Z", "+00:00")) <= datetime.fromisoformat(b.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return False


def replay_evidence(
    current: CapturedRound, candidates: list[CapturedRound],
) -> Optional[CapturedRound]:
    for candidate in candidates:
        if candidate.round_dir == current.round_dir or not follows_in_time(current, candidate):
            continue
        a, b = session_id(current), session_id(candidate)
        if a and b and a != b:
            continue
        if current.round_dir.parent != candidate.round_dir.parent and not (a and a == b):
            continue
        if base.context_continues(current, candidate):
            return candidate
    return None


def filter_truncated_rounds(
    rounds: list[CapturedRound], compact_rounds: list[CapturedRound],
) -> tuple[list[CapturedRound], list[CapturedRound], list[dict[str, Any]]]:
    kept, discarded, events = [], [], []
    for index, current in enumerate(rounds):
        if current.response_message.get("stop_reason") != "max_tokens":
            kept.append(current)
            continue
        blocks = base.normalized_content_for_context(current.response_message["content"])
        has_text = any(b.get("type") == "text" and b.get("text", "").strip() for b in blocks)
        has_tool = any(b.get("type") == "tool_use" for b in blocks)
        following = rounds[index + 1:index + 2]
        evidence = replay_evidence(current, following + compact_rounds)
        if not has_text and not has_tool:
            action = "discard_thinking_only_max_tokens"
            discarded.append(current)
        elif evidence is not None:
            action = "keep_replayed_max_tokens"
            kept.append(current)
        else:
            action = "discard_unaccepted_max_tokens"
            discarded.append(current)
        events.append({
            "agent": current.round_dir.parent.name, "round": current.round_dir.name,
            "response_path": str(current.round_dir / "response.json"),
            "stop_reason": "max_tokens",
            "action": action, "has_text": bool(has_text), "has_tool_calls": has_tool,
            "evidence_request": str(evidence.round_dir / "request.json") if evidence else None,
        })
    return kept, discarded, events


def merge_compact_samples(
    samples: list[ExportSample],
) -> tuple[list[ExportSample], list[dict[str, Any]], list[dict[str, Any]]]:
    removed: set[int] = set()
    consumed_main: set[int] = set()
    merged, unmerged = [], []
    for compact_index, compact in enumerate(samples):
        if not compact.agent_name.startswith("subagent_acompact-"):
            continue
        candidates = []
        reason = "missing_session_identity"
        for main_index, main in enumerate(samples):
            if main.agent_name != "main_agent" or main_index in consumed_main:
                continue
            a, b = session_id(main.last_round), session_id(compact.last_round)
            if not a or a != b:
                continue
            reason = "context_configuration_mismatch"
            if not context_configuration_compatible(main.last_round, compact.last_round):
                continue
            reason = "history_not_fully_covered"
            if not follows_in_time(main.last_round, compact.last_round):
                continue
            history = main.full_history
            if compact.history[:len(history)] == history:
                candidates.append((len(history), main_index))
        if not candidates:
            unmerged.append({"compact_id": compact.record["id"], "reason": reason})
            continue
        longest = max(length for length, _ in candidates)
        matches = [i for length, i in candidates if length == longest]
        if len(matches) != 1:
            unmerged.append({"compact_id": compact.record["id"], "reason": "ambiguous_main_prefix"})
            continue
        main_index = matches[0]
        main = samples[main_index]
        record = copy.deepcopy(compact.record)
        record["id"] = main.record["id"]
        samples[main_index] = ExportSample(
            main.agent_name, main.segment_number, compact.segment, record,
            main.source_rounds + compact.source_rounds,
        )
        consumed_main.add(main_index)
        removed.add(compact_index)
        merged.append({
            "main_id": main.record["id"], "compact_id": compact.record["id"],
            "output_id": record["id"], "covered_messages": longest,
            "source_rounds": samples[main_index].source_rounds,
        })
    return [sample for i, sample in enumerate(samples) if i not in removed], merged, unmerged


def mask_discarded_history(
    sample: ExportSample, discarded: list[CapturedRound], reasoning_mode: str,
    standard_structure: bool,
) -> list[dict[str, Any]]:
    events = []
    raw_messages = sample.last_round.request_body["messages"]
    system_offset = int(bool(base.system_text(sample.last_round.request_body.get("system"))))
    for source in discarded:
        if not follows_in_time(source, sample.last_round):
            continue
        a, b = session_id(source), session_id(sample.last_round)
        if a and b and a != b:
            continue
        if not (a and a == b) and source.round_dir.parent.name != sample.agent_name:
            continue
        prefix = base.normalized_messages(source.request_body["messages"])
        n = len(prefix)
        expected = base.normalized_message_for_context({
            "role": "assistant", "content": source.response_message["content"],
        })
        if len(sample.history) <= n or sample.history[:n] != prefix or sample.history[n] != expected:
            continue
        converted_prefix, _ = base.convert_history_messages(raw_messages[:n], reasoning_mode, standard_structure)
        index = system_offset + len(converted_prefix)
        message = sample.record["messages"][index]
        if message.get("role") == "assistant":
            message["role"] = "masked_assistant"
            events.append({"record_id": sample.record["id"], "message_index": index,
                           "source_response": str(source.round_dir / "response.json")})
    return events


def build_task(
    task_dir: Path, reasoning_mode: str, standard_structure: bool = True,
    mask_compact_assistant_history: bool = True, merge_compact_trajectories: bool = True,
) -> tuple[list[ExportSample], dict[str, Any]]:
    loaded: dict[str, list[CapturedRound]] = {}
    report: dict[str, Any] = {
        "agents": 0, "rounds": 0, "context_splits": 0, "original_context_splits": 0,
        "errors": [], "recoveries": [], "truncations": [], "merged_compacts": [],
        "unmerged_compacts": [], "masked_truncated_history": [], "main_session_files": [],
    }
    for agent_dir in base.discover_agent_dirs(task_dir):
        report["agents"] += 1
        if agent_dir.name == "main_agent" and (agent_dir / "agent.json").is_file():
            report["main_session_files"].extend(base.read_json_object(agent_dir / "agent.json").get("session_files", []))
        try:
            rounds, recoveries = load_agent_rounds_with_recovery(agent_dir)
        except base.IncompleteResponseError as exc:
            report["errors"].append({"task": task_dir.name, "agent": agent_dir.name,
                                     "response_path": str(exc.path), "reason": str(exc), "details": exc.details})
            continue
        loaded[agent_dir.name] = rounds
        report["rounds"] += len(rounds)
        report["recoveries"].extend(dict(item, task=task_dir.name, agent=agent_dir.name) for item in recoveries)
        if agent_dir.name == "main_agent":
            for current in rounds:
                association = current.association
                report["main_session_files"].extend(association.get("session_files", []))
                report["main_session_files"].extend(association.get("source_files", []))
                if association.get("session_file"):
                    report["main_session_files"].append(association["session_file"])
    report["main_session_files"] = sorted(set(report["main_session_files"]))
    compact_rounds = [current for name, rounds in loaded.items()
                      if name.startswith("subagent_acompact-") for current in rounds]
    samples, discarded = [], []
    for agent_name, rounds in loaded.items():
        report["original_context_splits"] += max(0, len(base.split_rounds_by_context(rounds)) - 1)
        kept, removed, truncations = filter_truncated_rounds(rounds, compact_rounds)
        discarded.extend(removed)
        report["truncations"].extend(dict(event, task=task_dir.name) for event in truncations)
        segments = base.split_rounds_by_context(kept)
        report["context_splits"] += max(0, len(segments) - 1)
        for number, segment in enumerate(segments, 1):
            record_id = f"{task_dir.name}__{agent_name}__{number}"
            record = base.build_sharegpt_record(segment, record_id, reasoning_mode, standard_structure)
            samples.append(ExportSample(agent_name, number, segment, record,
                                        [str(current.round_dir) for current in segment]))
    if merge_compact_trajectories:
        samples, report["merged_compacts"], report["unmerged_compacts"] = merge_compact_samples(samples)
    for sample in samples:
        if mask_compact_assistant_history and sample.agent_name.startswith("subagent_acompact-"):
            mask_assistant_history(sample.record)
        report["masked_truncated_history"].extend(
            mask_discarded_history(sample, discarded, reasoning_mode, standard_structure))
    report["source_mapping"] = [{"record_id": sample.record["id"], "source_rounds": sample.source_rounds}
                                for sample in samples]
    return samples, report


def resolve_task_group(input_dir: Path, task_dir: Path, requested: str) -> str:
    if requested != "auto":
        return requested
    if input_dir.name in {"successful", "failed"}:
        return input_dir.name
    metadata_path = task_dir / "task.json"
    if metadata_path.is_file():
        output_group = base.read_json_object(metadata_path).get("output_group", "")
        if isinstance(output_group, str) and output_group.split("/")[0] in {"successful", "failed"}:
            return output_group.split("/")[0]
    return "unknown"


def classify_limit_failure(task_dir: Path, group: str, session_files: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"task": task_dir.name, "group": group, "classification": "regular",
                              "reason": "unknown_task_group" if group == "unknown" else "not_a_failed_task",
                              "confirmed": False}
    if group != "failed":
        return result
    last_event = None
    unavailable = []
    for filename in session_files:
        path = Path(filename)
        if not path.is_absolute():
            path = task_dir / path
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict) or event.get("isSidechain") or event.get("is_sidechain"):
                        continue
                    if event.get("type") not in {"assistant", "user"}:
                        continue
                    timestamp = event.get("timestamp")
                    if not isinstance(timestamp, str):
                        continue
                    if last_event is None or timestamp >= last_event.get("timestamp", ""):
                        last_event = event
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            unavailable.append({"path": str(path), "error": str(exc)})
    if not session_files or unavailable or last_event is None:
        result.update(classification="limit_exceeded", reason="session_unavailable",
                      session_errors=unavailable, confirmed=False)
        return result
    error = last_event.get("error")
    message = last_event.get("message") or {}
    content = message.get("content", "") if isinstance(message, dict) else ""
    text = content if isinstance(content, str) else "\n".join(
        block.get("text", "") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str))
    reason = None
    if last_event.get("isApiErrorMessage") is True:
        if error == "max_output_tokens":
            reason = "max_output_tokens"
        elif error == "context_length_exceeded" or "prompt is too long" in text.lower():
            reason = "context_length_exceeded"
    result.update(reason=reason or "no_terminal_limit_error", confirmed=bool(reason))
    if reason:
        result["classification"] = "limit_exceeded"
    return result


def export_sharegpt_recovered_merged(
    input_dir: Path,
    output_dir: Path,
    reasoning_mode: str,
    standard_structure: bool = True,
    mask_compact_assistant_history: bool = True,
    merge_compact_trajectories: bool = True,
    task_group: str = "auto",
) -> dict[str, Any]:
    """执行兼容原格式的导出，并额外生成 recovery_report.json。"""

    if reasoning_mode not in base.REASONING_MODES:
        raise ValueError(
            f"reasoning_mode 必须是 {', '.join(base.REASONING_MODES)}，"
            f"收到 {reasoning_mode!r}"
        )
    if task_group not in {"auto", "successful", "failed"}:
        raise ValueError("task_group 必须是 auto、successful 或 failed")
    tasks_root = base.resolve_tasks_root(input_dir)
    base.ensure_empty_output(output_dir)

    task_count = 0
    agent_count = 0
    round_count = 0
    file_count = 0
    split_count = 0
    masked_assistant_count = 0
    record_paths: list[Path] = []
    limit_paths: list[Path] = []
    errors: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []
    optimizations: dict[str, list[Any]] = {key: [] for key in (
        "merged_compacts", "unmerged_compacts", "truncations", "masked_truncated_history",
        "source_mapping", "task_classifications",
    )}
    original_split_count = 0
    limit_task_count = 0

    for task_dir in sorted(path for path in tasks_root.iterdir() if path.is_dir()):
        agent_dirs = base.discover_agent_dirs(task_dir)
        if not agent_dirs:
            continue
        task_count += 1
        samples, task_report = build_task(
            task_dir, reasoning_mode, standard_structure, mask_compact_assistant_history,
            merge_compact_trajectories,
        )
        agent_count += task_report["agents"]
        round_count += task_report["rounds"]
        split_count += task_report["context_splits"]
        original_split_count += task_report["original_context_splits"]
        errors.extend(task_report["errors"])
        recoveries.extend(task_report["recoveries"])
        for key in optimizations:
            if key != "task_classifications":
                optimizations[key].extend(dict(item, task=task_dir.name) for item in task_report[key])
        group = resolve_task_group(input_dir, task_dir, task_group)
        classification = classify_limit_failure(task_dir, group, task_report["main_session_files"])
        optimizations["task_classifications"].append(classification)
        limited = classification["classification"] == "limit_exceeded"
        limit_task_count += int(limited)
        task_output = (output_dir / "limit_exceeded" if limited else output_dir) / task_dir.name
        task_output.mkdir(parents=True, exist_ok=False)
        for sample in samples:
            record_path = task_output / f"{sample.agent_name}_{sample.segment_number}.json"
            write_json(record_path, sample.record, compact=True)
            (limit_paths if limited else record_paths).append(record_path)
            file_count += 1
            masked_assistant_count += sum(message.get("role") == "masked_assistant"
                                          for message in sample.record["messages"])

    combined_jsonl_path = write_combined_jsonl(output_dir, record_paths)
    limit_jsonl_path = write_combined_jsonl(output_dir / "limit_exceeded", limit_paths) if limit_task_count else None
    error_report_path = output_dir / "export_errors.json"
    recovery_report_path = output_dir / "recovery_report.json"
    optimization_report_path = output_dir / "optimization_report.json"
    write_json(
        error_report_path,
        {"error_count": len(errors), "errors": errors},
    )
    write_json(
        recovery_report_path,
        {
            "recovered_round_count": len(recoveries),
            "recoveries": recoveries,
        },
    )
    write_json(optimization_report_path, optimizations)

    report = {
        "input_dir": str(input_dir.resolve()),
        "output_dir": str(output_dir.resolve()),
        "reasoning_mode": reasoning_mode,
        "standard_structure": standard_structure,
        "mask_compact_assistant_history": mask_compact_assistant_history,
        "merge_compact_trajectories": merge_compact_trajectories,
        "task_group": task_group,
        "masked_assistant_messages": masked_assistant_count,
        "tasks": task_count,
        "agents": agent_count,
        "rounds": round_count,
        "sharegpt_files": file_count,
        "combined_jsonl": str(combined_jsonl_path.resolve()),
        "combined_jsonl_records": len(record_paths),
        "limit_exceeded_jsonl": str(limit_jsonl_path.resolve()) if limit_jsonl_path else None,
        "limit_exceeded_jsonl_records": len(limit_paths),
        "limit_exceeded_tasks": limit_task_count,
        "merged_compacts": len(optimizations["merged_compacts"]),
        "discarded_truncated_rounds": sum(event["action"].startswith("discard_")
                                          for event in optimizations["truncations"]),
        "masked_truncated_history": len(optimizations["masked_truncated_history"]),
        "context_splits": split_count,
        "original_context_splits": original_split_count,
        "recovered_rounds": len(recoveries),
        "skipped_agents": len(errors),
        "error_report": str(error_report_path.resolve()),
        "recovery_report": str(recovery_report_path.resolve()),
        "optimization_report": str(optimization_report_path.resolve()),
        "format": (
            "hf-tool-calling-with-flat-aliases"
            if standard_structure
            else "example-flat-tool-calling"
        ),
    }
    write_json(output_dir / "export_report.json", report)
    return report


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="恢复响应、筛选截断、合并压缩轨迹，并分类超限失败任务"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="finalize.py/finalize-harbor.py 输出目录，目录下必须包含 tasks/",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="ShareGPT 输出根目录；必须不存在或为空",
    )
    parser.add_argument(
        "--reasoning-mode",
        choices=base.REASONING_MODES,
        default="separate",
        help=(
            "separate: thinking 单独写入 reasoning_content；"
            "inline: 使用 <think>...</think> 拼到 assistant.content；默认 separate"
        ),
    )
    standard_group = parser.add_mutually_exclusive_group()
    standard_group.add_argument(
        "--standard-structure",
        dest="standard_structure",
        action="store_true",
        help="生成标准 type/function 结构并保留扁平别名（默认）",
    )
    standard_group.add_argument(
        "--no-standard-structure",
        dest="standard_structure",
        action="store_false",
        help="关闭标准结构，只生成示例扁平格式",
    )
    parser.set_defaults(standard_structure=True)
    parser.add_argument(
        "--mask-compact-assistant-history",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "仅对未合并的正常及恢复出的 acompact 子 agent，将最终 ShareGPT 消息中最后一条 "
            "assistant 之前的 assistant role 改为 masked_assistant（默认开启）"
        ),
    )
    parser.add_argument(
        "--merge-compact-trajectories", action=argparse.BooleanOptionalAction, default=True,
        help="完整覆盖且 model/system/tools 兼容时，将压缩轨迹合入 main（默认开启）",
    )
    parser.add_argument(
        "--task-group", choices=("auto", "successful", "failed"), default="auto",
        help="输入任务组；auto 从输入目录名或 task.json.output_group 识别，无法识别时不做失败分类",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    report = export_sharegpt_recovered_merged(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        reasoning_mode=args.reasoning_mode,
        standard_structure=args.standard_structure,
        mask_compact_assistant_history=args.mask_compact_assistant_history,
        merge_compact_trajectories=args.merge_compact_trajectories,
        task_group=args.task_group,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
