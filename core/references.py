"""事件处理阶段的参考图接管规划（纯逻辑，不依赖 AstrBot 运行时）。

明确附带在消息中的 Image 组件属于事件级临时文件，必须在事件处理阶段完成
本地化并读入内存，不能推迟到后台任务（事件结束后）才读取。本模块把这段
“接管 + 去重 + 延迟清单”逻辑抽成纯 Python，便于在不完整 mock AstrBot 的
环境下做单元测试。

对本地不可读的明确 Image 组件按来源分类处理：
- HTTP(S)：不调用 converter，进入 strict deferred，由后台
  ``fetch_reference`` 执行逐跳 SSRF 校验与大小限制，避免绕过 imago_fusion 自身的
  远程下载策略；
- ``data:``/``base64://``：事件阶段立即用 ``fetch_reference(None, ...)``
  解码接管（不需要 session），保留魔数识别与大小校验；
- 本地路径 / ``file:`` URI：直接读入内存；
- 裸文件名 / 无可用 source / 无法归类形态：事件阶段用
  ``convert_to_file_path()`` 接管。

仍无法解析时抛异常（严格失败），不静默丢图。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse
from collections.abc import Callable
from typing import Any

from .errors import ProviderTimeout
from .models import ImageInput
from .network import fetch_reference
from .security import redact

try:  # aiohttp 在纯逻辑测试环境可能缺失
    from aiohttp import ClientError as _AiohttpClientError
except ImportError:  # pragma: no cover - 源码环境
    class _AiohttpClientError(Exception):  # type: ignore[too-many-ancestors]
        pass

_HTTP_URL = re.compile(r"https?://[^\s<>\]\[()\"']+")
_IMAGE_PLACEHOLDERS = ("[图片]", "[Image]", "[image]")
_SUFFIX_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def quoted_reply_missing_images(
    *, strict: bool, image_sources, quoted_text: str = ""
) -> bool:
    """严格引用回复是否应判定为“引用消息图片无法获取”。

    严格模式只在**完全取不到引用内容**时才算失败：取到正文（且正文不是图片
    占位符）说明引用消息确实存在、只是不含图，此时按纯文生图继续，不应把整轮
    绘图请求打断（用户回复一条文字消息后要求画图是常见用法）。
    """
    if not strict or image_sources:
        return False
    text = str(quoted_text or "").strip()
    if text and not any(marker in text for marker in _IMAGE_PLACEHOLDERS):
        return False
    return True


class ReferencePlanner:
    """在事件处理阶段同步接管明确 Image 组件，生成本地参考图与延迟解析清单。

    plan() 返回 (local_references, deferred)：
    - local_references: 已读入内存的 ImageInput 列表（按内容哈希去重）；
    - deferred: 需要解析的延迟项（引用消息通常由调用方在事件阶段前台解析）：
        {"kind": "reply", "component": ..., "strict": bool}  引用消息远端回退
        {"kind": "source", "source": url, "strict": True}    明确 Image 组件的远程 HTTP(S) 参考图
        {"kind": "source", "source": url, "strict": False}   消息正文里的公网 URL

    reply 项的 strict 判定：引用正文出现图片标记，或“只有引用 id、没有可检查
    正文”的纯引用时为 True（无法排除含图，必须严格失败，避免静默丢图）；只有
    存在明确不含图的引用正文时才为 False（宽松，解析不到也不阻断）。

    明确的 Image 组件本地不可读时按来源分类：
    - HTTP(S) 进入 strict deferred（后台执行 SSRF 与大小校验，不用 converter）；
    - data:/base64:// 事件阶段立即用 fetch_reference 解码接管；
    - 本地路径/file: URI 直接读入；
    - 裸文件名/无可用 source/无法归类形态用 convert_to_file_path() 接管；
    仍无法解析时抛异常（严格失败），不会静默丢图。
    """

    def __init__(self, *, max_upload_bytes: Callable[[], int], extract_quoted_message_images=None):
        self._max_upload_bytes = max_upload_bytes
        self._extract_quoted = extract_quoted_message_images

    @staticmethod
    def _sources_of(component: Any) -> list[str]:
        sources: list[str] = []
        for field in ("path", "url", "file"):
            value = getattr(component, field, None)
            if isinstance(value, str) and value.strip():
                sources.append(value.strip())
        return sources

    @staticmethod
    def _path_of(component: Any) -> Path | None:
        for field in ("path", "url", "file"):
            value = getattr(component, field, None)
            if not isinstance(value, str) or not value.strip():
                continue
            source = value.strip()
            if source.startswith("file:"):
                parsed = urlparse(source)
                source = unquote(parsed.path or "")
                if re.match(r"^/[A-Za-z]:/", source):
                    source = source[1:]
            if source.startswith(("http://", "https://", "data:", "base64://")):
                continue
            try:
                path = Path(source).expanduser().resolve()
                if path.is_file() and not path.is_symlink():
                    return path
            except OSError:
                continue
        return None

    def _read_path(self, path: Path) -> ImageInput | None:
        try:
            path = Path(path).expanduser().resolve()
        except OSError:
            return None
        if not path.is_file() or path.is_symlink():
            return None
        data = path.read_bytes()
        if not data or len(data) > self._max_upload_bytes():
            raise ValueError("图片格式或大小不符合要求")
        mime = _SUFFIX_MIME.get(path.suffix.lower())
        if not mime:
            raise ValueError("图片格式或大小不符合要求")
        return ImageInput(data=data, mime_type=mime, filename=path.name)

    def _read_component(self, component: Any) -> ImageInput | None:
        path = self._path_of(component)
        if path is None:
            return None
        return self._read_path(path)

    @staticmethod
    async def _convert_component_to_path(component: Any) -> Path | None:
        converter = getattr(component, "convert_to_file_path", None)
        if not callable(converter):
            return None
        try:
            value = await converter()
        except Exception:
            return None
        if not value:
            return None
        try:
            return Path(str(value)).expanduser().resolve()
        except OSError:
            return None

    async def plan(self, components) -> tuple[list[ImageInput], list[dict]]:
        local_references: list[ImageInput] = []
        deferred: list[dict] = []
        seen: set[bytes] = set()

        def add_local(image: ImageInput) -> None:
            digest = hashlib.sha256(image.data).digest()
            if digest not in seen:
                seen.add(digest)
                local_references.append(image)

        def contains_image(chain) -> bool:
            for item in chain or []:
                name = type(item).__name__.lower()
                if "image" in name:
                    return True
                if name == "reply" and contains_image(getattr(item, "chain", None) or []):
                    return True
            return False

        def reply_indicates_image(component) -> bool:
            """判定 Reply 是否应按 strict 等待引用消息图片。

            返回 True 的情形：
            - 引用正文（message_str / chain 文本）出现 [图片]/[Image]/[image] 标记；
            - 只有引用 id、且没有任何可检查正文（无 chain 或 chain/message_str
              均为空）的“纯引用”——无法排除含图，必须按 strict 处理，避免后台
              静默丢图。
            返回 False 仅在存在可检查的引用正文且其中没有图片标记时（明确不含图）。
            """
            has_reply_id = any(
                str(getattr(component, field, "") or "").strip()
                for field in ("id", "message_id", "messageId")
            )
            message_str = str(getattr(component, "message_str", "") or "")
            chain_texts = [
                str(getattr(item, "text", "") or getattr(item, "content", "") or "")
                for item in getattr(component, "chain", None) or []
            ]
            markers = ("[图片]", "[Image]", "[image]")
            if any(marker in message_str for marker in markers):
                return True
            if any(marker in text for text in chain_texts for marker in markers):
                return True
            if has_reply_id and not message_str and not any(chain_texts):
                # 纯引用：有 id 但没有可检查正文（含 chain 仅空文本项），无法排除
                # 含图，按严格处理。
                return True
            return False

        async def walk(chain) -> None:
            for component in chain or []:
                name = type(component).__name__.lower()
                if name == "reply":
                    reply_chain = getattr(component, "chain", None) or []
                    has_embedded_image = contains_image(reply_chain)
                    await walk(reply_chain)
                    if not has_embedded_image and self._extract_quoted is not None:
                        deferred.append({
                            "kind": "reply",
                            "component": component,
                            "strict": reply_indicates_image(component),
                        })
                    continue
                if "image" in name:
                    image = self._read_component(component)
                    if image is not None:
                        add_local(image)
                        continue
                    sources = self._sources_of(component)
                    http_source = next(
                        (value for value in sources if value.startswith(("http://", "https://"))),
                        None,
                    )
                    if http_source is not None:
                        # 远程 URL 不在此处下载：交给后台 _resolve_deferred_references
                        # -> fetch_reference，保留 imago_fusion 自身的 SSRF 校验和大小限制。
                        deferred.append({"kind": "source", "source": http_source, "strict": True})
                        continue
                    inline_source = next(
                        (value for value in sources if value.startswith(("data:", "base64://"))),
                        None,
                    )
                    if inline_source is not None:
                        image = await fetch_reference(
                            None,
                            inline_source,
                            max_bytes=self._max_upload_bytes(),
                            block_private=False,
                        )
                        add_local(image)
                        continue
                    # 裸文件名 / 无可用 source / 无法归类形态：事件阶段用 converter 接管。
                    converted = await self._convert_component_to_path(component)
                    if converted is not None:
                        image = self._read_path(converted)
                        if image is not None:
                            add_local(image)
                            continue
                    raise ValueError("参考图无法获取")
                text = getattr(component, "text", None) or getattr(component, "content", None)
                if isinstance(text, str):
                    for url in _HTTP_URL.findall(text):
                        deferred.append({
                            "kind": "source",
                            "source": url.rstrip(".,;!?。，；！？"),
                            "strict": False,
                        })

        await walk(components)
        return local_references, deferred


# ---------------------------------------------------------------------------
# v1.3.0 拒绝判定（纯函数）
# ---------------------------------------------------------------------------
# 设计要点（contract rev3）：
# - 证据优先级：结构化字段（``ProviderError.code`` / ``.finish_reason`` /
#   ``.block_reason``，``NoOutputError`` 同）→ 错误文本正则 → ``unknown``；
# - HTTP 状态码本身**不是**判定依据：只有 400/413/415/422 才允许走文本正则；
#   401/403/404/429/5xx/None 在无结构化证据时永不触发；
# - HTTP 200 的内容拒绝必须由适配器抛结构化 ``NoOutputError``（status=None）；
# - ``safety`` 不可文字降级（只可能走实验遮挡），``unknown``/``text_rejected``
#   同理不降级；只有 ``image_unsupported`` / ``image_invalid`` 可直接改文字重绘。
CAPTION_MAX_IMAGES = 4
REJECTION_STATUSES = frozenset({400, 413, 415, 422})

_STRUCTURED_SAFETY = frozenset({
    "content_policy", "contentpolicy", "content_policy_violation", "safety",
    "moderation", "nsfw", "sensitive", "prohibited", "blocked", "blocklist",
    "content_filter", "data_inspection_failed", "datainspectionfailed",
    "image_safety", "spii", "recitation", "unsafe",
})
_STRUCTURED_UNSUPPORTED = frozenset({
    "unsupported_image", "image_not_supported", "images_not_supported",
    "text_only", "textonly", "does_not_support_image", "no_image_support",
    "image_unsupported", "image_input_unsupported", "vision_not_supported",
})
_STRUCTURED_TEXT_REJECTED = frozenset({
    "text_rejected", "prompt_rejected", "prompt_too_long", "input_too_long",
    "invalid_prompt",
})
_STRUCTURED_INVALID = frozenset({
    "invalid_image", "image_invalid", "image_too_large", "image_size_invalid",
    "invalid_image_size", "bad_image", "image_decode_failed",
})

_TEXT_ONLY = re.compile(
    r"(does\s+not|doesn't|not)\s+support\s+(?:image|vision)|text[- ]only|"
    r"not\s+a\s+vision|images?\s+(?:are|is)\s+not\s+supported",
    re.I,
)
_UNSUPPORTED = re.compile(
    r"(?:invalid|unsupported|not\s+supported|not\s+allowed|cannot|can't|"
    r"unable\s+to\s+process|rejected)[^\n]{0,40}images?|"
    r"images?[^\n]{0,40}(?:invalid|unsupported|not\s+supported|not\s+allowed|"
    r"too\s+large|exceed)|(?:input|reference)\s+image",
    re.I,
)
_IMAGE_SIZE = re.compile(
    r"image[^\n]{0,60}(?:size|dimension|resolution)[^\n]{0,40}"
    r"(?:exceed|limit|invalid|too)",
    re.I,
)
_ZH_IMAGE = re.compile(r"(?:图片|图像|参考图)[^\n]{0,20}(?:不支持|无效|过大|超限|无法处理)")
_SAFETY_TEXT = re.compile(
    r"content[\s_-]?(?:polic|filter|moderation)|safety|moderation|nsfw|"
    r"prohibited|blocklist|sensitive|violat(?:e|es|ion)|"
    r"不安全|敏感|违规|内容审核|涉黄|色情",
    re.I,
)
_TEXT_REJECTED = re.compile(
    r"prompt[^\n]{0,20}(?:too\s+long|exceed)|input[^\n]{0,20}too\s+long|"
    r"max(?:imum)?\s+(?:tokens|length)|上下文.{0,10}(?:过长|超限)",
    re.I,
)
_NON_IMAGE_EXCLUSIONS = re.compile(
    r"api[\s_-]?key|unauthoriz|forbidden|permission|quota|rate[\s_-]?limit|"
    r"insufficient|balance|billing|model\s+not\s+found|no\s+such\s+model",
    re.I,
)


@dataclass(frozen=True)
class RejectionVerdict:
    """一次失败尝试的拒绝判定结果。

    ``kind`` 取值：``image_unsupported`` / ``image_invalid`` / ``safety`` /
    ``text_rejected`` / ``unknown``。``convertible`` 表示是否允许改文字重绘；
    ``safety`` 只可能走实验遮挡路径。
    """

    kind: str = "unknown"
    convertible: bool = False
    evidence: str = ""
    detail: str = ""


def _structured_tokens(exc: Any) -> list[tuple[str, str]]:
    """取出异常上的结构化字段（code / finish_reason / block_reason）。"""
    found: list[tuple[str, str]] = []
    for name in ("code", "finish_reason", "block_reason"):
        value = getattr(exc, name, None)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            found.append((name, text))
    return found


def classify_rejection(
    exc: BaseException,
    *,
    api_type: str = "",
    model: str = "",
) -> RejectionVerdict:
    """把一次失败尝试判定为可降级 / 安全 / 文本 / 未知。

    Args:
        exc: 适配器/调度器捕获到的异常（``ProviderError``/``NoOutputError`` 等）。
        api_type: 节点协议，仅用于日志与排障，不改变判定。
        model: 模型名，同上。

    Returns:
        ``RejectionVerdict``；``detail`` 已脱敏并截断到 200 字符。
    """
    raw = redact(str(exc) or "")
    detail = raw[:200]
    tokens = _structured_tokens(exc)
    normalized = {value.strip().lower() for _name, value in tokens}
    evidence = ",".join(f"{name}:{redact(value)[:60]}" for name, value in tokens)

    # 1) 排除层（rev3.1 §6.3）：传输/鉴权/限流/服务端故障与"无任何证据的 None"
    #    永远不参与判定；有结构化业务证据的 None（HTTP 200 拒绝）仍可分类。
    status = getattr(exc, "status", None)
    if isinstance(exc, (asyncio.TimeoutError, ProviderTimeout)):
        return RejectionVerdict("unknown", False, evidence or "transport", detail)
    if isinstance(exc, _AiohttpClientError):
        return RejectionVerdict("unknown", False, evidence or "transport", detail)
    if status in {401, 403, 429} or (isinstance(status, int) and status >= 500):
        return RejectionVerdict("unknown", False, evidence or f"status:{status}", detail)
    if not tokens and status not in REJECTION_STATUSES:
        return RejectionVerdict("unknown", False, evidence or f"status:{status}", detail)

    # 2) 判定层：结构化证据优先；``block_reason=OTHER`` 单独 = unknown。
    if normalized == {"other"}:
        return RejectionVerdict("unknown", False, evidence or "structured", detail)
    if normalized & _STRUCTURED_SAFETY:
        return RejectionVerdict("safety", False, evidence or "structured", detail)
    if normalized & _STRUCTURED_UNSUPPORTED:
        return RejectionVerdict("image_unsupported", True, evidence or "structured", detail)
    if normalized & _STRUCTURED_INVALID:
        return RejectionVerdict("image_invalid", True, evidence or "structured", detail)
    # 结构化 text_rejected（prompt 被拒）：恒不可降级，且优先于 safety。
    if normalized & _STRUCTURED_TEXT_REJECTED:
        return RejectionVerdict("text_rejected", False, evidence or "structured", detail)

    lowered = raw
    # 3) 文本层：text_rejected 与 safety 互斥，证据同现时 text_rejected 优先。
    if _TEXT_REJECTED.search(lowered) or _NON_IMAGE_EXCLUSIONS.search(lowered):
        return RejectionVerdict("text_rejected", False, evidence or "text", detail)
    if _SAFETY_TEXT.search(lowered):
        return RejectionVerdict("safety", False, evidence or "text", detail)
    if _TEXT_ONLY.search(lowered) or _ZH_IMAGE.search(lowered):
        return RejectionVerdict("image_unsupported", True, evidence or "text", detail)
    if _IMAGE_SIZE.search(lowered):
        return RejectionVerdict("image_invalid", True, evidence or "text", detail)
    if _UNSUPPORTED.search(lowered):
        return RejectionVerdict("image_invalid", True, evidence or "text", detail)
    return RejectionVerdict("unknown", False, evidence or "text", detail)
