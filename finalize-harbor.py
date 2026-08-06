from __future__ import annotations

"""整理 Harbor jobs 目录中的 Claude Code 轨迹。

本脚本适配下面这种目录布局，其中 job 根目录的直接子目录名就是 task_id：

    <job-root>/<task_id>/agent/sessions/projects/**/*.jsonl
    <job-root>/<task_id>/verifier/reward.txt

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


def reward_content_is_success(reward_content: str) -> bool:
    """把 reward.txt 的字符串内容转换为任务是否成功。

    Harbor verifier 当前写入 ``0`` 或 ``1``。读取文件产生的首尾空白和换行会被
    忽略；去除空白后严格等于 ``1`` 才返回 True，``0``、空内容和其他值均返回
    False。将判断单独封装后，也可以直接对已读取的字符串做单元测试。
    """

    return reward_content.strip() == "1"


def read_task_success(task_dir: Path) -> bool:
    """读取 ``<task_id>/verifier/reward.txt`` 并判断该 task 是否成功。

    reward 文件不存在或无法读取时按失败处理。这样启用 ``--only-successful`` 后，
    不会因为 verifier 结果缺失而错误保留轨迹。
    """

    reward_path = task_dir / "verifier" / "reward.txt"
    try:
        reward_content = reward_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return reward_content_is_success(reward_content)


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


def make_successful_location_filter() -> Callable[[SessionLocation], bool]:
    """创建带缓存的成功 task 过滤器，避免每个 round 重复读取 reward.txt。"""

    success_cache: dict[Path, bool] = {}

    def is_successful(location: SessionLocation) -> bool:
        task_dir = Path(location.task_dir)
        if task_dir not in success_cache:
            success_cache[task_dir] = read_task_success(task_dir)
        return success_cache[task_dir]

    return is_successful


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
        help="只转换 verifier/reward.txt 内容为 1 的成功 task 轨迹",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    session_files = discover_harbor_job_session_files(args.harbor_run_dir)
    location_filter = (
        make_successful_location_filter() if args.only_successful else None
    )
    report = finalize_dataset(
        capture_root=args.capture_dir,
        harbor_root=args.harbor_run_dir,
        output_root=args.output_dir,
        session_files=session_files,
        task_context_resolver=harbor_job_task_context,
        location_filter=location_filter,
        # 显式传入本文件的白名单，使 finalize-harbor.py 可以独立控制输出字段。
        request_output_parts=FINAL_REQUEST_PARTS,
        response_output_parts=FINAL_RESPONSE_PARTS,
    )
    report.update(
        {
            "harbor_layout": "jobs/<task_id>/agent/sessions/projects",
            "only_successful": args.only_successful,
            "discovered_session_files": len(session_files),
        }
    )
    write_json(args.output_dir / "finalization_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
