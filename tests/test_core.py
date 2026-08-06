from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from capture_core import (
    AnthropicMessageAggregator,
    RequestCapture,
    SSEDecoder,
    header_records,
    write_json,
)
from finalize import CaptureRecord, final_request, final_response, finalize_dataset
from proxy import parse_args


TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".test_tmp"
TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)


def workspace_temporary_directory():
    # 沙箱环境可能禁止写系统 TEMP，测试临时目录固定放在项目工作区内。
    return tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT)


def sse(event: str, payload: dict) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")


def complete_text_stream(message_id: str, text: str) -> bytes:
    return b"".join(
        [
            sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": message_id,
                        "type": "message",
                        "role": "assistant",
                        "model": "test-model",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 2, "output_tokens": 1},
                    },
                },
            ),
            sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            ),
            sse(
                "content_block_stop",
                {"type": "content_block_stop", "index": 0},
            ),
            sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 3},
                },
            ),
            sse("message_stop", {"type": "message_stop"}),
        ]
    )


class AggregatorTests(unittest.TestCase):
    def test_stream_can_be_aggregated_across_arbitrary_chunks(self):
        raw = complete_text_stream("msg_text", "你好")
        decoder = SSEDecoder(AnthropicMessageAggregator())
        records = []
        for offset in range(0, len(raw), 7):
            records.extend(decoder.feed(raw[offset : offset + 7]))
        records.extend(decoder.finish())

        self.assertGreater(len(records), 0)
        self.assertTrue(decoder.aggregator.complete)
        self.assertEqual(decoder.aggregator.message_id, "msg_text")
        self.assertEqual(decoder.aggregator.message["content"][0]["text"], "你好")
        self.assertEqual(decoder.aggregator.message["stop_reason"], "end_turn")
        self.assertEqual(decoder.aggregator.message["usage"]["output_tokens"], 3)

    def test_thinking_and_tool_input_are_aggregated(self):
        decoder = SSEDecoder(AnthropicMessageAggregator())
        raw = b"".join(
            [
                sse(
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_tool",
                            "type": "message",
                            "role": "assistant",
                            "model": "test-model",
                            "content": [],
                            "stop_reason": None,
                            "usage": {},
                        },
                    },
                ),
                sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {
                            "type": "thinking",
                            "thinking": "",
                            "signature": "",
                        },
                    },
                ),
                sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "thinking_delta", "thinking": "分析"},
                    },
                ),
                sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "signature_delta", "signature": "sig"},
                    },
                ),
                sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
                sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 1,
                        "content_block": {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Bash",
                            "input": {},
                        },
                    },
                ),
                sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": '{"command":',
                        },
                    },
                ),
                sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": '"pwd"}',
                        },
                    },
                ),
                sse("content_block_stop", {"type": "content_block_stop", "index": 1}),
                sse("message_stop", {"type": "message_stop"}),
            ]
        )
        decoder.feed(raw)
        decoder.finish()
        message = decoder.aggregator.message
        self.assertEqual(message["content"][0]["thinking"], "分析")
        self.assertEqual(message["content"][0]["signature"], "sig")
        self.assertEqual(message["content"][1]["input"], {"command": "pwd"})


class CaptureIsolationTests(unittest.TestCase):
    def test_two_interleaved_streams_never_share_files(self):
        with workspace_temporary_directory() as temporary:
            root = Path(temporary)
            capture_a = RequestCapture(root, "cap_a")
            capture_b = RequestCapture(root, "cap_b")
            for capture in (capture_a, capture_b):
                capture.start_request(
                    method="POST",
                    path="/v1/messages",
                    query="",
                    url="http://proxy/v1/messages",
                    headers=[("content-type", "application/json")],
                    raw_body=b'{"model":"test"}',
                    upstream_url="http://upstream/v1/messages",
                    client_host="127.0.0.1",
                    client_port=1000,
                )
                capture.start_response(
                    status_code=200,
                    headers=[("content-type", "text/event-stream")],
                    is_sse=True,
                )

            raw_a = complete_text_stream("msg_a", "A")
            raw_b = complete_text_stream("msg_b", "B")
            midpoint_a = len(raw_a) // 2
            midpoint_b = len(raw_b) // 2
            capture_a.append_response(raw_a[:midpoint_a])
            capture_b.append_response(raw_b[:midpoint_b])
            capture_a.append_response(raw_a[midpoint_a:])
            capture_b.append_response(raw_b[midpoint_b:])
            capture_a.finalize()
            capture_b.finalize()

            response_a = json.loads((root / "completed/cap_a/response.json").read_text("utf-8"))
            response_b = json.loads((root / "completed/cap_b/response.json").read_text("utf-8"))
            self.assertEqual(response_a["message_id"], "msg_a")
            self.assertEqual(response_b["message_id"], "msg_b")
            self.assertNotIn(b"msg_b", (root / "completed/cap_a/response.body").read_bytes())
            self.assertNotIn(b"msg_a", (root / "completed/cap_b/response.body").read_bytes())

    def test_gzip_sse_keeps_raw_body_and_still_aggregates(self):
        with workspace_temporary_directory() as temporary:
            root = Path(temporary)
            capture = RequestCapture(root, "cap_gzip")
            capture.start_request(
                method="POST",
                path="/v1/messages",
                query="",
                url="http://proxy/v1/messages",
                headers=[("content-type", "application/json")],
                raw_body=b"{}",
                upstream_url="http://upstream/v1/messages",
                client_host="127.0.0.1",
                client_port=1000,
            )
            capture.start_response(
                status_code=200,
                headers=[
                    ("content-type", "text/event-stream"),
                    ("content-encoding", "gzip"),
                ],
                is_sse=True,
            )
            compressed = gzip.compress(complete_text_stream("msg_gzip", "压缩"))
            capture.append_response(compressed)
            capture.finalize()
            response = json.loads(
                (root / "completed/cap_gzip/response.json").read_text("utf-8")
            )
            self.assertEqual(response["message_id"], "msg_gzip")
            self.assertEqual(response["message"]["content"][0]["text"], "压缩")
            self.assertEqual(
                (root / "completed/cap_gzip/response.body").read_bytes(), compressed
            )


class FinalizerTests(unittest.TestCase):
    def _write_capture(self, root: Path, capture_id: str, message_id: str) -> None:
        directory = root / "raw/completed" / capture_id
        directory.mkdir(parents=True)
        request_body = json.dumps(
            {
                "model": "test-model",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": message_id}],
                "stream": True,
            },
            separators=(",", ":"),
        ).encode()
        response_body = complete_text_stream(message_id, "ok")
        (directory / "request.body").write_bytes(request_body)
        (directory / "response.body").write_bytes(response_body)
        write_json(
            directory / "request.json",
            {
                "capture_id": capture_id,
                "captured_at": "2026-01-01T00:00:00Z",
                "path": "/v1/messages",
                "is_messages_request": True,
            },
        )
        write_json(
            directory / "response.json",
            {
                "capture_id": capture_id,
                "message_id": message_id,
                "stream": True,
                "message": {"id": message_id, "type": "message", "content": []},
            },
        )
        write_json(directory / "state.json", {"state": "complete"})

    def test_harbor_main_and_subagent_are_finalized(self):
        with workspace_temporary_directory() as temporary:
            root = Path(temporary)
            capture_root = root / "captures"
            harbor_root = root / "harbor-run"
            output_root = root / "dataset"
            self._write_capture(capture_root, "cap_main", "msg_main")
            self._write_capture(capture_root, "cap_sub", "msg_sub")

            task_root = harbor_root / "tasks/task_a"
            write_json(
                task_root / "final_status.json",
                {"task_id": "task:a", "agent_id": "harbor-agent-1"},
            )
            project = task_root / "logs/run/cc_session/.claude/projects/-workspace"
            project.mkdir(parents=True)
            main_lines = [
                {
                    "type": "assistant",
                    "sessionId": "session-1",
                    "uuid": "u1",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "message": {"id": "msg_main", "content": [{"type": "text", "text": "a"}]},
                },
                {
                    "type": "assistant",
                    "sessionId": "session-1",
                    "uuid": "u2",
                    "timestamp": "2026-01-01T00:00:02Z",
                    "message": {"id": "msg_main", "content": [{"type": "tool_use"}]},
                },
            ]
            (project / "session-1.jsonl").write_text(
                "\n".join(json.dumps(item) for item in main_lines) + "\n",
                encoding="utf-8",
            )
            subagents = project / "session-1/subagents"
            subagents.mkdir(parents=True)
            (subagents / "agent-sub1.jsonl").write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "sessionId": "session-1",
                        "agentId": "sub1",
                        "isSidechain": True,
                        "timestamp": "2026-01-01T00:00:03Z",
                        "message": {"id": "msg_sub", "content": []},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = finalize_dataset(capture_root, harbor_root, output_root)
            self.assertEqual(report["matched"], 2)
            self.assertEqual(report["tasks"], 1)
            main_response = json.loads(
                (output_root / "tasks/task_a/main_agent/round_000001/response.json").read_text("utf-8")
            )
            self.assertEqual(main_response["association"]["fragment_count"], 2)
            self.assertEqual(main_response["association"]["round"], 1)
            self.assertTrue(
                (output_root / "tasks/task_a/subagent_sub1/round_000001/request.json").exists()
            )


class OutputPartsTests(unittest.TestCase):
    def setUp(self):
        # body 文件不存在时 final_request/final_response 会使用空 bytes，足以验证
        # 顶层字段白名单而不创建测试目录。
        self.record = CaptureRecord(
            capture_dir=Path("__nonexistent_capture_for_output_parts_test__"),
            request={"capture_id": "cap_parts"},
            response={},
            state={},
        )

    def test_removing_parts_really_removes_top_level_output(self):
        request = final_request(
            self.record,
            None,
            output_parts=["capture_id", "body"],
        )
        response = final_response(
            self.record,
            None,
            output_parts=["message", "state"],
        )
        self.assertEqual(list(request), ["capture_id", "body"])
        self.assertEqual(list(response), ["message", "state"])

    def test_unknown_or_duplicate_parts_raise(self):
        with self.assertRaisesRegex(ValueError, "不支持的顶层字段"):
            final_request(self.record, None, output_parts=["capture_id", "unknown"])
        with self.assertRaisesRegex(ValueError, "重复的顶层字段"):
            final_response(self.record, None, output_parts=["state", "state"])


class ProxyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_forwards_auth_stream_and_duplicate_headers(self):
        import httpx

        from proxy import Settings, create_app

        with workspace_temporary_directory() as temporary:
            root = Path(temporary)
            seen: dict[str, object] = {}

            async def upstream_handler(request: httpx.Request) -> httpx.Response:
                seen["api_key"] = request.headers.get("x-api-key")
                seen["body"] = await request.aread()
                return httpx.Response(
                    200,
                    headers=[
                        ("content-type", "text/event-stream"),
                        ("x-duplicate", "one"),
                        ("x-duplicate", "two"),
                    ],
                    stream=httpx.ByteStream(
                        complete_text_stream("msg_proxy", "代理成功")
                    ),
                )

            app = create_app(
                Settings(
                    listen_host="127.0.0.1",
                    listen_port=30303,
                    upstream_url="http://upstream",
                    log_dir=root / "captures",
                    timeout_seconds=30,
                ),
                upstream_transport=httpx.MockTransport(upstream_handler),
            )
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://proxy",
                ) as client:
                    response = await client.post(
                        "/v1/messages",
                        headers={"x-api-key": "harbor-secret"},
                        json={
                            "model": "test-model",
                            "max_tokens": 10,
                            "messages": [{"role": "user", "content": "hello"}],
                            "stream": True,
                        },
                    )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(seen["api_key"], "harbor-secret")
            self.assertIn(b'"model":"test-model"', seen["body"])
            self.assertEqual(response.headers.get_list("x-duplicate"), ["one", "two"])
            completed = list((root / "captures/raw/completed").iterdir())
            self.assertEqual(len(completed), 1)
            captured_response = json.loads(
                (completed[0] / "response.json").read_text(encoding="utf-8")
            )
            captured_request = json.loads(
                (completed[0] / "request.json").read_text(encoding="utf-8")
            )
            self.assertEqual(captured_response["message_id"], "msg_proxy")
            api_key_record = next(
                item
                for item in captured_request["headers"]
                if item["name"].lower() == "x-api-key"
            )
            self.assertEqual(api_key_record["value"], "<redacted>")


class ConfigurationTests(unittest.TestCase):
    def test_authentication_is_passthrough_only(self):
        settings = parse_args(
            [
                "--upstream-url",
                "http://upstream",
                "--listen-host",
                "127.0.0.1",
            ]
        )
        self.assertFalse(hasattr(settings, "upstream_api_key"))
        records = header_records([("X-Api-Key", "secret")])
        self.assertEqual(records[0]["value"], "<redacted>")
        self.assertIn("value_sha256", records[0])


if __name__ == "__main__":
    unittest.main()
