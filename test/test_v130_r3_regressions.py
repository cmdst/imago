"""t20/R2-01..04 回归：真实调用入口，而不是手工计数探针。

覆盖：
- R2-01 调度器 ``invoke`` 与真实 ``NativeAdapter`` 的子请求计数（成功/失败/取消/超时）；
- R2-02 拒绝分类冻结表（结构化别名、字段语义、OTHER 规则、参数 400 保护）；
- R2-03 原生包装保留可信上游 status 与 transport 排除证据（流式/非流式）；
- R2-04 HTTP 200 业务拒绝走结构化 ``NoOutputError``，终态 NO_OUTPUT 且不退款。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import types
import unittest
from pathlib import Path

import aiohttp

from imago_fusion.core import runtime
from imago_fusion.core.errors import NoOutputError, ProviderError
from imago_fusion.core.models import (
    DrawTask,
    GenerationRequest,
    ProviderConfig,
    RuntimeConfig,
    TaskState,
)
from imago_fusion.core.references import classify_rejection
from imago_fusion.providers.dashscope import DashScopeMultimodalAdapter
from imago_fusion.providers.gemini import GeminiAdapter
from imago_fusion.providers.native import NativeAdapter
from imago_fusion.services import quota_store as quota_module
from imago_fusion.services.scheduler import TaskScheduler


class FakeResponse:
    def __init__(self, *, completion_text: str = "", chain=None) -> None:
        self.completion_text = completion_text
        self.result_chain = chain


class FakeProvider:
    def __init__(self, frames: list | None = None) -> None:
        self.frames = frames or []
        self.client = types.SimpleNamespace(max_retries=2)

    async def text_chat_stream(self, **kwargs):
        self.stream_kwargs = kwargs
        for frame in self.frames:
            yield frame


class BrokenProvider(FakeProvider):
    async def text_chat_stream(self, **kwargs):
        if False:  # pragma: no cover - 让函数保持异步生成器
            yield None
        raise self.frames[0]


class FakeProviderManager:
    def __init__(self, provider) -> None:
        self.provider = provider

    async def get_provider_by_id(self, provider_id):
        return self.provider


class FakeContext:
    def __init__(self, *, response=None, provider=None, error: BaseException | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []
        self.provider_manager = FakeProviderManager(provider)

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        await asyncio.sleep(0)
        return self.response


class CountingContext(FakeContext):
    """每次调用返回不同图片 URL，避免原生适配器的去重把 3 次调用压成 1 张。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.counter = 0

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        self.counter += 1
        return FakeResponse(completion_text=f"![x](https://example.invalid/{self.counter}.png)")


class SlowContext(FakeContext):
    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(10)
        return self.response


class StatusError(Exception):
    """模拟上游 SDK 异常：可信 HTTP status 可来自属性或 ``response``。"""

    def __init__(self, status: int | None = None, code: str = "", *, response_status: int | None = None) -> None:
        super().__init__("invalid image payload")
        if status is not None:
            self.status = status
        if code:
            self.code = code
        if response_status is not None:
            self.response = types.SimpleNamespace(status=response_status)


def _task(count: int = 1) -> DrawTask:
    task = DrawTask("task-r3", "umo", GenerationRequest("cat", count=count))
    task.created_at = time.monotonic()
    return task


def _config(**overrides) -> RuntimeConfig:
    options = {"providers": (_node(),)}
    options.update(overrides)
    return RuntimeConfig(**options)


def _node(**overrides) -> ProviderConfig:
    base = {
        "id": "native_node",
        "api_type": "astrbot_native",
        "base_url": "",
        "api_keys": (),
        "astrbot_provider_id": "provider_x",
        "timeout": 10,
    }
    base.update(overrides)
    return ProviderConfig(**base)


class _SchedulerCfg:
    proxy_url = ""


def _scheduler() -> TaskScheduler:
    return TaskScheduler(lambda: _SchedulerCfg(), None, None, lambda *a: None)


class FakeJsonResponse:
    status = 200

    def __init__(self, payload) -> None:
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def json(self, content_type=None):
        return self.payload

    async def text(self, *args, **kwargs):
        import json

        return json.dumps(self.payload)


class FakeJsonSession:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeJsonResponse(self.payload)


class FrozenClassifierTests(unittest.TestCase):
    """R2-02：冻结分类表逐值断言，未知码不泛化。"""

    def _kind(self, exc) -> str:
        return classify_rejection(exc, api_type="", model="m").kind

    def test_structured_safety_aliases(self) -> None:
        for code in (
            "content_policy_violation",
            "safety_violation",
            "moderation_blocked",
            "prohibited_content",
            "image_safety",
            "DataInspectionFailed",
        ):
            with self.subTest(code=code):
                exc = ProviderError("blocked", status=200, code=code)
                verdict = classify_rejection(exc, api_type="dashscope_multimodal", model="m")
                self.assertEqual(verdict.kind, "safety")
                self.assertFalse(verdict.convertible)

    def test_finish_and_block_safety_values(self) -> None:
        for finish in ("SAFETY", "IMAGE_SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
            with self.subTest(finish=finish):
                exc = NoOutputError("no image", status=200, source="body", finish_reason=finish)
                self.assertEqual(self._kind(exc), "safety")
        for block in ("SAFETY", "PROHIBITED_CONTENT"):
            with self.subTest(block=block):
                exc = NoOutputError("no image", status=200, source="body", block_reason=block)
                self.assertEqual(self._kind(exc), "safety")
        exc = NoOutputError("no image", status=200, source="body", block_reason="BLOCKLIST")
        self.assertEqual(self._kind(exc), "text_rejected")

    def test_prompt_blocked_text_is_text_rejected(self) -> None:
        for text in (
            "prompt was blocked by safety filter",
            "Prompt blocked: policy",
            "提示词内容不合规",
        ):
            with self.subTest(text=text):
                exc = ProviderError(text, status=400)
                verdict = classify_rejection(exc, api_type="openai_image", model="m")
                self.assertEqual(verdict.kind, "text_rejected")
                self.assertFalse(verdict.convertible)

    def test_block_reason_other_rules(self) -> None:
        alone = NoOutputError("no image", status=200, source="body", block_reason="OTHER")
        self.assertEqual(self._kind(alone), "unknown")
        safety_text = NoOutputError(
            "no image", status=200, source="body", block_reason="OTHER"
        )
        safety_text.args = ("safety filter rejected the image",)
        self.assertEqual(self._kind(safety_text), "unknown")
        prompt_text = NoOutputError(
            "prompt was blocked by policy",
            status=200,
            source="body",
            block_reason="OTHER",
        )
        self.assertEqual(self._kind(prompt_text), "text_rejected")

    def test_reliable_unknown_code_does_not_fall_back_to_text(self) -> None:
        exc = ProviderError("unsupported input image", status=400, code="weird_code")
        self.assertEqual(self._kind(exc), "unknown")

    def test_excluded_statuses_and_transport(self) -> None:
        for status in (401, 403, 404, 429, 500, 503):
            with self.subTest(status=status):
                exc = ProviderError("blocked", status=status, code="moderation_blocked")
                self.assertEqual(self._kind(exc), "unknown")
        transport = ProviderError("boom", status=None, source="transport")
        self.assertEqual(self._kind(transport), "unknown")

    def test_parameter_rejection_is_not_image_rejection(self) -> None:
        for text in (
            "quality not supported for input image model",
            "image_size unsupported for this model",
            "thinking is not supported for this model",
        ):
            with self.subTest(text=text):
                exc = ProviderError(text, status=400)
                self.assertEqual(self._kind(exc), "unknown")


class NativeStatusTests(unittest.IsolatedAsyncioTestCase):
    """R2-03：真实 NativeAdapter 包装保留 status 与 transport 证据。"""

    async def _run(self, *, error, stream: bool = False):
        if stream:
            provider = BrokenProvider([error])
            context = FakeContext(provider=provider)
        else:
            provider = FakeProvider()
            context = FakeContext(provider=provider, error=error)
        runtime.configure(context=context)
        try:
            adapter = NativeAdapter(_node(stream=stream))
            with self.assertRaises(ProviderError) as caught:
                await adapter.generate(None, GenerationRequest("cat", count=1), "")
            return caught.exception
        finally:
            runtime.reset()

    async def test_non_stream_status_and_code_preserved(self) -> None:
        exc = await self._run(error=StatusError(status=429, code="invalid_image"))
        self.assertEqual(exc.status, 429)
        self.assertEqual(exc.code, "invalid_image")
        self.assertEqual(exc.source, "native")
        self.assertEqual(classify_rejection(exc).kind, "unknown")

    async def test_stream_status_preserved(self) -> None:
        exc = await self._run(error=StatusError(status=503, code="invalid_image"), stream=True)
        self.assertEqual(exc.status, 503)
        self.assertEqual(exc.source, "native")
        self.assertEqual(classify_rejection(exc).kind, "unknown")

    async def test_sdk_response_status_is_trusted(self) -> None:
        exc = await self._run(error=StatusError(code="invalid_image", response_status=503))
        self.assertEqual(exc.status, 503)
        self.assertEqual(classify_rejection(exc).kind, "unknown")

    async def test_connection_error_keeps_transport_evidence(self) -> None:
        exc = await self._run(error=aiohttp.ClientConnectionError("reset"))
        self.assertIsNone(exc.status)
        self.assertEqual(exc.source, "transport")
        self.assertEqual(classify_rejection(exc).kind, "unknown")


class InvokeAccountingTests(unittest.IsolatedAsyncioTestCase):
    """R2-01：真实 NativeAdapter 经 scheduler.invoke 的子请求计数。"""

    def setUp(self) -> None:
        self._response = FakeResponse(completion_text="![x](https://example.invalid/a.png)")
        self._context = CountingContext(provider=FakeProvider())
        runtime.configure(context=self._context)
        self.scheduler = _scheduler()

    def tearDown(self) -> None:
        runtime.reset()

    async def test_successful_calls_are_counted_once(self) -> None:
        task = _task(count=3)
        results = await self.scheduler.invoke(
            _node(), GenerationRequest("cat", count=3), timeout=5, task=task
        )
        self.assertEqual(len(results), 3)
        self.assertEqual(task.runtime["native_subcalls"], 3)
        await self.scheduler.invoke(
            _node(), GenerationRequest("cat", count=3), timeout=5, task=task
        )
        self.assertEqual(task.runtime["native_subcalls"], 6)

    async def test_partial_failures_are_counted(self) -> None:
        self._context = FakeContext(provider=FakeProvider(), error=ProviderError("upstream refused"))
        runtime.configure(context=self._context)
        task = _task(count=2)
        with self.assertRaises(ProviderError):
            await self.scheduler.invoke(
                _node(), GenerationRequest("cat", count=2), timeout=5, task=task
            )
        self.assertEqual(task.runtime["native_subcalls"], 2)

    async def test_cancellation_is_counted(self) -> None:
        self._context = SlowContext(provider=FakeProvider())
        runtime.configure(context=self._context)
        task = _task(count=1)
        pending = asyncio.create_task(
            self.scheduler.invoke(_node(), GenerationRequest("cat", count=1), timeout=5, task=task)
        )
        await asyncio.sleep(0.05)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(task.runtime["native_subcalls"], 1)

    async def test_timeout_is_counted(self) -> None:
        self._context = SlowContext(provider=FakeProvider())
        runtime.configure(context=self._context)
        task = _task(count=1)
        with self.assertRaises(asyncio.TimeoutError):
            await self.scheduler.invoke(
                _node(), GenerationRequest("cat", count=1), timeout=0.05, task=task
            )
        self.assertEqual(task.runtime["native_subcalls"], 1)


class GenerateDoubleCountTests(unittest.IsolatedAsyncioTestCase):
    """R2-01：``_generate`` 自身的 finally 与 ``invoke`` 计数不重叠。"""

    def setUp(self) -> None:
        self._context = CountingContext(provider=FakeProvider())
        runtime.configure(context=self._context)

    def tearDown(self) -> None:
        runtime.reset()

    async def test_generate_counts_each_adapter_call_once(self) -> None:
        scheduler = TaskScheduler(
            lambda: _config(providers=(_node(),), generation_timeout=120),
            None,
            lambda task, results: _true(),
            lambda *a: None,
        )
        task = _task(count=3)
        await scheduler._execute(task)
        self.assertEqual(task.runtime["native_subcalls"], 3)

    async def test_native_internal_calls_stay_capped_at_four(self) -> None:
        task = _task(count=9)
        results = await _scheduler().invoke(
            _node(), GenerationRequest("cat", count=9), timeout=5, task=task
        )
        self.assertEqual(len(results), 4)
        self.assertEqual(task.runtime["native_subcalls"], 4)


class Http200RejectionTests(unittest.IsolatedAsyncioTestCase):
    """R2-04：200 业务拒绝结构化且按 NO_OUTPUT 结算。"""

    async def test_dashscope_200_business_rejection_is_no_output(self) -> None:
        payload = {"code": "DataInspectionFailed", "message": "input data may contain inappropriate content"}
        session = FakeJsonSession(payload)
        adapter = DashScopeMultimodalAdapter(
            ProviderConfig(
                id="ds",
                api_type="dashscope_multimodal",
                base_url="https://example.invalid/api",
                model="wanx",
                api_keys=("k",),
            )
        )
        with self.assertRaises(NoOutputError) as caught:
            await adapter.generate(session, GenerationRequest("cat"), "k")
        exc = caught.exception
        self.assertEqual(exc.status, 200)
        self.assertEqual(exc.code, "DataInspectionFailed")
        self.assertEqual(exc.source, "body")
        self.assertEqual(classify_rejection(exc).kind, "safety")

    async def test_gemini_finish_only_rejection_keeps_status(self) -> None:
        payload = {"candidates": [{"finishReason": "IMAGE_SAFETY", "content": {"parts": []}}]}
        session = FakeJsonSession(payload)
        adapter = GeminiAdapter(
            ProviderConfig(
                id="gm",
                api_type="gemini_official",
                base_url="https://example.invalid/v1beta",
                model="gemini-3.1-flash-image",
                api_keys=("k",),
            )
        )
        with self.assertRaises(NoOutputError) as caught:
            await adapter.generate(session, GenerationRequest("cat"), "k")
        exc = caught.exception
        self.assertEqual(exc.status, 200)
        self.assertEqual(exc.source, "body")
        self.assertEqual(exc.finish_reason, "IMAGE_SAFETY")
        self.assertEqual(classify_rejection(exc).kind, "safety")

    async def test_dashscope_real_execution_settles_no_output_without_refund(self) -> None:
        payload = {"code": "DataInspectionFailed", "message": "unsafe"}
        node = ProviderConfig(
            id="ds",
            api_type="dashscope_multimodal",
            base_url="https://example.invalid/api",
            model="wanx",
            api_keys=("k",),
            timeout=30,
        )
        scheduler = TaskScheduler(lambda: _config(providers=(node,)), FakeJsonSession(payload), lambda task, results: _true(), lambda *a: None)
        task = _task()
        await scheduler._execute(task)
        self.assertEqual(task.state, TaskState.NO_OUTPUT)
        self.assertEqual(quota_module.terminal_refund_amount(task.state, 1), 0)


async def _true() -> bool:
    return True


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
