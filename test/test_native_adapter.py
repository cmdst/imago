"""Unit tests for the AstrBot-native adapter contract and the native scheduler path.

These tests run without a live AstrBot: the two AstrBot imports used by the
adapter (``astrbot.api.message_components.Image`` inside ``_extract``) are stubbed
in ``sys.modules`` before the adapter is exercised, which is exactly the
"fake LLMResponse / MessageChain" strategy DESIGN §1.1 asks for.

Covered contract points (DESIGN §2):

- L1 ``result_chain`` extraction, L2 Markdown extraction, reference whitelist;
- response de-duplication across frames and retries;
- text-only responses surface the model's own reason in ``NoOutputError``;
- the adapter performs **no** download, and the private-network guard is applied
  by ``core.network.fetch_reference`` when the result is materialized;
- ``API_TYPES`` and ``ADAPTERS`` stay in sync.
"""

import asyncio
import base64
import sys
import types
import unittest

import aiohttp
from imago_fusion.core import runtime
from imago_fusion.core.config import API_TYPES, load_config, matches_template
from imago_fusion.core.errors import NoOutputError, ProviderError, ReferenceImageError
from imago_fusion.core.models import (
    GenerationRequest,
    ImageInput,
    ProviderConfig,
)
from imago_fusion.core.network import fetch_reference
from imago_fusion.providers import ADAPTERS
from imago_fusion.providers.base import ProviderAdapter
from imago_fusion.providers.native import NativeAdapter


class FakeImage:
    """Stand-in for ``astrbot.api.message_components.Image``."""

    def __init__(self, file: str = "", url: str = "") -> None:
        self.file = file
        self.url = url


def _install_fake_astrbot_modules() -> None:
    """Register the minimal AstrBot module tree the adapter imports lazily."""
    api = types.ModuleType("astrbot.api")
    components = types.ModuleType("astrbot.api.message_components")
    components.Image = FakeImage
    api.message_components = components
    sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.message_components"] = components


class FakeChain:
    def __init__(self, components: list) -> None:
        self.chain = components


class FakeResponse:
    def __init__(self, *, completion_text: str = "", chain=None) -> None:
        self.completion_text = completion_text
        self.result_chain = chain


class FakeProvider:
    def __init__(self, frames: list, *, max_retries: int = 2) -> None:
        self.frames = frames
        # Mirrors the ``AsyncOpenAI`` client AstrBot builds: its retry default is
        # the SDK's (2), which is the hidden loop the adapter has to turn off.
        self.client = types.SimpleNamespace(max_retries=max_retries)

    async def text_chat_stream(self, **kwargs):
        self.stream_kwargs = kwargs
        for frame in self.frames:
            yield frame


class FakeProviderManager:
    def __init__(self, provider) -> None:
        self.provider = provider

    async def get_provider_by_id(self, provider_id):
        return self.provider


class FakeContext:
    def __init__(
        self, *, response=None, provider=None, error: Exception | None = None
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []
        self.provider_manager = FakeProviderManager(provider)

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _config(**overrides) -> ProviderConfig:
    base = {
        "id": "native_node",
        "api_type": "astrbot_native",
        "base_url": "",
        "api_keys": (),
        "model": "",
        "astrbot_provider_id": "cpa_Gemini/web-gemini-image",
    }
    base.update(overrides)
    return ProviderConfig(**base)


class NativeExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_fake_astrbot_modules()

    def test_result_chain_http_image_becomes_a_url_reference(self):
        context = FakeContext(
            response=FakeResponse(
                chain=FakeChain([FakeImage(url="https://host/a.png")])
            )
        )
        adapter = NativeAdapter(_config(), context=context)
        results = asyncio.run(adapter.generate(None, GenerationRequest("cat"), ""))
        self.assertEqual([item.url for item in results], ["https://host/a.png"])
        self.assertIsNone(results[0].data)
        # api_key is ignored: the AstrBot provider owns the credential.
        self.assertEqual(
            context.calls[0]["chat_provider_id"], "cpa_Gemini/web-gemini-image"
        )
        self.assertEqual(context.calls[0]["request_max_retries"], 1)

    def test_result_chain_base64_file_reference_is_forwarded_untouched(self):
        chain = FakeChain([FakeImage(file="base64://iVBORw0KGgo=")])
        context = FakeContext(response=FakeResponse(chain=chain))
        results = asyncio.run(
            NativeAdapter(_config(), context=context).generate(
                None, GenerationRequest("cat"), ""
            )
        )
        # The adapter must not decode: the payload reaches core.network verbatim.
        self.assertEqual(results[0].url, "base64://iVBORw0KGgo=")

    def test_markdown_extraction_handles_data_urls_urls_and_angle_wrapping(self):
        text = (
            "done ![a](data:image/png;base64,iVBORw0KGgo=) "
            "![b](<https://host/b.png>) ![c](//host/c.png)"
        )
        context = FakeContext(response=FakeResponse(completion_text=text))
        results = asyncio.run(
            NativeAdapter(_config(), context=context).generate(
                None, GenerationRequest("cat"), ""
            )
        )
        self.assertEqual(
            [item.url for item in results],
            [
                "data:image/png;base64,iVBORw0KGgo=",
                "https://host/b.png",
                "http://host/c.png",
            ],
        )

    def test_unacceptable_references_are_skipped_not_downloaded(self):
        text = "![a](file:///etc/passwd) ![b](/root/secret.png) ![c](base64://AAAA)"
        context = FakeContext(response=FakeResponse(completion_text=text))
        results = asyncio.run(
            NativeAdapter(_config(), context=context).generate(
                None, GenerationRequest("cat"), ""
            )
        )
        self.assertEqual([item.url for item in results], ["base64://AAAA"])

    def test_references_are_encoded_as_data_urls_for_the_provider(self):
        reference = ImageInput(b"\x89PNG\r\n\x1a\n", "image/png")
        context = FakeContext(
            response=FakeResponse(
                chain=FakeChain([FakeImage(url="https://host/a.png")])
            )
        )
        request = GenerationRequest("cat", references=[reference])
        asyncio.run(
            NativeAdapter(_config(), context=context).generate(None, request, "")
        )
        self.assertEqual(
            context.calls[0]["image_urls"], ["data:image/png;base64,iVBORw0KGgo="]
        )

    def test_text_only_response_reports_the_model_reason(self):
        context = FakeContext(
            response=FakeResponse(completion_text="blocked by safety policy")
        )
        with self.assertRaises(NoOutputError) as ctx:
            asyncio.run(
                NativeAdapter(_config(), context=context).generate(
                    None, GenerationRequest("cat"), ""
                )
            )
        self.assertIn("blocked by safety policy", str(ctx.exception))

    def test_missing_provider_id_is_a_configuration_error(self):
        adapter = NativeAdapter(_config(astrbot_provider_id=""), context=FakeContext())
        with self.assertRaises(ProviderError):
            asyncio.run(adapter.generate(None, GenerationRequest("cat"), ""))

    def test_provider_not_found_is_mapped_to_a_diagnosable_error(self):
        class ProviderNotFoundError(Exception):
            pass

        context = FakeContext(error=ProviderNotFoundError("Provider x not found"))
        with self.assertRaises(ProviderError) as ctx:
            asyncio.run(
                NativeAdapter(_config(), context=context).generate(
                    None, GenerationRequest("cat"), ""
                )
            )
        self.assertIn("不存在", str(ctx.exception))

    def test_runtime_context_is_used_when_the_adapter_has_none(self):
        context = FakeContext(
            response=FakeResponse(
                chain=FakeChain([FakeImage(url="https://host/a.png")])
            )
        )
        runtime.configure(context=context)
        try:
            results = asyncio.run(
                NativeAdapter(_config()).generate(None, GenerationRequest("cat"), "")
            )
        finally:
            runtime.reset()
        self.assertEqual([item.url for item in results], ["https://host/a.png"])


class NativeStreamAndCountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _install_fake_astrbot_modules()

    def test_stream_frames_accumulate_and_duplicate_frames_collapse(self):
        frames = [
            FakeResponse(chain=FakeChain([FakeImage(url="https://host/a.png")])),
            FakeResponse(chain=FakeChain([FakeImage(url="https://host/a.png")])),
            FakeResponse(chain=FakeChain([FakeImage(url="https://host/b.png")])),
        ]
        provider = FakeProvider(frames)
        context = FakeContext(provider=provider)
        adapter = NativeAdapter(_config(stream=True), context=context)
        results = asyncio.run(adapter.generate(None, GenerationRequest("cat"), ""))
        self.assertEqual(
            [item.url for item in results], ["https://host/a.png", "https://host/b.png"]
        )
        self.assertEqual(provider.stream_kwargs["request_max_retries"], 1)

    def test_single_image_response_is_returned_for_multi_count_requests(self):
        context = FakeContext(
            response=FakeResponse(
                chain=FakeChain([FakeImage(url="https://host/a.png")])
            )
        )
        results = asyncio.run(
            NativeAdapter(_config(), context=context).generate(
                None, GenerationRequest("cat", count=3), ""
            )
        )
        # Three attempts are allowed, but one image is a valid (partial) success.
        self.assertEqual(len(results), 1)
        self.assertLessEqual(len(context.calls), 3)

    def test_non_stream_call_disables_the_sdk_retry_loop(self):
        provider = FakeProvider([], max_retries=2)
        context = FakeContext(
            response=FakeResponse(
                chain=FakeChain([FakeImage(url="https://host/a.png")])
            ),
            provider=provider,
        )
        asyncio.run(
            NativeAdapter(_config(), context=context).generate(
                None, GenerationRequest("cat"), ""
            )
        )
        # The hidden SDK loop must not spend the node budget on a second attempt
        # after a timeout; the scheduler's fallback owns retrying instead.
        self.assertEqual(provider.client.max_retries, 0)

    def test_stream_call_disables_the_sdk_retry_loop(self):
        provider = FakeProvider(
            [FakeResponse(chain=FakeChain([FakeImage(url="https://host/a.png")]))],
        )
        asyncio.run(
            NativeAdapter(
                _config(stream=True), context=FakeContext(provider=provider)
            ).generate(None, GenerationRequest("cat"), "")
        )
        self.assertEqual(provider.client.max_retries, 0)

    def test_failed_attempts_still_return_the_successful_image(self):
        responses = [
            ProviderError("upstream 500"),
            FakeResponse(chain=FakeChain([FakeImage(url="https://host/a.png")])),
        ]

        class FlakyContext(FakeContext):
            async def llm_generate(self, **kwargs):
                self.calls.append(kwargs)
                value = responses.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value

        context = FlakyContext()
        results = asyncio.run(
            NativeAdapter(_config(), context=context).generate(
                None, GenerationRequest("cat", count=2), ""
            )
        )
        self.assertEqual([item.url for item in results], ["https://host/a.png"])


class NativeResultMaterializationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        _install_fake_astrbot_modules()

    async def test_private_network_guard_applies_when_the_result_is_fetched(self):
        """The adapter returns a reference; core.network refuses to download it."""
        context = FakeContext(
            response=FakeResponse(
                chain=FakeChain([FakeImage(url="http://127.0.0.1/a.png")])
            )
        )
        results = await NativeAdapter(_config(), context=context).generate(
            None, GenerationRequest("cat"), ""
        )
        self.assertEqual(results[0].url, "http://127.0.0.1/a.png")
        async with aiohttp.ClientSession() as session:
            with self.assertRaises(ReferenceImageError) as ctx:
                await fetch_reference(
                    session,
                    results[0].url,
                    max_bytes=1024 * 1024,
                    block_private=True,
                )
        self.assertIn("私网", str(ctx.exception))

    async def test_base64_reference_is_decoded_and_magic_checked_downstream(self):
        payload = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
        context = FakeContext(
            response=FakeResponse(
                chain=FakeChain([FakeImage(file=f"base64://{payload}")])
            )
        )
        results = await NativeAdapter(_config(), context=context).generate(
            None, GenerationRequest("cat"), ""
        )
        async with aiohttp.ClientSession() as session:
            image = await fetch_reference(
                session,
                results[0].url,
                max_bytes=1024 * 1024,
                block_private=True,
            )
        self.assertEqual(image.mime_type, "image/png")
        self.assertTrue(image.data.startswith(b"\x89PNG"))


class NativeConfigTests(unittest.TestCase):
    def test_api_types_matches_the_adapter_registry(self):
        self.assertEqual(API_TYPES, set(ADAPTERS))

    def test_native_node_needs_no_base_url_or_keys(self):
        cfg = load_config(
            {
                "providers": [
                    {
                        "id": "native_node",
                        "api_type": "astrbot_native",
                        "astrbot_provider_id": "cpa_Gemini/web-gemini-image",
                        "stream": True,
                    }
                ]
            }
        )
        self.assertEqual(len(cfg.providers), 1)
        node = cfg.providers[0]
        self.assertEqual(node.astrbot_provider_id, "cpa_Gemini/web-gemini-image")
        self.assertEqual(node.api_keys, ())
        self.assertTrue(node.stream)

    def test_native_node_without_provider_id_is_dropped(self):
        cfg = load_config(
            {"providers": [{"id": "native_node", "api_type": "astrbot_native"}]}
        )
        self.assertEqual(cfg.providers, ())

    def test_http_node_still_requires_base_url_and_keys(self):
        cfg = load_config(
            {
                "providers": [
                    {
                        "id": "http_node",
                        "api_type": "openai_image",
                        "base_url": "https://x",
                    }
                ]
            }
        )
        self.assertEqual(cfg.providers, ())

    def test_fallback_order_sorts_nodes_but_keeps_ties_stable(self):
        cfg = load_config(
            {
                "providers": [
                    {
                        "id": "a",
                        "api_type": "openai_image",
                        "base_url": "https://x",
                        "api_keys": ["k"],
                        "fallback_order": 5,
                    },
                    {
                        "id": "b",
                        "api_type": "openai_image",
                        "base_url": "https://x",
                        "api_keys": ["k"],
                        "fallback_order": 1,
                    },
                    {
                        "id": "c",
                        "api_type": "openai_image",
                        "base_url": "https://x",
                        "api_keys": ["k"],
                    },
                    {
                        "id": "d",
                        "api_type": "openai_image",
                        "base_url": "https://x",
                        "api_keys": ["k"],
                        "fallback_order": 1,
                    },
                ]
            }
        )
        self.assertEqual([item.id for item in cfg.providers], ["c", "b", "d", "a"])


class TemplateOverrideGuardTests(unittest.TestCase):
    """`模板名/模型名` 覆写判据与「同名原生节点」碰撞守卫。"""

    @staticmethod
    def _nodes():
        return [
            # 同名标识：模板名 == astrbot_native 节点 id
            ProviderConfig(
                "cpa_Gemini",
                "astrbot_native",
                "",
                (),
                astrbot_provider_id="cpa_Gemini/web-gemini-image",
            ),
            ProviderConfig(
                "node_b", "gemini_official", "https://example.invalid/v1beta", ("k",)
            ),
        ]

    def test_collision_with_native_node_id_skips_the_override(self):
        nodes = self._nodes()
        self.assertFalse(
            matches_template(nodes, "cpa_Gemini/other-model", enabled=True),
            "模板名与 astrbot_native 节点 id 同名时必须跳过覆写，避免遮蔽原生路由",
        )

    def test_normal_override_still_applies_when_no_collision(self):
        nodes = self._nodes()
        self.assertTrue(matches_template(nodes, "node_b/model_override", enabled=True))
        # 非原生类型的同名节点不触发守卫（只有 astrbot_native 会被遮蔽）
        self.assertTrue(matches_template(nodes, "node_b/another", enabled=True))

    def test_disabled_switch_never_overrides(self):
        nodes = self._nodes()
        self.assertFalse(
            matches_template(nodes, "node_b/model_override", enabled=False)
        )
        self.assertFalse(
            matches_template(nodes, "cpa_Gemini/other-model", enabled=False)
        )

    def test_reference_without_slash_or_unknown_template_is_not_an_override(self):
        nodes = self._nodes()
        self.assertFalse(matches_template(nodes, "node_b", enabled=True))
        self.assertFalse(matches_template(nodes, "unknown/model", enabled=True))


class FileUriKeyTests(unittest.TestCase):
    """Regression tests for the Files API key-splicing fix (DESIGN §4 B1).

    1.1.0 吸收上游 imago v1.3.0 的 F1 修复后，第三个参数 ``api_host`` 承载「同域」
    判据：只有与节点 ``base_url`` 同 netloc 的 fileUri 才补 key，跨域一律原样返回
    （中转把结果图放到自己的对象存储时，补 key 等于把密钥交给第三方日志）。
    """

    def setUp(self):
        from imago_fusion.providers.gemini import _file_uri_with_key

        self.append = _file_uri_with_key
        self.key = "AIzaTESTKEY123"
        self.host = "relay.local"

    def test_plain_url_gets_the_key(self):
        self.assertEqual(
            self.append("https://relay.local/files/abc", self.key, self.host),
            "https://relay.local/files/abc?key=AIzaTESTKEY123",
        )

    def test_existing_query_keeps_the_key_as_a_query_parameter(self):
        self.assertEqual(
            self.append("https://relay.local/files/abc?alt=media", self.key, self.host),
            "https://relay.local/files/abc?alt=media&key=AIzaTESTKEY123",
        )

    def test_monkey_parameter_is_not_mistaken_for_an_existing_key(self):
        self.assertEqual(
            self.append("https://relay.local/files/abc?monkey=1", self.key, self.host),
            "https://relay.local/files/abc?monkey=1&key=AIzaTESTKEY123",
        )

    def test_fragment_stays_last_so_the_key_is_actually_sent(self):
        self.assertEqual(
            self.append("https://relay.local/files/abc?alt=media#frag", self.key, self.host),
            "https://relay.local/files/abc?alt=media&key=AIzaTESTKEY123#frag",
        )

    def test_valueless_key_parameter_is_recognised(self):
        self.assertEqual(
            self.append("https://relay.local/files/abc?key", self.key, self.host),
            "https://relay.local/files/abc?key",
        )

    def test_apiKey_alias_is_recognised(self):
        self.assertEqual(
            self.append("https://relay.local/files/abc?apiKey=zz", self.key, self.host),
            "https://relay.local/files/abc?apiKey=zz",
        )

    def test_existing_key_is_left_alone(self):
        self.assertEqual(
            self.append("https://relay.local/files/abc?key=other", self.key, self.host),
            "https://relay.local/files/abc?key=other",
        )

    def test_non_http_references_are_never_modified(self):
        self.assertEqual(self.append("base64://AAAA", self.key, self.host), "base64://AAAA")
        self.assertEqual(self.append("", self.key, self.host), "")

    def test_empty_key_is_not_appended(self):
        self.assertEqual(
            self.append("https://relay.local/files/abc", "", self.host),
            "https://relay.local/files/abc",
        )

    def test_cross_domain_uri_never_receives_the_key(self):
        # 中转把结果图放到第三方对象存储时，绝不能把 Gemini key 拼上去（F1）。
        self.assertEqual(
            self.append("https://cdn.third-party.invalid/files/abc", self.key, self.host),
            "https://cdn.third-party.invalid/files/abc",
        )
        self.assertEqual(
            self.append("https://relay.local.evil.invalid/files/abc", self.key, self.host),
            "https://relay.local.evil.invalid/files/abc",
        )

    def test_empty_host_never_appends_the_key(self):
        # base_url 没有 netloc（空串 / 裸路径）时一律不补：宁可下载 401 也不泄密钥。
        self.assertEqual(
            self.append("https://relay.local/files/abc", self.key, ""),
            "https://relay.local/files/abc",
        )
        self.assertEqual(
            self.append("https://relay.local/files/abc", self.key, "   "),
            "https://relay.local/files/abc",
        )


class GeminiFileUriCallSiteTests(unittest.IsolatedAsyncioTestCase):
    """`GeminiAdapter.generate()` 必须把节点 netloc 传给 `_file_uri_with_key()`。

    只测 `_file_uri_with_key` 本身不够：同域判据的入参来自 `base_url`，调用点漏传
    就会退化成「一律不补 key」（结果图下载 401）；传错（例如传整个 URL）则会静默
    变成「只对某些 URL 补」。这里用假 session 走一遍真实 `generate()`。
    """

    class _Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def json(self, content_type=None):
            return self.payload

    class _Session:
        def __init__(self, payload):
            self.payload = payload
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return GeminiFileUriCallSiteTests._Response(self.payload)

    async def test_same_host_file_uri_gets_the_key_but_cross_domain_does_not(self):
        from imago_fusion.providers.gemini import GeminiAdapter

        adapter = GeminiAdapter(ProviderConfig(
            "gemini", "gemini_official", "https://relay.local/v1beta", ("k",), model="m",
        ))
        payload = {"candidates": [{"content": {"parts": [
            {"fileData": {"fileUri": "https://relay.local/v1beta/files/abc"}},
            {"fileData": {"fileUri": "https://cdn.third-party.invalid/files/abc"}},
        ]}}]}
        session = self._Session(payload)
        results = await adapter.generate(session, GenerationRequest("画一张测试图"), "k1")
        self.assertEqual(
            results[0].url, "https://relay.local/v1beta/files/abc?key=k1",
            "同域 fileUri 必须补 key，否则结果图下载 401",
        )
        self.assertEqual(
            results[1].url, "https://cdn.third-party.invalid/files/abc",
            "跨域 fileUri 绝不能被拼上 key（等于把密钥交给第三方存储）",
        )


class CommonPayloadMimeTests(unittest.TestCase):
    """`parse_common` 的 base64 结果必须按魔数定 MIME（T8-F1，与 imago B4 同形缺陷）。

    base64 结果没有 MIME 声明；落盘时 `materialize_result` 按 `mime_type` 决定扩展名，
    一律 image/png 会把 JPEG/WEBP 字节写成 `.png`，部分平台按扩展名处理时被拒或显示异常。
    """

    @staticmethod
    def _parse(raw: bytes):
        payload = {"data": [{"b64_json": base64.b64encode(raw).decode()}]}
        return ProviderAdapter.parse_common(payload)[0]

    def test_base64_jpeg_detects_mime_by_magic(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
        result = self._parse(jpeg)
        self.assertEqual(result.mime_type, "image/jpeg")
        self.assertEqual(result.data, jpeg)

    def test_base64_png_detects_mime_by_magic(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        self.assertEqual(self._parse(png).mime_type, "image/png")

    def test_base64_unknown_format_falls_back_to_png(self):
        # 魔数无法识别时保留旧的 image/png 行为（不引入新的失败面）。
        self.assertEqual(self._parse(b"not-an-image").mime_type, "image/png")


class RedactionTests(unittest.TestCase):
    def test_bare_key_query_parameter_is_masked(self):
        from imago_fusion.core.security import redact

        masked = redact(
            "download failed https://relay.local/files/abc?key=AIzaSECRET&alt=media"
        )
        self.assertNotIn("AIzaSECRET", masked)
        self.assertIn("key=<redacted>", masked)

    def test_named_credentials_are_still_masked(self):
        from imago_fusion.core.security import redact

        self.assertEqual(redact("api_key=abc123"), "api_key=<redacted>")
        self.assertEqual(redact("apikey: abc123"), "apikey=<redacted>")
        self.assertEqual(redact("token=abc123"), "token=<redacted>")


if __name__ == "__main__":
    unittest.main()
