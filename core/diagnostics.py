"""脱敏的尝试级诊断（v1.4.1 C-TIMEOUT §5.8，L3 上传窗口）。

目标：失败时能回答"**卡在哪一段、传了多少字节、花了多久、预算多少**"，而日志与
``task.json`` 里**不出现**图片内容、base64、凭据或完整 URL。

事实来源：
* 上传字节数来自 aiohttp 的 ``on_request_chunk_sent`` 回调（真·分片计数），
  不是按图片大小猜的；
* 请求阶段来自 ``on_request_start`` / ``on_request_end`` / ``on_response_chunk_received``
  的真实回调顺序；
* 只有在拿不到任何 trace 事实时（例如原生节点走 AstrBot 自己的 provider，
  或测试里的替身 session）才退回"按异常类型归类"，并显式标 ``phase=unknown``。

本模块不依赖 AstrBot，也不做任何 IO；所有字段都是元数据。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

try:  # pragma: no cover - 取决于运行环境
    import aiohttp
except Exception:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

PHASE_CONNECT = "connect"
PHASE_UPLOAD = "upload"
PHASE_WAIT_RESPONSE = "wait_response"
PHASE_READ_BODY = "read_body"
PHASE_UNKNOWN = "unknown"

MAX_DIAGNOSTIC_ENTRIES = 12
MAX_DETAIL_CHARS = 160

_CURRENT: ContextVar["AttemptTrace | None"] = ContextVar("imago_fusion_attempt_trace", default=None)


@dataclass(slots=True)
class AttemptTrace:
    """单次适配器调用（一个 attempt）的上传事实。"""

    started_at: float = 0.0
    request_started_at: float | None = None
    request_ended_at: float | None = None
    response_started_at: float | None = None
    last_chunk_at: float | None = None
    up_bytes: int = 0
    chunks: int = 0
    requests: int = 0
    notes: list[str] = field(default_factory=list)

    def begin(self) -> "AttemptTrace":
        self.started_at = time.monotonic()
        return self

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - (self.started_at or time.monotonic()))

    def record_chunk(self, size: int) -> None:
        self.chunks += 1
        self.up_bytes += max(0, int(size))
        self.last_chunk_at = time.monotonic()

    def phase(self, exc: BaseException | None = None) -> str:
        """按 trace 事实判定阶段；没有事实时才看异常类型。"""
        if self.request_started_at is None:
            if self.requests == 0 and exc is not None and _looks_like_connect(exc):
                return PHASE_CONNECT
            return PHASE_UNKNOWN if self.requests == 0 else PHASE_CONNECT
        if self.response_started_at is not None:
            return PHASE_READ_BODY
        if self.request_ended_at is not None:
            return PHASE_WAIT_RESPONSE
        if self.chunks > 0:
            return PHASE_UPLOAD
        return PHASE_UPLOAD if self.requests else PHASE_CONNECT


def _looks_like_connect(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    return any(token in name for token in ("connector", "connect", "dns", "ssl", "certificate"))


def fallback_phase(exc: BaseException) -> str:
    """无 trace 事实时的保守归类：类型名→阶段，未知就 ``unknown``。"""
    name = type(exc).__name__.lower()
    if any(token in name for token in ("connector", "connect", "dns", "ssl", "certificate")):
        return PHASE_CONNECT
    if "timeout" in name:
        return PHASE_WAIT_RESPONSE
    if any(token in name for token in ("payload", "disconnect", "reset", "response", "protocol")):
        return PHASE_READ_BODY
    return PHASE_UNKNOWN


@contextmanager
def attempt_scope() -> Iterator[AttemptTrace]:
    """为一个 attempt 打开 trace 收集作用域（异常/取消都安全退出）。"""
    trace = AttemptTrace().begin()
    token = _CURRENT.set(trace)
    try:
        yield trace
    finally:
        _CURRENT.reset(token)


def current() -> AttemptTrace | None:
    """取当前 attempt 的 trace（没有作用域时返回 ``None``）。"""
    return _CURRENT.get()


def push(trace: AttemptTrace) -> object:
    """把 trace 绑到当前上下文（返回重置令牌，配合 :func:`pop`）。"""
    return _CURRENT.set(trace)


def pop(token: object) -> None:
    """恢复绑定前的上下文（``token`` 由 :func:`push` 返回）。"""
    try:
        _CURRENT.reset(token)  # type: ignore[arg-type]
    except (ValueError, RuntimeError):  # pragma: no cover - 跨上下文重置时的保守处理
        pass


def _chunk_size(params: Any) -> int:
    chunk = getattr(params, "chunk", None)
    if isinstance(chunk, (bytes, bytearray, memoryview)):
        return len(chunk)
    for attr in ("length", "size", "bytes_sent"):
        value = getattr(params, attr, None)
        if isinstance(value, int):
            return max(0, value)
    return 0


def build_trace_configs() -> tuple[Any, ...]:
    """构造挂在 ``aiohttp.ClientSession(trace_configs=…)`` 上的 trace 配置。

    Returns:
        长度为 0（aiohttp 不可用）或 1 的元组，可直接展开进 ``ClientSession``。
    """
    if aiohttp is None:  # pragma: no cover - 运行环境总是带 aiohttp
        return ()
    trace_config = aiohttp.TraceConfig()

    async def _on_request_start(_session, _ctx, _params) -> None:
        trace = _CURRENT.get()
        if trace is None:
            return
        trace.requests += 1
        if trace.request_started_at is None:
            trace.request_started_at = time.monotonic()

    async def _on_request_chunk_sent(_session, _ctx, params) -> None:
        trace = _CURRENT.get()
        if trace is None:
            return
        trace.record_chunk(_chunk_size(params))

    async def _on_request_end(_session, _ctx, _params) -> None:
        trace = _CURRENT.get()
        if trace is not None:
            trace.request_ended_at = time.monotonic()

    async def _on_request_exception(_session, _ctx, _params) -> None:
        trace = _CURRENT.get()
        if trace is not None and trace.request_ended_at is None:
            trace.request_ended_at = time.monotonic()

    async def _on_response_chunk_received(_session, _ctx, _params) -> None:
        trace = _CURRENT.get()
        if trace is not None and trace.response_started_at is None:
            trace.response_started_at = time.monotonic()

    trace_config.on_request_start.append(_on_request_start)
    trace_config.on_request_chunk_sent.append(_on_request_chunk_sent)
    trace_config.on_request_end.append(_on_request_end)
    trace_config.on_request_exception.append(_on_request_exception)
    trace_config.on_response_chunk_received.append(_on_response_chunk_received)
    return (trace_config,)


def record(
    task: Any,
    *,
    provider_id: str,
    model: str,
    phase: str,
    elapsed: float,
    up_bytes: int,
    timeout: float,
    error_type: str,
    status: int | None = None,
    detail: str = "",
) -> dict:
    """把一条脱敏诊断追加进 ``task.runtime["attempt_diagnostics"]``。

    Returns:
        写入的那条记录（测试可直接断言，不必再读 runtime）。
    """
    entry = {
        "provider_id": str(provider_id or "")[:64],
        "model": str(model or "")[:64],
        "phase": str(phase or PHASE_UNKNOWN)[:24],
        "elapsed": round(max(0.0, float(elapsed)), 2),
        "up_bytes": max(0, int(up_bytes)),
        "timeout": round(max(0.0, float(timeout)), 2),
        "error_type": str(error_type or "")[:64],
        "status": status,
        "detail": str(detail or "")[:MAX_DETAIL_CHARS],
    }
    runtime = getattr(task, "runtime", None)
    if isinstance(runtime, dict):
        entries = runtime.setdefault("attempt_diagnostics", [])
        if isinstance(entries, list):
            entries.append(entry)
            del entries[:-MAX_DIAGNOSTIC_ENTRIES]
    return entry


def log_line(
    *,
    provider_id: str,
    model: str,
    phase: str,
    elapsed: float,
    up_bytes: int,
    timeout: float,
    error_type: str,
    detail: str = "",
) -> str:
    """§5.8 要求的单行日志形态（不含 URL/图片/凭据）。"""
    suffix = f" detail={str(detail or '')[:MAX_DETAIL_CHARS]}" if detail else ""
    return (
        f"绘图模型失败 node={str(provider_id or '')[:64]} model={str(model or '')[:64]} "
        f"phase={str(phase or PHASE_UNKNOWN)} elapsed={max(0.0, float(elapsed)):.1f}s "
        f"up_bytes={max(0, int(up_bytes))} timeout={max(0.0, float(timeout)):.1f}s "
        f"error_type={str(error_type or '')[:64]}{suffix}"
    )


def as_manifest(entries: Any) -> list[dict]:
    """把 runtime 里的诊断列表整理成可写入 task.json 的形状（有界、已脱敏）。"""
    if not isinstance(entries, list):
        return []
    cleaned: list[dict] = []
    for entry in entries[-MAX_DIAGNOSTIC_ENTRIES:]:
        if not isinstance(entry, dict):
            continue
        cleaned.append({key: entry.get(key) for key in (
            "provider_id", "model", "phase", "elapsed", "up_bytes", "timeout", "error_type", "status"
        )})
    return cleaned
