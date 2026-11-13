"""v1.3.0 修复轮（BF-01..12）的执行级回归。

覆盖 t5 独立评审的 12 条 findings：降级调用额度/预算/终态、拒绝分类器证据规则、
适配器参数容器、遮挡产物校验、通知开关、原始 prompt 重建、提交入口可用性。
"""

import asyncio
import base64
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from imago_fusion.core.errors import ConfigurationError, NoOutputError, ProviderError
from imago_fusion.core.models import (
    DrawTask,
    ExperimentalReferenceConfig,
    GenerationRequest,
    ImageInput,
    ImageResult,
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


def _gate_config(**overrides):
    options = {
        "providers": (_node("node_a"), _node("occ")),
        "generation_timeout": 120,
        "vision_provider_id": "vision",
        "reference_fallback": ReferenceFallbackConfig(enable=True, max_extra_attempts=1),
        "experimental_reference": ExperimentalReferenceConfig(enable=False),
    }
    options.update(overrides)
    return RuntimeConfig(**options)


def _locked(task, prompt="原文画面", provider_id="node_a", count=1):
    release = GenerationRequest(prompt, count=count, references=(ImageInput(PNG, "image/png"),))
    task.runtime["fallback_request"] = {
        "request": release,
        "provider_id": provider_id,
        "model": "model",
        "api_type": "custom_endpoint",
        "thinking": "",
        "user_prompt": prompt,
        "prompt_has_auto_caption": False,
    }
    task.runtime["reference_rejection"] = {"kind": "image_invalid", "status": 400}
    return task.runtime["fallback_request"]


def _task():
    task = DrawTask("task-1", "umo", GenerationRequest("原文画面", references=(ImageInput(PNG, "image/png"),)))
    task.created_at = time.monotonic()
    task.runtime["original_user_prompt"] = "原文画面"
    return task


class SchedulerGateTests(unittest.IsolatedAsyncioTestCase):
    """BF-01/10/12：额度上限、通知开关与提交入口。"""

    def _scheduler(self, config, rescued):
        async def rescue(*args):
            rescued.append(args)

        return TaskScheduler(lambda: config, None, None, lambda *a: None, rescue=rescue)

    async def test_zero_extra_attempts_never_calls_rescue(self):
        rescued = []
        config = _gate_config(reference_fallback=ReferenceFallbackConfig(enable=True, max_extra_attempts=0))
        scheduler = self._scheduler(config, rescued)
        task = _task()
        _locked(task)
        await scheduler._maybe_reference_fallback(task, config, 100.0)
        self.assertEqual(rescued, [])

    async def test_capacity_safety_notice_hidden_when_notify_off(self):
        rescued = []
        exp = ExperimentalReferenceConfig(enable=True, occluder_provider_id="occ", max_images=1)
        fb = ReferenceFallbackConfig(enable=True, max_extra_attempts=1, notify_user=False)
        config = _gate_config(experimental_reference=exp, reference_fallback=fb)
        scheduler = self._scheduler(config, rescued)
        task = _task()
        task.request.references = (ImageInput(PNG, "image/png"), ImageInput(PNG, "image/png"))
        locked = _locked(task)
        locked["request"] = GenerationRequest("原文画面", references=tuple(task.request.references))
        task.runtime["reference_rejection"] = {"kind": "safety", "status": 200}
        await scheduler._maybe_reference_fallback(task, config, 100.0)
        self.assertEqual(rescued, [])
        self.assertEqual(task.runtime["reference_fallback"]["reason"], "capacity_safety")
        self.assertEqual(task.runtime["reference_fallback"]["notice"], "")

    async def test_native_subcalls_accumulate(self):
        task = _task()
        adapter = SimpleNamespace(subcalls=3)
        TaskScheduler._record_adapter_subcalls(task, adapter)
        TaskScheduler._record_adapter_subcalls(task, adapter)
        self.assertEqual(task.runtime["native_subcalls"], 6)

    async def test_submit_rejects_when_all_nodes_disabled(self):
        from imago_fusion.main import ImagoFusion

        plugin = object.__new__(ImagoFusion)
        plugin.raw_config = {
            "providers": [
                {"id": "off", "api_type": "custom_endpoint", "base_url": "https://example.invalid/v1", "api_keys": ["k"], "enabled": False},
            ],
        }
        with self.assertRaises(ConfigurationError):
            await plugin._submit(SimpleNamespace(), "draw")


class _FakeContext:
    def __init__(self, text="参考图里有一个白色方块", on_call=None):
        self.text = text
        self.on_call = on_call
        self.calls = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_call is not None:
            self.on_call()
        return SimpleNamespace(completion_text=self.text)


class _FakeScheduler:
    def __init__(self, *, fail_ids=(), results=None, results_by_id=None):
        self.invocations = []
        self.fail_ids = set(fail_ids)
        self.results = results or [ImageResult(data=PNG, mime_type="image/png")]
        self.results_by_id = results_by_id or {}

    async def invoke(self, provider, request, *, timeout=None, task=None):
        self.invocations.append((provider, request, timeout))
        if provider.id in self.fail_ids:
            raise ProviderError("boom", status=500)
        return list(self.results_by_id.get(provider.id, self.results))


class _Store:
    def __init__(self, base):
        self.base = base

    def task_dir(self, task_id):
        return self.base / task_id


class FallbackExecutionTests(unittest.IsolatedAsyncioTestCase):
    """BF-01/02/03/06/10/11：降级执行体的额度、预算、终态与产品校验。"""

    def setUp(self):
        from imago_fusion.main import ImagoFusion

        self._tmp = tempfile.TemporaryDirectory()
        self.plugin = object.__new__(ImagoFusion)
        self.plugin.context = _FakeContext()
        self.plugin.scheduler = _FakeScheduler()
        self.plugin.session = None
        self.plugin.store = _Store(Path(self._tmp.name))
        self.plugin.raw_config = {}

    def tearDown(self):
        self._tmp.cleanup()

    def _config(self, *, exp=None, fb=None):
        return _gate_config(
            providers=(_node("node_a"), _node("occ")),
            reference_fallback=fb or ReferenceFallbackConfig(enable=True),
            experimental_reference=exp or ExperimentalReferenceConfig(enable=False),
        )

    async def test_final_failure_propagates_and_is_counted_once(self):
        exp = ExperimentalReferenceConfig(enable=True, occluder_provider_id="occ", max_images=1)
        self.plugin.scheduler = _FakeScheduler(fail_ids={"node_a"})
        task = _task()
        locked = _locked(task)
        with self.assertRaises(ProviderError):
            await self.plugin._reference_fallback(task, self._config(exp=exp), 100.0, {"kind": "safety", "status": 200}, locked)
        ids = [call[0].id for call in self.plugin.scheduler.invocations]
        self.assertEqual(ids, ["occ", "node_a"])
        info = task.runtime["reference_fallback"]
        self.assertEqual(info["extra_attempts"], 1)
        self.assertEqual(info["mode"], "occlusion")

    async def test_cancelled_during_vision_sends_nothing(self):
        task = _task()

        def cancel():
            task.state = TaskState.CANCELLED

        self.plugin.context = _FakeContext(on_call=cancel)
        locked = _locked(task)
        await self.plugin._reference_fallback(task, self._config(), 100.0, {"kind": "image_invalid", "status": 400}, locked)
        self.assertEqual(self.plugin.scheduler.invocations, [])
        self.assertEqual(task.runtime["reference_fallback"]["reason"], "cancelled")

    async def test_budget_left_after_vision_sends_nothing(self):
        task = _task()
        started = time.monotonic()
        task.created_at = started - 79  # 120 - 79 = 41s：够 vision 预算

        def burn():
            task.created_at = time.monotonic() - 110  # 之后只剩 10s < 15s 余量

        self.plugin.context = _FakeContext(on_call=burn)
        locked = _locked(task)
        await self.plugin._reference_fallback(task, self._config(), 41.0, {"kind": "image_invalid", "status": 400}, locked)
        self.assertEqual(self.plugin.scheduler.invocations, [])
        self.assertEqual(task.runtime["reference_fallback"]["reason"], "budget")

    async def test_missing_original_prompt_is_unrebuildable(self):
        task = _task()
        locked = _locked(task, prompt="")
        await self.plugin._reference_fallback(task, self._config(), 100.0, {"kind": "image_invalid", "status": 400}, locked)
        self.assertEqual(self.plugin.scheduler.invocations, [])
        self.assertEqual(task.runtime["reference_fallback"]["reason"], "prompt_unrebuildable")

    async def test_notify_off_hides_text_notice(self):
        fb = ReferenceFallbackConfig(enable=True, max_extra_attempts=1, notify_user=False)
        task = _task()
        await self.plugin._reference_fallback(task, self._config(fb=fb), 100.0, {"kind": "image_invalid", "status": 400}, _locked(task))
        self.assertTrue(self.plugin.scheduler.invocations)
        self.assertEqual(task.runtime["reference_fallback"]["notice"], "")

    async def test_occlusion_rejects_non_image_result(self):
        exp = ExperimentalReferenceConfig(enable=True, occluder_provider_id="occ", max_images=1)
        self.plugin.scheduler = _FakeScheduler(results=[ImageResult(data=b"NOT_AN_IMAGE_TEST", mime_type="text/plain")])
        task = _task()
        await self.plugin._reference_fallback(task, self._config(exp=exp), 100.0, {"kind": "safety", "status": 200}, _locked(task))
        self.assertEqual([call[0].id for call in self.plugin.scheduler.invocations], ["occ"])
        self.assertEqual(task.runtime["reference_fallback"]["reason"], "occlusion_failed")

    async def test_occlusion_edits_each_image_once_with_suffix(self):
        exp = ExperimentalReferenceConfig(enable=True, occluder_provider_id="occ", occlusion_prompt="挡住敏感处", max_images=2)
        task = _task()
        task.request.references = (ImageInput(PNG, "image/png"), ImageInput(PNG, "image/png"))
        locked = _locked(task)
        locked["request"] = GenerationRequest("原文画面", references=tuple(task.request.references))
        await self.plugin._reference_fallback(task, self._config(exp=exp), 100.0, {"kind": "safety", "status": 200}, locked)
        editors = [call for call in self.plugin.scheduler.invocations if call[0].id == "occ"]
        self.assertEqual(len(editors), 2)
        for _provider, request, _timeout in editors:
            self.assertEqual(len(request.references), 1)
            self.assertTrue(request.prompt.endswith("只输出图片，不要文字说明。"))
        target = [call for call in self.plugin.scheduler.invocations if call[0].id == "node_a"][-1]
        names = [image.filename for image in target[1].references]
        self.assertEqual(names, ["proc-1.png", "proc-2.png"])

    async def test_multi_results_pass_through_without_proc_leak(self):
        exp = ExperimentalReferenceConfig(enable=True, occluder_provider_id="occ", max_images=1)
        task = _task()
        locked = _locked(task)
        self.plugin.scheduler = _FakeScheduler(
            results_by_id={"node_a": [ImageResult(data=PNG, mime_type="image/png"), ImageResult(data=PNG, mime_type="image/png")]},
        )
        results = await self.plugin._reference_fallback(task, self._config(exp=exp), 100.0, {"kind": "safety", "status": 200}, locked)
        self.assertEqual(len(results), 2)
        inputs = list((Path(self._tmp.name) / "task-1" / "inputs").glob("proc-*"))
        self.assertEqual(len(inputs), 1)
        self.assertFalse((Path(self._tmp.name) / "task-1" / "outputs").exists())


class TerminalMatrixTests(unittest.IsolatedAsyncioTestCase):
    """BF-03/F11：最终降级异常必须走既有终态分类与退款政策。"""

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
        config = _gate_config()
        await self._scheduler(config, NoOutputError("no image", status=200))._execute(task)
        self.assertEqual(task.state, TaskState.NO_OUTPUT)
        self.assertEqual(terminal_refund_amount(task.state, 1), 0)

    async def test_final_timeout_means_timed_out_and_no_refund(self):
        from imago_fusion.services.quota_store import terminal_refund_amount

        task = _task()
        await self._scheduler(_gate_config(), asyncio.TimeoutError())._execute(task)
        self.assertEqual(task.state, TaskState.TIMED_OUT)
        self.assertEqual(terminal_refund_amount(task.state, 1), 0)

    async def test_final_provider_error_means_failed_and_refund(self):
        from imago_fusion.services.quota_store import terminal_refund_amount

        task = _task()
        await self._scheduler(_gate_config(), ProviderError("upstream 500", status=500))._execute(task)
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(terminal_refund_amount(task.state, 1), 1)

    async def test_cancelled_propagates_and_refunds(self):
        from imago_fusion.services.quota_store import terminal_refund_amount

        task = _task()
        scheduler = self._scheduler(_gate_config(), asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await scheduler._execute(task)
        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertEqual(terminal_refund_amount(task.state, 1), 1)


class FallbackExtraTests(unittest.IsolatedAsyncioTestCase):
    """BF-01/06：技术拒绝下遮挡失败转文字、实验摘要容量与失败告知。"""

    def setUp(self):
        from imago_fusion.main import ImagoFusion

        self._tmp = tempfile.TemporaryDirectory()
        self.plugin = object.__new__(ImagoFusion)
        self.plugin.context = _FakeContext()
        self.plugin.scheduler = _FakeScheduler()
        self.plugin.session = None
        self.plugin.store = _Store(Path(self._tmp.name))
        self.plugin.raw_config = {}

    def tearDown(self):
        self._tmp.cleanup()

    def _config(self, *, exp=None, fb=None):
        return _gate_config(
            providers=(_node("node_a"), _node("occ")),
            reference_fallback=fb or ReferenceFallbackConfig(enable=True),
            experimental_reference=exp or ExperimentalReferenceConfig(enable=False),
        )

    async def test_technical_occlusion_failure_still_sends_one_text_request(self):
        exp = ExperimentalReferenceConfig(enable=True, occluder_provider_id="occ", max_images=1)
        self.plugin.scheduler = _FakeScheduler(results=[ImageResult(data=b"NOT_AN_IMAGE", mime_type="text/plain")])
        task = _task()
        locked = _locked(task)
        results = await self.plugin._reference_fallback(task, self._config(exp=exp), 100.0, {"kind": "image_invalid", "status": 400}, locked)
        self.assertTrue(results)
        ids = [call[0].id for call in self.plugin.scheduler.invocations]
        self.assertEqual(ids, ["occ", "node_a"])
        final = self.plugin.scheduler.invocations[-1][1]
        self.assertEqual(final.references, ())
        self.assertEqual(task.runtime["reference_fallback"]["mode"], "text")

    async def test_experimental_caption_respects_its_own_cap(self):
        exp = ExperimentalReferenceConfig(enable=True, occluder_provider_id="occ", max_images=1, caption_max_chars=80)
        self.plugin.context = _FakeContext(text="长" * 300)
        task = _task()
        locked = _locked(task)
        await self.plugin._reference_fallback(task, self._config(exp=exp), 100.0, {"kind": "safety", "status": 200}, locked)
        prompt = self.plugin.scheduler.invocations[-1][1].prompt
        self.assertIn("长" * 80, prompt)
        self.assertNotIn("长" * 81, prompt)

    async def test_failure_notice_present_when_notify_on(self):
        exp = ExperimentalReferenceConfig(enable=True, occluder_provider_id="occ", max_images=1)
        self.plugin.scheduler = _FakeScheduler(fail_ids={"node_a"})
        task = _task()
        locked = _locked(task)
        with self.assertRaises(ProviderError):
            await self.plugin._reference_fallback(task, self._config(exp=exp), 100.0, {"kind": "safety", "status": 200}, locked)
        self.assertTrue(task.runtime["reference_fallback"]["notice"])
