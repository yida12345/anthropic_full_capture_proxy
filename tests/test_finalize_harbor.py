from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from capture_core import write_json
from finalize import finalize_dataset


MODULE_PATH = Path(__file__).resolve().parents[1] / "finalize-harbor.py"
SPEC = importlib.util.spec_from_file_location("finalize_harbor", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
finalize_harbor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(finalize_harbor)

TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".test_tmp"
TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)


class HarborResultClassificationTests(unittest.TestCase):
    def test_result_reward_lists_are_loaded_across_evals(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as temporary:
            result_path = Path(temporary) / "result.json"
            write_json(
                result_path,
                {
                    "stats": {
                        "evals": {
                            "eval-a": {
                                "reward_stats": {
                                    "reward": {
                                        "1.0": ["success-with-error"],
                                        "0.0": ["failed", "success-with-error"],
                                    }
                                }
                            },
                            "eval-b": {
                                "reward_stats": {
                                    "reward": {"1.0": ["success-b"], "0.0": []}
                                }
                            },
                        }
                    }
                },
            )

            successful, failed = finalize_harbor.load_result_reward_task_ids(
                result_path
            )

            self.assertEqual(successful, {"success-with-error", "success-b"})
            self.assertEqual(failed, {"failed"})

    def test_only_one_point_zero_determines_success(self):
        successful = {"success-even-if-runtime-errored"}
        resolver = finalize_harbor.make_result_output_group_resolver(successful)
        success_location = type(
            "Location", (), {"task_id": "success-even-if-runtime-errored"}
        )()
        unknown_location = type("Location", (), {"task_id": "no-reward"})()

        self.assertEqual(resolver(success_location), "successful/tasks")
        self.assertEqual(resolver(unknown_location), "failed/tasks")

    def test_task_without_session_is_not_exported_or_counted(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as temporary:
            root = Path(temporary)
            harbor_root = root / "job"
            capture_root = root / "captures"
            output_root = root / "output"
            harbor_root.mkdir()
            capture_root.mkdir()

            # result.json 中有 task，但 job 中没有它的 session 文件。
            successful = {"errored-without-session"}
            report = finalize_dataset(
                capture_root=capture_root,
                harbor_root=harbor_root,
                output_root=output_root,
                session_files=[],
                task_context_resolver=finalize_harbor.harbor_job_task_context,
                task_output_group_resolver=(
                    finalize_harbor.make_result_output_group_resolver(successful)
                ),
            )

            self.assertEqual(report["tasks"], 0)
            self.assertEqual(report["matched"], 0)
            self.assertEqual(report["task_output_groups"], {})
            self.assertFalse((output_root / "successful").exists())
            self.assertFalse((output_root / "failed").exists())


if __name__ == "__main__":
    unittest.main()
