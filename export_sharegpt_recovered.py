from __future__ import annotations

"""从 finalize 数据导出 ShareGPT，并安全恢复被重试替代的坏流式响应。

本脚本保留 ``export_sharegpt.py`` 的输出格式，但在加载 agent rounds 时增加两种
有事实依据的恢复路径：

* 不完整响应后紧跟语义相同的完整重试时，丢弃不完整响应；
* 下一请求的历史中包含上一 assistant 响应时，使用该历史内容修复聚合结果。

无法由完整重试或后续请求历史证明的响应仍会被拒绝，不会把 partial 数据伪装成
完整 SFT 数据。原始 ``export_sharegpt.py`` 不受影响。
"""

import argparse
import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import export_sharegpt as base
from capture_core import write_json


@dataclass(frozen=True)
class SourceRound:
    """尚未应用完整性策略的 finalize round。"""

    number: int
    round_dir: Path
    request_body: dict[str, Any]
    response: dict[str, Any]
    response_message: dict[str, Any]
    response_path: Path


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
    return SourceRound(
        number=number,
        round_dir=round_dir,
        request_body=request_body,
        response=response,
        response_message=response_message,
        response_path=response_path,
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


def as_round_record(source: SourceRound, content: Any = None) -> base.RoundRecord:
    response_message = copy.deepcopy(source.response_message)
    if content is not None:
        response_message["content"] = content
    return base.RoundRecord(
        number=source.number,
        round_dir=source.round_dir,
        request_body=source.request_body,
        response_message=response_message,
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
) -> tuple[list[base.RoundRecord], list[dict[str, Any]]]:
    """加载 agent，恢复有后续事实源证明的响应并报告所有恢复动作。"""

    sources = [
        load_source_round(path, number)
        for number, path in discover_numbered_round_dirs(agent_dir)
    ]
    rounds: list[base.RoundRecord] = []
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


def export_sharegpt_recovered(
    input_dir: Path,
    output_dir: Path,
    reasoning_mode: str,
    standard_structure: bool = True,
) -> dict[str, Any]:
    """执行兼容原格式的导出，并额外生成 recovery_report.json。"""

    if reasoning_mode not in base.REASONING_MODES:
        raise ValueError(
            f"reasoning_mode 必须是 {', '.join(base.REASONING_MODES)}，"
            f"收到 {reasoning_mode!r}"
        )
    tasks_root = base.resolve_tasks_root(input_dir)
    base.ensure_empty_output(output_dir)

    task_count = 0
    agent_count = 0
    round_count = 0
    file_count = 0
    split_count = 0
    errors: list[dict[str, Any]] = []
    recoveries: list[dict[str, Any]] = []

    for task_dir in sorted(path for path in tasks_root.iterdir() if path.is_dir()):
        agent_dirs = base.discover_agent_dirs(task_dir)
        if not agent_dirs:
            continue
        task_count += 1
        task_output = output_dir / task_dir.name
        task_output.mkdir(parents=True, exist_ok=False)

        for agent_dir in agent_dirs:
            agent_count += 1
            try:
                rounds, agent_recoveries = load_agent_rounds_with_recovery(agent_dir)
            except base.IncompleteResponseError as exc:
                errors.append(
                    {
                        "task": task_dir.name,
                        "agent": agent_dir.name,
                        "response_path": str(exc.path),
                        "reason": str(exc),
                        "details": exc.details,
                    }
                )
                continue

            for recovery in agent_recoveries:
                recoveries.append(
                    {"task": task_dir.name, "agent": agent_dir.name, **recovery}
                )
            round_count += len(rounds)
            segments = base.split_rounds_by_context(rounds)
            split_count += max(0, len(segments) - 1)
            for segment_number, segment in enumerate(segments, 1):
                file_stem = f"{agent_dir.name}_{segment_number}"
                record_id = f"{task_dir.name}__{agent_dir.name}__{segment_number}"
                record = base.build_sharegpt_record(
                    segment,
                    record_id,
                    reasoning_mode,
                    standard_structure,
                )
                write_json(task_output / f"{file_stem}.json", record, compact=True)
                file_count += 1

    error_report_path = output_dir / "export_errors.json"
    recovery_report_path = output_dir / "recovery_report.json"
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

    return {
        "input_dir": str(input_dir.resolve()),
        "output_dir": str(output_dir.resolve()),
        "reasoning_mode": reasoning_mode,
        "standard_structure": standard_structure,
        "tasks": task_count,
        "agents": agent_count,
        "rounds": round_count,
        "sharegpt_files": file_count,
        "context_splits": split_count,
        "recovered_rounds": len(recoveries),
        "skipped_agents": len(errors),
        "error_report": str(error_report_path.resolve()),
        "recovery_report": str(recovery_report_path.resolve()),
        "format": (
            "hf-tool-calling-with-flat-aliases"
            if standard_structure
            else "example-flat-tool-calling"
        ),
    }


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="恢复被完整重试替代的坏流式响应，并导出 ShareGPT SFT 文件"
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
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    report = export_sharegpt_recovered(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        reasoning_mode=args.reasoning_mode,
        standard_structure=args.standard_structure,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
