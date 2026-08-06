import argparse
import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional

from capture_core import RequestCapture, initialize_capture_root


# 逐跳 header 只对当前 TCP/HTTP 连接有效，代理不能把它们直接传给下一跳。
# Host 和 Content-Length 由 httpx 根据新的上游 URL/body 重新生成。


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
REQUEST_HEADERS_TO_DROP = HOP_BY_HOP_HEADERS | {"host", "content-length"}
RESPONSE_HEADERS_TO_DROP = HOP_BY_HOP_HEADERS


@dataclass(frozen=True)
class Settings:
    listen_host: str
    listen_port: int
    upstream_url: str
    log_dir: Path
    timeout_seconds: float


def filtered_headers(
    headers: Iterable[tuple[str, str]], drop: set[str]
) -> list[tuple[str, str]]:
    """保持 header 的顺序和重复项，只删除不允许逐跳转发的字段。"""
    return [(key, value) for key, value in headers if key.lower() not in drop]


def request_header_items(request: Any) -> list[tuple[str, str]]:
    return [
        (key.decode("latin-1"), value.decode("latin-1"))
        for key, value in request.headers.raw
    ]


def build_upstream_url(base_url: str, request: Any) -> str:
    base = base_url.rstrip("/")
    query = request.url.query
    return f"{base}{request.url.path}" + (f"?{query}" if query else "")


def compact_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def apply_raw_response_headers(response: Any, headers: list[tuple[str, str]]) -> Any:
    """Starlette 的 headers 字典会合并重复项，直接设置 raw_headers 才能保留它们。"""
    response.raw_headers = [
        (key.encode("latin-1"), value.encode("latin-1")) for key, value in headers
    ]
    return response


def create_app(settings: Settings, upstream_transport: Any = None) -> Any:
    import httpx
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import StreamingResponse

    capture_root = settings.log_dir / "raw"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize_capture_root(capture_root)
        timeout = httpx.Timeout(settings.timeout_seconds, read=None)
        app.state.client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            http2=False,
            transport=upstream_transport,
        )
        try:
            yield
        finally:
            await app.state.client.aclose()

    app = FastAPI(title="Anthropic full capture proxy", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "upstream_url": settings.upstream_url,
            "capture_root": str(capture_root),
        }

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def transparent_proxy(path: str, request: Request) -> Response:
        # 请求 body 先完整读取一次：同一份原始 bytes 同时用于落盘和上游转发，
        # 不重新 json.dumps，因此不会改变字段顺序、空白或模型参数。
        raw_body = await request.body()
        incoming_headers = request_header_items(request)
        upstream_url = build_upstream_url(settings.upstream_url, request)
        capture = RequestCapture(capture_root)

        # 每个入站请求创建独立 capture 目录。主/子 agent 即使并发，数据也不会写进同一文件。
        capture.start_request(
            method=request.method,
            path=request.url.path,
            query=request.url.query,
            url=str(request.url),
            headers=incoming_headers,
            raw_body=raw_body,
            upstream_url=upstream_url,
            client_host=request.client.host if request.client else None,
            client_port=request.client.port if request.client else None,
        )

        upstream_headers = filtered_headers(incoming_headers, REQUEST_HEADERS_TO_DROP)
        upstream_request = app.state.client.build_request(
            request.method,
            upstream_url,
            headers=upstream_headers,
            content=raw_body,
        )

        try:
            upstream_response = await app.state.client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            error_body = compact_json_bytes(
                {"type": "error", "error": {"type": "proxy_error", "message": error_text}}
            )
            response_headers = [("content-type", "application/json")]
            capture.start_response(
                status_code=502,
                headers=response_headers,
                is_sse=False,
                source="proxy",
            )
            capture.append_response(error_body)
            capture.finalize(transport_error=error_text)
            return Response(content=error_body, status_code=502, media_type="application/json")

        upstream_response_headers = list(upstream_response.headers.multi_items())
        content_type = upstream_response.headers.get("content-type", "")
        is_sse = "text/event-stream" in content_type.lower()
        capture.start_response(
            status_code=upstream_response.status_code,
            headers=upstream_response_headers,
            is_sse=is_sse,
        )
        client_response_headers = filtered_headers(
            upstream_response_headers, RESPONSE_HEADERS_TO_DROP
        )

        if not is_sse:
            # 非流式响应必须收齐后才能构造普通 Response；仍按 chunk 逐段写入事实源文件。
            try:
                response_chunks: list[bytes] = []
                async for chunk in upstream_response.aiter_raw():
                    capture.append_response(chunk)
                    response_chunks.append(chunk)
                response_body = b"".join(response_chunks)
                capture.finalize()
            except BaseException as exc:
                error_text = f"{type(exc).__name__}: {exc}"
                capture.finalize(
                    transport_error=error_text,
                    client_disconnected=isinstance(exc, asyncio.CancelledError),
                )
                raise
            finally:
                await upstream_response.aclose()
            response = Response(
                content=response_body,
                status_code=upstream_response.status_code,
            )
            return apply_raw_response_headers(response, client_response_headers)

        async def relay() -> AsyncIterator[bytes]:
            # relay 是单个请求独享的异步生成器，capture 也是局部变量；并发请求之间
            # 不共享 response.body 文件句柄或 SSE 聚合状态。
            transport_error: Optional[str] = None
            client_disconnected = False
            try:
                async for chunk in upstream_response.aiter_raw():
                    # 先把上游字节写入本请求的原始文件，再原样交给 CC；聚合器不修改转发内容。
                    capture.append_response(chunk)
                    yield chunk
            except BaseException as exc:
                transport_error = f"{type(exc).__name__}: {exc}"
                client_disconnected = isinstance(exc, asyncio.CancelledError)
                raise
            finally:
                await upstream_response.aclose()
                capture.finalize(
                    transport_error=transport_error,
                    client_disconnected=client_disconnected,
                )

        response = StreamingResponse(
            relay(),
            status_code=upstream_response.status_code,
        )
        return apply_raw_response_headers(response, client_response_headers)

    return app


def parse_args(argv: Optional[list[str]] = None) -> Settings:
    parser = argparse.ArgumentParser(
        description="透明转发 Anthropic API，并完整保存每次请求、响应和流式事件"
    )
    parser.add_argument(
        "--listen-host",
        default=os.getenv("PROXY_LISTEN_HOST", "0.0.0.0"),
        help="代理监听地址，默认读取 PROXY_LISTEN_HOST",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        default=int(os.getenv("PROXY_LISTEN_PORT", "30303")),
        help="代理监听端口，默认读取 PROXY_LISTEN_PORT",
    )
    parser.add_argument(
        "--upstream-url",
        default=os.getenv("UPSTREAM_URL"),
        required=os.getenv("UPSTREAM_URL") is None,
        help="上游 Anthropic 兼容服务 base URL，不要包含 /v1/messages",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(os.getenv("CAPTURE_LOG_DIR", "capture_logs")),
        help="原始采集目录，默认读取 CAPTURE_LOG_DIR",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "300")),
        help="连接/写入超时；流式读取不设置总超时",
    )
    args = parser.parse_args(argv)
    return Settings(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        upstream_url=args.upstream_url,
        log_dir=args.log_dir,
        timeout_seconds=args.timeout_seconds,
    )


def main() -> None:
    import uvicorn

    settings = parse_args()
    # 单 worker 已能并发处理多个 CC，并避免不同进程同时管理同一个监听端口和生命周期。
    uvicorn.run(create_app(settings), host=settings.listen_host, port=settings.listen_port, workers=1)


if __name__ == "__main__":
    main()
