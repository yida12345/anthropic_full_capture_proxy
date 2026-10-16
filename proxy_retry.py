"""完整缓存 Anthropic 响应后再交付，并自动重试失败的上游请求。

原有 proxy.py 和 proxy_lb.py 保留为独立入口。
"""

import argparse
import asyncio
import json
import logging
import math
import os
import random
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from capture_core import RequestCapture, initialize_capture_root, write_json
from proxy import (
    REQUEST_HEADERS_TO_DROP,
    RESPONSE_HEADERS_TO_DROP,
    apply_raw_response_headers,
    build_upstream_url,
    compact_json_bytes,
    filtered_headers,
    request_header_items,
)

logger = logging.getLogger("uvicorn.error")
RETRY_STATUS_CODES = {408, 429, 500, 502, 503, 504, 529}
PERMANENT_SSE_ERRORS = {
    "invalid_request_error", "authentication_error", "permission_error", "not_found_error",
}


@dataclass(frozen=True)
class Settings:
    listen_host: str
    listen_port: int
    upstream_url: str
    log_dir: Path
    timeout_seconds: float = 300
    connect_timeout_seconds: float = 30
    read_timeout_seconds: float = 600
    attempt_timeout_seconds: float = 3600
    request_timeout_seconds: float = 86400
    max_retries: int = 20
    retry_delay_seconds: float = 1
    retry_max_delay_seconds: float = 30

    def __post_init__(self) -> None:
        for name in (
            "timeout_seconds", "connect_timeout_seconds", "read_timeout_seconds",
            "attempt_timeout_seconds", "request_timeout_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("retry_delay_seconds", "retry_max_delay_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.retry_delay_seconds > self.retry_max_delay_seconds:
            raise ValueError("retry_delay_seconds must not exceed retry_max_delay_seconds")

    def policy(self) -> dict[str, Any]:
        values = asdict(self)
        return {name: value for name, value in values.items()
                if name not in {"listen_host", "listen_port", "upstream_url", "log_dir"}}


def retry_after_seconds(value: Optional[str]) -> float:
    if not value:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


def stream_validation_error(capture: RequestCapture) -> tuple[Optional[str], bool]:
    """校验已完成采集的 SSE，包括由 capture_core 解码的压缩响应。"""
    response = capture.response_record
    # 先检查错误事件，再判断完成标记，避免重试永久性 API 错误。
    events = capture.paths.completed_dir / "sse_events.jsonl"
    if events.exists():
        with events.open(encoding="utf-8") as handle:
            for line in handle:
                event = json.loads(line)
                payload = event.get("data")
                if isinstance(payload, dict) and payload.get("type") == "error":
                    error = payload.get("error") or {}
                    kind = error.get("type", "api_error") if isinstance(error, dict) else "api_error"
                    return f"UpstreamSSEError: {kind}", kind not in PERMANENT_SSE_ERRORS
                if event.get("data_raw") and not isinstance(payload, dict):
                    return "InvalidSSE: event data is not a JSON object", True
    if response.get("content_decoding_error"):
        return "InvalidSSE: " + response["content_decoding_error"], True
    if response.get("aggregation_complete") is not True or not isinstance(response.get("message"), dict):
        return "IncompleteSSE: missing message_start or message_stop", True
    if response.get("aggregation_errors"):
        return "InvalidSSE: " + "; ".join(response["aggregation_errors"]), True
    return None, False


def mark_failed(capture: RequestCapture, error: str, archive_root: Path) -> Path:
    """将失败尝试移出轨迹导出扫描的 raw/completed 目录，单独归档。"""
    response = capture.response_record
    response["transport_error"] = error
    response["proxy_delivery"] = "rejected"
    source = capture.paths.completed_dir
    write_json(source / "response.json", response)
    state = json.loads((source / "state.json").read_text())
    state.update(state="partial", transport_error=error)
    write_json(source / "state.json", state)
    destination = archive_root / capture.capture_id
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    return destination


def create_app(settings: Settings, upstream_transport: Any = None) -> Any:
    import httpx
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import StreamingResponse

    capture_root = settings.log_dir / "raw"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize_capture_root(capture_root)
        app.state.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.timeout_seconds, connect=settings.connect_timeout_seconds,
                read=settings.read_timeout_seconds,
            ),
            follow_redirects=False, http2=False, transport=upstream_transport,
        )
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(title="Anthropic buffered retry capture proxy", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok", "upstream_url": settings.upstream_url,
            "capture_root": str(capture_root), "buffer_response": True,
            "retry_policy": settings.policy(),
        }

    @app.api_route(
        "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def buffered_proxy(path: str, request: Request) -> Response:
        raw_body = await request.body()
        headers = request_header_items(request)
        upstream_url = build_upstream_url(settings.upstream_url, request)
        group_id = "req_" + uuid.uuid4().hex
        archive_root = capture_root / "retry_attempts" / group_id
        # 模型推理、token 计数及只读端点支持重试。
        # 其他写入端点只尝试一次，避免重复执行其副作用。
        normalized = request.url.path.rstrip("/")
        can_retry = request.method in {"GET", "HEAD", "OPTIONS"} or (
            request.method == "POST" and (
                normalized.endswith("/v1/messages")
                or normalized.endswith("/v1/messages/count_tokens")
            )
        )
        max_attempts = 1 + (settings.max_retries if can_retry else 0)

        def new_capture(number: int) -> RequestCapture:
            capture = RequestCapture(capture_root)
            capture.start_request(
                method=request.method, path=request.url.path, query=request.url.query,
                url=str(request.url), headers=headers, raw_body=raw_body,
                upstream_url=upstream_url,
                client_host=request.client.host if request.client else None,
                client_port=request.client.port if request.client else None,
            )
            capture.request_record.update(proxy_request_id=group_id, proxy_attempt=number)
            write_json(capture.paths.inflight_dir / "request.json", capture.request_record)
            return capture

        def proxy_error(error: str, status: int, number: int) -> Response:
            content = compact_json_bytes({
                "type": "error", "error": {"type": "proxy_error", "message": error},
            })
            capture = new_capture(number)
            capture.start_response(
                status_code=status, headers=[("content-type", "application/json")],
                is_sse=False, source="proxy",
            )
            capture.append_response(content)
            capture.finalize(transport_error=error)
            return Response(content=content, status_code=status, media_type="application/json")

        def replay(capture: RequestCapture, response_headers: list[tuple[str, str]], body_path: Path) -> Response:
            async def chunks() -> AsyncIterator[bytes]:
                delivered = False
                delivery_error = None
                try:
                    with body_path.open("rb") as handle:
                        while chunk := await asyncio.to_thread(handle.read, 65536):
                            yield chunk
                    delivered = True
                except BaseException as exc:
                    delivery_error = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    capture.response_record["proxy_delivery"] = "delivered" if delivered else "interrupted"
                    if not delivered:
                        capture.response_record.update(
                            transport_error=delivery_error or "ClientDeliveryError: replay interrupted",
                            client_disconnected=True,
                        )
                        state_path = capture.paths.completed_dir / "state.json"
                        state = json.loads(state_path.read_text())
                        state.update(state="partial", client_disconnected=True,
                                     transport_error=capture.response_record["transport_error"])
                        write_json(state_path, state)
                    write_json(capture.paths.completed_dir / "response.json", capture.response_record)

            response = (
                Response(status_code=capture.response_record["status_code"])
                if request.method == "HEAD" else
                StreamingResponse(chunks(), status_code=capture.response_record["status_code"])
            )
            return apply_raw_response_headers(
                response, filtered_headers(response_headers, RESPONSE_HEADERS_TO_DROP),
            )

        async def run_attempts() -> Response:
            deadline = asyncio.get_running_loop().time() + settings.request_timeout_seconds
            last_error = "Upstream request failed"
            attempts = 0
            for number in range(1, max_attempts + 1):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return proxy_error(f"RequestTimeout: {last_error}", 504, attempts)
                attempts = number
                capture = new_capture(number)
                upstream_response = None
                response_headers: list[tuple[str, str]] = []
                retry_after = 0.0
                retryable = True
                retry_status = False
                try:
                    async with asyncio.timeout(min(settings.attempt_timeout_seconds, remaining)):
                        upstream_request = app.state.client.build_request(
                            request.method, upstream_url,
                            headers=filtered_headers(headers, REQUEST_HEADERS_TO_DROP), content=raw_body,
                        )
                        upstream_response = await app.state.client.send(upstream_request, stream=True)
                        response_headers = list(upstream_response.headers.multi_items())
                        is_sse = "text/event-stream" in upstream_response.headers.get("content-type", "").lower()
                        capture.start_response(
                            status_code=upstream_response.status_code,
                            headers=response_headers, is_sse=is_sse,
                        )
                        async for chunk in upstream_response.aiter_raw():
                            capture.append_response(chunk)
                    capture.finalize()
                    retry_status = upstream_response.status_code in RETRY_STATUS_CODES
                    if retry_status:
                        last_error = f"UpstreamHTTPError: HTTP {upstream_response.status_code}"
                        retry_after = retry_after_seconds(upstream_response.headers.get("retry-after"))
                    elif is_sse and 200 <= upstream_response.status_code < 300 and normalized.endswith("/v1/messages"):
                        error, retryable = stream_validation_error(capture)
                        if error is None:
                            return replay(capture, response_headers, capture.paths.completed_dir / "response.body")
                        last_error = error
                    else:
                        return replay(capture, response_headers, capture.paths.completed_dir / "response.body")
                except (httpx.TransportError, TimeoutError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    capture.abandon(last_error)
                except BaseException as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    if not capture.response_record:
                        capture.start_response(status_code=502, headers=[], is_sse=False, source="proxy")
                    capture.finalize(
                        transport_error=error, client_disconnected=isinstance(exc, asyncio.CancelledError),
                    )
                    mark_failed(capture, error, archive_root)
                    raise
                finally:
                    if upstream_response is not None:
                        await upstream_response.aclose()

                # 最后一次尝试仍返回 HTTP 错误时，保留上游状态、响应体和
                # Retry-After 等响应头，供客户端处理。
                if retry_status and number == max_attempts:
                    return replay(capture, response_headers, capture.paths.completed_dir / "response.body")
                mark_failed(capture, last_error, archive_root)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return proxy_error(f"RequestTimeout: {last_error}", 504, number)
                if not retryable or number == max_attempts:
                    return proxy_error(last_error, 502, number)
                backoff = min(settings.retry_max_delay_seconds,
                              settings.retry_delay_seconds * 2 ** min(number - 1, 30))
                delay = max(retry_after, random.uniform(backoff / 2, backoff))
                logger.warning("Buffered proxy %s attempt %s/%s failed: %s; retry in %.1fs",
                               group_id, number, max_attempts, last_error, min(delay, remaining))
                await asyncio.sleep(min(delay, remaining))
            raise AssertionError("unreachable")

        async def wait_disconnect() -> None:
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    return

        work = asyncio.create_task(run_attempts())
        disconnected = asyncio.create_task(wait_disconnect())
        try:
            done, _ = await asyncio.wait({work, disconnected}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return work.result()
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            return Response(status_code=499)
        finally:
            for task in (work, disconnected):
                if not task.done():
                    task.cancel()
            await asyncio.gather(work, disconnected, return_exceptions=True)

    return app


def parse_args(argv: Optional[list[str]] = None) -> Settings:
    parser = argparse.ArgumentParser(description="完整缓存 Anthropic 响应，失败重试后原样回放 SSE")
    parser.add_argument("--listen-host", default=os.getenv("PROXY_LISTEN_HOST", "0.0.0.0"))
    parser.add_argument("--listen-port", type=int, default=int(os.getenv("PROXY_LISTEN_PORT", "30303")))
    parser.add_argument("--upstream-url", default=os.getenv("UPSTREAM_URL"), required=os.getenv("UPSTREAM_URL") is None)
    parser.add_argument("--log-dir", type=Path, default=Path(os.getenv("CAPTURE_LOG_DIR", "capture_logs")))
    for flag, env_name, default, kind in (
        ("timeout-seconds", "UPSTREAM_TIMEOUT_SECONDS", 300, float),
        ("connect-timeout-seconds", "UPSTREAM_CONNECT_TIMEOUT_SECONDS", 30, float),
        ("read-timeout-seconds", "UPSTREAM_READ_TIMEOUT_SECONDS", 600, float),
        ("attempt-timeout-seconds", "UPSTREAM_ATTEMPT_TIMEOUT_SECONDS", 3600, float),
        ("request-timeout-seconds", "UPSTREAM_REQUEST_TIMEOUT_SECONDS", 86400, float),
        ("max-retries", "UPSTREAM_MAX_RETRIES", 20, int),
        ("retry-delay-seconds", "UPSTREAM_RETRY_DELAY_SECONDS", 1, float),
        ("retry-max-delay-seconds", "UPSTREAM_RETRY_MAX_DELAY_SECONDS", 30, float),
    ):
        parser.add_argument("--" + flag, type=kind, default=kind(os.getenv(env_name, str(default))))
    args = parser.parse_args(argv)
    try:
        return Settings(**vars(args))
    except ValueError as exc:
        parser.error(str(exc))


def main() -> None:
    import uvicorn
    settings = parse_args()
    uvicorn.run(create_app(settings), host=settings.listen_host, port=settings.listen_port, workers=1)


if __name__ == "__main__":
    main()
