from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import export_sharegpt_recovered as legacy
import export_sharegpt_recovered_merged as merged
from capture_core import write_json


def user(text):
    return {"role": "user", "content": text}


def assistant(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def thought(text="unfinished reasoning"):
    return [{"type": "thinking", "thinking": text}]


class MergedExporterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / "input"
        self.task = self.input / "tasks/task_a"

    def add_round(self, agent, number, messages, content, *, stop="end_turn", system="system",
                  model="model", tools=None, session="session_a", complete=True):
        directory = self.task / agent / f"round_{number:06d}"
        write_json(directory / "request.json", {
            "association": {"session_id": session},
            "body": {"json": {"model": model, "system": system,
                                "tools": tools or [], "messages": copy.deepcopy(messages)}},
        })
        write_json(directory / "response.json", {
            "message": {"id": f"{agent}_{number}", "role": "assistant", "content": copy.deepcopy(content),
                        "stop_reason": stop},
            "state": {"state": "complete" if complete else "partial"},
            "transport": {"aggregation_complete": complete},
        })
        return directory

    def export(self, **kwargs):
        output = self.root / "output"
        report = merged.export_sharegpt_recovered_merged(self.input, output, "separate", **kwargs)
        records = [json.loads(line) for line in (output / "sharegpt.jsonl").read_text().splitlines()]
        audit = json.loads((output / "optimization_report.json").read_text())
        return output, report, records, audit

    def pair(self, **compact_kwargs):
        self.add_round("main_agent", 1, [user("question")], assistant("answer")["content"])
        return self.add_round("subagent_acompact-auto", 1,
                              [user("question"), assistant("answer"), user("summarize")],
                              assistant("summary")["content"], **compact_kwargs)

    def session(self, events):
        path = self.root / "session.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in events))
        write_json(self.task / "main_agent/agent.json", {"session_id": "session_a", "session_files": [str(path)]})
        return path

    def test_merge_preserves_main_id_and_supervises_history_once(self):
        self.pair()
        output, report, records, audit = self.export()
        self.assertEqual(report["merged_compacts"], 1)
        self.assertEqual(report["combined_jsonl_records"], 1)
        self.assertEqual(records[0]["id"], "task_a__main_agent__1")
        self.assertEqual([m["content"] for m in records[0]["messages"] if m["role"] == "assistant"],
                         ["answer", "summary"])
        self.assertEqual(len(audit["merged_compacts"][0]["source_rounds"]), 2)
        self.assertFalse(list((output / "task_a").glob("subagent_*.json")))

    def test_recovered_compact_name_is_supported(self):
        directory = self.pair().parent
        directory.rename(directory.with_name("subagent_acompact-recovered-boundary"))
        _, report, _, _ = self.export()
        self.assertEqual(report["merged_compacts"], 1)

    def test_context_changes_and_missing_session_keep_separate_samples(self):
        for option in [{"system": "summary system"}, {"model": "other model"}, {"session": "other"},
                       {"session": None}]:
            with self.subTest(option=option), tempfile.TemporaryDirectory() as temporary:
                previous_root, previous_input, previous_task = self.root, self.input, self.task
                self.root = Path(temporary)
                self.input = self.root / "input"
                self.task = self.input / "tasks/task_a"
                self.pair(**option)
                _, report, records, _ = self.export()
                self.assertEqual(report["merged_compacts"], 0)
                self.assertEqual(len(records), 2)
                compact = next(r for r in records if "acompact" in r["id"])
                self.assertEqual([m["role"] for m in compact["messages"]],
                                 ["system", "user", "masked_assistant", "user", "assistant"])
                self.root, self.input, self.task = previous_root, previous_input, previous_task

    def test_tool_removal_prevents_merge(self):
        tool = {"name": "Bash", "description": "shell", "input_schema": {"type": "object"}}
        self.add_round("main_agent", 1, [user("q")], assistant("a")["content"], tools=[tool])
        self.add_round("subagent_acompact-auto", 1, [user("q"), assistant("a"), user("summary")],
                       assistant("s")["content"])
        _, report, records, _ = self.export()
        self.assertEqual(report["merged_compacts"], 0)
        self.assertEqual(len(records), 2)

    def test_added_unchanged_tools_allow_merge(self):
        self.pair(tools=[{"name": "Read", "input_schema": {"type": "object"}}])
        _, report, records, _ = self.export()
        self.assertEqual(report["merged_compacts"], 1)
        self.assertEqual(records[0]["tools"][0]["name"], "Read")

    def test_ambiguous_main_prefix_is_not_guessed(self):
        self.pair()
        directory = self.add_round("main_agent", 2, [user("question")], assistant("answer")["content"])
        request = json.loads((directory / "request.json").read_text())
        request["body"]["json"]["temperature"] = 0.5
        write_json(directory / "request.json", request)
        _, report, records, audit = self.export()
        self.assertEqual(report["merged_compacts"], 0)
        self.assertEqual(len(records), 3)
        self.assertEqual(audit["unmerged_compacts"][0]["reason"], "ambiguous_main_prefix")

    def test_one_main_can_only_be_consumed_once(self):
        self.pair()
        self.add_round("subagent_acompact-second", 1,
                       [user("question"), assistant("answer"), user("summarize")], assistant("summary 2")["content"])
        _, report, records, _ = self.export()
        self.assertEqual(report["merged_compacts"], 1)
        self.assertEqual(len(records), 2)
        self.assertEqual(sum(m["role"] == "assistant" and m["content"] == "answer"
                             for r in records for m in r["messages"]), 1)

    def test_different_tasks_are_never_paired(self):
        self.add_round("main_agent", 1, [user("q")], assistant("a")["content"])
        original = self.task
        self.task = self.input / "tasks/task_b"
        self.add_round("subagent_acompact-auto", 1, [user("q"), assistant("a"), user("summary")],
                       assistant("s")["content"])
        self.task = original
        _, report, records, _ = self.export()
        self.assertEqual(report["merged_compacts"], 0)
        self.assertEqual(len(records), 2)

    def test_discarded_thinking_does_not_duplicate_earlier_history(self):
        history = [user("q"), assistant("first answer"), user("continue")]
        self.add_round("main_agent", 1, [user("q")], assistant("first answer")["content"])
        self.add_round("main_agent", 2, history, thought(), stop="max_tokens")
        self.add_round("main_agent", 3, history + [user("output limit, continue")], assistant("done")["content"])
        _, report, records, _ = self.export()
        self.assertEqual(report["original_context_splits"], 1)
        self.assertEqual(report["context_splits"], 0)
        self.assertEqual(report["discarded_truncated_rounds"], 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(sum(m.get("content") == "first answer" for m in records[0]["messages"]), 1)
        self.assertFalse(any("unfinished reasoning" in str(m) for m in records[0]["messages"]))

    def test_replayed_thinking_is_kept_as_masked_context(self):
        self.add_round("main_agent", 1, [user("q")], thought(), stop="max_tokens")
        self.add_round("main_agent", 2,
                       [user("q"), {"role": "assistant", "content": thought()}, user("continue")],
                       assistant("done")["content"])
        _, report, records, _ = self.export()
        self.assertEqual(report["masked_truncated_history"], 1)
        self.assertEqual(records[0]["messages"][2]["role"], "masked_assistant")
        self.assertEqual(records[0]["messages"][2]["reasoning_content"], "unfinished reasoning")

    def test_merge_does_not_unmask_discarded_thinking(self):
        self.add_round("main_agent", 1, [user("q")], thought(), stop="max_tokens")
        history = [user("q"), {"role": "assistant", "content": thought()}, user("continue")]
        self.add_round("main_agent", 2, history, assistant("answer")["content"])
        self.add_round("subagent_acompact-auto", 1, history + [assistant("answer"), user("summarize")],
                       assistant("summary")["content"])
        _, report, records, _ = self.export()
        self.assertEqual(report["merged_compacts"], 1)
        self.assertEqual(records[0]["messages"][2]["role"], "masked_assistant")
        self.assertEqual([m["content"] for m in records[0]["messages"] if m["role"] == "assistant"],
                         ["answer", "summary"])

    def test_replayed_partial_text_is_kept_with_continuation(self):
        blocks = thought("completed reasoning") + [{"type": "text", "text": "partial sentence"}]
        self.add_round("main_agent", 1, [user("q")], blocks, stop="max_tokens")
        self.add_round("main_agent", 2, [user("q"), {"role": "assistant", "content": blocks}, user("continue")],
                       assistant("the rest")["content"])
        _, report, records, audit = self.export()
        self.assertEqual(report["discarded_truncated_rounds"], 0)
        self.assertEqual(report["context_splits"], 0)
        self.assertEqual(len(records), 1)
        self.assertEqual([m["content"] for m in records[0]["messages"] if m["role"] == "assistant"],
                         ["partial sentence", "the rest"])
        self.assertEqual(audit["truncations"][0]["action"], "keep_replayed_max_tokens")

    def test_terminal_partial_text_drops_only_terminal_target(self):
        self.add_round("main_agent", 1, [user("q")], assistant("valid answer")["content"])
        self.add_round("main_agent", 2, [user("q"), assistant("valid answer"), user("more")],
                       thought("complete reasoning") + [{"type": "text", "text": "unfinished"}], stop="max_tokens")
        _, report, records, _ = self.export()
        self.assertEqual(report["discarded_truncated_rounds"], 1)
        self.assertEqual(records[0]["messages"][-1]["content"], "valid answer")

    def test_partial_text_can_be_accepted_by_compact_request(self):
        blocks = thought("reason") + [{"type": "text", "text": "partial"}]
        self.add_round("main_agent", 1, [user("q")], blocks, stop="max_tokens")
        self.add_round("subagent_acompact-auto", 1,
                       [user("q"), {"role": "assistant", "content": blocks}, user("summarize")],
                       assistant("summary")["content"])
        _, report, records, _ = self.export()
        self.assertEqual(report["discarded_truncated_rounds"], 0)
        self.assertEqual(report["merged_compacts"], 1)
        self.assertEqual(len(records), 1)

    def test_normal_thinking_and_empty_text_tool_calls_are_preserved(self):
        self.add_round("main_agent", 1, [user("q")], thought("normal thinking"))
        tool = {"type": "tool_use", "id": "call", "name": "Read", "input": {"path": "a"}}
        self.add_round("main_agent", 2, [user("q"), {"role": "assistant", "content": thought("normal thinking")},
                                        user("read")], thought("use tool") + [tool], stop="tool_use")
        _, report, records, _ = self.export()
        self.assertEqual(report["discarded_truncated_rounds"], 0)
        self.assertEqual(records[0]["messages"][-1]["tool_calls"][0]["name"], "Read")

    def test_recovery_from_following_history_is_preserved(self):
        self.add_round("main_agent", 1, [user("q")], [], complete=False)
        self.add_round("main_agent", 2, [user("q"), assistant("recovered answer"), user("next")],
                       assistant("done")["content"])
        _, report, records, _ = self.export()
        self.assertEqual(report["recovered_rounds"], 1)
        self.assertEqual(records[0]["messages"][2]["content"], "recovered answer")

    def test_recovered_string_content_can_be_kept_after_truncation(self):
        self.add_round("main_agent", 1, [user("q")], thought("aggregation differs"), stop="max_tokens")
        self.add_round("main_agent", 2, [user("q"), {"role": "assistant", "content": "partial text"},
                                        user("continue")], assistant("done")["content"])
        _, report, records, _ = self.export()
        self.assertEqual(report["recovered_rounds"], 1)
        self.assertEqual(report["discarded_truncated_rounds"], 0)
        self.assertEqual(records[0]["messages"][2]["content"], "partial text")

    def test_partial_prefix_does_not_remove_longer_main(self):
        self.add_round("main_agent", 1, [user("q")], assistant("answer")["content"])
        self.add_round("main_agent", 2, [user("q"), assistant("answer"), user("more")],
                       assistant("longer answer")["content"])
        self.add_round("subagent_acompact-auto", 1,
                       [user("q"), assistant("answer"), user("summarize")], assistant("summary")["content"])
        _, report, records, _ = self.export()
        self.assertEqual(report["merged_compacts"], 0)
        self.assertEqual(len(records), 2)

    def test_other_subagents_are_not_merged(self):
        directory = self.pair().parent
        directory.rename(directory.with_name("subagent_explore"))
        _, report, records, _ = self.export()
        self.assertEqual(report["merged_compacts"], 0)
        self.assertEqual(len(records), 2)

    def test_agent_metadata_supplies_missing_round_session_identity(self):
        self.pair(session=None)
        write_json(self.task / "subagent_acompact-auto/agent.json", {"session_id": "session_a"})
        _, report, _, _ = self.export()
        self.assertEqual(report["merged_compacts"], 1)

    def test_time_order_compares_timezone_offsets(self):
        self.pair()
        for agent, timestamp in [("main_agent", "2026-01-01T10:00:00+08:00"),
                                 ("subagent_acompact-auto", "2026-01-01T03:00:00Z")]:
            path = self.task / agent / "round_000001/request.json"
            request = json.loads(path.read_text())
            request["association"]["first_timestamp"] = timestamp
            write_json(path, request)
        _, report, _, _ = self.export()
        self.assertEqual(report["merged_compacts"], 1)

    def test_incomplete_response_without_evidence_remains_an_error(self):
        self.add_round("main_agent", 1, [user("q")], thought(), complete=False)
        _, report, records, _ = self.export()
        self.assertEqual(report["skipped_agents"], 1)
        self.assertEqual(records, [])

    def test_complete_retry_replaces_incomplete_response(self):
        self.add_round("main_agent", 1, [user("q")], [], complete=False)
        self.add_round("main_agent", 2, [user("q")], assistant("retry succeeded")["content"])
        _, report, records, _ = self.export()
        self.assertEqual(report["recovered_rounds"], 1)
        self.assertEqual(records[0]["messages"][-1]["content"], "retry succeeded")

    def test_complete_end_turn_still_sanitizes_phantom_tool(self):
        self.add_round("main_agent", 1, [user("q")],
                       assistant("valid text")["content"] + [{"type": "tool_use", "id": "ghost"}])
        _, report, records, _ = self.export()
        self.assertEqual(report["recovered_rounds"], 1)
        self.assertEqual(records[0]["messages"][-1]["content"], "valid text")
        self.assertNotIn("tool_calls", records[0]["messages"][-1])

    def test_unknown_response_blocks_are_still_rejected(self):
        self.add_round("main_agent", 1, [user("q")], [{"type": "image", "source": {}}])
        with self.assertRaises(ValueError):
            self.export()

    def test_nonempty_output_is_still_rejected(self):
        self.pair()
        self.export()
        with self.assertRaises(FileExistsError):
            self.export()

    def test_limit_failure_is_separate_in_files_and_jsonl(self):
        self.add_round("main_agent", 1, [user("q")], assistant("valid answer")["content"])
        self.add_round("main_agent", 2, [user("q"), assistant("valid answer"), user("more")], thought(),
                       stop="max_tokens")
        self.session([{"type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
                       "isApiErrorMessage": True, "error": "max_output_tokens",
                       "message": {"content": [{"type": "text", "text": "output limit"}]}}])
        output, report, records, audit = self.export(task_group="failed")
        self.assertEqual(records, [])
        self.assertEqual(report["limit_exceeded_tasks"], 1)
        self.assertEqual(report["limit_exceeded_jsonl_records"], 1)
        isolated = json.loads((output / "limit_exceeded/sharegpt.jsonl").read_text())
        self.assertEqual(isolated["messages"][-1]["content"], "valid answer")
        self.assertTrue(audit["task_classifications"][0]["confirmed"])

    def test_missing_session_is_quarantined_without_claiming_confirmation(self):
        self.add_round("main_agent", 1, [user("q")], assistant("answer")["content"])
        _, report, records, audit = self.export(task_group="failed")
        self.assertEqual(records, [])
        self.assertEqual(report["limit_exceeded_tasks"], 1)
        self.assertFalse(audit["task_classifications"][0]["confirmed"])
        self.assertEqual(audit["task_classifications"][0]["reason"], "session_unavailable")

    def test_unreadable_session_is_also_quarantined(self):
        self.add_round("main_agent", 1, [user("q")], assistant("answer")["content"])
        path = self.session([])
        path.unlink()
        _, report, _, audit = self.export(task_group="failed")
        self.assertEqual(report["limit_exceeded_tasks"], 1)
        self.assertEqual(len(audit["task_classifications"][0]["session_errors"]), 1)

    def test_regular_and_isolated_tasks_have_disjoint_jsonl(self):
        self.add_round("main_agent", 1, [user("q")], assistant("isolated")["content"])
        original = self.task
        self.task = self.input / "tasks/task_b"
        self.add_round("main_agent", 1, [user("q")], assistant("regular")["content"])
        self.session([{"type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
                       "message": {"content": "regular"}}])
        self.task = original
        output, report, records, _ = self.export(task_group="failed")
        isolated = [json.loads(line) for line in (output / "limit_exceeded/sharegpt.jsonl").read_text().splitlines()]
        self.assertEqual([r["id"] for r in records], ["task_b__main_agent__1"])
        self.assertEqual([r["id"] for r in isolated], ["task_a__main_agent__1"])
        self.assertEqual(report["sharegpt_files"], 2)

    def test_inline_flat_export_keeps_format_when_merging(self):
        self.pair()
        report = merged.export_sharegpt_recovered_merged(self.input, self.root / "inline", "inline",
                                                        standard_structure=False)
        record = json.loads((self.root / "inline/sharegpt.jsonl").read_text())
        self.assertEqual(report["merged_compacts"], 1)
        self.assertFalse(any("reasoning_content" in m for m in record["messages"]))

    def test_all_discarded_still_reports_limit_task(self):
        self.add_round("main_agent", 1, [user("q")], thought(), stop="max_tokens")
        output, report, records, _ = self.export(task_group="failed")
        self.assertEqual(report["limit_exceeded_tasks"], 1)
        self.assertEqual(report["sharegpt_files"], 0)
        self.assertTrue((output / "limit_exceeded/task_a").is_dir())
        self.assertEqual((output / "limit_exceeded/sharegpt.jsonl").read_text(), "")

    def test_intermediate_limit_error_followed_by_answer_is_not_terminal(self):
        self.add_round("main_agent", 1, [user("q")], assistant("done")["content"])
        self.session([
            {"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "isApiErrorMessage": True,
             "error": "max_output_tokens", "message": {"content": "limit"}},
            {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z", "message": {"content": "done"}},
        ])
        _, report, records, _ = self.export(task_group="failed")
        self.assertEqual(report["limit_exceeded_tasks"], 0)
        self.assertEqual(len(records), 1)

    def test_context_limit_failure_and_automatic_group_detection(self):
        self.add_round("main_agent", 1, [user("q")], assistant("answer")["content"])
        self.session([{"type": "assistant", "timestamp": "2026-01-01T00:00:00Z", "isApiErrorMessage": True,
                       "error": "invalid_request", "message": {"content": "Prompt is too long"}}])
        write_json(self.task / "task.json", {"output_group": "failed/tasks"})
        _, report, _, audit = self.export()
        self.assertEqual(report["limit_exceeded_tasks"], 1)
        self.assertEqual(audit["task_classifications"][0]["reason"], "context_length_exceeded")

    def test_unmodified_case_matches_original_exporter(self):
        self.add_round("main_agent", 1, [user("q")], assistant("answer")["content"])
        _, _, records, _ = self.export()
        legacy_output = self.root / "legacy"
        legacy.export_sharegpt_recovered(self.input, legacy_output, "separate")
        self.assertEqual(records, [json.loads((legacy_output / "sharegpt.jsonl").read_text())])

    def test_cli_keeps_original_options_and_adds_explicit_controls(self):
        args = merged.parse_args(["--input-dir", "in", "--output-dir", "out", "--reasoning-mode", "inline",
                                  "--no-standard-structure", "--no-mask-compact-assistant-history",
                                  "--no-merge-compact-trajectories", "--task-group", "failed"])
        self.assertFalse(args.standard_structure)
        self.assertFalse(args.mask_compact_assistant_history)
        self.assertFalse(args.merge_compact_trajectories)
        self.assertEqual(args.task_group, "failed")


if __name__ == "__main__":
    unittest.main()
