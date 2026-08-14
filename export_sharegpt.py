from __future__ import annotations

"""把 finalize.py 生成的 task/agent/round 数据集导出为 SFT 对话文件。

默认输出 Hugging Face/TRL 推荐的标准工具调用层级，同时保留示例 sharegpt.json
使用的扁平 ``name/arguments`` 和 ``name/description/parameters`` 别名。使用
``--no-standard-structure`` 时，只生成示例扁平结构：

* 标准读取端使用 ``tool_call.function`` 和 ``tool.function``；
* 示例读取端继续使用对象外层的扁平字段；
* 标准嵌套字段是事实源，写出前强制检查扁平别名与它完全相同。

同一个 agent 的相邻 round 只有在语义上下文连续时才合并。比较时会忽略每轮都会
变化但不改变对话语义的 billing cch、cache_control 和 thinking signature。
"""

import argparse
import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from capture_core import write_json


ROUND_DIRECTORY_PATTERN = re.compile(r"^round_(\d+)$")
BILLING_HEADER_PREFIX = "x-anthropic-billing-header:"
REASONING_MODES = ("separate", "inline")


@dataclass(frozen=True)
class RoundRecord:
    """一个 agent round 中构建 ShareGPT 所需的请求、响应和来源路径。"""

    number: int
    round_dir: Path
    request_body: dict[str, Any]
    response_message: dict[str, Any]


class IncompleteResponseError(ValueError):
    """响应采集不完整；该 agent 可以跳过并记录到导出错误报告。"""

    def __init__(self, message: str, path: Path, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.path = path
        self.details = details


def read_json_object(path: Path) -> dict[str, Any]:
    """严格读取 JSON object；SFT 导出不静默跳过损坏数据。"""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"无法读取文件: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"文件不是合法 JSON: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"文件顶层必须是 JSON object: {path}")
    return value


def require_dict(value: Any, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{description} 必须是 JSON object")
    return value


def require_list(value: Any, description: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{description} 必须是 JSON array")
    return value


def validate_completed_response(response: dict[str, Any], path: Path) -> None:
    """在完整性字段存在时拒绝 partial 或聚合未完成的响应。"""

    state = response.get("state")
    transport = response.get("transport")
    details = {
        "state": state.get("state") if isinstance(state, dict) else None,
        "aggregation_complete": (
            transport.get("aggregation_complete") if isinstance(transport, dict) else None
        ),
        "transport_error": (
            transport.get("transport_error") if isinstance(transport, dict) else None
        ),
        "client_disconnected": (
            transport.get("client_disconnected") if isinstance(transport, dict) else None
        ),
        "aggregation_errors": (
            transport.get("aggregation_errors") if isinstance(transport, dict) else []
        ),
    }
    if isinstance(state, dict):
        state_value = state.get("state")
        if isinstance(state_value, str) and state_value != "complete":
            raise IncompleteResponseError(
                f"响应不是 complete，不能用于 SFT: {path}: {state_value}",
                path,
                details,
            )

    if isinstance(transport, dict) and transport.get("aggregation_complete") is False:
        raise IncompleteResponseError(
            f"SSE 聚合未完成，不能用于 SFT: {path}", path, details
        )


def load_round(round_dir: Path, number: int) -> RoundRecord:
    request_path = round_dir / "request.json"
    response_path = round_dir / "response.json"
    request = read_json_object(request_path)
    response = read_json_object(response_path)
    validate_completed_response(response, response_path)

    body = require_dict(request.get("body"), f"{request_path} 的 body")
    request_body = require_dict(body.get("json"), f"{request_path} 的 body.json")
    response_message = require_dict(
        response.get("message"), f"{response_path} 的 message"
    )
    require_list(request_body.get("messages"), f"{request_path} 的 body.json.messages")
    require_list(response_message.get("content"), f"{response_path} 的 message.content")
    return RoundRecord(
        number=number,
        round_dir=round_dir,
        request_body=request_body,
        response_message=response_message,
    )


def load_agent_rounds(agent_dir: Path) -> list[RoundRecord]:
    """按 round 数字排序，拒绝形似 round_ 但编号不合法或重复的目录。"""

    numbered_dirs: list[tuple[int, Path]] = []
    seen_numbers: set[int] = set()
    for path in agent_dir.iterdir():
        if not path.is_dir():
            continue
        match = ROUND_DIRECTORY_PATTERN.fullmatch(path.name)
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
    return [load_round(path, number) for number, path in sorted(numbered_dirs)]


def remove_nonsemantic_fields(value: Any) -> Any:
    """生成上下文连续性比较值，不修改实际输出内容。

    cache_control 只影响 API prompt cache；thinking signature 是 Claude 回传验证值，
    两者都不改变用于 SFT 的可见对话语义。
    """

    if isinstance(value, list):
        return [remove_nonsemantic_fields(item) for item in value]
    if not isinstance(value, dict):
        return value

    result: dict[str, Any] = {}
    is_thinking = value.get("type") == "thinking"
    for key, item in value.items():
        if key == "cache_control":
            continue
        if is_thinking and key == "signature":
            continue
        result[key] = remove_nonsemantic_fields(item)
    return result


def normalized_system(system: Any) -> Any:
    """删除每轮 cch 都会变化的内部 billing header，再进行语义规范化。"""

    if isinstance(system, str):
        return None if system.lstrip().startswith(BILLING_HEADER_PREFIX) else system
    if system is None:
        return []
    blocks = require_list(system, "request.system")
    kept: list[Any] = []
    for block in blocks:
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"].lstrip().startswith(BILLING_HEADER_PREFIX)
        ):
            continue
        kept.append(block)
    return remove_nonsemantic_fields(kept)


def normalized_messages(messages: Any) -> list[Any]:
    return require_list(remove_nonsemantic_fields(messages), "request.messages")


def context_continues(previous: RoundRecord, current: RoundRecord) -> bool:
    """判断 current 是否是 previous 的同一语义上下文继续增长。

    除 model/system/tools 一致外，current.messages 必须保留 previous.messages 的
    完整规范化前缀，并紧接着包含 previous response 的 assistant content。
    """

    previous_request = previous.request_body
    current_request = current.request_body
    if previous_request.get("model") != current_request.get("model"):
        return False
    if normalized_system(previous_request.get("system")) != normalized_system(
        current_request.get("system")
    ):
        return False
    if remove_nonsemantic_fields(previous_request.get("tools", [])) != remove_nonsemantic_fields(
        current_request.get("tools", [])
    ):
        return False

    old_messages = normalized_messages(previous_request.get("messages"))
    new_messages = normalized_messages(current_request.get("messages"))
    if len(new_messages) <= len(old_messages):
        return False
    if new_messages[: len(old_messages)] != old_messages:
        return False

    expected_assistant = remove_nonsemantic_fields(
        {"role": "assistant", "content": previous.response_message.get("content")}
    )
    return new_messages[len(old_messages)] == expected_assistant


def split_rounds_by_context(rounds: list[RoundRecord]) -> list[list[RoundRecord]]:
    if not rounds:
        return []
    segments: list[list[RoundRecord]] = [[rounds[0]]]
    for current in rounds[1:]:
        if context_continues(segments[-1][-1], current):
            segments[-1].append(current)
        else:
            segments.append([current])
    return segments


def validate_hybrid_tool_call(tool_call: dict[str, Any]) -> None:
    """保证扁平兼容别名与标准 function 数据完全相同。"""

    function = require_dict(tool_call.get("function"), "tool_call.function")
    if tool_call.get("type") != "function":
        raise ValueError("tool_call.type 必须是 function")
    if tool_call.get("name") != function.get("name"):
        raise ValueError("tool_call.name 与 tool_call.function.name 不一致")
    if tool_call.get("arguments") != function.get("arguments"):
        raise ValueError("tool_call.arguments 与 tool_call.function.arguments 不一致")


def make_tool_call(
    block: dict[str, Any], description: str, standard_structure: bool
) -> dict[str, Any]:
    call_id = block.get("id")
    name = block.get("name")
    arguments = block.get("input")
    if not isinstance(call_id, str) or not call_id:
        raise ValueError(f"{description} 的 tool_use.id 必须是非空字符串")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{description} 的 tool_use.name 必须是非空字符串")
    if not isinstance(arguments, dict):
        raise ValueError(f"{description} 的 tool_use.input 必须是 JSON object")

    # 扁平字段始终存在，兼容 shili/sharegpt.json 的读取方式。
    tool_call: dict[str, Any] = {
        "name": name,
        "arguments": copy.deepcopy(arguments),
    }
    if standard_structure:
        canonical_arguments = copy.deepcopy(arguments)
        tool_call.update(
            {
                "id": call_id,
                "type": "function",
                # function 是 Hugging Face/TRL 推荐的标准结构，也是事实源。
                "function": {
                    "name": name,
                    "arguments": canonical_arguments,
                },
            }
        )
        validate_hybrid_tool_call(tool_call)
    return tool_call


def validate_hybrid_tool_definition(tool: dict[str, Any]) -> None:
    function = require_dict(tool.get("function"), "tool.function")
    if tool.get("type") != "function":
        raise ValueError("tool.type 必须是 function")
    for key in ("name", "description", "parameters"):
        if tool.get(key) != function.get(key):
            raise ValueError(f"tool.{key} 与 tool.function.{key} 不一致")


def make_tool_definition(
    tool: dict[str, Any], index: int, standard_structure: bool
) -> dict[str, Any]:
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"request.tools[{index}].name 必须是非空字符串")
    description = tool.get("description")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise ValueError(f"request.tools[{index}].description 必须是字符串")
    parameters = tool.get("input_schema", tool.get("parameters", {}))
    if not isinstance(parameters, dict):
        raise ValueError(f"request.tools[{index}] 的 input_schema/parameters 必须是对象")

    # 扁平字段始终存在，兼容示例格式。
    result: dict[str, Any] = {
        "name": name,
        "description": description,
        "parameters": copy.deepcopy(parameters),
    }
    if standard_structure:
        canonical_parameters = copy.deepcopy(parameters)
        result.update(
            {
                "type": "function",
                # 标准嵌套字段是事实源。
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": canonical_parameters,
                },
            }
        )
        validate_hybrid_tool_definition(result)
    return result


def text_from_blocks(value: Any, description: str) -> str:
    """把字符串或纯 text block 列表转换为 SFT tool/user 文本。"""

    if isinstance(value, str):
        return value
    blocks = require_list(value, description)
    parts: list[str] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != "text":
            block_type = block.get("type") if isinstance(block, dict) else type(block).__name__
            raise ValueError(
                f"{description}[{index}] 是不支持的非文本 block: {block_type!r}"
            )
        text = block.get("text")
        if not isinstance(text, str):
            raise ValueError(f"{description}[{index}].text 必须是字符串")
        parts.append(text)
    return "".join(parts)


def convert_assistant_content(
    content: Any,
    reasoning_mode: str,
    standard_structure: bool,
    tool_names_by_id: dict[str, str],
    description: str,
) -> dict[str, Any]:
    """转换 Anthropic assistant content blocks，并登记 tool call id/name。"""

    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    blocks = require_list(content, description)
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    has_thinking_block = False
    tool_calls: list[dict[str, Any]] = []

    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ValueError(f"{description}[{index}] 必须是对象")
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError(f"{description}[{index}].text 必须是字符串")
            text_parts.append(text)
        elif block_type == "thinking":
            thinking = block.get("thinking")
            if not isinstance(thinking, str):
                raise ValueError(f"{description}[{index}].thinking 必须是字符串")
            has_thinking_block = True
            thinking_parts.append(thinking)
        elif block_type == "tool_use":
            tool_call = make_tool_call(
                block,
                f"{description}[{index}]",
                standard_structure,
            )
            # 即使关闭标准结构，原始 id 仍在转换过程中用于关联 tool_result；
            # 只是最终示例格式不把该 id 写入 JSON。
            call_id = block["id"]
            name = block["name"]
            previous_name = tool_names_by_id.get(call_id)
            if previous_name is not None and previous_name != name:
                raise ValueError(f"同一 tool call id 对应不同工具名: {call_id}")
            tool_names_by_id[call_id] = name
            tool_calls.append(tool_call)
        else:
            raise ValueError(f"{description}[{index}] 是不支持的 block: {block_type!r}")

    text = "".join(text_parts)
    reasoning = "\n".join(thinking_parts)
    message: dict[str, Any] = {"role": "assistant"}
    if reasoning_mode == "separate":
        if has_thinking_block:
            message["reasoning_content"] = reasoning
        message["content"] = text
    elif reasoning_mode == "inline":
        if has_thinking_block:
            inline_reasoning = f"<think>\n{reasoning}\n</think>"
            message["content"] = (
                inline_reasoning + ("\n\n" + text if text else "")
            )
        else:
            message["content"] = text
    else:
        raise ValueError(f"不支持的 reasoning mode: {reasoning_mode}")

    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def convert_tool_result(
    block: dict[str, Any],
    tool_names_by_id: dict[str, str],
    description: str,
    standard_structure: bool,
) -> dict[str, Any]:
    call_id = block.get("tool_use_id")
    if not isinstance(call_id, str) or not call_id:
        raise ValueError(f"{description}.tool_use_id 必须是非空字符串")
    name = tool_names_by_id.get(call_id)
    if name is None:
        raise ValueError(f"{description} 找不到对应的 tool_use: {call_id}")
    content = text_from_blocks(block.get("content", ""), f"{description}.content")
    if not standard_structure:
        # 严格保持 shili/sharegpt.json 的 tool 消息形态。
        return {"role": "tool", "content": content}
    return {
        "role": "tool",
        "name": name,
        "tool_call_id": call_id,
        "content": content,
        "is_error": bool(block.get("is_error", False)),
    }


def convert_user_content(
    content: Any,
    tool_names_by_id: dict[str, str],
    description: str,
    standard_structure: bool,
) -> list[dict[str, Any]]:
    """保留 user text 与一个或多个 tool_result block 的原始先后顺序。"""

    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    blocks = require_list(content, description)
    messages: list[dict[str, Any]] = []
    pending_text: list[str] = []

    def flush_text() -> None:
        if pending_text:
            messages.append({"role": "user", "content": "".join(pending_text)})
            pending_text.clear()

    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ValueError(f"{description}[{index}] 必须是对象")
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise ValueError(f"{description}[{index}].text 必须是字符串")
            pending_text.append(text)
        elif block_type == "tool_result":
            flush_text()
            messages.append(
                convert_tool_result(
                    block,
                    tool_names_by_id,
                    f"{description}[{index}]",
                    standard_structure,
                )
            )
        else:
            raise ValueError(f"{description}[{index}] 是不支持的 block: {block_type!r}")
    flush_text()
    return messages


def convert_history_messages(
    messages: Any, reasoning_mode: str, standard_structure: bool
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """转换 request.messages，并返回已知 tool call id 到工具名的映射。"""

    source_messages = require_list(messages, "request.messages")
    result: list[dict[str, Any]] = []
    tool_names_by_id: dict[str, str] = {}
    for index, message in enumerate(source_messages):
        if not isinstance(message, dict):
            raise ValueError(f"request.messages[{index}] 必须是对象")
        role = message.get("role")
        content = message.get("content", "")
        if role == "user":
            result.extend(
                convert_user_content(
                    content,
                    tool_names_by_id,
                    f"request.messages[{index}].content",
                    standard_structure,
                )
            )
        elif role == "assistant":
            result.append(
                convert_assistant_content(
                    content,
                    reasoning_mode,
                    standard_structure,
                    tool_names_by_id,
                    f"request.messages[{index}].content",
                )
            )
        else:
            raise ValueError(f"request.messages[{index}].role 不支持: {role!r}")
    return result, tool_names_by_id


def system_text(system: Any) -> str:
    """把 Anthropic system 字符串/文本 block 列表合成一条 system 消息。"""

    if system is None:
        return ""
    if isinstance(system, str):
        return "" if system.lstrip().startswith(BILLING_HEADER_PREFIX) else system
    blocks = require_list(system, "request.system")
    parts: list[str] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict) or block.get("type") != "text":
            block_type = block.get("type") if isinstance(block, dict) else type(block).__name__
            raise ValueError(f"request.system[{index}] 不支持: {block_type!r}")
        text = block.get("text")
        if not isinstance(text, str):
            raise ValueError(f"request.system[{index}].text 必须是字符串")
        if text.lstrip().startswith(BILLING_HEADER_PREFIX):
            continue
        parts.append(text)
    return "\n".join(parts)


def build_sharegpt_record(
    segment: list[RoundRecord],
    record_id: str,
    reasoning_mode: str,
    standard_structure: bool = True,
) -> dict[str, Any]:
    """用连续 segment 最后一轮 request 历史加最后 response 构建完整对话。"""

    if not segment:
        raise ValueError("不能从空 segment 构建 ShareGPT")
    last_round = segment[-1]
    request = last_round.request_body

    messages: list[dict[str, Any]] = []
    converted_system = system_text(request.get("system"))
    if converted_system:
        messages.append({"role": "system", "content": converted_system})

    history, tool_names_by_id = convert_history_messages(
        request.get("messages"), reasoning_mode, standard_structure
    )
    messages.extend(history)
    messages.append(
        convert_assistant_content(
            last_round.response_message.get("content"),
            reasoning_mode,
            standard_structure,
            tool_names_by_id,
            f"{last_round.round_dir}/response.message.content",
        )
    )

    source_tools = request.get("tools", [])
    tools = [
        make_tool_definition(
            require_dict(tool, f"request.tools[{index}]"),
            index,
            standard_structure,
        )
        for index, tool in enumerate(require_list(source_tools, "request.tools"))
    ]
    return {
        "id": record_id,
        "messages": messages,
        "tools": tools,
    }


def resolve_tasks_root(input_dir: Path) -> Path:
    tasks_root = input_dir / "tasks"
    if not tasks_root.is_dir():
        raise ValueError(f"输入目录缺少 tasks/ 子目录: {input_dir}")
    return tasks_root


def ensure_empty_output(output_dir: Path) -> None:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"输出目录不是空目录: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)


def discover_agent_dirs(task_dir: Path) -> list[Path]:
    agents = [
        path
        for path in task_dir.iterdir()
        if path.is_dir()
        and (path.name == "main_agent" or path.name.startswith("subagent_"))
    ]
    return sorted(agents, key=lambda path: (path.name != "main_agent", path.name))


def export_sharegpt(
    input_dir: Path,
    output_dir: Path,
    reasoning_mode: str,
    standard_structure: bool = True,
) -> dict[str, Any]:
    if reasoning_mode not in REASONING_MODES:
        raise ValueError(
            f"reasoning_mode 必须是 {', '.join(REASONING_MODES)}，收到 {reasoning_mode!r}"
        )
    tasks_root = resolve_tasks_root(input_dir)
    ensure_empty_output(output_dir)

    task_count = 0
    agent_count = 0
    round_count = 0
    file_count = 0
    split_count = 0
    errors: list[dict[str, Any]] = []

    for task_dir in sorted(path for path in tasks_root.iterdir() if path.is_dir()):
        agent_dirs = discover_agent_dirs(task_dir)
        if not agent_dirs:
            continue
        task_count += 1
        task_output = output_dir / task_dir.name
        task_output.mkdir(parents=True, exist_ok=False)

        for agent_dir in agent_dirs:
            agent_count += 1
            try:
                rounds = load_agent_rounds(agent_dir)
            except IncompleteResponseError as exc:
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
            round_count += len(rounds)
            segments = split_rounds_by_context(rounds)
            split_count += max(0, len(segments) - 1)
            for segment_number, segment in enumerate(segments, 1):
                file_stem = f"{agent_dir.name}_{segment_number}"
                record_id = f"{task_dir.name}__{agent_dir.name}__{segment_number}"
                record = build_sharegpt_record(
                    segment,
                    record_id,
                    reasoning_mode,
                    standard_structure,
                )
                # SFT 文件使用与 shili/sharegpt.json 相同的紧凑单行 JSON；代理原始
                # 采集和 finalize 产物仍保持默认的缩进格式，便于人工审计。
                write_json(task_output / f"{file_stem}.json", record, compact=True)
                file_count += 1

    error_report_path = output_dir / "export_errors.json"
    write_json(
        error_report_path,
        {"error_count": len(errors), "errors": errors},
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
        "skipped_agents": len(errors),
        "error_report": str(error_report_path.resolve()),
        "format": (
            "hf-tool-calling-with-flat-aliases"
            if standard_structure
            else "example-flat-tool-calling"
        ),
    }


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 finalize 输出的 task/agent/round 数据集转换为混合兼容 ShareGPT SFT 文件"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        required=True,
        help="finalize.py/finalize-harbor.py 的 --output-dir，目录下必须包含 tasks/",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="ShareGPT 输出根目录；必须不存在或为空",
    )
    parser.add_argument(
        "--reasoning-mode",
        choices=REASONING_MODES,
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
        help="生成标准 type/function 结构并同时保留示例扁平别名（默认）",
    )
    standard_group.add_argument(
        "--no-standard-structure",
        dest="standard_structure",
        action="store_false",
        help="关闭标准结构，只生成 shili/sharegpt.json 使用的示例扁平格式",
    )
    parser.set_defaults(standard_structure=True)
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    report = export_sharegpt(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        reasoning_mode=args.reasoning_mode,
        standard_structure=args.standard_structure,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
