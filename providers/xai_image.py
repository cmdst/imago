from __future__ import annotations

import base64
from urllib.parse import urljoin

from .openai_image import OpenAIImageAdapter
from ..core.models import GenerationRequest
from ..core.params import xai_aspect_ratio


class XaiImageAdapter(OpenAIImageAdapter):
    """xAI ``images`` 协议（generations + edits）。

    与 OpenAI 的关键差异（spec 协议能力 E5/E6）：

    - **没有** ``quality`` 请求字段（它只是定价档），``resolution`` 才是分辨率档
      （``1k``/``1.5k``/``2k``）；节点参数里显式写 ``quality`` 仍按"未文档化透传"发送；
    - 改图只收 ``application/json``：单图 ``image: {"url": …}``，多图
      ``images: [{"url": …}, …]``（``<IMAGE_0>`` 可指代）；只收表单的中转可用
      ``edit_mode=multipart`` 回退到父类的 multipart 提交；
    - 比例同时由 ``size``/``aspect_ratio`` 折算成 ``aspect_ratio``（像素按 gcd 约分，
      ``auto`` 原样直传）。

    ``thinking`` 在 xAI 没有承载字段，只会记 DEBUG（``resolve_thinking``）。
    """

    # 单图 JSON 字段名；multipart 回退也用它（父类默认是 ``image[]``）。
    image_field = "image"
    # resolution 不放进 dedicated：它必须走合并链（调用级 > 节点级），否则调用级
    # 2k 会被节点 1k 覆盖（BF-09）。size/edit_mode 有独立槽位。
    _dedicated = frozenset({"size", "edit_mode"})

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._quality_warned = False

    def _node_options(self, request: GenerationRequest) -> dict[str, object]:
        """取节点参数里的 ``resolution``/``edit_mode`` 等专用项，并返回剩余通用参数。"""
        params = self.merged_params(request, reserved=self._dedicated)
        self.log_param_errors()
        self.thinking_decision()
        resolution = str(params.pop("resolution", "") or "").strip().lower()
        if resolution and resolution not in {"1k", "1.5k", "2k"}:
            self._debug(
                "参数 忽略 api_type=xai_image key=resolution reason=invalid_value",
            )
            resolution = ""
        if resolution:
            params["resolution"] = resolution
        if "quality" in params and not self._quality_warned:
            # xAI 只在定价档出现 quality，请求 schema 没有该字段；用户显式写时
            # 仍按"未文档化透传"发送，但必须提示供应商可能拒绝（BF-09）。
            self._quality_warned = True
            self._warn(
                "参数 透传 api_type=xai_image key=quality reason=undocumented"
                "（xAI 无质量请求字段，供应商可能拒绝）",
            )
        return params

    def edit_mode(self) -> str:
        """参考图提交方式：``json``（默认，官方口径）或 ``multipart``（兼容中转）。"""
        node_params, _errors = self.node_params()
        value = str(node_params.get("edit_mode", "json") or "json").strip().lower()
        return value if value in {"json", "multipart"} else "json"

    def _aspect(self, request: GenerationRequest) -> str:
        """比例来源：调用级 ``aspect_ratio`` 优先，其次尺寸折算。"""
        explicit = xai_aspect_ratio(request.aspect_ratio)
        if explicit:
            return explicit
        return (
            xai_aspect_ratio(request.size)
            or xai_aspect_ratio(self.node_size())
            or xai_aspect_ratio(self.config.default_size)
        )

    async def generate(self, session, request: GenerationRequest, api_key: str) -> list:
        headers = {"Authorization": f"Bearer {api_key}"}
        params = self._node_options(request)
        aspect = self._aspect(request)
        if aspect:
            params["aspect_ratio"] = aspect
        base = self.config.base_url.rstrip("/") + "/"
        if not request.references:
            payload: dict[str, object] = {
                "model": self.config.model,
                "prompt": request.prompt,
                "n": request.count,
                **params,
            }
            async with session.post(
                urljoin(base, "images/generations"),
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            ) as response:
                return self.parse_common(await self.response_json(response), status=200)
        if self.edit_mode() == "multipart":
            return await self._edit_multipart(
                session, request, api_key, urljoin(base, "images/edits"),
                extra_fields=params, size=self.effective_size(request),
                image_field=self.image_field,
            )
        body: dict[str, object] = {
            "model": self.config.model,
            "prompt": request.prompt,
            "n": request.count,
            **params,
        }
        if len(request.references) == 1:
            body["image"] = {"url": self.data_url(request.references[0])}
        else:
            body["images"] = [
                {"url": self.data_url(image)} for image in request.references
            ]
        async with session.post(
            urljoin(base, "images/edits"),
            headers={**headers, "Content-Type": "application/json"},
            json=body,
        ) as response:
            return self.parse_common(await self.response_json(response), status=200)

    @staticmethod
    def encode_reference(image) -> str:  # pragma: no cover - 兼容旧调用形态
        """把参考图编成裸 base64（旧路径用；JSON 协议一律走 ``data:`` URL）。"""
        return base64.b64encode(image.data).decode("ascii")
