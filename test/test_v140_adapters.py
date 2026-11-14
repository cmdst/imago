"""v1.4.0 契约 §10.4：适配器空引用合法性、参数保留、原生子调用计数。

T-A1 覆盖全部 **7 种** ``api_type``：纯文字请求（``references=[]``）出去的
wire 里不得残留任何图片字段 / 多模态 part。T-A2 验证 ``count``/``size``/
``extra_params`` 仍按既有规则合并。T-A3 验证原生适配器的 ``subcalls`` 是
实例级累加（带图一次 + 纯文字一次 = 两次内部子请求都算）。

另外用 §4.5 B7 的方式核对降级识图的**发送面**（``system_prompt`` 用降级专用
``FALLBACK_CAPTION_SYSTEM``、``image_urls`` 是被拒尝试的参考图、
``request_max_retries=0``）——只断言实参，不声称供应商模型必然遵从。
"""

from __future__ import annotations

import asyncio
import base64
import sys
import time
import types
import unittest
from types import SimpleNamespace

from imago_fusion.core.errors import NoOutputError
from imago_fusion.core.models import DrawTask, GenerationRequest, ImageInput, ProviderConfig
from imago_fusion.core.prompting import REFERENCE_CAPTION_SYSTEM
from imago_fusion.providers.custom import CustomEndpointAdapter
from imago_fusion.providers.dashscope import DashScopeMultimodalAdapter
from imago_fusion.providers.gemini import GeminiAdapter
from imago_fusion.providers.native import NativeAdapter
from imago_fusion.providers.openai_chat import OpenAIChatAdapter
from imago_fusion.providers.openai_image import OpenAIImageAdapter
from imago_fusion.providers.xai_image import XaiImageAdapter
from imago_fusion.services.scheduler import FALLBACK_CAPTION_SYSTEM

PNG = b"\x89PNG\r\n\x1a\n" + bytes(16)
JPEG = b"\xff\xd8\xff\xe0" + bytes(16)
IMAGE_KEYS = ("image", "images", "image[]", "input_image", "input_images", "mask")


class _Captured(Exception):
    """在请求真正发出前截住 wire 载荷（无需构造合法响应）。"""

    def __init__(self, payload):
        super().__init__("captured")
        self.payload = payload


class _Session:
    def __init__(self):
        self.payload = None

    def post(self, url, **kwargs):
        self.payload = kwargs.get("json") if "json" in kwargs else kwargs.get("data")
        self.url = url
        raise _Captured(self.payload)


def _config(api_type: str, **overrides) -> ProviderConfig:
    base = {
        "id": f"node-{api_type}",
        "api_type": api_type,
        "base_url": "https://api.invalid/v1",
        "api_keys": ("key",),
        "model": "model-1",
        "default_size": "1024x1024",
        "timeout": 180,
    }
    base.update(overrides)
    return ProviderConfig(**base)


def _run(adapter, request):
    session = _Session()
    try:
        asyncio.run(adapter.generate(session, request, "key"))
    except _Captured:
        return session.payload
    return session.payload


class EmptyReferenceWireTests(unittest.TestCase):
    """T-A1：``references=[]`` 时 7 种适配器的 wire 里都没有图片。"""

    def test_openai_image(self):
        body = _run(OpenAIImageAdapter(_config("openai_image")), GenerationRequest("p"))
        self.assertIsInstance(body, dict)
        for key in IMAGE_KEYS:
            self.assertNotIn(key, body)

    def test_openai_chat(self):
        body = _run(OpenAIChatAdapter(_config("openai_chat")), GenerationRequest("p"))
        # chat 走 messages；纯文字时 content 是字符串，绝不能是 image_url 数组。
        text = repr(body)
        self.assertNotIn("image_url", text)
        self.assertNotIn("data:image", text)
        self.assertNotIn("base64", text)

    def test_gemini(self):
        body = _run(GeminiAdapter(_config("gemini_official")), GenerationRequest("p"))
        text = repr(body)
        self.assertNotIn("inlineData", text)
        self.assertNotIn("inline_data", text)
        self.assertNotIn("data:image", text)

    def test_dashscope(self):
        adapter = DashScopeMultimodalAdapter(_config("dashscope_multimodal"))
        body = adapter.build_body(GenerationRequest("p"))
        content = body["input"]["messages"][0]["content"]
        self.assertEqual([part.get("text") for part in content], ["p"])
        self.assertFalse(any("image" in part for part in content))

    def test_custom_endpoint(self):
        body = _run(CustomEndpointAdapter(_config("custom_endpoint")), GenerationRequest("p"))
        self.assertEqual(body["references"], [])

    def test_xai_image(self):
        body = _run(XaiImageAdapter(_config("xai_image")), GenerationRequest("p"))
        for key in IMAGE_KEYS:
            self.assertNotIn(key, body)

    def test_astrbot_native(self):
        captured: list[dict] = []

        class _Manager:
            async def get_provider_by_id(self, _provider_id):
                return SimpleNamespace(client=SimpleNamespace(max_retries=2))

        class _Context:
            provider_manager = _Manager()

            async def llm_generate(self, **kwargs):
                captured.append(kwargs)
                return SimpleNamespace(completion_text="", result_chain=None)

        adapter = NativeAdapter(
            _config("astrbot_native", base_url="", astrbot_provider_id="p/x"),
            context=_Context(),
        )
        with self.assertRaises(NoOutputError):
            asyncio.run(adapter.generate(None, GenerationRequest("p", count=1), "key"))
        self.assertTrue(captured)
        for call in captured:
            self.assertEqual(list(call.get("image_urls") or []), [])


class ParameterPreservationTests(unittest.TestCase):
    """T-A2：纯文字请求仍带 count/size/extra_params。"""

    def test_custom_keeps_count_size_and_extra_params(self):
        adapter = CustomEndpointAdapter(_config("custom_endpoint", custom_params="quality=high"))
        request = GenerationRequest("p", count=2, size="1*1", extra_params={"seed": 7})
        body = _run(adapter, request)
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["size"], "1*1")
        self.assertEqual(body["parameters"]["quality"], "high")
        self.assertEqual(body["parameters"]["seed"], 7)

    def test_dashscope_keeps_count_and_size(self):
        adapter = DashScopeMultimodalAdapter(_config("dashscope_multimodal"))
        body = adapter.build_body(GenerationRequest("p", count=3, size="1024*1024"))
        self.assertEqual(body["parameters"]["n"], 3)
        self.assertEqual(body["parameters"]["size"], "1024*1024")


class NativeSubcallCountTests(unittest.TestCase):
    """T-A3：带图一次 + 纯文字一次，``subcalls`` 累加且节点切换不重置。"""

    def test_subcalls_accumulate_across_two_calls(self):
        try:
            from astrbot.api.message_components import Image as RealImage
        except Exception:  # pragma: no cover - 仅在裸环境下跳过
            self.skipTest("缺少 astrbot.api.message_components")
        urls = iter(["https://cdn.invalid/a.png", "https://cdn.invalid/b.png"])

        class _Provider:
            client = SimpleNamespace(max_retries=2)

        class _Manager:
            async def get_provider_by_id(self, _provider_id):
                return _Provider()

        class _Context:
            provider_manager = _Manager()

            async def llm_generate(self, **_kwargs):
                # 非流式路径：一次 llm_generate = 一次内部子请求，返回一张图。
                item = RealImage(file="", url=next(urls))
                return SimpleNamespace(
                    completion_text="", result_chain=SimpleNamespace(chain=[item]),
                )

        adapter = NativeAdapter(
            _config("astrbot_native", base_url="", astrbot_provider_id="p/x"),
            context=_Context(),
        )
        with_image = GenerationRequest("p", references=(ImageInput(PNG, "image/png"),))
        asyncio.run(adapter.generate(None, with_image, "key"))
        first = adapter.subcalls
        self.assertEqual(first, 1)
        asyncio.run(adapter.generate(None, GenerationRequest("p"), "key"))
        # 两次调用各自内部一次子请求，实例级累加（节点切换不重置）。
        self.assertEqual(adapter.subcalls, 2)


class FallbackCaptionWireTests(unittest.IsolatedAsyncioTestCase):
    """§4.5 B7：降级识图的发送面（实参级，不假称模型行为）。"""

    def _plugin(self):
        from imago_fusion.main import ImagoFusion

        plugin = object.__new__(ImagoFusion)
        plugin.raw_config = {}
        self.calls = []

        class _Context:
            async def llm_generate(inner, **kwargs):
                self.calls.append(kwargs)
                return SimpleNamespace(completion_text="  一名短发女性站在窗边。  ")

        plugin.context = _Context()
        return plugin

    async def test_caption_request_uses_dedicated_system_prompt_and_no_retries(self):
        plugin = self._plugin()
        config = SimpleNamespace(vision_provider_id="vision-1", generation_timeout=120)
        task = DrawTask(id="0123456789abcdef", umo="u", request=GenerationRequest("原文"))
        task.created_at = time.monotonic()
        task.runtime["original_user_prompt"] = "原文"
        text = await plugin._fallback_caption(
            task,
            config,
            "参考图用户说明",
            [ImageInput(PNG, "image/png"), ImageInput(JPEG, "image/jpeg")],
            timeout=20,
            max_chars=200,
        )
        self.assertEqual(text, "一名短发女性站在窗边。")
        call = self.calls[0]
        self.assertIs(call["system_prompt"], FALLBACK_CAPTION_SYSTEM)
        self.assertNotEqual(FALLBACK_CAPTION_SYSTEM, REFERENCE_CAPTION_SYSTEM)
        self.assertEqual(call["chat_provider_id"], "vision-1")
        self.assertEqual(call["prompt"], "参考图用户说明")
        self.assertEqual(call["request_max_retries"], 0)
        expected = [
            "data:image/png;base64," + base64.b64encode(PNG).decode("ascii"),
            "data:image/jpeg;base64," + base64.b64encode(JPEG).decode("ascii"),
        ]
        self.assertEqual(call["image_urls"], expected)

    async def test_caption_returns_empty_without_vision_provider(self):
        plugin = self._plugin()
        config = SimpleNamespace(vision_provider_id="", generation_timeout=120)
        task = DrawTask(id="0123456789abcdef", umo="u", request=GenerationRequest("原文"))
        task.created_at = time.monotonic()
        text = await plugin._fallback_caption(
            task, config, "p", [ImageInput(PNG, "image/png")], timeout=20, max_chars=200,
        )
        self.assertEqual(text, "")
        self.assertEqual(self.calls, [])
