"""依据 Kimi Wire/Context 与 Hermes 原生 SQLite schema 构造隔离会话。"""
from __future__ import annotations

import json
import hashlib
import sqlite3
import tempfile
import unittest
import asyncio
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from api_protocols import CHAT, RESPONSES, normalize_request, normalize_response, request_protocol
from capture_core import RequestCapture, write_json
from export_sharegpt import export_sharegpt, IncompleteResponseError, validate_completed_response
from export_sharegpt_recovered import export_sharegpt_recovered
from finalize import finalize_dataset, parse_args
from session_adapters import history_matches, response_matches, hermes_compaction_matches


def write_jsonl(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values), encoding="utf-8")


def chat_response(identifier, content, *, calls=None, reasoning=None, finish="stop"):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    if reasoning:
        message["reasoning_content"] = reasoning
    return {"id": identifier, "object": "chat.completion", "model": "test-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}]}


def capture(root, body, response=None, *, path="/v1/chat/completions", events=None, finalize_kwargs=None):
    item = RequestCapture(root / "captures" / "raw")
    item.start_request(method="POST", path=path, query="", url="http://proxy" + path,
        headers=[("Authorization", "Bearer local-test")], raw_body=json.dumps(body).encode(),
        upstream_url="http://upstream" + path, client_host=None, client_port=None)
    item.start_response(status_code=200, headers=[("content-type", "text/event-stream" if events is not None else "application/json")],
        is_sse=events is not None)
    if events is None:
        raw = json.dumps(response).encode()
    else:
        raw = b"".join(("data: " + (event if isinstance(event, str) else json.dumps(event)) + "\n\n").encode() for event in events)
    # UTF-8、事件行、JSON 和工具参数均跨网络块。
    for start in range(0, len(raw), 7):
        item.append_response(raw[start:start + 7])
    item.finalize(**(finalize_kwargs or {}))
    return item


def read_response(item):
    return json.loads((item.paths.completed_dir / "response.json").read_text())


def db_fixture(path, sessions, messages):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.executescript("""
            CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, model TEXT,
                model_config TEXT, system_prompt TEXT, parent_session_id TEXT, started_at REAL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                content TEXT, tool_calls TEXT, tool_call_id TEXT, tool_name TEXT,
                reasoning TEXT, reasoning_content TEXT, timestamp REAL,
                message_uid TEXT, active INTEGER, api_content TEXT, _compressed_summary INTEGER);
        """)
        for session in sessions:
            values = {"source": "cli", "model": "test-model", "started_at": 1, **session}
            keys = list(values)
            db.execute(f"INSERT INTO sessions({','.join(keys)}) VALUES({','.join('?' for _ in keys)})", [values[k] for k in keys])
        for i, message in enumerate(messages, 1):
            values = {"id": i, "timestamp": float(i), "active": 1, **message}
            if isinstance(values.get("tool_calls"), list):
                values["tool_calls"] = json.dumps(values["tool_calls"])
            keys = list(values)
            db.execute(f"INSERT INTO messages({','.join(keys)}) VALUES({','.join('?' for _ in keys)})", [values[k] for k in keys])
        db.commit()


class ProtocolTests(unittest.TestCase):
    def test_disconnect_after_terminal_marker_keeps_complete_but_early_disconnect_does_not(self):
        for done in (False, True):
            with self.subTest(done=done), tempfile.TemporaryDirectory() as temporary:
                events = [{"id": "closed", "choices": [{"index": 0,
                    "delta": {"content": "answer"}, "finish_reason": "stop"}]}]
                if done:
                    events.append("[DONE]")
                item = capture(Path(temporary), {"model": "test-model", "messages": []},
                    events=events, finalize_kwargs={"transport_error": "CancelledError: client closed",
                        "client_disconnected": True})
                state = json.loads((item.paths.completed_dir / "state.json").read_text())
                self.assertEqual(state["state"], "complete" if done else "partial")
                response = read_response(item)
                self.assertEqual(response["disconnect_after_complete"], done)
                self.assertEqual(response["transport_error"], "CancelledError: client closed")

    def test_disconnect_after_done_does_not_admit_corrupt_stream(self):
        with tempfile.TemporaryDirectory() as temporary:
            item = capture(Path(temporary), {"model": "test-model", "messages": []},
                events=["bad json", {"id": "bad", "choices": [{"index": 0,
                    "delta": {"content": "answer"}, "finish_reason": "stop"}]}, "[DONE]"],
                finalize_kwargs={"transport_error": "CancelledError: client closed", "client_disconnected": True})
            self.assertEqual(json.loads((item.paths.completed_dir / "state.json").read_text())["state"], "partial")

    def test_endpoint_detection_excludes_auxiliary(self):
        self.assertEqual(request_protocol("POST", "/coding/v1/chat/completions"), CHAT)
        self.assertEqual(request_protocol("POST", "/v1/responses"), RESPONSES)
        self.assertIsNone(request_protocol("POST", "/v1/messages/count_tokens"))
        self.assertIsNone(request_protocol("GET", "/v1/chat/completions"))

    def test_chat_sse_thinking_and_fragmented_tool_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = [
                {"id": "chat_1", "model": "test-model", "choices": [{"index": 0, "delta": {"reasoning_content": "想一想"}}]},
                {"id": "chat_1", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read", "arguments": '{"path":'}}]}}]},
                {"id": "chat_1", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"例子"}'}}]}, "finish_reason": "tool_calls"}]},
                {"id": "chat_1", "choices": [], "usage": {"completion_tokens": 10}}, "[DONE]",
            ]
            item = capture(root, {"model": "test-model", "messages": []}, events=events)
            response = read_response(item)
            self.assertTrue(response["aggregation_complete"])
            self.assertEqual(json.loads((item.paths.completed_dir / "state.json").read_text())["state"], "complete")
            native = response["message"]
            self.assertEqual(native["usage"]["completion_tokens"], 10)
            normalized = normalize_response(native, CHAT)
            self.assertEqual(normalized["content"][0]["thinking"], "想一想")
            self.assertEqual(normalized["content"][1]["input"], {"path": "例子"})

    def test_chat_missing_done_or_finish_is_partial(self):
        cases = [
            [{"id": "x", "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}]}],
            [{"id": "x", "choices": [{"index": 0, "delta": {"content": "x"}}]}, "[DONE]"],
            ["{broken", {"id": "x", "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}]}, "[DONE]"],
        ]
        for events in cases:
            with self.subTest(events=events), tempfile.TemporaryDirectory() as temporary:
                item = capture(Path(temporary), {"messages": []}, events=events)
                self.assertFalse(read_response(item)["aggregation_complete"])
                self.assertEqual(json.loads((item.paths.completed_dir / "state.json").read_text())["state"], "partial")

    def test_responses_completion_and_partial(self):
        completed = {"id": "resp_1", "object": "response", "status": "completed", "model": "test-model",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "答案"}]}]}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = [{"type": "response.created", "response": {"id": "resp_1", "status": "in_progress", "output": []}},
                {"type": "response.completed", "response": completed}]
            item = capture(root, {"input": "问题"}, path="/v1/responses", events=events)
            self.assertEqual(read_response(item)["message"], completed)
            self.assertTrue(read_response(item)["aggregation_complete"])
            self.assertEqual(json.loads((item.paths.completed_dir / "state.json").read_text())["state"], "complete")
            partial = capture(root, {"input": "问题"}, path="/v1/responses", events=events[:1])
            self.assertEqual(read_response(partial)["message_id"], "resp_1")
            self.assertFalse(read_response(partial)["aggregation_complete"])

    def test_anthropic_errors_cannot_be_marked_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            item = capture(Path(temporary), {"messages": []}, path="/v1/messages", events=[
                {"type": "message_start", "message": {"id": "x", "content": []}},
                {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "lost"}},
                {"type": "message_stop"},
            ])
            self.assertFalse(read_response(item)["aggregation_complete"])
            with self.assertRaises(IncompleteResponseError):
                validate_completed_response({"state": {"state": "complete"}, "transport": {"aggregation_errors": ["bad"]}}, Path("fixture"))

    def test_unsupported_stateful_and_multiple_choices_are_rejected(self):
        with self.assertRaises(ValueError):
            normalize_request({"input": "next", "previous_response_id": "resp_old"}, RESPONSES)
        response = chat_response("x", "text")
        response["choices"].append(response["choices"][0])
        with self.assertRaises(ValueError):
            normalize_response(response, CHAT)


class SessionAdapterTests(unittest.TestCase):
    def test_hermes_reasoning_pad_is_only_an_empty_history_projection(self):
        stored = [{"role": "assistant", "content": [{"type": "text", "text": "answer"}]}]
        replay = [{"role": "assistant", "content": [{"type": "thinking", "thinking": " "},
            {"type": "text", "text": "answer"}]}]
        self.assertTrue(history_matches(stored, replay, "hermes"))
        self.assertFalse(response_matches(stored, replay, "hermes"))
        self.assertTrue(response_matches(replay, stored, "hermes"))
        replay[0]["content"][0]["thinking"] = "real reasoning"
        self.assertFalse(history_matches(stored, replay, "hermes"))

    def test_hermes_compact_merged_tail_requires_exact_native_user(self):
        request = {"messages": [{"role": "user", "content": [{"type": "text", "text":
            "You are a summarization agent creating a context checkpoint."}]}]}
        answer = [{"role": "assistant", "content": [{"type": "text", "text": "exact summary"}]}]
        boundary = {"summary": "[CONTEXT COMPACTION — REFERENCE ONLY] wrapper\nexact summary\n\n"
            "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---\n\nreal user"}
        self.assertTrue(hermes_compaction_matches(request, answer, {"source_users": ["real user"]}, boundary))
        self.assertFalse(hermes_compaction_matches(request, answer, {"source_users": ["different user"]}, boundary))

    def test_hermes_rotation_copy_uid_keeps_one_original_round(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [{"session_id": sid, "role": role, "content": text, "message_uid": uid}
                for sid in ("parent", "child") for role, text, uid in (
                    ("user", "same request", "u"), ("assistant", "same answer", "a"))]
            db_fixture(root / "state.db", [{"id": "parent"}, {"id": "child", "parent_session_id": "parent"}], rows)
            capture(root, {"model": "test-model", "messages": [{"role": "user", "content": "same request"}]},
                chat_response("original", "same answer"))
            report = finalize_dataset(root / "captures", root / "state.db", root / "dataset", client="hermes")
            self.assertEqual((report["matched"], report["conflicts"]), (1, 0))
            response = json.loads(next((root / "dataset/tasks").rglob("response.json")).read_text())
            self.assertEqual(response["association"]["session_id"], "parent")

    def test_kimi_native_compaction_requires_prompt_summary_and_request_fingerprints(self):
        for broken in (None, "summary", "prompt", "system", "tools", "completion"):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                system = "native system"
                request = {"model": "test-model", "system": system, "tools": [], "messages": [
                    {"role": "user", "content": "original user"},
                    {"role": "assistant", "content": "original answer"},
                    {"role": "user", "content": "You are about to run out of context. Create a handoff summary for the\nmodel that will resume this task after the earlier conversation is cleared."}]}
                events = [{"type": "full_compaction.begin", "agentId": "main", "source": "manual"},
                    {"type": "llm.request", "agentId": "main", "kind": "compaction", "model": "test-model",
                     "systemPromptHash": hashlib.sha256(system.encode()).hexdigest(),
                     "toolsHash": hashlib.sha256(b"[]").hexdigest()},
                    {"type": "context.apply_compaction", "agentId": "main", "summary": "native summary"},
                    {"type": "full_compaction.complete", "agentId": "main"}]
                if broken == "summary":
                    events[2]["summary"] = "different"
                elif broken == "prompt":
                    request["messages"][-1]["content"] = "ordinary conversation"
                elif broken == "system":
                    request["system"] = "wrong system"
                elif broken == "tools":
                    events[1]["toolsHash"] = "wrong tools"
                elif broken == "completion":
                    events[-1]["type"] = "full_compaction.cancel"
                write_jsonl(root / "sessions/s/agents/main/wire.jsonl", events)
                capture(root, request, {"id": "summary", "role": "assistant", "content": [
                    {"type": "thinking", "thinking": "original summary reasoning"},
                    {"type": "text", "text": "native summary\n"}], "stop_reason": "end_turn"}, path="/v1/messages")
                report = finalize_dataset(root / "captures", root / "sessions", root / "dataset", client="kimi")
                self.assertEqual(report["matched"], int(broken is None))
                if broken is None:
                    exported = export_sharegpt_recovered(root / "dataset", root / "export", "separate")
                    self.assertEqual(exported["masked_assistant_messages"], 1)
                    records = [json.loads(line) for line in (root / "export/sharegpt.jsonl").read_text().splitlines()]
                    self.assertEqual(records[0]["messages"][-1]["reasoning_content"], "original summary reasoning")
                    self.assertEqual(records[0]["messages"][-1]["content"], "native summary\n")

    def test_two_captures_cannot_claim_one_kimi_compaction_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = [{"type": "full_compaction.begin", "agentId": "main"},
                {"type": "llm.request", "agentId": "main", "kind": "compaction", "model": "test-model",
                 "systemPromptHash": hashlib.sha256(b"").hexdigest(), "toolsHash": hashlib.sha256(b"[]").hexdigest()},
                {"type": "context.apply_compaction", "agentId": "main", "summary": "same"},
                {"type": "full_compaction.complete", "agentId": "main"}]
            write_jsonl(root / "sessions/s/wire.jsonl", events)
            request = {"model": "test-model", "messages": [{"role": "user", "content":
                "You are about to run out of context. Create a handoff summary for the\nmodel that will resume this task after the earlier conversation is cleared."}]}
            for identifier in ("first", "second"):
                capture(root, request, {"id": identifier, "role": "assistant", "content": [{"type": "text", "text": "same"}],
                    "stop_reason": "end_turn"}, path="/v1/messages")
            report = finalize_dataset(root / "captures", root / "sessions", root / "dataset", client="kimi")
            self.assertEqual((report["matched"], report["conflicts"]), (0, 2))

    def test_hermes_compaction_native_grounding_and_reasoning_echo_pad(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                summary = "## Historical Task Snapshot\nModel task paraphrase\n\n## Goal\nKeep exact marker"
                ground = "## Historical Task Snapshot\nUser asked (deterministic, from compacted turns): 'remember marker'\nHistorical only; newer protected-tail messages after this summary win.\n\n## Goal\nKeep exact marker"
                if corrupt:
                    ground = ground.replace("exact marker", "wrong marker")
                wrapper = "[CONTEXT COMPACTION — REFERENCE ONLY] Native wrapper\n" + ground + "\n\n--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---\n\nremember marker"
                db_fixture(root / "state.db", [{"id": "parent"}, {"id": "child", "parent_session_id": "parent"}], [
                    {"session_id": "parent", "role": "user", "content": "remember marker", "active": 0},
                    {"session_id": "child", "role": "user", "content": wrapper, "_compressed_summary": 1},
                    {"session_id": "child", "role": "assistant", "content": "kept reply"},
                    {"session_id": "child", "role": "user", "content": "continue"},
                    {"session_id": "child", "role": "assistant", "content": "after answer"}])
                capture(root, {"model": "test-model", "messages": [{"role": "user", "content":
                    "You are a summarization agent creating a context checkpoint. Summarize native turns."}]}, chat_response("summary", summary))
                capture(root, {"model": "test-model", "messages": [{"role": "user", "content": wrapper},
                    {"role": "assistant", "content": "kept reply", "reasoning_content": " "},
                    {"role": "user", "content": "continue"}]}, chat_response("after", "after answer"))
                report = finalize_dataset(root / "captures", root / "state.db", root / "dataset", client="hermes")
                self.assertEqual(report["matched"], 1 if corrupt else 2)
                self.assertEqual(report["session_scan"]["compact_matched"], int(not corrupt))
                exported = export_sharegpt_recovered(root / "dataset", root / "export", "separate")
                self.assertEqual(exported["skipped_agents"], 0)

    def test_kimi_anthropic_replay_without_thinking_keeps_captured_reasoning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_jsonl(root / "sessions/s/wire.jsonl", [
                {"message": {"type": "StatusUpdate", "payload": {"message_id": identifier}}}
                for identifier in ("tool", "final")])
            tool = {"type": "tool_use", "id": "Read:0", "name": "Read", "input": {"path": "a"}}
            first = {"model": "test-model", "messages": [{"role": "user", "content": "read a"}]}
            reply = {"id": "tool", "role": "assistant", "content": [
                {"type": "thinking", "thinking": "original reason"}, tool], "stop_reason": "tool_use"}
            capture(root, first, reply, path="/v1/messages")
            second = {**first, "messages": first["messages"] + [
                {"role": "assistant", "content": [{**tool, "id": "Read_0"}]}, {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "Read_0", "content": "file"}]}]}
            capture(root, second, {"id": "final", "role": "assistant", "content": [
                {"type": "text", "text": "answer"}], "stop_reason": "end_turn"}, path="/v1/messages")
            report = finalize_dataset(root / "captures", root / "sessions", root / "dataset", client="kimi")
            self.assertEqual(report["matched"], 2)
            report = export_sharegpt_recovered(root / "dataset", root / "export", "separate")
            self.assertEqual(report["recovered_rounds"], 0)
            records = [json.loads(line) for line in (root / "export/sharegpt.jsonl").read_text().splitlines()]
            self.assertTrue(any(m.get("reasoning_content") == "original reason" for row in records for m in row["messages"]))
            self.assertTrue(any(t.get("id") == "Read:0" for row in records for m in row["messages"] for t in m.get("tool_calls", [])))

    def test_hermes_trimmed_tool_text_matches_without_losing_captured_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            call = {"id": "read", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a"}'}}
            history = [{"role": "user", "content": "read a"},
                {"role": "assistant", "content": "", "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "read", "content": "file"}]
            stored = [{"session_id": "s", **m} for m in history]
            stored[1]["reasoning_content"] = "reason"
            stored.append({"session_id": "s", "role": "assistant", "content": "answer"})
            db_fixture(root / "state.db", [{"id": "s"}], stored)
            capture(root, {"model": "test-model", "messages": history[:1]},
                chat_response("tool", "\n\n\n", calls=[call], reasoning="reason", finish="tool_calls"))
            capture(root, {"model": "test-model", "messages": history}, chat_response("final", " answer\n"))
            report = finalize_dataset(root / "captures", root / "state.db", root / "dataset", client="hermes")
            self.assertEqual((report["matched"], report["unmatched"]), (2, 0))
            export_sharegpt(root / "dataset", root / "strict", "separate")
            report = export_sharegpt_recovered(root / "dataset", root / "recovered", "separate")
            self.assertEqual(report["recovered_rounds"], 0)
            messages = [m for line in (root / "recovered/sharegpt.jsonl").read_text().splitlines()
                        for m in json.loads(line)["messages"]]
            self.assertTrue(any(m.get("content") == "\n\n\n" and m.get("reasoning_content") == "reason" for m in messages))
            self.assertTrue(any(m.get("content") == " answer\n" for m in messages))
            # 文本或思考内容发生实质变化时，仍不能关联。
            capture(root, {"model": "test-model", "messages": history}, chat_response("wrong", "different"))
            report = finalize_dataset(root / "captures", root / "state.db", root / "dataset2", client="hermes")
            self.assertEqual((report["matched"], report["unmatched"]), (2, 1))

    def test_kimi_v2_native_journal_ids_and_millisecond_timestamps(self):
        # 来源：真实 Kimi Code 2.0.0 运行生成的 Wire protocol 1.5 记录。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_jsonl(root / "sessions/wd_work/session_v2/agents/main/wire.jsonl", [
                {"type": "metadata", "protocol_version": "1.5", "created_at": 1791619744177},
                {"type": "context.append_message", "agentId": "main", "message": {
                    "id": "msg_internal", "role": "user", "content": [{"type": "text", "text": "question"}]}, "time": 1791619744228},
                {"type": "context.append_loop_event", "agentId": "main", "event": {
                    "type": "step.end", "messageId": "chat_provider", "finishReason": "end_turn"}, "time": 1791619744317},
                {"type": "agent.message.appended", "kind": "event", "message": {
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "answer"}], "toolCalls": []},
                    "meta": {"messageId": "chat_provider"}}, "time": 1791619744316},
            ])
            for identifier in ("chat_provider", "msg_internal"):
                capture(root, {"model": "test-model", "messages": [{"role": "user", "content": "question"}]}, chat_response(identifier, "answer"))
            report = finalize_dataset(root / "captures", root / "sessions", root / "dataset", client="kimi")
            self.assertEqual((report["matched"], report["unmatched"], report["conflicts"]), (1, 1, 0))
            response = json.loads((root / "dataset/tasks/session_v2/main_agent/round_000001/response.json").read_text())
            self.assertTrue(response["association"]["first_timestamp"].startswith("2026-10-10"))

    def test_kimi_wire_main_subagent_and_nested_mirror(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "sessions" / "workspace-hash" / "kimi-session"
            write_jsonl(session / "wire.jsonl", [
                {"timestamp": 1, "message": {"type": "StatusUpdate", "payload": {"message_id": "chat_main"}}},
                {"timestamp": 2, "message": {"type": "SubagentEvent", "payload": {"agent_id": "child", "event": {"type": "StatusUpdate", "payload": {"message_id": "chat_child"}}}}},
            ])
            write_jsonl(session / "subagents" / "child" / "wire.jsonl", [
                {"timestamp": 2, "message": {"type": "StatusUpdate", "payload": {"message_id": "chat_child"}}}])
            for identifier in ("chat_main", "chat_child"):
                capture(root, {"model": "test-model", "messages": [{"role": "user", "content": "问题"}]}, chat_response(identifier, "答案"))
            report = finalize_dataset(root / "captures", root / "sessions", root / "dataset", client="kimi")
            self.assertEqual(report["matched"], 2)
            self.assertEqual(report["conflicts"], 0)
            self.assertTrue((root / "dataset/tasks/kimi-session/subagent_child/round_000001").is_dir())
            output = export_sharegpt(root / "dataset", root / "export", "separate")
            self.assertEqual(output["rounds"], 2)

    def test_kimi_context_thinking_and_session_key_disambiguation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for sid in ("s1", "s2"):
                write_jsonl(root / "sessions" / "hash" / sid / "context.jsonl", [
                    {"role": "_system_prompt", "content": "system"}, {"role": "_checkpoint", "id": 0},
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": [{"type": "think", "think": "reason", "encrypted": "signature"}, {"type": "text", "text": "answer"}]},
                ])
            capture(root, {"model": "test-model", "prompt_cache_key": "s1", "messages": [
                {"role": "system", "content": "system"}, {"role": "user", "content": "question"}]},
                chat_response("chat_context", "answer", reasoning="reason"))
            report = finalize_dataset(root / "captures", root / "sessions", root / "dataset", client="kimi")
            self.assertEqual(report["matched"], 1)
            self.assertTrue((root / "dataset/tasks/s1").is_dir())
            output = export_sharegpt_recovered(root / "dataset", root / "export", "separate")
            self.assertEqual(output["combined_jsonl_records"], 1)

    def test_kimi_ambiguous_history_and_missing_logs_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for sid in ("s1", "s2"):
                write_jsonl(root / "sessions" / sid / "context.jsonl", [
                    {"role": "user", "content": "same"}, {"role": "assistant", "content": "same"}])
            capture(root, {"model": "test-model", "messages": [{"role": "user", "content": "same"}]}, chat_response("ambiguous", "same"))
            capture(root, {"model": "test-model", "messages": [{"role": "user", "content": "different"}]}, chat_response("unmatched", "same"))
            report = finalize_dataset(root / "captures", root / "sessions", root / "dataset", client="kimi")
            self.assertEqual((report["conflicts"], report["unmatched"], report["auxiliary"]), (1, 1, 0))

    def test_kimi_wire_claim_blocks_history_retry_regardless_of_capture_order(self):
        for accepted_first in (False, True):
            with self.subTest(accepted_first=accepted_first), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                write_jsonl(root / "sessions/s/wire.jsonl", [
                    {"message": {"type": "StatusUpdate", "payload": {"message_id": "accepted"}}}])
                write_jsonl(root / "sessions/s/context.jsonl", [
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": "answer"},
                    {"role": "user", "content": "next question"},
                    {"role": "assistant", "content": "next answer"}])
                for identifier in (("accepted", "retry") if accepted_first else ("retry", "accepted")):
                    capture(root, {"model": "test-model", "messages": [{"role": "user", "content": "question"}]},
                        chat_response(identifier, "answer"))
                # 同一会话的其它轮次仍可使用 context 回退，不能禁用整个 session。
                capture(root, {"model": "test-model", "messages": [
                    {"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"},
                    {"role": "user", "content": "next question"}]}, chat_response("next", "next answer"))
                report = finalize_dataset(root / "captures", root / "sessions", root / "dataset", client="kimi")
                self.assertEqual((report["matched"], report["conflicts"], report["unmatched"]), (2, 1, 0))
                exported = export_sharegpt_recovered(root / "dataset", root / "export", "separate")
                self.assertEqual(exported["rounds"], 2)
                records = [json.loads(line) for line in (root / "export/sharegpt.jsonl").read_text().splitlines()]
                self.assertCountEqual([record["messages"][-1]["content"] for record in records],
                                      ["answer", "next answer"])

    def test_kimi_wire_claim_does_not_reserve_another_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for sid in ("s1", "s2"):
                write_jsonl(root / "sessions" / sid / "context.jsonl", [
                    {"role": "user", "content": "same"}, {"role": "assistant", "content": "same"}])
            write_jsonl(root / "sessions/s1/wire.jsonl", [
                {"message": {"type": "StatusUpdate", "payload": {"message_id": "accepted"}}}])
            for identifier, sid in (("accepted", "s1"), ("independent", "s2")):
                capture(root, {"model": "test-model", "prompt_cache_key": sid,
                    "messages": [{"role": "user", "content": "same"}]}, chat_response(identifier, "same"))
            report = finalize_dataset(root / "captures", root / "sessions", root / "dataset", client="kimi")
            self.assertEqual((report["matched"], report["conflicts"]), (2, 0))

    def test_kimi_partial_id_claim_still_reserves_complete_native_answer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_jsonl(root / "sessions/s/context.jsonl", [
                {"role": "user", "content": "question"}, {"role": "assistant", "content": "answer"}])
            write_jsonl(root / "sessions/s/wire.jsonl", [
                {"message": {"type": "StatusUpdate", "payload": {"message_id": "accepted"}}}])
            capture(root, {"model": "test-model", "messages": [{"role": "user", "content": "question"}]},
                chat_response("accepted", "answer", finish=None))
            capture(root, {"model": "test-model", "messages": [{"role": "user", "content": "question"}]},
                chat_response("retry", "answer"))
            report = finalize_dataset(root / "captures", root / "sessions", root / "dataset", client="kimi")
            self.assertEqual((report["matched"], report["conflicts"]), (1, 1))

    def test_hermes_sqlite_tool_rounds_and_subagent_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            call = {"id": "call_read", "type": "function", "function": {"name": "read", "arguments": '{"path":"a"}'}}
            history = [{"role": "user", "content": "read a"},
                       {"role": "assistant", "content": None, "reasoning_content": "reason", "tool_calls": [call]},
                       {"role": "tool", "tool_call_id": "call_read", "content": "file contents"}]
            db = root / "hermes/state.db"
            db_fixture(db, [{"id": "main"}, {"id": "child", "source": "subagent", "parent_session_id": "main"}], [
                {"session_id": "main", **m} for m in history] + [
                {"session_id": "main", "role": "assistant", "content": "done"},
                {"session_id": "child", "role": "user", "content": "child question"},
                {"session_id": "child", "role": "assistant", "content": "child answer"},
            ])
            before = db.read_bytes()
            tool = {"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}
            capture(root, {"model": "test-model", "messages": history[:1], "tools": [tool]}, chat_response("chat_tool", None, calls=[call], reasoning="reason", finish="tool_calls"))
            capture(root, {"model": "test-model", "messages": history, "tools": [tool]}, chat_response("chat_final", "done"))
            capture(root, {"model": "test-model", "messages": [{"role": "user", "content": "child question"}]}, chat_response("chat_child", "child answer"))
            report = finalize_dataset(root / "captures", db, root / "dataset", client="hermes")
            self.assertEqual(report["matched"], 3)
            self.assertEqual(db.read_bytes(), before)
            export_sharegpt(root / "dataset", root / "export", "separate")
            record = json.loads((root / "export/main/main_agent_1.json").read_text())
            self.assertEqual([m["role"] for m in record["messages"]], ["user", "assistant", "tool", "assistant"])
            self.assertEqual(record["messages"][1]["reasoning_content"], "reason")
            self.assertEqual(record["messages"][2]["tool_call_id"], "call_read")
            self.assertTrue((root / "dataset/tasks/main/subagent_child").exists())

    def test_hermes_history_may_omit_stored_reasoning_but_answer_must_match(self):
        # 真实 Hermes Chat provider 对通用模型不重放 reasoning，数据库仍保存它。
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            call = {"id": "read_id", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a"}'}}
            history = [{"role": "user", "content": "question"},
                {"role": "assistant", "content": "", "tool_calls": [call]},
                {"role": "tool", "tool_call_id": "read_id", "content": "file"}]
            stored = [{"session_id": "s", **m} for m in history]
            stored[1]["reasoning_content"] = "reason"
            stored.append({"session_id": "s", "role": "assistant", "content": "answer", "reasoning_content": "final reason"})
            db_fixture(root / "state.db", [{"id": "s"}], stored)
            capture(root, {"model": "test-model", "messages": history[:1]}, chat_response("first", "", calls=[call], reasoning="reason", finish="tool_calls"))
            capture(root, {"model": "test-model", "messages": history}, chat_response("last", "answer", reasoning="final reason"))
            report = finalize_dataset(root / "captures", root / "state.db", root / "dataset", client="hermes")
            self.assertEqual((report["matched"], report["unmatched"]), (2, 0))
            output = export_sharegpt_recovered(root / "dataset", root / "export", "separate")
            self.assertEqual(output["skipped_agents"], 0)
            self.assertEqual(output["rounds"], 2)
            self.assertEqual(output["recovered_rounds"], 0)
            records = [json.loads(line) for line in (root / "export/sharegpt.jsonl").read_text().splitlines()]
            self.assertEqual(records[0]["messages"][-1]["reasoning_content"], "reason")
            # 不把未知/不同的响应 reasoning 也忽略掉。
            capture(root, {"model": "test-model", "messages": history}, chat_response("wrong_thinking", "answer", reasoning="different"))
            report = finalize_dataset(root / "captures", root / "state.db", root / "dataset2", client="hermes")
            self.assertEqual((report["matched"], report["unmatched"]), (2, 1))

    def test_hermes_repeated_history_remains_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_fixture(root / "state.db", [{"id": "s"}], [
                {"session_id": "s", "role": role, "content": "same"} for role in ("user", "assistant", "user", "assistant")])
            capture(root, {"model": "test-model", "messages": [{"role": "user", "content": "same"}]}, chat_response("chat_same", "same"))
            report = finalize_dataset(root / "captures", root / "state.db", root / "dataset", client="hermes")
            self.assertEqual((report["matched"], report["conflicts"]), (0, 1))

    def test_multiple_http_responses_cannot_share_one_history_witness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_fixture(root / "state.db", [{"id": "s"}], [
                {"session_id": "s", "role": "user", "content": "question"},
                {"session_id": "s", "role": "assistant", "content": "answer"}])
            for identifier in ("retry_one", "retry_two"):
                capture(root, {"model": "test-model", "messages": [{"role": "user", "content": "question"}]}, chat_response(identifier, "answer"))
            report = finalize_dataset(root / "captures", root / "state.db", root / "dataset", client="hermes")
            self.assertEqual((report["matched"], report["conflicts"]), (0, 2))

    def test_hermes_structured_content_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_fixture(root / "state.db", [{"id": "s", "model": "new-model"}], [
                {"session_id": "s", "role": "user", "content": '\u0000json:[{"type":"text","text":"question"}]'},
                {"session_id": "s", "role": "assistant", "content": "answer"}])
            capture(root, {"model": "test-model", "messages": [{"role": "user", "content": "question"}]}, chat_response("x", "answer"))
            report = finalize_dataset(root / "captures", root / "state.db", root / "dataset", client="hermes")
            self.assertEqual(report["matched"], 1)

    def test_hermes_uid_deduplicates_compaction_generations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            messages = []
            for active in (0, 1):
                messages.extend([{"session_id": "s", "role": "user", "content": "question", "active": active},
                    {"session_id": "s", "role": "assistant", "content": "answer", "active": active, "message_uid": "same-uid"}])
            db_fixture(root / "state.db", [{"id": "s"}], messages)
            capture(root, {"model": "test-model", "messages": [{"role": "user", "content": "question"}]}, chat_response("x", "answer"))
            report = finalize_dataset(root / "captures", root / "state.db", root / "dataset", client="hermes")
            self.assertEqual((report["matched"], report["conflicts"]), (1, 0))

    def test_hermes_api_content_matches_wire_and_responses_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            db_fixture(root / "state.db", [{"id": "s", "system_prompt": "system"}], [
                {"session_id": "s", "role": "user", "content": "original", "api_content": "injected original"},
                {"session_id": "s", "role": "assistant", "content": "answer"}])
            response = {"id": "resp_1", "object": "response", "model": "test-model", "status": "completed",
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}]}
            capture(root, {"model": "test-model", "instructions": "system", "input": "injected original"}, response, path="/v1/responses")
            report = finalize_dataset(root / "captures", root / "state.db", root / "dataset", client="hermes")
            self.assertEqual(report["matched"], 1)
            export_sharegpt(root / "dataset", root / "export", "inline")
            self.assertEqual(json.loads((root / "export/s/main_agent_1.json").read_text())["messages"][1]["content"], "injected original")

    def test_cli_accepts_session_alias_and_missing_source_fails(self):
        args = parse_args(["--client", "hermes", "--session-dir", "state.db", "--capture-dir", "capture", "--output-dir", "output"])
        self.assertEqual(args.harbor_run_dir, Path("state.db"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(ValueError):
                finalize_dataset(root / "captures", root / "missing", root / "output", client="hermes")
            self.assertFalse((root / "output").exists())


class ProxyClientIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_chat_and_responses_preserve_wire_bytes(self):
        import httpx
        from proxy import Settings, create_app

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seen = {}

            async def handler(request):
                seen[request.url.path] = await request.aread()
                await asyncio.sleep(0)
                if request.url.path.endswith("responses"):
                    events = [{"type": "response.completed", "response": {"id": "resp_wire", "object": "response", "status": "completed", "output": []}}]
                else:
                    events = [{"id": "chat_wire", "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}]}, "[DONE]"]
                raw = "".join("data: " + (e if isinstance(e, str) else json.dumps(e)) + "\n\n" for e in events).encode()
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=httpx.ByteStream(raw))

            app = create_app(Settings("127.0.0.1", 30303, "http://upstream", root / "captures", 30), httpx.MockTransport(handler))
            bodies = {"/v1/chat/completions": b'{ "model": "test-model", "messages": [], "stream": true }',
                      "/v1/responses": b'{ "model": "test-model", "input": "hello", "stream": true }'}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
                    responses = await asyncio.gather(*(client.post(path, content=body) for path, body in bodies.items()))
            self.assertEqual(seen, bodies)
            self.assertTrue(all(response.status_code == 200 for response in responses))
            completed = list((root / "captures/raw/completed").iterdir())
            self.assertEqual(len(completed), 2)
            ids = set()
            for directory in completed:
                response = json.loads((directory / "response.json").read_text())
                ids.add(response["message_id"])
                self.assertTrue(response["aggregation_complete"])
                self.assertIn((directory / "response.body").read_bytes(), [r.content for r in responses])
            self.assertEqual(ids, {"chat_wire", "resp_wire"})


class LayoutClientTests(unittest.TestCase):
    def test_native_clients_keep_existing_layout_result_filters(self):
        project = Path(__file__).resolve().parents[1]
        layouts = [("finalize-harbor.py", "kimi", "jobs"), ("finalize-node.py", "hermes", "node"),
                   ("finalize-node-split.py", "kimi", "node"), ("finalize-proc-split.py", "hermes", "proc")]
        for script, client, layout in layouts:
            with self.subTest(script=script), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                run = root / "run"
                for successful, task in ((True, "task_success"), (False, "task_failed")):
                    if layout == "jobs":
                        directory = run / task
                        logs = directory / "agent/sessions"
                    elif layout == "node":
                        directory = run / "worker0/logs" / task
                        logs = directory / "logs"
                        results = run / "worker0/result"
                        results.mkdir(parents=True, exist_ok=True)
                        (results / (task + ".log")).write_text(
                            json.dumps({"vul_exit_code": 139 if successful else 0, "fix_exit_code": 0}))
                    else:
                        directory = run / "group" / task
                        logs = directory / "logs"
                        write_json(directory / "result.json", {"checks": [{"score": 1.0 if successful else 0.0}]})
                    if client == "kimi":
                        write_jsonl(logs / "sessions/hash/session/wire.jsonl", [
                            {"timestamp": 1, "message": {"type": "StatusUpdate", "payload": {"message_id": task}}}])
                    else:
                        db_fixture(logs / "state.db", [{"id": task}], [
                            {"session_id": task, "role": "user", "content": task},
                            {"session_id": task, "role": "assistant", "content": "answer"}])
                    capture(root, {"model": "test-model", "messages": [{"role": "user", "content": task}]}, chat_response(task, "answer"))
                if layout == "jobs":
                    write_json(run / "result.json", {"stats": {"evals": {"test": {"reward_stats": {"reward": {"1.0": ["task_success"], "0.0": ["task_failed"]}}}}}})
                # node 的目录命名必须遵守现有 result 日志的 task-prefix/hash 还原规则。
                if layout == "node":
                    for task in ("task_success", "task_failed"):
                        old = run / "worker0/logs" / task
                        old.rename(old.with_name(task.replace("_", "-")))
                command = [sys.executable, str(project / script), "--client", client,
                    "--capture-dir", str(root / "captures"), "--harbor-run-dir", str(run),
                    "--output-dir", str(root / "dataset"), "--only-successful"]
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["matched"], 1)
                self.assertEqual(report["filtered_captures"], 1)


if __name__ == "__main__":
    unittest.main()
