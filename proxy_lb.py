import argparse
import asyncio
import os
import ipaddress
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from capture_core import RequestCapture, initialize_capture_root

# 共用原代理的透明转发工具；原 proxy.py 保持不变。
from proxy import (
    REQUEST_HEADERS_TO_DROP,
    RESPONSE_HEADERS_TO_DROP,
    apply_raw_response_headers,
    build_upstream_url,
    compact_json_bytes,
    filtered_headers,
    request_header_items,
)


@dataclass(frozen=True)
class Settings:
    listen_host: str
    listen_port: int
    upstream_urls: tuple[str, ...]
    balance: str
    log_dir: Path
    timeout_seconds: float


def load_backends(path: Path) -> tuple[str, ...]:
    """每行一个 IP:端口；IPv6 使用 [IP]:端口。统一使用 HTTP。"""
    urls: list[str] = []
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            host, port_text = line.rsplit(":", 1)
            if host.startswith("[") and host.endswith("]"):
                ip = ipaddress.IPv6Address(host[1:-1])
            else:
                ip = ipaddress.IPv4Address(host)
            if not port_text.isascii() or not port_text.isdecimal():
                raise ValueError("端口必须是整数")
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise ValueError("端口必须在 1–65535 之间")
            address = f"[{ip}]" if ip.version == 6 else str(ip)
            url = f"http://{address}:{port}"
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: 需要 IP:端口（IPv6 用 [IP]:端口）: {line!r}") from exc
        if url in urls:
            raise ValueError(f"{path}:{number}: 后端地址重复: {line}")
        urls.append(url)
    if not urls:
        raise ValueError(f"{path}: 后端列表不能为空")
    return tuple(urls)


class BackendPool:
    def __init__(self, urls: tuple[str, ...], balance: str):
        if not urls:
            raise ValueError("后端列表不能为空")
        if balance not in {"round-robin", "random"}:
            raise ValueError("balance 必须是 round-robin 或 random")
        self.urls = urls
        self.balance = balance
        self.next_index = 0

    def select(self) -> str:
        # 单 worker 的事件循环内没有 await，选择和索引更新不会被其他请求打断。
        if self.balance == "random":
            return random.choice(self.urls)
        url = self.urls[self.next_index]
        self.next_index = (self.next_index + 1) % len(self.urls)
        return url


def create_app(settings: Settings, upstream_transport: Any = None) -> Any:
    import httpx
    from fastapi import FastAPI, Request, Response
    from fastapi.responses import StreamingResponse

    capture_root = settings.log_dir / "raw"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        initialize_capture_root(capture_root)
        app.state.backend_pool = BackendPool(settings.upstream_urls, settings.balance)
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

    app = FastAPI(title="Anthropic load-balanced full capture proxy", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "upstream_urls": list(settings.upstream_urls),
            "balance": settings.balance,
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
        selected_backend = app.state.backend_pool.select()
        upstream_url = build_upstream_url(selected_backend, request)
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
        "--backends-file",
        type=Path,
        default=os.getenv("UPSTREAM_BACKENDS_FILE"),
        required=os.getenv("UPSTREAM_BACKENDS_FILE") is None,
        help="后端列表文件，每行一个 IP:端口；读取 UPSTREAM_BACKENDS_FILE",
    )
    parser.add_argument(
        "--balance",
        choices=["round-robin", "random"],
        default=os.getenv("PROXY_BALANCE", "round-robin"),
        help="均衡方式，默认 round-robin；也可设置 PROXY_BALANCE",
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
    try:
        upstream_urls = load_backends(args.backends_file)
        if args.balance not in {"round-robin", "random"}:
            raise ValueError("PROXY_BALANCE 必须是 round-robin 或 random")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return Settings(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        upstream_urls=upstream_urls,
        balance=args.balance,
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
