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

import hashlib
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from .security import redact_debug

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
        """按**真实可观察事实**判定阶段，判不出来就是 ``unknown``。

        允许使用的只有两类事实：

        * 异常类型本身点明"连不上对端"（``ClientConnectorError`` 等）；
        * 已经收到过响应（``on_response_chunk_received`` 触发过）。

        "请求已发出但既没响应、也没记录到分片"这类情况无法区分是没连上、传了一半
        还是等响应超时，一律 ``unknown``——不拿 ``requests``/``chunks`` 计数去猜。
        """
        if exc is not None and _looks_like_connect(exc):
            return PHASE_CONNECT
        if self.response_started_at is not None:
            return PHASE_READ_BODY
        return PHASE_UNKNOWN


#: 只有这些类型名才**确证**"根本没连上"：连接器/DNS/证书类异常。
#: 注意不能宽泛匹配 ``connect``：``ServerDisconnectedError`` 也含这个词，
#: 但它表示"连上之后被对端断开"，与 connect 阶段相反（CORE-05）。
_CONNECT_TOKENS = (
    # 具体到"连接建立失败"的子类：ClientConnectorError 及其 DNS 变体、
    # 代理连接器、OS 级拒绝、DNS 解析、TLS/证书。**不收** ClientConnectionError
    # 这个宽泛基类（它同时覆盖断连/超时等"已连上"的情形）。
    "clientconnectorerror",
    "clientconnectordnserror",
    "clientproxyconnectionerror",
    "connectionrefusederror",
    "connectorerror",
    "gaierror",
    "sslerror",
    "sslcertverificationerror",
    "certificateerror",
)


def _looks_like_connect(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    return any(token in name for token in _CONNECT_TOKENS)


def fallback_phase(exc: BaseException) -> str:
    """无 trace 事实时的保守归类（契约 §5.8 / 评审 R3-02）。

    只有**能确证"根本没连上"**的异常才记 ``connect``；其余一律 ``unknown``：

    * ``ServerDisconnectedError`` / ``ClientConnectionError`` / ``TimeoutError`` 等
      既可能"连上后中断"也可能"根本没连上"，没有 trace 时无法判定 ⇒ ``unknown``；
    * 绝不按 ``disconnect``/``reset``/``payload`` 之类子串推断 ``read_body``——
      这些词不能证明响应体已经开始读取。

    与 :meth:`AttemptTrace.phase` 共用同一份 :data:`_CONNECT_TOKENS`，避免两处口径漂移。
    """
    if _looks_like_connect(exc):
        return PHASE_CONNECT
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
    up_bytes: int | None,
    timeout: float,
    error_type: str,
    status: int | None = None,
    detail: str = "",
) -> dict:
    """把一条脱敏诊断追加进 ``task.runtime["attempt_diagnostics"]``。

    ``up_bytes`` 允许 ``None``：没有分片回调事实时序列化成 ``"unknown"``，不拿图片
    大小或响应长度顶替（队长口径 CORE-05）。

    Returns:
        写入的那条记录（测试可直接断言，不必再读 runtime）。
    """
    entry = {
        "provider_id": str(provider_id or "")[:64],
        "model": str(model or "")[:64],
        "phase": str(phase or PHASE_UNKNOWN)[:24],
        "elapsed": round(max(0.0, float(elapsed)), 2),
        "up_bytes": "unknown" if up_bytes is None else max(0, int(up_bytes)),
        "timeout": round(max(0.0, float(timeout)), 2),
        "error_type": str(error_type or "")[:64],
        "status": status,
        "detail": _clean_detail(detail),
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
    up_bytes: int | None,
    timeout: float,
    error_type: str,
    detail: str = "",
) -> str:
    """§5.8 要求的单行日志形态（不含 URL/图片/凭据）。"""
    detail_text = _clean_detail(detail)
    suffix = f" detail={detail_text}" if detail_text else ""
    observed = "unknown" if up_bytes is None else max(0, int(up_bytes))
    return (
        f"绘图模型失败 node={str(provider_id or '')[:64]} model={str(model or '')[:64]} "
        f"phase={str(phase or PHASE_UNKNOWN)} elapsed={max(0.0, float(elapsed)):.1f}s "
        f"up_bytes={observed} timeout={max(0.0, float(timeout)):.1f}s "
        f"error_type={str(error_type or '')[:64]}{suffix}"
    )


def _clean_detail(value: Any) -> str:
    """诊断细节先脱敏（URL/路径/长数字/凭据）再截断。"""
    return redact_debug(str(value or ""))[:MAX_DETAIL_CHARS]


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


# --------------------------------------------------- Persona 观测投影（v1.4.2 C-PERSONA）
# 值域封闭：来源/图库状态都是有限枚举，"不可判定"必须显式标 ``unknown``，不许猜。
PERSONA_SOURCE_UMO = "umo_forced"
PERSONA_SOURCE_CONVERSATION = "conversation"
PERSONA_SOURCE_RUNNER = "runner_config"
PERSONA_SOURCE_IMPLICIT = "implicit_default"
PERSONA_SOURCE_UNKNOWN = "unknown"
PERSONA_SOURCES = (
    PERSONA_SOURCE_UMO,
    PERSONA_SOURCE_CONVERSATION,
    PERSONA_SOURCE_RUNNER,
    PERSONA_SOURCE_IMPLICIT,
    PERSONA_SOURCE_UNKNOWN,
)

PERSONA_LIBRARY_OK = "ok"
PERSONA_LIBRARY_EMPTY = "empty"
PERSONA_LIBRARY_MISSING = "missing"
PERSONA_LIBRARY_ERROR = "error"
PERSONA_LIBRARY_STATES = (
    PERSONA_LIBRARY_OK,
    PERSONA_LIBRARY_EMPTY,
    PERSONA_LIBRARY_MISSING,
    PERSONA_LIBRARY_ERROR,
)

#: 别名域前缀：换前缀即等于换一整套别名（旧日志不再能和新日志关联）。
_PERSONA_ALIAS_SALT = "imago-fusion/persona-alias/v1"
_PERSONA_ALIAS_HEX = 12
#: 外部标签（节点 id / 模型名）的投影上限，防止异常长的配置值撑爆日志与 task.json。
MAX_LABEL_CHARS = 48


def persona_alias(persona_id: Any) -> str:
    """把 Persona 原始 ID 投影成**稳定、不可逆、不可枚举**的观测别名。

    稳定 ⇒ 同一 ID 跨任务/跨重启得到同一别名，日志可关联；不可逆 ⇒ 只暴露
    ``sha256(域前缀 ‖ ID)`` 的前 48 bit，不留可还原片段；不可枚举 ⇒ 空值单独成
    ``persona-none``，且**禁止**用图库目录名（可逆编码）冒充别名（验收 t3）。

    Args:
        persona_id: 宿主解析出来的 Persona 标识；空/None 视为"无"。

    Returns:
        形如 ``persona-1a2b3c4d5e6f`` 的短别名。
    """
    text = str(persona_id or "")
    if not text:
        return "persona-none"
    digest = hashlib.sha256(f"{_PERSONA_ALIAS_SALT}:{text}".encode("utf-8")).hexdigest()
    return f"persona-{digest[:_PERSONA_ALIAS_HEX]}"


def safe_label(value: Any) -> str:
    """节点/模型等**外部标签**的安全投影：先脱敏（URL/路径/长数字/凭据），再截断。"""
    text = redact_debug(str(value or "")).strip()
    text = "".join(char for char in text if char.isprintable())
    return text[:MAX_LABEL_CHARS]


def as_reference_plans(entries: Any, *, limit: int = MAX_DIAGNOSTIC_ENTRIES) -> list[dict]:
    """把 runtime 里的"逐腿参考图实发计划"整理成可写入 ``task.json`` 的有界形状。

    只投影固定键（节点/模型标签过 :func:`safe_label`），不落任何 Persona 原始 ID、
    账号、会话、消息 ID、图片内容或本地路径。
    """
    if not isinstance(entries, list):
        return []
    cleaned: list[dict] = []
    for entry in entries[-limit:]:
        if not isinstance(entry, dict):
            continue
        cleaned.append({
            "node": safe_label(entry.get("node")),
            "model": safe_label(entry.get("model")),
            "kind": str(entry.get("kind") or "")[:16],
            "reference_limit": max(0, int(entry.get("reference_limit", 0) or 0)),
            "explicit_sent": max(0, int(entry.get("explicit_sent", 0) or 0)),
            "persona_sent": max(0, int(entry.get("persona_sent", 0) or 0)),
            "persona_pool": max(0, int(entry.get("persona_pool", 0) or 0)),
            "persona_budget": max(0, int(entry.get("persona_budget", 0) or 0)),
            "library": str(entry.get("library") or "")[:16],
            "reason": str(entry.get("reason") or "")[:32],
        })
    return cleaned
