import asyncio
import gzip
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from proxy_retry import Settings, create_app, parse_args


def sse(kind, **payload):
    return (f"event: {kind}\ndata: " + json.dumps({"type": kind, **payload}) + "\n\n").encode()


def complete_stream(text="success"):
    return b"".join([
        sse("message_start", message={
            "id": "msg_" + text, "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": text}], "usage": {},
        }),
        sse("message_delta", delta={"stop_reason": "end_turn"}),
        sse("message_stop"),
    ])


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks, failure=None, release=None):
        self.chunks = chunks
        self.failure = failure
        self.release = release
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
            await asyncio.sleep(0)
        if self.release:
            await self.release.wait()
        if self.failure:
            raise self.failure

    async def aclose(self):
        self.closed = True


class BufferedRetryTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, handler, **overrides):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = Settings(
                "127.0.0.1", 30304, "http://upstream", root,
                retry_delay_seconds=0, retry_max_delay_seconds=0,
                **overrides,
            )
            app = create_app(settings, httpx.MockTransport(handler))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://proxy",
                ) as client:
                    response = await client.post(
                        "/glm53/v1/messages?beta=true", content=b'{ "stream": true }',
                        headers={"x-api-key": "test-secret"},
                    )
                    health = (await client.get("/healthz")).json()
            selected = []
            failures = []
            for directory, output in ((root / "raw/completed", selected),
                                      (root / "raw/retry_attempts", failures)):
                if directory.exists():
                    for path in directory.rglob("response.json"):
                        output.append({
                            "response": json.loads(path.read_text()),
                            "request": json.loads(path.with_name("request.json").read_text()),
                            "body": path.with_name("response.body").read_bytes(),
                            "request_body": path.with_name("request.body").read_bytes(),
                        })
            self.assertEqual(list((root / "raw/inflight").iterdir()), [])
            return response, selected, failures, health

    async def test_partial_stream_is_discarded_then_success_is_replayed(self):
        streams = []
        requests = []
        good = complete_stream()

        async def handler(request):
            requests.append((str(request.url), await request.aread(), request.headers["x-api-key"]))
            stream = (
                ChunkStream([b'event: message_start\ndata: {"type":"message_start"}\n\n'],
                            httpx.ReadError("packet loss"))
                if len(requests) == 1 else ChunkStream([good[:23], good[23:]])
            )
            streams.append(stream)
            return httpx.Response(200, headers=[("content-type", "text/event-stream"),
                                                ("x-duplicate", "a"), ("x-duplicate", "b")], stream=stream)

        response, selected, failures, health = await self.exercise(handler)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, good)
        self.assertEqual(response.headers.get_list("x-duplicate"), ["a", "b"])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[0][0], "http://upstream/glm53/v1/messages?beta=true")
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertEqual(len(selected), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(selected[0]["body"], good)
        self.assertIn("ReadError", failures[0]["response"]["transport_error"])
        self.assertEqual(selected[0]["request"]["proxy_request_id"], failures[0]["request"]["proxy_request_id"])
        self.assertEqual(selected[0]["request"]["proxy_attempt"], 2)
        self.assertNotIn("test-secret", json.dumps(selected[0]["request"]))
        self.assertTrue(health["buffer_response"])
        self.assertEqual(health["retry_policy"]["max_retries"], 20)

    async def test_network_errors_can_recover_after_many_attempts(self):
        errors = [httpx.ConnectError, httpx.ReadError, httpx.WriteTimeout, httpx.RemoteProtocolError] * 3
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            if calls <= len(errors):
                raise errors[calls - 1]("unstable link", request=request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=ChunkStream([complete_stream()]))

        response, selected, failures, _ = await self.exercise(handler)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, 13)
        self.assertEqual(len(failures), 12)
        self.assertEqual(len(selected), 1)

    async def test_clean_eof_without_message_stop_is_retried(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            raw = complete_stream("failed")[:-len(sse("message_stop"))] if calls == 1 else complete_stream()
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream([raw]))

        response, selected, failures, _ = await self.exercise(handler)
        self.assertEqual(response.content, complete_stream())
        self.assertEqual(calls, 2)
        self.assertIn("IncompleteSSE", failures[0]["response"]["transport_error"])
        self.assertEqual(len(selected), 1)

    async def test_sse_error_after_message_stop_is_not_delivered(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            raw = complete_stream()
            if calls == 1:
                raw += sse("error", error={"type": "overloaded_error"})
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ChunkStream([raw]))

        response, _, failures, _ = await self.exercise(handler)
        self.assertEqual(response.content, complete_stream())
        self.assertEqual(calls, 2)
        self.assertIn("overloaded_error", failures[0]["response"]["transport_error"])

    async def test_exhaustion_returns_error_without_partial_stream(self):
        async def handler(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=ChunkStream([sse("ping")]))

        response, selected, failures, _ = await self.exercise(handler, max_retries=2)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["type"], "proxy_error")
        self.assertEqual(len(failures), 3)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["response"]["source"], "proxy")

    async def test_http_errors_retry_but_permanent_errors_do_not(self):
        for status, expected in ((429, 3), (503, 3), (529, 3), (400, 1), (401, 1)):
            calls = 0

            async def handler(request):
                nonlocal calls
                calls += 1
                return httpx.Response(status, headers={"retry-after": "0"}, stream=ChunkStream([b"error body"]))

            with self.subTest(status=status):
                response, selected, failures, _ = await self.exercise(handler, max_retries=2)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.content, b"error body")
                self.assertEqual(calls, expected)
                self.assertEqual(len(selected), 1)
                self.assertEqual(len(failures), expected - 1)

    async def test_permanent_sse_error_does_not_retry(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=ChunkStream([sse("error", error={"type": "invalid_request_error"})]))

        response, _, failures, _ = await self.exercise(handler)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(calls, 1)
        self.assertEqual(len(failures), 1)

    async def test_compressed_stream_is_validated_and_preserved(self):
        raw = complete_stream()
        compressed = gzip.compress(raw)

        async def handler(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
                                  stream=ChunkStream([compressed[:15], compressed[15:]]))

        response, selected, failures, _ = await self.exercise(handler)
        self.assertEqual(response.content, raw)
        self.assertEqual(selected[0]["body"], compressed)
        self.assertEqual(failures, [])

    async def test_non_sse_read_failure_is_retried_without_body_mixing(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, stream=ChunkStream(
                [b"partial"] if calls == 1 else [b'{"result":"ok"}'],
                httpx.ReadError("disconnected") if calls == 1 else None,
            ))

        response, _, failures, _ = await self.exercise(handler)
        self.assertEqual(response.json(), {"result": "ok"})
        self.assertEqual(failures[0]["body"], b"partial")

    async def test_attempt_timeout_retries(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.Event().wait()
            return httpx.Response(200, stream=ChunkStream([b"ok"]))

        response, _, failures, _ = await self.exercise(handler, attempt_timeout_seconds=0.02)
        self.assertEqual(response.content, b"ok")
        self.assertEqual(calls, 2)
        self.assertIn("TimeoutError", failures[0]["response"]["transport_error"])

    async def test_total_budget_stops_a_hung_upstream(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            await asyncio.Event().wait()

        response, _, failures, _ = await self.exercise(
            handler, attempt_timeout_seconds=1, request_timeout_seconds=0.02,
        )
        self.assertEqual(response.status_code, 504)
        self.assertEqual(calls, 1)
        self.assertEqual(len(failures), 1)

    async def test_concurrent_requests_keep_retries_and_capture_separate(self):
        counts = {}

        async def handler(request):
            key = json.loads(await request.aread())["key"]
            counts[key] = counts.get(key, 0) + 1
            await asyncio.sleep(0)
            if counts[key] == 1:
                raise httpx.ReadError("temporary failure")
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=ChunkStream([complete_stream(str(key))]))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(Settings("127.0.0.1", 30304, "http://upstream", root,
                                      retry_delay_seconds=0, retry_max_delay_seconds=0),
                             httpx.MockTransport(handler))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
                    responses = await asyncio.gather(*[
                        client.post("/v1/messages", json={"key": key}) for key in range(8)
                    ])
            self.assertEqual(counts, {key: 2 for key in range(8)})
            for key, response in enumerate(responses):
                self.assertEqual(response.content, complete_stream(str(key)))
            self.assertEqual(len(list((root / "raw/completed").iterdir())), 8)
            self.assertEqual(len(list((root / "raw/retry_attempts").rglob("response.json"))), 8)

    async def test_unrelated_mutation_is_not_retried(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            raise httpx.WriteError("uncertain delivery")

        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(Settings("127.0.0.1", 30304, "http://upstream", Path(temporary)),
                             httpx.MockTransport(handler))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
                    response = await client.post("/side-effect", content=b"{}")
            self.assertEqual(response.status_code, 502)
            self.assertEqual(calls, 1)

    async def test_headers_are_held_until_complete_and_disconnect_cancels(self):
        for disconnect in (False, True):
            with self.subTest(disconnect=disconnect), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                began = asyncio.Event()
                release = asyncio.Event()
                cancelled = asyncio.Event()
                calls = 0

                async def handler(request):
                    nonlocal calls
                    calls += 1
                    began.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        cancelled.set()
                        raise
                    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                          stream=ChunkStream([complete_stream()]))

                app = create_app(Settings("127.0.0.1", 30304, "http://upstream", root), httpx.MockTransport(handler))
                incoming = asyncio.Queue()
                await incoming.put({"type": "http.request", "body": b"{}", "more_body": False})
                messages = []

                async def send(message):
                    messages.append(message)

                scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                         "http_version": "1.1", "method": "POST", "scheme": "http",
                         "path": "/v1/messages", "raw_path": b"/v1/messages", "query_string": b"",
                         "root_path": "", "headers": [], "server": ("proxy", 80), "client": ("client", 1)}
                async with app.router.lifespan_context(app):
                    task = asyncio.create_task(app(scope, incoming.get, send))
                    await asyncio.wait_for(began.wait(), 1)
                    self.assertEqual(messages, [])
                    if disconnect:
                        await incoming.put({"type": "http.disconnect"})
                    else:
                        release.set()
                    await asyncio.wait_for(task, 1)
                self.assertEqual(calls, 1)
                if disconnect:
                    self.assertTrue(cancelled.is_set())
                    self.assertEqual(messages[0]["status"], 499)
                    failed = next((root / "raw/retry_attempts").rglob("response.json"))
                    self.assertTrue(json.loads(failed.read_text())["client_disconnected"])
                else:
                    self.assertEqual(messages[0]["status"], 200)
                    self.assertEqual(b"".join(m.get("body", b"") for m in messages), complete_stream())

    def test_invalid_settings_are_rejected(self):
        for name, value in (("max_retries", -1), ("read_timeout_seconds", 0),
                            ("request_timeout_seconds", float("inf"))):
            with self.subTest(name=name), self.assertRaises(ValueError):
                Settings("127.0.0.1", 30304, "http://upstream", Path("."), **{name: value})
        settings = parse_args(["--upstream-url", "http://upstream", "--max-retries", "25"])
        self.assertEqual(settings.max_retries, 25)


if __name__ == "__main__":
    unittest.main()
