import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from proxy_lb import BackendPool, Settings, create_app, load_backends, parse_args


class BackendConfigurationTests(unittest.TestCase):
    def test_addresses_and_invalid_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "backends.txt"
            config.write_text("127.0.0.1:8000\n\n[::1]:8001\n")
            self.assertEqual(
                load_backends(config),
                ("http://127.0.0.1:8000", "http://[::1]:8001"),
            )
            settings = parse_args(["--backends-file", str(config), "--balance", "random"])
            self.assertEqual(settings.balance, "random")
            for content in (
                "", "http://127.0.0.1:8000", "127.0.0.1:0",
                "127.0.0.1:65536", "not-an-ip:8000", "::1:8000",
                "127.0.0.1:8000\n127.0.0.1:8000\n",
            ):
                with self.subTest(content=content):
                    config.write_text(content)
                    with self.assertRaises(ValueError):
                        load_backends(config)

    def test_random_uses_uniform_choice_from_full_pool(self):
        urls = ("http://127.0.0.1:8000", "http://127.0.0.1:8001")
        pool = BackendPool(urls, "random")
        with patch("proxy_lb.random.choice", return_value=urls[1]) as choose:
            self.assertEqual(pool.select(), urls[1])
            choose.assert_called_once_with(urls)


class LoadBalancedProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_round_robin_preserves_stream_and_capture(self):
        urls = ("http://127.0.0.1:8000", "http://127.0.0.1:8001")
        body = b'{ "model": "same-model", "stream": true }'
        stream = b'event: ping\ndata: {"type":"ping"}\n\n'
        seen = []

        async def upstream(request):
            seen.append(str(request.url))
            self.assertEqual(await request.aread(), body)
            self.assertEqual(request.headers["x-api-key"], "test-secret")
            await asyncio.sleep(0)
            return httpx.Response(
                200,
                headers=[("content-type", "text/event-stream"),
                         ("x-duplicate", "a"), ("x-duplicate", "b")],
                stream=httpx.ByteStream(stream),
            )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = create_app(
                Settings("127.0.0.1", 30304, urls, "round-robin", root, 30),
                upstream_transport=httpx.MockTransport(upstream),
            )
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://proxy"
                ) as client:
                    health = (await client.get("/healthz")).json()
                    self.assertEqual(health["upstream_urls"], list(urls))
                    self.assertEqual(health["balance"], "round-robin")
                    responses = await asyncio.gather(*[
                        client.post("/glm53/v1/messages?test=1", content=body,
                                    headers={"x-api-key": "test-secret"})
                        for _ in range(7)
                    ])
            expected = [urls[i % 2] + "/glm53/v1/messages?test=1" for i in range(7)]
            self.assertEqual(seen, expected)
            for response in responses:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, stream)
                self.assertEqual(response.headers.get_list("x-duplicate"), ["a", "b"])
            completed = list((root / "raw/completed").iterdir())
            self.assertEqual(len(completed), 7)
            captured_urls = []
            for directory in completed:
                record = json.loads((directory / "request.json").read_text())
                captured_urls.append(record["upstream_url"])
                self.assertEqual((directory / "request.body").read_bytes(), body)
                self.assertEqual((directory / "response.body").read_bytes(), stream)
                self.assertNotIn("test-secret", (directory / "request.json").read_text())
            self.assertCountEqual(captured_urls, expected)

    async def test_failure_does_not_retry_and_next_request_uses_next_backend(self):
        seen = []

        async def upstream(request):
            seen.append(request.url.port)
            if request.url.port == 8000:
                raise httpx.ConnectError("unavailable", request=request)
            return httpx.Response(429, stream=httpx.ByteStream(b"rate limited"))

        with tempfile.TemporaryDirectory() as temporary:
            app = create_app(
                Settings("127.0.0.1", 30304,
                         ("http://127.0.0.1:8000", "http://127.0.0.1:8001"),
                         "round-robin", Path(temporary), 30),
                upstream_transport=httpx.MockTransport(upstream),
            )
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://proxy"
                ) as client:
                    failure = await client.post("/v1/messages", content=b"{}")
                    limited = await client.post("/v1/messages", content=b"{}")
            self.assertEqual(failure.status_code, 502)
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.content, b"rate limited")
            self.assertEqual(seen, [8000, 8001])
            self.assertEqual(len(list((Path(temporary) / "raw/completed").iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
