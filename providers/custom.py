from __future__ import annotations

import base64

from .base import ProviderAdapter
from ..core.models import GenerationRequest
from ..core.params import format_param


class CustomEndpointAdapter(ProviderAdapter):
    """自定义 JSON 接口（``free`` 模式：未知参数整包透传给 ``parameters``）。

    v1.3.0 起：节点 ``custom_params`` 与调用级 ``extra_params`` 合并后放入
    ``parameters``（调用级覆盖节点级）；``size``/``aspect_ratio`` 用专用槽；调用级
    ``extra_params`` 里的布尔/整数按 JSON 标量发送，其余保持字符串。
    """

    _dedicated = frozenset({"size"})

    async def generate(self, session, request: GenerationRequest, api_key: str) -> list:
        size = self.effective_size(request)
        params = self.merged_params(request, reserved=self._dedicated)
        self.log_param_errors()
        self.thinking_decision()  # custom 不自动注入思考；需要时由用户显式写参
        parameters = {
            key: self._scalar(value) for key, value in params.items()
        }
        body = {
            "prompt": request.prompt,
            "model": self.config.model,
            "count": request.count,
            "aspect_ratio": request.aspect_ratio,
            "references": [
                {"mime_type": image.mime_type, "data": base64.b64encode(image.data).decode()}
                for image in request.references
            ],
            "parameters": parameters,
        }
        if size:  # 留空/未指定：不发送该字段，交给上游
            body["size"] = size
        async with session.post(
            self.config.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        ) as response:
            return self.parse_common(await self.response_json(response), status=200)

    @staticmethod
    def _scalar(value: object) -> object:
        """把配置解析出的值还原成 JSON 标量（``true``/``false`` 字符串→bool 等）。"""
        if isinstance(value, (bool, int, float)):
            return value
        text = format_param(value)
        lowered = text.strip().lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        return text
