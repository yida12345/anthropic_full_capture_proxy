from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import json
import os
import shutil
import time
import uuid
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


SCHEMA_VERSION = 1
SENSITIVE_REQUEST_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "x-api-key",
}


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def write_json(path: Path, value: Any, *, compact: bool = False) -> None:
    """先写临时文件再替换，避免进程中断后留下半个 JSON。

    默认使用缩进格式，便于人工检查代理和 finalize 产物；``compact=True`` 时使用
    无额外空白的单行 JSON，适合 ShareGPT/JSONL 风格的训练数据文件。两种模式都
    保留 UTF-8 中文原文，并在文件末尾写一个换行符。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex}")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        if compact:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def append_jsonl(path: Path, value: Any) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def parse_json_object(raw_body: bytes) -> Optional[dict[str, Any]]:
    try:
        value = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def body_representation(raw_body: bytes) -> dict[str, Any]:
    """在最终数据集中保留可解析内容，同时保留原始字节的可逆表示。"""
    parsed: Any = None
    is_json = False
    try:
        parsed = json.loads(raw_body)
        is_json = True
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass

    result: dict[str, Any] = {
        "size_bytes": len(raw_body),
        "sha256": sha256_bytes(raw_body),
    }
    if is_json:
        result["json"] = parsed
    try:
        result["raw_utf8"] = raw_body.decode("utf-8")
    except UnicodeDecodeError:
        result["raw_base64"] = base64.b64encode(raw_body).decode("ascii")
    return result


def header_records(
    headers: Iterable[tuple[str, str]], *, redact_sensitive: bool = True
) -> list[dict[str, Any]]:
    """使用列表而不是字典，保留同名 header 的原始顺序和重复项。"""
    records: list[dict[str, Any]] = []
    for name, value in headers:
        record: dict[str, Any] = {"name": name}
        if redact_sensitive and name.lower() in SENSITIVE_REQUEST_HEADERS:
            encoded = value.encode("utf-8", errors="surrogatepass")
            record.update(
                {
                    "value": "<redacted>",
                    "redacted": True,
                    "value_sha256": sha256_bytes(encoded),
                }
            )
        else:
            record["value"] = value
        records.append(record)
    return records


def extract_header_value(headers: Iterable[tuple[str, str]], name: str) -> Optional[str]:
    target = name.lower()
    for key, value in headers:
        if key.lower() == target:
            return value
    return None


def decode_content_body(raw_body: bytes, content_encoding: Optional[str]) -> bytes:
    """解码常见 HTTP Content-Encoding；原始字节仍由 response.body 单独保存。"""
    if not content_encoding:
        return raw_body
    result = raw_body
    encodings = [value.strip().lower() for value in content_encoding.split(",") if value.strip()]
    for encoding in reversed(encodings):
        if encoding in {"identity", ""}:
            continue
        if encoding in {"gzip", "x-gzip"}:
            result = gzip.decompress(result)
        elif encoding == "deflate":
            try:
                result = zlib.decompress(result)
            except zlib.error:
                result = zlib.decompress(result, -zlib.MAX_WBITS)
        else:
            raise ValueError(f"暂不支持解码 Content-Encoding: {encoding}")
    return result


class AnthropicMessageAggregator:
    """把 Anthropic SSE 事件聚合为与非流式响应等价的 Message。

    原始 SSE 始终另行保存；聚合失败只会让 complete=false，不会丢掉原始响应。
    """

    def __init__(self) -> None:
        self.message: Optional[dict[str, Any]] = None
        self.complete = False
        self.started = False
        self.errors: list[str] = []
        self._tool_json_buffers: dict[int, str] = {}

    @property
    def message_id(self) -> Optional[str]:
        if not isinstance(self.message, dict):
            return None
        value = self.message.get("id")
        return value if isinstance(value, str) else None

    def consume(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type")

        if event_type == "message_start":
            message = payload.get("message")
            if not isinstance(message, dict):
                self.errors.append("message_start.message 不是对象")
                return
            self.message = copy.deepcopy(message)
            if not isinstance(self.message.get("content"), list):
                self.message["content"] = []
            self.started = True
            return

        if event_type == "error":
            self.errors.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            return

        if event_type == "ping":
            return

        if self.message is None:
            self.errors.append(f"在 message_start 之前收到事件: {event_type!r}")
            return

        if event_type == "content_block_start":
            index = payload.get("index")
            block = payload.get("content_block")
            if not isinstance(index, int) or index < 0 or not isinstance(block, dict):
                self.errors.append("content_block_start 缺少合法 index/content_block")
                return
            content = self.message.setdefault("content", [])
            while len(content) <= index:
                content.append(None)
            content[index] = copy.deepcopy(block)
            if block.get("type") in {"tool_use", "server_tool_use"}:
                self._tool_json_buffers[index] = ""
            return

        if event_type == "content_block_delta":
            self._consume_content_delta(payload)
            return

        if event_type == "content_block_stop":
            index = payload.get("index")
            if isinstance(index, int):
                self._finish_tool_json(index)
            return

        if event_type == "message_delta":
            delta = payload.get("delta")
            if isinstance(delta, dict):
                for key, value in delta.items():
                    self.message[key] = copy.deepcopy(value)
            usage = payload.get("usage")
            if isinstance(usage, dict):
                current_usage = self.message.get("usage")
                if not isinstance(current_usage, dict):
                    current_usage = {}
                    self.message["usage"] = current_usage
                current_usage.update(copy.deepcopy(usage))
            return

        if event_type == "message_stop":
            for index in list(self._tool_json_buffers):
                self._finish_tool_json(index)
            self.complete = True

    def _content_block(self, index: int) -> Optional[dict[str, Any]]:
        if not isinstance(self.message, dict):
            return None
        content = self.message.get("content")
        if not isinstance(content, list) or index < 0 or index >= len(content):
            return None
        block = content[index]
        return block if isinstance(block, dict) else None

    def _consume_content_delta(self, payload: dict[str, Any]) -> None:
        index = payload.get("index")
        delta = payload.get("delta")
        if not isinstance(index, int) or not isinstance(delta, dict):
            self.errors.append("content_block_delta 缺少合法 index/delta")
            return
        block = self._content_block(index)
        if block is None:
            self.errors.append(f"content_block_delta 指向不存在的 index={index}")
            return

        delta_type = delta.get("type")
        if delta_type == "text_delta" and isinstance(delta.get("text"), str):
            block["text"] = str(block.get("text") or "") + delta["text"]
        elif delta_type == "thinking_delta" and isinstance(delta.get("thinking"), str):
            block["thinking"] = str(block.get("thinking") or "") + delta["thinking"]
        elif delta_type == "signature_delta" and isinstance(delta.get("signature"), str):
            block["signature"] = delta["signature"]
        elif delta_type == "input_json_delta" and isinstance(delta.get("partial_json"), str):
            self._tool_json_buffers[index] = self._tool_json_buffers.get(index, "") + delta["partial_json"]
        elif delta_type == "citations_delta" and isinstance(delta.get("citation"), dict):
            citations = block.get("citations")
            if not isinstance(citations, list):
                citations = []
                block["citations"] = citations
            citations.append(copy.deepcopy(delta["citation"]))

    def _finish_tool_json(self, index: int) -> None:
        if index not in self._tool_json_buffers:
            return
        raw_json = self._tool_json_buffers.pop(index)
        if not raw_json:
            return
        block = self._content_block(index)
        if block is None:
            return
        try:
            block["input"] = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            block["input_json_partial"] = raw_json
            self.errors.append(f"index={index} 的 tool input JSON 不完整: {exc}")

    def snapshot(self) -> Optional[dict[str, Any]]:
        return copy.deepcopy(self.message)


class SSEDecoder:
    """增量解析 SSE；能够处理事件行和 JSON 数据跨网络 chunk 的情况。

    网络 chunk 只是传输层分片，既不保证按行结束，也不保证一个 chunk 对应一个
    SSE event。因此 feed() 只按换行拆行，直到空行才 dispatch 完整事件。
    """

    def __init__(self, aggregator: AnthropicMessageAggregator) -> None:
        self.aggregator = aggregator
        self._buffer = b""
        self._event_name: Optional[str] = None
        self._data_lines: list[bytes] = []
        self.event_count = 0

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        if not chunk:
            return []
        self._buffer += chunk
        records: list[dict[str, Any]] = []
        while b"\n" in self._buffer:
            raw_line, self._buffer = self._buffer.split(b"\n", 1)
            record = self._consume_line(raw_line.rstrip(b"\r"))
            if record is not None:
                records.append(record)
        return records

    def finish(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if self._buffer:
            record = self._consume_line(self._buffer.rstrip(b"\r"))
            self._buffer = b""
            if record is not None:
                records.append(record)
        if self._event_name is not None or self._data_lines:
            record = self._dispatch()
            if record is not None:
                records.append(record)
        return records

    def _consume_line(self, line: bytes) -> Optional[dict[str, Any]]:
        if not line:
            return self._dispatch()
        if line.startswith(b":"):
            return None
        field, separator, value = line.partition(b":")
        if separator and value.startswith(b" "):
            value = value[1:]
        if field == b"event":
            self._event_name = value.decode("utf-8", errors="replace")
        elif field == b"data":
            self._data_lines.append(value)
        return None

    def _dispatch(self) -> Optional[dict[str, Any]]:
        if self._event_name is None and not self._data_lines:
            return None
        data_bytes = b"\n".join(self._data_lines)
        event_name = self._event_name
        self._event_name = None
        self._data_lines = []

        data_text = data_bytes.decode("utf-8", errors="replace")
        payload: Any = None
        if data_text and data_text != "[DONE]":
            try:
                payload = json.loads(data_text)
            except json.JSONDecodeError:
                payload = None
        if isinstance(payload, dict):
            self.aggregator.consume(payload)

        self.event_count += 1
        record: dict[str, Any] = {
            "sequence": self.event_count,
            "received_at": utc_timestamp(),
            "event": event_name,
            "data_raw": data_text,
        }
        if payload is not None:
            record["data"] = payload
        return record


@dataclass(frozen=True)
class CapturePaths:
    capture_id: str
    inflight_dir: Path
    completed_dir: Path


class RequestCapture:
    """一次入站 HTTP 请求的全部采集状态。

    该对象只服务一个请求；并发请求从不共享 response 文件或 SSE 缓冲区。
    """

    def __init__(self, root: Path, capture_id: Optional[str] = None) -> None:
        value = capture_id or "cap_" + uuid.uuid4().hex
        self.paths = CapturePaths(
            capture_id=value,
            inflight_dir=root / "inflight" / value,
            completed_dir=root / "completed" / value,
        )
        self.started_at = utc_timestamp()
        self._started_monotonic = time.monotonic()
        self.request_record: dict[str, Any] = {}
        self.response_record: dict[str, Any] = {}
        self._response_handle: Any = None
        self._events_handle: Any = None
        self._response_hash = hashlib.sha256()
        self._response_size = 0
        self._decoder: Optional[SSEDecoder] = None
        self._response_content_encoding: Optional[str] = None
        self._defer_sse_decode = False
        self._finalized = False

    @property
    def capture_id(self) -> str:
        return self.paths.capture_id

    def start_request(
        self,
        *,
        method: str,
        path: str,
        query: str,
        url: str,
        headers: list[tuple[str, str]],
        raw_body: bytes,
        upstream_url: str,
        client_host: Optional[str],
        client_port: Optional[int],
    ) -> None:
        # 先进入 inflight。只有 finalize() 完成元数据写入后，整个目录才原子移动到
        # completed；因此目录位置本身也表达了请求是否已经收尾。
        self.paths.inflight_dir.mkdir(parents=True, exist_ok=False)
        (self.paths.inflight_dir / "request.body").write_bytes(raw_body)
        request_json = parse_json_object(raw_body)
        self.request_record = {
            "schema_version": SCHEMA_VERSION,
            "capture_id": self.capture_id,
            "captured_at": self.started_at,
            "method": method,
            "path": path,
            "query": query,
            "url": url,
            "upstream_url": upstream_url,
            "headers": header_records(headers, redact_sensitive=True),
            "content_type": extract_header_value(headers, "content-type"),
            "body_size_bytes": len(raw_body),
            "body_sha256": sha256_bytes(raw_body),
            "body_json": request_json,
            "client": {"host": client_host, "port": client_port},
            "is_messages_request": method.upper() == "POST" and path.rstrip("/") == "/v1/messages",
        }
        write_json(self.paths.inflight_dir / "request.json", self.request_record)
        write_json(
            self.paths.inflight_dir / "state.json",
            {
                "schema_version": SCHEMA_VERSION,
                "capture_id": self.capture_id,
                "state": "request_captured",
                "updated_at": utc_timestamp(),
            },
        )

    def start_response(
        self,
        *,
        status_code: int,
        headers: list[tuple[str, str]],
        is_sse: bool,
        source: str = "upstream",
    ) -> None:
        self.response_record = {
            "schema_version": SCHEMA_VERSION,
            "capture_id": self.capture_id,
            "started_at": utc_timestamp(),
            "status_code": status_code,
            "headers": header_records(headers, redact_sensitive=False),
            "content_type": extract_header_value(headers, "content-type"),
            "request_id": extract_header_value(headers, "request-id"),
            "stream": is_sse,
            "source": source,
        }
        self._response_content_encoding = extract_header_value(headers, "content-encoding")
        self._response_handle = (self.paths.inflight_dir / "response.body").open("wb")
        if is_sse:
            self._decoder = SSEDecoder(AnthropicMessageAggregator())
            self._defer_sse_decode = bool(
                self._response_content_encoding
                and self._response_content_encoding.lower().strip() != "identity"
            )
            self._events_handle = (self.paths.inflight_dir / "sse_events.jsonl").open(
                "w", encoding="utf-8", newline="\n"
            )
        write_json(
            self.paths.inflight_dir / "state.json",
            {
                "schema_version": SCHEMA_VERSION,
                "capture_id": self.capture_id,
                "state": "streaming" if is_sse else "receiving_response",
                "updated_at": utc_timestamp(),
            },
        )

    def append_response(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self._response_handle is None:
            raise RuntimeError("start_response must be called before append_response")
        self._response_handle.write(chunk)
        self._response_hash.update(chunk)
        self._response_size += len(chunk)
        if self._decoder is not None and not self._defer_sse_decode:
            for event_record in self._decoder.feed(chunk):
                self._write_event(event_record)

    def _write_event(self, event_record: dict[str, Any]) -> None:
        if self._events_handle is None:
            return
        self._events_handle.write(
            json.dumps(event_record, ensure_ascii=False, separators=(",", ":")) + "\n"
        )

    def finalize(
        self,
        *,
        transport_error: Optional[str] = None,
        client_disconnected: bool = False,
    ) -> None:
        # finalize 设计为幂等，流式生成器的 finally 和其他异常清理路径即使重复调用
        # 也不会重复关闭句柄或第二次移动目录。
        if self._finalized:
            return
        self._finalized = True
        if self._response_handle is not None:
            self._response_handle.flush()
            self._response_handle.close()
            self._response_handle = None

        response_path = self.paths.inflight_dir / "response.body"
        raw_body = response_path.read_bytes() if response_path.exists() else b""
        decoded_body = raw_body
        decoding_error: Optional[str] = None
        try:
            decoded_body = decode_content_body(raw_body, self._response_content_encoding)
        except (OSError, EOFError, ValueError, zlib.error) as exc:
            decoding_error = f"{type(exc).__name__}: {exc}"

        if self._decoder is not None:
            if self._defer_sse_decode and decoding_error is None:
                for event_record in self._decoder.feed(decoded_body):
                    self._write_event(event_record)
            if decoding_error is not None:
                self._decoder.aggregator.errors.append(decoding_error)
            for event_record in self._decoder.finish():
                self._write_event(event_record)
        if self._events_handle is not None:
            self._events_handle.flush()
            self._events_handle.close()
            self._events_handle = None

        aggregator = self._decoder.aggregator if self._decoder is not None else None
        non_stream_json = parse_json_object(decoded_body) if self._decoder is None else None
        message_id: Optional[str] = None
        if aggregator is not None:
            message_id = aggregator.message_id
        elif isinstance(non_stream_json, dict) and isinstance(non_stream_json.get("id"), str):
            message_id = non_stream_json["id"]

        aggregation_complete = bool(aggregator and aggregator.complete)
        self.response_record.update(
            {
                "finished_at": utc_timestamp(),
                "duration_ms": round((time.monotonic() - self._started_monotonic) * 1000),
                "body_size_bytes": self._response_size,
                "body_sha256": "sha256:" + self._response_hash.hexdigest(),
                "content_encoding": self._response_content_encoding,
                "decoded_body_size_bytes": len(decoded_body),
                "decoded_body_sha256": sha256_bytes(decoded_body),
                "content_decoding_error": decoding_error,
                "body_json": non_stream_json,
                "message_id": message_id,
                "message": aggregator.snapshot() if aggregator is not None else non_stream_json,
                "aggregation_complete": aggregation_complete if aggregator is not None else None,
                "aggregation_errors": aggregator.errors if aggregator is not None else [],
                "sse_event_count": self._decoder.event_count if self._decoder is not None else 0,
                "transport_error": transport_error,
                "client_disconnected": client_disconnected,
            }
        )
        write_json(self.paths.inflight_dir / "response.json", self.response_record)

        if transport_error:
            state = "partial"
        elif aggregator is not None and not aggregator.complete:
            state = "partial"
        else:
            state = "complete"
        write_json(
            self.paths.inflight_dir / "state.json",
            {
                "schema_version": SCHEMA_VERSION,
                "capture_id": self.capture_id,
                "state": state,
                "updated_at": utc_timestamp(),
                "message_id": message_id,
                "transport_error": transport_error,
                "client_disconnected": client_disconnected,
            },
        )
        self.paths.completed_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(self.paths.inflight_dir, self.paths.completed_dir)

    def abandon(self, error: str) -> None:
        """仅在尚未形成响应时使用，仍把请求移入 completed 供后处理审计。"""
        if not self.response_record:
            self.start_response(
                status_code=502,
                headers=[("content-type", "application/json")],
                is_sse=False,
                source="proxy",
            )
            body = json.dumps(
                {"type": "error", "error": {"type": "proxy_error", "message": error}},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self.append_response(body)
        self.finalize(transport_error=error)


def initialize_capture_root(root: Path) -> None:
    (root / "inflight").mkdir(parents=True, exist_ok=True)
    (root / "completed").mkdir(parents=True, exist_ok=True)


def copy_capture_directory(source: Path, destination: Path) -> None:
    """测试和离线工具使用的安全复制辅助函数。"""
    if destination.exists():
        raise FileExistsError(destination)
    shutil.copytree(source, destination)
