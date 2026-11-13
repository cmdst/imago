"""v1.3.0：适配器的参数容器、思考控制与 xAI 参考图上行方式。"""

import base64
import unittest

from imago_fusion.core.models import GenerationRequest, ImageInput, ProviderConfig
from imago_fusion.providers.custom import CustomEndpointAdapter
from imago_fusion.providers.dashscope import DashScopeMultimodalAdapter
from imago_fusion.providers.gemini import GeminiAdapter
from imago_fusion.providers.openai_chat import OpenAIChatAdapter
from imago_fusion.providers.openai_image import OpenAIImageAdapter
from imago_fusion.providers.xai_image import XaiImageAdapter

IMAGE = ImageInput(b"image-bytes", "image/png", "ref.png")


class _Response:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload


class _Context:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Context(_Response(self.payload))


def _adapter(cls, **overrides):
    options = {
        "id": "node", "api_type": "openai_image",
        "base_url": "https://example.invalid/v1", "api_keys": ("key",),
        "model": "gpt-image-test",
    }
    options.update(overrides)
    return cls(ProviderConfig(**options))


def _payload(url="https://example.invalid/out.png"):
    return {"data": [{"url": url}]}


class XaiImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_uses_aspect_and_resolution_without_quality(self):
        adapter = _adapter(XaiImageAdapter, api_type="xai_image", custom_params="resolution=2k")
        session = _Session(_payload())
        await adapter.generate(session, GenerationRequest("draw", size="1280x720"), "key")
        body = session.calls[0][1]["json"]
        self.assertEqual(session.calls[0][0], "https://example.invalid/v1/images/generations")
        self.assertEqual(body["aspect_ratio"], "16:9")
        self.assertEqual(body["resolution"], "2k")
        self.assertNotIn("quality", body)

    async def test_single_reference_uses_image_object(self):
        adapter = _adapter(XaiImageAdapter, api_type="xai_image")
        session = _Session(_payload())
        await adapter.generate(
            session, GenerationRequest("draw", references=[IMAGE]), "key",
        )
        body = session.calls[0][1]["json"]
        self.assertTrue(body["image"]["url"].startswith("data:image/png;base64,"))
        self.assertNotIn("images", body)

    async def test_multiple_references_use_images_list(self):
        adapter = _adapter(XaiImageAdapter, api_type="xai_image")
        session = _Session(_payload())
        await adapter.generate(
            session, GenerationRequest("draw", references=[IMAGE, IMAGE]), "key",
        )
        body = session.calls[0][1]["json"]
        self.assertEqual(len(body["images"]), 2)
        self.assertNotIn("image", body)
        self.assertTrue(all(item["url"].startswith("data:") for item in body["images"]))

    async def test_multipart_edit_mode_uses_image_field_and_hides_switch(self):
        adapter = _adapter(
            XaiImageAdapter, api_type="xai_image", custom_params="edit_mode=multipart",
        )
        session = _Session(_payload())
        await adapter.generate(
            session, GenerationRequest("draw", references=[IMAGE]), "key",
        )
        kwargs = session.calls[0][1]
        self.assertIn("data", kwargs)
        self.assertNotIn("json", kwargs)
        form = kwargs["data"]
        names = [field[0]["name"] for field in form._fields]
        self.assertIn("image", names)
        self.assertNotIn("edit_mode", names)

    async def test_explicit_quality_is_passed_through_with_warning(self):
        adapter = _adapter(
            XaiImageAdapter, api_type="xai_image", custom_params="quality=low",
        )
        session = _Session(_payload())
        await adapter.generate(session, GenerationRequest("draw"), "key")
        self.assertEqual(session.calls[0][1]["json"]["quality"], "low")

    async def test_invalid_resolution_is_dropped(self):
        adapter = _adapter(
            XaiImageAdapter, api_type="xai_image", custom_params="resolution=8k",
        )
        session = _Session(_payload())
        await adapter.generate(session, GenerationRequest("draw"), "key")
        self.assertNotIn("resolution", session.calls[0][1]["json"])


class OpenAIImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_quality_and_thinking_are_independent_fields(self):
        adapter = _adapter(
            OpenAIImageAdapter, custom_params="quality=medium", thinking="high",
        )
        session = _Session(_payload())
        await adapter.generate(session, GenerationRequest("draw"), "key")
        body = session.calls[0][1]["json"]
        self.assertEqual(body["quality"], "medium")
        self.assertEqual(body["complexity"], "high")

    async def test_empty_thinking_sends_no_complexity(self):
        adapter = _adapter(OpenAIImageAdapter)
        session = _Session(_payload())
        await adapter.generate(session, GenerationRequest("draw"), "key")
        self.assertNotIn("complexity", session.calls[0][1]["json"])

    async def test_call_params_override_node_params(self):
        adapter = _adapter(OpenAIImageAdapter, custom_params="quality=low")
        session = _Session(_payload())
        await adapter.generate(
            session, GenerationRequest("draw", extra_params={"quality": "high"}), "key",
        )
        self.assertEqual(session.calls[0][1]["json"]["quality"], "high")


class OpenAIChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_effort_maps_and_size_is_not_sent(self):
        adapter = _adapter(
            OpenAIChatAdapter, api_type="openai_chat", thinking="max",
            custom_params="size=1024x1024",
        )
        session = _Session({"choices": [{"message": {"images": [{"url": "https://example.invalid/a.png"}]}}]})
        await adapter.generate(session, GenerationRequest("draw", size="1024x1024"), "key")
        body = session.calls[0][1]["json"]
        self.assertEqual(body["reasoning_effort"], "xhigh")
        self.assertNotIn("size", body)


class GeminiTests(unittest.IsolatedAsyncioTestCase):
    def _payload(self):
        return {"candidates": [{"content": {"parts": [
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(b"img").decode()}},
        ]}}]}

    async def test_image_size_and_thinking_land_in_generation_config(self):
        adapter = _adapter(
            GeminiAdapter, api_type="gemini_official",
            model="gemini-3.1-flash-lite-image",
            custom_params="image_size=2K", thinking="medium",
        )
        session = _Session(self._payload())
        await adapter.generate(session, GenerationRequest("draw"), "key")
        config = session.calls[0][1]["json"]["generationConfig"]
        self.assertEqual(config["imageConfig"]["imageSize"], "2K")
        self.assertEqual(config["thinkingConfig"]["thinkingLevel"], "HIGH")

    async def test_undeclared_model_gets_no_thinking_config(self):
        adapter = _adapter(
            GeminiAdapter, api_type="gemini_official",
            model="gemini-3-pro-image", thinking="high",
        )
        session = _Session(self._payload())
        await adapter.generate(session, GenerationRequest("draw"), "key")
        self.assertNotIn("thinkingConfig", session.calls[0][1]["json"]["generationConfig"])


class DashScopeAndCustomTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashscope_merges_typed_node_and_call_params(self):
        adapter = _adapter(
            DashScopeMultimodalAdapter, api_type="dashscope_multimodal",
            custom_params="negative_prompt=模糊\nseed=7\nwatermark=yes",
        )
        session = _Session({"output": {"choices": [{"message": {"content": [
            {"image": "https://example.invalid/out.png"},
        ]}}]}})
        await adapter.generate(
            session, GenerationRequest("draw", size="1024x1536"), "key",
        )
        parameters = session.calls[0][1]["json"]["parameters"]
        self.assertEqual(parameters["negative_prompt"], "模糊")
        self.assertEqual(parameters["seed"], 7)
        self.assertIs(parameters["watermark"], True)
        self.assertEqual(parameters["size"], "1024x1536")

    async def test_custom_endpoint_passes_parameters_and_scalars(self):
        adapter = _adapter(
            CustomEndpointAdapter, api_type="custom_endpoint",
            base_url="https://example.invalid/api", custom_params="strength=0.75\nflag=true",
        )
        session = _Session({"data": [{"url": "https://example.invalid/out.png"}]})
        await adapter.generate(session, GenerationRequest("draw", count=2), "key")
        body = session.calls[0][1]["json"]
        self.assertEqual(body["parameters"]["strength"], "0.75")
        self.assertIs(body["parameters"]["flag"], True)
        self.assertEqual(body["count"], 2)


if __name__ == "__main__":
    unittest.main()
