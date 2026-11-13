from __future__ import annotations

import re
from urllib.parse import urljoin

from .base import ProviderAdapter
from ..core.errors import NoOutputError, UnsupportedResponse
from ..core.models import GenerationRequest, ImageResult


class OpenAIChatAdapter(ProviderAdapter):
    """OpenAI ``chat/completions`` 生图（兼容中转）。

    v1.3.0 起：节点参数与调用级 ``extra_params`` 合并进请求体；``thinking`` 独立映射
    到 ``reasoning_effort``（``max`` → ``xhigh``）。``size`` 在 Chat 协议里没有承载
    字段，节点参数里写了也**不发送**，只记 DEBUG（不静默丢，见 rev3 §5）。
    """

    _dedicated = frozenset({"size"})
    _thinking_conflicts = ("reasoning_effort", "complexity", "thinking_level")

    async def generate(self, session, request: GenerationRequest, api_key: str) -> list[ImageResult]:
        content: object = request.prompt
        if request.references:
            content = [{"type": "text", "text": request.prompt}]
            content.extend(
                {"type": "image_url", "image_url": {"url": self.data_url(image)}}
                for image in request.references
            )
        params = self.merged_params(request, reserved=self._dedicated)
        self.log_param_errors()
        if "size" in params:  # 理论上已被 reserved 过滤，防御性保留
            params.pop("size")
            self._debug(
                "参数 忽略 api_type=openai_chat key=size reason=no_container",
            )
        decision = self.thinking_decision()
        if decision.applied:
            for key in self._thinking_conflicts:
                if key in params:
                    params.pop(key)
                    self._warn("thinking 覆盖 custom_params 中的 %s（节点 %s）", key, self.config.id)
            params[decision.carrier] = decision.value
        body = {
            "model": self.config.model,
            "messages": [{"role": "user", "content": content}],
            "n": request.count,
            **params,
        }
        url = urljoin(self.config.base_url.rstrip("/") + "/", "chat/completions")
        async with session.post(
            url, headers={"Authorization": f"Bearer {api_key}"}, json=body,
        ) as response:
            payload = await self.response_json(response)
        if not isinstance(payload, dict):
            raise UnsupportedResponse("Chat 上游响应格式无效")
        choices = payload.get("choices", [])
        if not isinstance(choices, list):
            raise UnsupportedResponse("Chat 上游 choices 格式无效")
        results: list[ImageResult] = []
        finish_reason = ""
        refusal = ""
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            if not finish_reason:
                finish_reason = str(choice.get("finish_reason") or "")
            message = choice.get("message", {})
            if not isinstance(message, dict):
                continue
            if not refusal and isinstance(message.get("refusal"), str):
                refusal = message["refusal"].strip()
            images = message.get("images", []) or []
            if not isinstance(images, list):
                images = []
            for item in images:
                if not isinstance(item, (dict, str)):
                    continue
                value = item.get("image_url", item) if isinstance(item, dict) else item
                value = value.get("url", "") if isinstance(value, dict) else value
                if isinstance(value, str) and value:
                    results.append(ImageResult(url=value))
            text = message.get("content", "")
            if isinstance(text, list):
                for part in text:
                    if isinstance(part, dict) and part.get("type") in {"image", "image_url"}:
                        value = part.get("image_url") or part.get("url") or part.get("data")
                        if isinstance(value, dict):
                            value = value.get("url")
                        if value:
                            results.append(ImageResult(url=value))
            elif isinstance(text, str):
                for value in re.findall(
                    r"(?:https?://[^\s)]+|data:image/[^;]+;base64,[A-Za-z0-9+/=]+)", text,
                ):
                    results.append(ImageResult(url=value))
        if not results:
            # HTTP 200 但内容被拒：必须抛出**结构化**信号，拒绝分类器才认（rev3 §4）。
            if refusal:
                raise NoOutputError(
                    "Chat 上游拒绝了本次请求",
                    code="refusal",
                    finish_reason=finish_reason,
                    block_reason=refusal[:200],
                    status=200,
                    source="body",
                )
            if finish_reason.lower() in {"content_filter", "safety", "blocked", "refusal"}:
                raise NoOutputError(
                    "Chat 上游因内容安全策略未返回图片",
                    code=finish_reason.lower(),
                    finish_reason=finish_reason,
                    status=200,
                    source="body",
                )
            raise NoOutputError("Chat 响应中没有图片", finish_reason=finish_reason)
        return results
