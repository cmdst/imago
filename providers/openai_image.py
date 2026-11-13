from __future__ import annotations

from urllib.parse import urljoin

from .base import ProviderAdapter
from ..core.models import GenerationRequest, ImageInput
from ..core.params import format_param


class OpenAIImageAdapter(ProviderAdapter):
    """OpenAI ``images`` 协议（generations JSON / edits multipart）。

    v1.3.0 起支持节点级参数与思考控制：``quality`` 等 ``custom_params`` 按
    "节点级 → 调用级 → 保留键" 的顺序合并进请求体；``thinking`` 独立映射到
    ``complexity``（OpenAI 的"规划算力"，与渲染质量无关），并覆盖用户误写在
    ``custom_params`` 里的同名/近义键。
    """

    # 有专用槽位的键：``size`` 由请求或 default_size 决定，不从通用参数里取。
    _dedicated = frozenset({"size"})
    _thinking_conflicts = ("complexity", "reasoning_effort", "thinking_level")

    def _body_params(self, request: GenerationRequest) -> dict[str, object]:
        """合并节点/调用参数并叠加思考控制，返回要写进请求体的键值表。"""
        params = self.merged_params(request, reserved=self._dedicated)
        self.log_param_errors()
        decision = self.thinking_decision()
        if decision.applied:
            for key in self._thinking_conflicts:
                if key in params:
                    params.pop(key)
                    self._warn(
                        "thinking 覆盖 custom_params 中的 %s（节点 %s）",
                        key, self.config.id,
                    )
            params[decision.carrier] = decision.value
        return params

    async def generate(self, session, request: GenerationRequest, api_key: str) -> list:
        headers = {"Authorization": f"Bearer {api_key}"}
        model = self.config.model
        size = self.effective_size(request)
        params = self._body_params(request)
        if not request.references:
            url = urljoin(self.config.base_url.rstrip("/") + "/", "images/generations")
            payload: dict[str, object] = {
                "model": model,
                "prompt": request.prompt,
                "n": request.count,
                **params,
            }
            if size:  # 留空/未指定：不发送该字段，交给上游
                payload["size"] = size
            async with session.post(
                url,
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            ) as response:
                return self.parse_common(await self.response_json(response), status=200)
        return await self._edit_multipart(
            session, request, api_key, urljoin(self.config.base_url.rstrip("/") + "/", "images/edits"),
            extra_fields=params, size=size,
        )

    async def _edit_multipart(
        self,
        session,
        request: GenerationRequest,
        api_key: str,
        url: str,
        *,
        extra_fields: dict[str, object] | None = None,
        size: str = "",
        image_field: str = "image[]",
    ) -> list:
        """以 multipart 提交参考图编辑（OpenAI 默认 ``image[]``；xAI 兼容中转用 ``image``）。"""
        form = __import__("aiohttp").FormData()
        form.add_field("model", self.config.model)
        form.add_field("prompt", request.prompt)
        form.add_field("n", str(request.count))
        if size:
            form.add_field("size", size)
        for key, value in (extra_fields or {}).items():
            form.add_field(key, format_param(value))
        for index, image in enumerate(request.references):
            form.add_field(
                image_field,
                image.data,
                filename=image.filename or f"reference-{index}",
                content_type=image.mime_type,
            )
        headers = {"Authorization": f"Bearer {api_key}"}
        async with session.post(url, headers=headers, data=form) as response:
            return self.parse_common(await self.response_json(response), status=200)

    def reference_url(self, image: ImageInput) -> str:
        """参考图的 JSON 形态：base64 data URL（OpenAI 与 xAI 都接受）。"""
        return self.data_url(image)
