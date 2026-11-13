from __future__ import annotations

import base64
from typing import Any

from .base import ProviderAdapter
from ..core.errors import NoOutputError, ProviderError
from ..core.models import GenerationRequest, ImageResult


_BOOLEAN_PARAMS = {"prompt_extend", "watermark"}
_INTEGER_PARAMS = {"seed"}


def _data_url(mime_type: str, data: bytes) -> str:
    return f"data:{mime_type};base64,{base64.b64encode(data).decode()}"


def _parameter_value(key: str, value: Any) -> Any:
    """把参数值转成百炼 ``parameters`` 需要的类型（已是 bool/int 的原样保留）。"""
    if isinstance(value, bool):
        return value
    if key in _BOOLEAN_PARAMS:
        if isinstance(value, (int, float)):
            return bool(value)
        lowered = str(value).strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        raise ProviderError(f"DashScope 参数 {key} 必须是布尔值")
    if key in _INTEGER_PARAMS:
        if isinstance(value, int):
            return value
        try:
            return int(str(value))
        except ValueError as exc:
            raise ProviderError(f"DashScope 参数 {key} 必须是整数") from exc
    return value


class DashScopeMultimodalAdapter(ProviderAdapter):
    """百炼多模态生图。

    v1.3.0 起：节点 ``custom_params``（``prompt_extend``/``watermark``/``seed``/
    ``negative_prompt``/未知键）与调用级 ``extra_params`` 一起合并进 ``parameters``；
    ``size`` 仍走专用槽（调用级 → 节点参数 → 节点默认）。百炼没有思考控制字段，
    ``thinking`` 只会记 DEBUG（``core.params.resolve_thinking``）。
    """

    _dedicated = frozenset({"size", "n"})

    async def generate(self, session, request: GenerationRequest, api_key: str) -> list[ImageResult]:
        async with session.post(
            self.config.base_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=self.build_body(request),
        ) as response:
            payload = await self.response_json(response)
        return self.parse_response(payload)

    def build_body(self, request: GenerationRequest) -> dict[str, Any]:
        content = [
            {"image": _data_url(image.mime_type, image.data)}
            for image in request.references
        ]
        content.append({"text": request.prompt})

        params = self.merged_params(request, reserved=self._dedicated)
        self.log_param_errors()
        self.thinking_decision()  # 百炼无思考字段：只写 DEBUG，不发任何东西
        parameters: dict[str, Any] = {
            "prompt_extend": True,
            "n": request.count,
        }
        # size 原样透传（不换算 x→*）：需要星号的模型请在配置或参数里直接写 1024*1024
        size = self.effective_size(request)
        if size:
            parameters["size"] = size
        for key, value in params.items():
            parameters[key] = _parameter_value(key, value)

        return {
            "model": self.config.model,
            "input": {
                "messages": [{"role": "user", "content": content}],
            },
            "parameters": parameters,
        }

    @staticmethod
    def parse_response(payload: Any) -> list[ImageResult]:
        results: list[ImageResult] = []
        if isinstance(payload, dict):
            output = payload.get("output", {})
            choices = output.get("choices", []) if isinstance(output, dict) else []
            for choice in choices if isinstance(choices, list) else []:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message", {})
                content = message.get("content", []) if isinstance(message, dict) else []
                for item in content if isinstance(content, list) else []:
                    image = item.get("image") if isinstance(item, dict) else None
                    if isinstance(image, str) and image:
                        results.append(ImageResult(url=image))
            if not results and payload.get("code"):
                from ..core.security import redact_debug

                code = redact_debug(str(payload.get("code", "")))
                message = redact_debug(str(payload.get("message", "")))
                detail = f" code={code}" if code else ""
                if message:
                    detail += f" message={message}"
                # HTTP 200 的业务拒绝（有 code/message、无图）按契约抛结构化
                # NoOutputError：它已完成一次计费请求，不能按 FAILED 退款（R2-04）。
                raise NoOutputError(
                    f"DashScope{detail}",
                    status=200,
                    code=str(payload.get("code", "")),
                    source="body",
                )
        if not results:
            raise NoOutputError("DashScope 响应中没有可识别的图片")
        return results
