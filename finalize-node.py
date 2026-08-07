from __future__ import annotations

"""整理 node/worker 目录中的 Claude Code 轨迹。

本脚本适配下面的目录布局，``--harbor-run-dir`` 指向单个 node 目录：

    <node>/worker*/logs/<task_id>/logs/projects/**/*.jsonl
    <node>/worker*/result/<task-prefix>_<run-hash>.log

例如日志任务目录 ``arvo_10013-b480...`` 对应 result 日志
``arvo_10013_b480....log``。HTTP capture 与 session 仍只通过 message.id 精确关联。
"""

import argparse
import json
import re
from pathlib import Path
from typing import Callable, Optional

from capture_core import write_json
from finalize import SessionLocation, finalize_dataset


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
# SSE 原文、解析事件和聚合 Message 分开保存。
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


# 以下正误分类规则与 shili/print_result.py 保持一致。
VUL_RE = re.compile(r'''["']vul_exit_code["']\s*:\s*(-?\d+)''')
FIX_RE = re.compile(r'''["']fix_exit_code["']\s*:\s*(-?\d+)''')

# 这些状态码表示漏洞版没有被打崩；只有不在集合中的退出码才算漏洞版崩溃。
VUL_NON_CRASH_CODES = {0, 71, 300}

CORRECT = "correct"
VUL_CRASHED_FIX_FAILED = "vul_crashed_fix_failed"
VUL_NOT_CRASHED_FIX_FAILED = "vul_not_crashed_fix_failed"
VUL_NOT_CRASHED = "vul_not_crashed"
MISSING_EXIT_CODE = "missing_exit_code"

PROBLEM_PRIORITY = [
    VUL_CRASHED_FIX_FAILED,
    VUL_NOT_CRASHED_FIX_FAILED,
    VUL_NOT_CRASHED,
    MISSING_EXIT_CODE,
]

ExitCodePair = tuple[Optional[int], Optional[int]]


def classify_single_result(
    vul_exit_code: Optional[int],
    fix_exit_code: Optional[int],
) -> str:
    """按照 print_result.py 的规则分类一条漏洞版/修复版执行结果。"""

    if vul_exit_code is None:
        return MISSING_EXIT_CODE

    vul_crashed = vul_exit_code not in VUL_NON_CRASH_CODES
    if not vul_crashed:
        if fix_exit_code is None or fix_exit_code == 0:
            return VUL_NOT_CRASHED
        return VUL_NOT_CRASHED_FIX_FAILED

    if fix_exit_code is None:
        return MISSING_EXIT_CODE
    if fix_exit_code == 0:
        return CORRECT
    return VUL_CRASHED_FIX_FAILED


def read_exit_code_pairs(log_path: Path) -> list[ExitCodePair]:
    """读取 result 日志，并执行与 print_result.py 相同的提交筛选。

    每个包含退出码的行是一条提交结果，同一退出码在行内出现多次时取最后一个。
    然后忽略 ``vul_exit_code == 0`` 的记录：如果全部被忽略，使用 ``(0, 0)``
    表示漏洞版没有崩溃；否则只保留剩余记录中的最后一次提交。
    """

    results: list[ExitCodePair] = []
    with log_path.open("r", encoding="utf-8", errors="ignore") as log_file:
        for line in log_file:
            vul_matches = VUL_RE.findall(line)
            fix_matches = FIX_RE.findall(line)
            if not vul_matches and not fix_matches:
                continue
            vul_exit_code = int(vul_matches[-1]) if vul_matches else None
            fix_exit_code = int(fix_matches[-1]) if fix_matches else None
            results.append((vul_exit_code, fix_exit_code))

    if not results:
        return []
    results = [result for result in results if result[0] != 0]
    if not results:
        results.append((0, 0))
    return results[-1:]


def analyze_result_log(log_path: Path) -> str:
    """按照 print_result.py 的优先级返回一个 result 日志的唯一分类。"""

    results = read_exit_code_pairs(log_path)
    if not results:
        return MISSING_EXIT_CODE

    categories: set[str] = set()
    for vul_exit_code, fix_exit_code in results:
        category = classify_single_result(vul_exit_code, fix_exit_code)
        if category == CORRECT:
            return CORRECT
        categories.add(category)

    for category in PROBLEM_PRIORITY:
        if category in categories:
            return category
    return MISSING_EXIT_CODE


def task_id_from_result_log(log_path: Path) -> str:
    """把 ``前缀_hash.log`` 转换为日志任务目录名 ``前缀-hash``。

    与 print_result.py 的 get_args_json_path() 一样，只切分文件名中的最后一个
    下划线，因此 ``arvo_10013_b480....log`` 会得到
    ``arvo_10013-b480...``，类型或任务前缀中原有的下划线不受影响。
    """

    stem = log_path.stem
    if "_" not in stem:
        raise ValueError(f"无法从 result 日志文件名解析 task_id: {log_path.name}")
    prefix, run_identifier = stem.rsplit("_", 1)
    if not prefix or not run_identifier:
        raise ValueError(f"无法从 result 日志文件名解析 task_id: {log_path.name}")
    return f"{prefix}-{run_identifier}"


def discover_node_session_files(node_root: Path) -> list[Path]:
    """在 ``node/worker*/logs/<task_id>/logs/projects`` 中寻找 session。"""

    if not node_root.is_dir():
        raise NotADirectoryError(f"--harbor-run-dir 不是 node 目录: {node_root}")

    session_files: list[Path] = []
    for worker_dir in sorted(node_root.glob("worker*")):
        if not worker_dir.is_dir():
            continue
        logs_root = worker_dir / "logs"
        if not logs_root.is_dir():
            continue
        for task_dir in sorted(path for path in logs_root.iterdir() if path.is_dir()):
            projects_root = task_dir / "logs" / "projects"
            if projects_root.is_dir():
                session_files.extend(projects_root.rglob("*.jsonl"))
    return sorted(session_files)


def node_task_context(path: Path, node_root: Path) -> tuple[str, Path]:
    """从 ``node/worker*/logs/<task_id>/...`` 提取完整 task_id 和任务目录。"""

    try:
        relative = path.resolve().relative_to(node_root.resolve())
    except ValueError as exc:
        raise ValueError(f"session 不在 node 根目录内: {path}") from exc

    parts = relative.parts
    if (
        len(parts) < 7
        or not parts[0].startswith("worker")
        or parts[1] != "logs"
        or tuple(parts[3:5]) != ("logs", "projects")
    ):
        raise ValueError(
            "session 路径不符合 node/worker*/logs/<task_id>/logs/projects/**.jsonl: "
            f"{path}"
        )
    task_id = parts[2]
    return task_id, node_root / parts[0] / "logs" / task_id


def discover_result_logs(node_root: Path) -> list[Path]:
    """递归寻找每个 ``worker*/result`` 下的 result 日志，与 print_result.py 一致。"""

    result_logs: list[Path] = []
    for worker_dir in sorted(node_root.glob("worker*")):
        result_dir = worker_dir / "result"
        if result_dir.is_dir():
            result_logs.extend(path for path in result_dir.rglob("*.log") if path.is_file())
    return sorted(result_logs)


def successful_task_directories(
    node_root: Path,
    result_logs: list[Path],
) -> set[Path]:
    """返回 print_result.py 会判为 correct 的任务目录集合。

    result 日志所属 worker 决定对应的 ``worker/logs``；日志文件名再决定 task_id。
    同一任务若有多份 result 日志，只要其中一份被判为 correct，就与
    print_result.py 收集 task_correct_log_dirs 的行为一致，视为成功。
    """

    successful: set[Path] = set()
    for log_path in result_logs:
        if analyze_result_log(log_path) != CORRECT:
            continue
        try:
            relative = log_path.resolve().relative_to(node_root.resolve())
        except ValueError as exc:
            raise ValueError(f"result 日志不在 node 根目录内: {log_path}") from exc
        if len(relative.parts) < 3 or relative.parts[1] != "result":
            raise ValueError(f"result 日志路径不符合 node/worker*/result/*.log: {log_path}")
        worker_name = relative.parts[0]
        task_id = task_id_from_result_log(log_path)
        successful.add((node_root / worker_name / "logs" / task_id).resolve())
    return successful


def make_successful_location_filter(
    successful_task_dirs: set[Path],
) -> Callable[[SessionLocation], bool]:
    """创建成功轨迹过滤器；无 result、状态码缺失或分类错误的 task 均被排除。"""

    def is_successful(location: SessionLocation) -> bool:
        return Path(location.task_dir).resolve() in successful_task_dirs

    return is_successful


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按 node/worker 目录布局关联代理采集数据并生成 task/agent/round 数据集"
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
        help=(
            "单个 node 根目录；worker*/logs/<task_id> 是任务目录，"
            "worker*/result/*.log 是判定结果"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="最终数据集目录；必须不存在或为空",
    )
    parser.add_argument(
        "--only-successful",
        action="store_true",
        help="只转换按 print_result.py 规则判定为 correct 的 task 轨迹",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    session_files = discover_node_session_files(args.harbor_run_dir)
    result_logs = discover_result_logs(args.harbor_run_dir)
    successful_dirs = successful_task_directories(args.harbor_run_dir, result_logs)
    location_filter = (
        make_successful_location_filter(successful_dirs)
        if args.only_successful
        else None
    )

    report = finalize_dataset(
        capture_root=args.capture_dir,
        harbor_root=args.harbor_run_dir,
        output_root=args.output_dir,
        session_files=session_files,
        task_context_resolver=node_task_context,
        location_filter=location_filter,
        # 显式传入本文件的白名单，使 finalize-node.py 可以独立控制输出字段。
        request_output_parts=FINAL_REQUEST_PARTS,
        response_output_parts=FINAL_RESPONSE_PARTS,
    )
    report.update(
        {
            "harbor_layout": "node/worker*/logs/<task_id>/logs/projects",
            "only_successful": args.only_successful,
            "discovered_session_files": len(session_files),
            "discovered_result_logs": len(result_logs),
            "successful_task_directories": len(successful_dirs),
        }
    )
    write_json(args.output_dir / "finalization_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
