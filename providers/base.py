from __future__ import annotations

import base64
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from ..core.errors import NoOutputError, UnsupportedResponse
from ..core.models import GenerationRequest, ImageInput, ImageResult, ProviderConfig
from ..core.network import detect_image_mime
from ..core.params import (
    ThinkingDecision,
    effective_params,
    parse_custom_params,
    resolve_thinking,
)


class ProviderAdapter(ABC):
    """Base class for image provider adapters.

    Attributes:
        config: Frozen node configuration the adapter must honour.
        context: AstrBot plugin context, or None. Only
            :class:`~imago_fusion.providers.native.NativeAdapter` uses it (to call
            an AstrBot provider); HTTP adapters ignore it.
    """

    # 适配器自己管理、永远不允许被 ``custom_params``/``extra_params`` 覆盖的键。
    # 具体子类按协议再补 ``size``/``quality`` 等（见各自 ``_reserved_keys``）。
    _base_reserved = frozenset({
        "model", "prompt", "n", "count", "references",
        "image", "images", "mask", "input_image", "input_images",
    })

    def __init__(self, config: ProviderConfig, *, context: Any = None) -> None:
        self.config = config
        self.context = context
        self._node_params_cache: tuple[dict[str, Any], list[dict[str, Any]]] | None = None

    @abstractmethod
    async def generate(self, session: Any, request: GenerationRequest, api_key: str) -> list[ImageResult]: ...

    # ------------------------------------------------------------------ 节点参数
    def node_params(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """解析并缓存节点的 ``custom_params``（返回 ``值表`` 与 ``param_errors``）。

        同一适配器实例在一次请求里可能多次取值（尺寸、质量、思考），缓存避免重复
        解析与重复日志。
        """
        if self._node_params_cache is None:
            self._node_params_cache = parse_custom_params(
                self.config.custom_params, self.config.api_type,
            )
        return self._node_params_cache

    def log_param_errors(self) -> None:
        """把解析期的非法行写进 DEBUG（不报错、不失效节点，字典 §2）。"""
        _params, errors = self.node_params()
        for error in errors:
            self._debug(
                "参数 跳过 api_type=%s key=%s reason=%s line=%s",
                self.config.api_type, error.get("key", ""), error.get("reason", ""), error.get("line", 0),
            )

    def merged_params(
        self,
        request: GenerationRequest,
        *,
        reserved: Iterable[str] = (),
        drop_empty: bool = True,
    ) -> dict[str, Any]:
        """合并节点参数与调用级 ``extra_params``（调用级覆盖节点级）。

        Args:
            request: 本次生成请求。
            reserved: 额外保留键（子类把 ``size``/``quality`` 等显式字段列进来，
                避免同一字段既走专用槽又被塞进通用参数）。
            drop_empty: 是否丢弃 ``""``/``None``（"留空不发"）。
        """
        params, _ = self.node_params()
        return effective_params(
            params,
            request.extra_params,
            reserved=self._base_reserved | frozenset(reserved),
            drop_empty=drop_empty,
        )

    def node_size(self) -> str:
        """节点 ``custom_params`` 里的尺寸（优先级低于调用级、高于 ``default_size``）。"""
        params, _ = self.node_params()
        return str(params.get("size", "") or "").strip()

    def effective_size(self, request: GenerationRequest) -> str:
        """尺寸优先级：调用级 ``request.size`` → 节点参数 ``size`` → ``default_size``。"""
        return (request.size or self.node_size() or self.config.default_size).strip()

    # ------------------------------------------------------------------ 思考控制
    def thinking_decision(self) -> ThinkingDecision:
        """解析节点的 ``thinking`` 并按协议给出要发送的字段（不发送时 ``applied=False``）。"""
        decision = resolve_thinking(
            self.config.api_type, self.config.model, self.config.thinking,
        )
        for line in decision.debug:
            self._debug(line)
        if "gemini_thinking_cannot_disable" in decision.warnings:
            self._warn(
                "思考控制 忽略 api_type=%s value=off reason=cannot_disable"
                "（该图像模型的思考不可关闭）",
                self.config.api_type,
            )
        return decision

    @staticmethod
    def data_url(image: ImageInput) -> str:
        """把参考图编成 ``data:<mime>;base64,<...>``（JSON 协议用）。"""
        encoded = base64.b64encode(image.data).decode("ascii")
        return f"data:{image.mime_type};base64,{encoded}"

    # ------------------------------------------------------------------ 日志
    def _warn(self, message: str, *args: Any) -> None:
        """写 WARNING（懒导入 logger：纯逻辑测试环境也能导入本模块）。"""
        try:
            from astrbot.api import logger
        except ImportError:  # pragma: no cover - 仅在缺少 AstrBot 的源码环境触发
            return
        logger.warning("[ImagoFusion] " + message, *args)

    def _debug(self, message: str, *args: Any) -> None:
        """写 DEBUG（经插件运行时钩子，尊重面板里的日志级别）。"""
        from ..core import runtime

        runtime.debug_log(message, *args)

    @staticmethod
    def parse_common(payload: Any) -> list[ImageResult]:
        results: list[ImageResult] = []
        candidates = payload.get("data", []) if isinstance(payload, dict) else []
        if isinstance(candidates, dict):
            candidates = [candidates]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("image_url")
            encoded = item.get("b64_json") or item.get("base64")
            if isinstance(url, str) and url:
                results.append(ImageResult(url=url))
            elif isinstance(encoded, str) and encoded:
                try:
                    data = base64.b64decode(encoded, validate=True)
                except ValueError:
                    continue
                # base64 结果没有 MIME 声明：按文件魔数判定，否则 JPEG/WEBP 字节会被
                # 当成 image/png 落成 .png（materialize_result 按 mime_type 决定扩展名），
                # 部分平台按扩展名处理时会拒绝或显示异常。识别失败才回落 image/png。
                results.append(
                    ImageResult(
                        data=data,
                        mime_type=detect_image_mime(data) or "image/png",
                    )
                )
        if not results:
            raise NoOutputError("响应中没有可识别的图片")
        return results

    @staticmethod
    def _error_detail(payload: Any) -> str:
        """从错误响应体提取可诊断摘要。

        兼容三种常见形态：顶层字段（百炼 code/message）、OpenAI/Gemini 的嵌套
        ``error`` 对象（message/type/code）、``error`` 为纯字符串的中转站。
        全部经 redact_debug 脱敏并截断，避免把完整响应体打进日志。
        """
        from ..core.security import redact_debug

        if not isinstance(payload, dict):
            return ""
        error = payload.get("error")
        if isinstance(error, dict):
            merged = dict(payload)
            merged.update(error)
            payload = merged
        elif isinstance(error, str) and error.strip():
            return f"message={redact_debug(error)}"
        parts = []
        for key, label in (
            ("code", "code"),
            ("message", "message"),
            ("msg", "message"),
            ("type", "type"),
            ("err", "message"),
        ):
            value = payload.get(key)
            if value is not None and str(value).strip():
                parts.append(f"{label}={redact_debug(str(value))[:160]}")
        return " ".join(parts)

    @staticmethod
    async def response_json(response: Any) -> Any:
        if response.status >= 400:
            from ..core.errors import ProviderError
            from ..core.security import redact_debug
            detail = ""
            try:
                payload = await response.json(content_type=None)
                detail = ProviderAdapter._error_detail(payload)
            except Exception:
                payload = None
            if not detail:
                # 非 JSON 或未知结构：截断原始文本兜底，保证日志仍可诊断。
                try:
                    raw = await response.text()
                except Exception:
                    raw = ""
                if raw and str(raw).strip():
                    detail = "body=" + redact_debug(str(raw).strip())[:160]
            raise ProviderError(f"HTTP {response.status}{' ' + detail if detail else ''}", status=response.status)
        try:
            return await response.json(content_type=None)
        except Exception as exc:
            raise UnsupportedResponse("上游返回的不是有效 JSON") from exc
