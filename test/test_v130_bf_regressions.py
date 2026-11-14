"""v1.3.0 修复轮（BF-01..12）里仍然有效的执行级回归。

v1.4.0 已删除实验遮挡整条链路，因此这里只保留与它无关的部分：拒绝分类器的证据
规则、适配器参数容器（aspectRatio）、以及终态分类与退款政策。降级本身的执行级
用例在 ``test_v140_text_fallback.py``。
"""

import asyncio
import base64
import time
import unittest

from imago_fusion.core.errors import NoOutputError, ProviderError
from imago_fusion.core.models import (
    DrawTask,
    GenerationRequest,
    ImageInput,
    ProviderConfig,
    ReferenceFallbackConfig,
    RuntimeConfig,
    TaskState,
)
from imago_fusion.core.references import classify_rejection
from imago_fusion.providers.base import ProviderAdapter
from imago_fusion.providers.gemini import GeminiAdapter
from imago_fusion.providers.xai_image import XaiImageAdapter
from imago_fusion.services.scheduler import TaskScheduler

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _node(provider_id, *, api_type="custom_endpoint", base_url="https://example.invalid/v1", **kw):
    model = kw.pop("model", "model")
    return ProviderConfig(id=provider_id, api_type=api_type, base_url=base_url, api_keys=("key",), model=model, **kw)


class RejectionEvidenceTests(unittest.TestCase):
    """BF-04/05：状态码只是门槛，业务证据必须真实且按字段语义解释。"""

    def test_404_never_classifies(self):
        exc = ProviderError("HTTP 404 model not found", status=404, code="model_not_found")
        self.assertFalse(classify_rejection(exc, api_type="openai_image").convertible)

    def test_generic_default_code_is_not_evidence(self):
        # ProviderError 的类默认 code=provider_error 不能被当成上游业务码。
        # 类属性上的默认 code 不算结构化证据；此时仅凭 400 + 图像文本仍可判
        # （契约 §6 允许"白名单状态 + 窄文本"这条兜底路径）。
        exc = ProviderError("HTTP 400 unsupported input image", status=400)
        verdict = classify_rejection(exc, api_type="openai_image")
        self.assertIn(verdict.kind, {"image_unsupported", "image_invalid"})
        self.assertTrue(verdict.convertible)

    def test_block_reason_blocklist_is_text_rejected(self):
        exc = NoOutputError("blocked", block_reason="BLOCKLIST", status=200, source="body")
        verdict = classify_rejection(exc, api_type="gemini_official")
        self.assertEqual(verdict.kind, "text_rejected")
        self.assertFalse(verdict.convertible)

    def test_prompt_blocked_with_safety_finish_is_text_rejected(self):
        exc = NoOutputError(
            "blocked", code="prompt_blocked", finish_reason="SAFETY", status=200, source="body",
        )
        self.assertEqual(classify_rejection(exc, api_type="gemini_official").kind, "text_rejected")

    def test_block_reason_other_alone_is_unknown(self):
        exc = NoOutputError("blocked", block_reason="OTHER", status=200, source="body")
        self.assertEqual(classify_rejection(exc, api_type="gemini_official").kind, "unknown")

    def test_image_format_unsupported_is_invalid(self):
        exc = ProviderError("bad format", status=400, code="image_format_unsupported", source="body")
        self.assertEqual(classify_rejection(exc, api_type="openai_image").kind, "image_invalid")

    def test_safety_violation_is_safety(self):
        exc = ProviderError("blocked", status=400, code="safety_violation", source="body")
        self.assertEqual(classify_rejection(exc, api_type="openai_image").kind, "safety")

    def test_quality_param_error_is_not_image_rejection(self):
        exc = ProviderError(
            "HTTP 400 quality not supported for input image model", status=400,
        )
        self.assertEqual(classify_rejection(exc, api_type="openai_image").kind, "unknown")

    def test_unsupported_input_image_without_status_is_unknown(self):
        exc = ProviderError("unsupported input image")
        self.assertEqual(classify_rejection(exc, api_type="openai_image").kind, "unknown")


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return str(self._payload)


class BodyEvidenceTests(unittest.IsolatedAsyncioTestCase):
    """BF-05：HTTP 错误体与 HTTP 200 业务拒绝都要保留结构化字段。"""

    async def test_response_json_keeps_upstream_code(self):
        with self.assertRaises(ProviderError) as caught:
            await ProviderAdapter.response_json(
                _Response({"error": {"code": "image_not_supported", "message": "bad argument"}}, 400)
            )
        exc = caught.exception
        self.assertEqual(exc.status, 400)
        self.assertEqual(exc.code, "image_not_supported")
        self.assertEqual(exc.source, "body")

    async def test_parse_common_http200_body_error_is_structured(self):
        with self.assertRaises(NoOutputError) as caught:
            ProviderAdapter.parse_common(
                {"error": {"code": "image_not_supported", "message": "bad"}}, status=200,
            )
        exc = caught.exception
        self.assertEqual(exc.status, 200)
        self.assertEqual(exc.source, "body")
        self.assertEqual(exc.code, "image_not_supported")


class _Ctx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *_args):
        return False


class _Session:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Ctx(_Response(self.payload, self.status))


class AdapterContainerTests(unittest.IsolatedAsyncioTestCase):
    """BF-08/09：质量参数承载与 xAI 专用字段的回退顺序。"""

    async def test_gemini_forwards_explicit_quality(self):
        adapter = GeminiAdapter(_node("g", api_type="gemini_official", model="gemini-3.1-flash-image", custom_params="quality=high"))
        session = _Session({"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}}]}}]})
        await adapter.generate(session, GenerationRequest("draw"), "key")
        config = session.calls[0][1]["json"]["generationConfig"]
        self.assertEqual(config.get("quality"), "high")

    async def test_xai_caller_resolution_beats_node(self):
        adapter = XaiImageAdapter(_node("x", api_type="xai_image", custom_params="resolution=1k"))
        session = _Session({"data": [{"url": "https://example.invalid/a.png"}]})
        await adapter.generate(session, GenerationRequest("draw", extra_params={"resolution": "2k"}), "key")
        self.assertEqual(session.calls[0][1]["json"]["resolution"], "2k")

    async def test_xai_default_size_supplies_aspect(self):
        adapter = XaiImageAdapter(_node("x", api_type="xai_image", default_size="3:4"))
        session = _Session({"data": [{"url": "https://example.invalid/a.png"}]})
        await adapter.generate(session, GenerationRequest("draw"), "key")
        self.assertEqual(session.calls[0][1]["json"]["aspect_ratio"], "3:4")


def _terminal_config(**overrides):
    options = {
        "providers": (_node("node_a"),),
        "generation_timeout": 120,
        "vision_provider_id": "vision",
        "reference_fallback": ReferenceFallbackConfig(enable=True, max_retries_per_node=1),
    }
    options.update(overrides)
    return RuntimeConfig(**options)


def _task():
    task = DrawTask(
        "task-1", "umo",
        GenerationRequest("原文画面", references=(ImageInput(PNG, "image/png"),)),
    )
    task.created_at = time.monotonic()
    task.runtime["original_user_prompt"] = "原文画面"
    return task


class TerminalMatrixTests(unittest.IsolatedAsyncioTestCase):
    """BF-03/F11：终态分类与退款政策（v1.4.0 起不再有节点外救援，异常直接上抛）。"""

    def _scheduler(self, config, generated):
        async def runner(task, results):
            return True

        async def generate(task):
            raise generated

        scheduler = TaskScheduler(lambda: config, None, runner, lambda *a: None)
        scheduler._generate = generate
        return scheduler

    async def test_final_no_output_means_no_output_and_no_refund(self):
        from imago_fusion.services.quota_store import terminal_refund_amount

        task = _task()
        config = _terminal_config()
        await self._scheduler(config, NoOutputError("no image", status=200))._execute(task)
        self.assertEqual(task.state, TaskState.NO_OUTPUT)
        self.assertEqual(terminal_refund_amount(task.state, 1), 0)

    async def test_final_timeout_means_timed_out_and_no_refund(self):
        from imago_fusion.services.quota_store import terminal_refund_amount

        task = _task()
        await self._scheduler(_terminal_config(), asyncio.TimeoutError())._execute(task)
        self.assertEqual(task.state, TaskState.TIMED_OUT)
        self.assertEqual(terminal_refund_amount(task.state, 1), 0)

    async def test_final_provider_error_means_failed_and_refund(self):
        from imago_fusion.services.quota_store import terminal_refund_amount

        task = _task()
        await self._scheduler(_terminal_config(), ProviderError("upstream 500", status=500))._execute(task)
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(terminal_refund_amount(task.state, 1), 1)

    async def test_cancelled_propagates_and_refunds(self):
        from imago_fusion.services.quota_store import terminal_refund_amount

        task = _task()
        scheduler = self._scheduler(_terminal_config(), asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await scheduler._execute(task)
        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertEqual(terminal_refund_amount(task.state, 1), 1)


if __name__ == "__main__":
    unittest.main()
