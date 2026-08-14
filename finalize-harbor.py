from __future__ import annotations

"""整理 Harbor jobs 目录中的 Claude Code 轨迹。

本脚本适配下面这种目录布局，其中 job 根目录的直接子目录名就是 task_id：

    <job-root>/<task_id>/agent/sessions/projects/**/*.jsonl
    <job-root>/result.json

HTTP capture 与 session 的关联算法复用 finalize.py，仍然只通过 message.id 精确匹配。
"""

import argparse
import json
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
    # "transport",  # 请求方法、URL、header、时间、客户端等 HTTP 元数据
    "body",  # 请求原始 body 的 JSON/UTF-8/Base64 表示、大小和 SHA-256
    # "provenance",  # 本记录对应的原始 capture 目录和 body 文件
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
    # "sse_events",  # 按接收顺序解析出的 SSE 事件；非流式响应为空列表
    # "body",  # 原始响应 body（流式时是原始 SSE）的可逆表示和 SHA-256
    # "state",  # complete/partial、传输错误、客户端断开等采集状态
    # "provenance",  # 本记录对应的原始 capture 目录和 body 文件
]


def load_result_reward_task_ids(result_path: Path) -> tuple[set[str], set[str]]:
    """从 job 根目录的 result.json 读取 reward=1.0/0.0 的 task ID。"""

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"无法读取 Harbor result.json: {result_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Harbor result.json 不是有效 JSON: {result_path}: {exc}") from exc

    evals = result.get("stats", {}).get("evals")
    if not isinstance(evals, dict):
        raise ValueError(f"Harbor result.json 缺少 stats.evals: {result_path}")

    successful: set[str] = set()
    failed: set[str] = set()
    found_reward = False
    for eval_name, eval_result in evals.items():
        if not isinstance(eval_result, dict):
            continue
        reward = eval_result.get("reward_stats", {}).get("reward")
        if not isinstance(reward, dict):
            continue
        found_reward = True
        for reward_key, destination in (("1.0", successful), ("0.0", failed)):
            task_ids = reward.get(reward_key, [])
            if not isinstance(task_ids, list) or not all(
                isinstance(task_id, str) for task_id in task_ids
            ):
                raise ValueError(
                    f"stats.evals.{eval_name}.reward_stats.reward.{reward_key} "
                    "必须是 task ID 字符串数组"
                )
            destination.update(task_ids)

    if not found_reward:
        raise ValueError(f"Harbor result.json 中没有 reward_stats.reward: {result_path}")
    # 成功只由 1.0 集合决定。即使 result.json 的聚合数据异常，导致某个
    # task 同时出现在 1.0 和 0.0 中，也保留 1.0 的成功结论。
    failed.difference_update(successful)
    return successful, failed


def discover_harbor_job_session_files(harbor_root: Path) -> list[Path]:
    """按 Harbor jobs 布局寻找所有主 agent 和子 agent session。

    ``harbor_root`` 必须是一次 job 的根目录，例如：

        /data1/nfs/ztr/run/jobs/2026-08-03__10-38-16

    它的每个直接子目录被视为一个 task。脚本只在
    ``<task>/agent/sessions/projects`` 下递归寻找 ``*.jsonl``，所以不会误读 job
    根目录或 task 目录中的 config/result/trajectory 等其他 JSON/JSONL 文件。
    ``projects`` 内的主 session 和更深层的 ``subagents/agent-*.jsonl`` 都能找到。
    """

    if not harbor_root.is_dir():
        raise NotADirectoryError(f"--harbor-run-dir 不是目录: {harbor_root}")

    session_files: list[Path] = []
    for task_dir in sorted(path for path in harbor_root.iterdir() if path.is_dir()):
        projects_root = task_dir / "agent" / "sessions" / "projects"
        if not projects_root.is_dir():
            continue
        session_files.extend(projects_root.rglob("*.jsonl"))
    return sorted(session_files)


def harbor_job_task_context(path: Path, harbor_root: Path) -> tuple[str, Path]:
    """从 ``<job-root>/<task_id>/...`` 提取 task_id 和 task 根目录。

    与 finalize.py 的旧布局不同，这里没有中间的 ``tasks/`` 目录。例如
    ``cve-2010-5312__oGPeD8H`` 会被完整保留为 task_id。
    """

    try:
        relative = path.resolve().relative_to(harbor_root.resolve())
    except ValueError as exc:
        raise ValueError(f"session 不在 Harbor job 根目录内: {path}") from exc

    parts = relative.parts
    expected_prefix = ("agent", "sessions", "projects")
    if len(parts) < 5 or tuple(parts[1:4]) != expected_prefix:
        raise ValueError(
            "session 路径不符合 <job>/<task_id>/agent/sessions/projects/**.jsonl: "
            f"{path}"
        )
    task_id = parts[0]
    return task_id, harbor_root / task_id


def make_successful_location_filter(
    successful_task_ids: set[str],
) -> Callable[[SessionLocation], bool]:
    """只保留 result.json 中 reward=1.0 的 task。"""

    return lambda location: location.task_id in successful_task_ids


def make_result_output_group_resolver(
    successful_task_ids: set[str],
) -> Callable[[SessionLocation], str]:
    """已关联 session 的 task 中，1.0 输出到 successful，其他输出到 failed。"""

    return lambda location: (
        "successful/tasks"
        if location.task_id in successful_task_ids
        else "failed/tasks"
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按 Harbor jobs 目录布局关联代理采集数据并生成 task/agent/round 数据集"
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
            "Harbor job 根目录；其直接子目录名是 task_id，session 位于 "
            "<task_id>/agent/sessions/projects"
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
        help="只转换 result.json 中 reward=1.0 的成功 task 轨迹",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    session_files = discover_harbor_job_session_files(args.harbor_run_dir)
    successful_task_ids, failed_task_ids = load_result_reward_task_ids(
        args.harbor_run_dir / "result.json"
    )
    location_filter = (
        make_successful_location_filter(successful_task_ids)
        if args.only_successful
        else None
    )
    report = finalize_dataset(
        capture_root=args.capture_dir,
        harbor_root=args.harbor_run_dir,
        output_root=args.output_dir,
        session_files=session_files,
        task_context_resolver=harbor_job_task_context,
        location_filter=location_filter,
        task_output_group_resolver=make_result_output_group_resolver(
            successful_task_ids
        ),
        # 显式传入本文件的白名单，使 finalize-harbor.py 可以独立控制输出字段。
        request_output_parts=FINAL_REQUEST_PARTS,
        response_output_parts=FINAL_RESPONSE_PARTS,
    )
    report.update(
        {
            "harbor_layout": "jobs/<task_id>/agent/sessions/projects",
            "only_successful": args.only_successful,
            "discovered_session_files": len(session_files),
            # 这两项只是 result.json 中的分类清单大小，不是导出
            # 轨迹数。实际轨迹数以 task_output_groups 为准；无 session
            # 的异常 task 不会进入该统计。
            "result_reward_catalog": {
                "1.0": len(successful_task_ids),
                "0.0": len(failed_task_ids),
            },
            "exported_trajectory_tasks": {
                "successful": report.get("task_output_groups", {}).get(
                    "successful/tasks", 0
                ),
                "failed": report.get("task_output_groups", {}).get(
                    "failed/tasks", 0
                ),
            },
        }
    )
    # 即使某一类没有轨迹，也保持稳定的两目录输出结构。
    (args.output_dir / "successful" / "tasks").mkdir(parents=True, exist_ok=True)
    (args.output_dir / "failed" / "tasks").mkdir(parents=True, exist_ok=True)
    write_json(args.output_dir / "finalization_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
