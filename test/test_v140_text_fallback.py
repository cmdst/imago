"""v1.4.0：安全/内容合规拒绝后的**逐节点**参考图文字降级（真实 ``_generate`` 入口）。

这些用例直接驱动 ``TaskScheduler._generate``：假适配器记录每一次真实出站请求
（节点、模型、是否带参考图），假 captioner 记录每一次识图调用。断言的落点是
**实际出站形状与调用顺序**，不是内部实现细节——所以文字请求"没有图片"这件事
是在适配器收到的 ``request.references`` 上验的，而不是读某个标志位。

与 v1.3.0 的口径差（本轮反转）：
- 可触发原因从 {image_unsupported, image_invalid, safety} 收窄为**仅 safety**；
- 作用域从"任务级末尾救援"改为"节点内"，文字阶段失败就结束该节点；
- 额度是**每节点纯文字调用总数**（N，0..3），不是"额外次数"，也不按模型刷新。
"""

import asyncio
import unittest
from unittest import mock

from imago_fusion.core.errors import NoOutputError, ProviderError
from imago_fusion.core.models import (
    DrawTask,
    GenerationRequest,
    ImageInput,
    ImageResult,
    ProviderConfig,
    ReferenceFallbackConfig,
    RuntimeConfig,
    TaskState,
)
from imago_fusion.core.prompting import REFERENCE_RELATION_SUFFIX
from imago_fusion.services import scheduler as scheduler_module
from imago_fusion.services.scheduler import (
    FALLBACK_NOTICE_TEXT,
    FALLBACK_TEXT_HEADER,
    TaskScheduler,
)

IMAGE = b"\x89PNG\r\n\x1a\n-fake-bytes"


class DummySession:
    pass


def _provider(provider_id="node_a", *, enabled=True, models=(), limit=0):
    """节点：``models`` 的第一项当 ``model``，其余当 ``available_models``。

    ``_generate`` 的模型列表是 ``(provider.model, *provider.available_models)``
    去重后的结果，所以两种写法等价——这里用元组只是为了让"多模型"用例读起来直白。
    """
    chain = tuple(models) or ("model",)
    return ProviderConfig(
        provider_id,
        "custom_endpoint",
        "https://example.invalid/v1",
        ("key",),
        model=chain[0],
        available_models=chain[1:],
        reference_image_limit=limit,
        enabled=enabled,
    )


def _runtime(**kwargs):
    options = {
        "generation_timeout": 120,
        "vision_provider_id": "vision",
        "reference_fallback": ReferenceFallbackConfig(enable=True, max_retries_per_node=1),
    }
    options.update(kwargs)
    return RuntimeConfig(**options)


class _Adapter:
    """记录 ``(node, model, 是否带图)`` 的假适配器；行为由类属性注入。"""

    calls: list[tuple[str, str, int]] = []
    requests: list[GenerationRequest] = []
    behaviour = None

    def __init__(self, config):
        self.config = config

    async def generate(self, session, request, key):
        type(self).calls.append((self.config.id, self.config.model, len(request.references)))
        type(self).requests.append(request)
        handler = type(self).behaviour
        if handler is not None:
            return handler(self.config, request, key, len(type(self).calls) - 1)
        return [ImageResult(data=b"image")]


class _Captioner:
    """假识图：记录调用并按配置返回文本（或抛异常）。"""

    def __init__(self, text="一名短发女性站在窗边，白色上衣，侧光。", *, error=None,
                 on_call=None):
        self.text = text
        self.error = error
        self.on_call = on_call
        self.calls = []

    async def __call__(self, task, config, prompt_text, images, *, timeout, max_chars):
        self.calls.append(
            {
                "task": task,
                "prompt": prompt_text,
                "images": list(images),
                "timeout": timeout,
                "max_chars": max_chars,
            }
        )
        if self.on_call is not None:
            self.on_call()
        if self.error is not None:
            raise self.error
        return self.text


class TextFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old = scheduler_module.ADAPTERS.get("custom_endpoint")
        scheduler_module.ADAPTERS["custom_endpoint"] = _Adapter
        _Adapter.calls = []
        _Adapter.requests = []
        _Adapter.behaviour = None
        self.captioner = _Captioner()
        self.config_calls = 0

    def tearDown(self):
        if self._old is None:
            scheduler_module.ADAPTERS.pop("custom_endpoint", None)
        else:
            scheduler_module.ADAPTERS["custom_endpoint"] = self._old

    # ---------------------------------------------------------------- 工具
    def _scheduler(self, runtime, captioner=None):
        def getter():
            self.config_calls += 1
            return runtime

        return TaskScheduler(
            getter, DummySession(), lambda *_: True, lambda *_: None,
            captioner=self.captioner if captioner is None else captioner,
        )

    def _task(self, prompt="原文画面", references=None):
        refs = references if references is not None else [ImageInput(IMAGE, "image/png")]
        task = DrawTask("task-0001", "umo", GenerationRequest(prompt, references=refs))
        task.runtime["original_user_prompt"] = prompt
        return task

    @staticmethod
    def _safety():
        return ProviderError("blocked", status=400, block_reason="SAFETY")

    # ---------------------------------------------------------------- T-S1
    async def test_safety_rejected_node_retries_once_without_images(self):
        def behaviour(config, request, key, index):
            if index == 0:
                raise self._safety()
            return [ImageResult(data=b"image")]

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            results = await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual([call[2] for call in _Adapter.calls], [1, 0])
        self.assertEqual([call[1] for call in _Adapter.calls], ["model", "model"])
        self.assertTrue(results)
        # 识图只一次，且拿到的是被拒那次尝试的实际图集。
        self.assertEqual(len(self.captioner.calls), 1)
        self.assertEqual(len(self.captioner.calls[0]["images"]), 1)
        info = task.runtime["reference_fallback"]
        self.assertTrue(info["attempted"])
        self.assertEqual(info["mode"], "text")
        self.assertEqual(info["vision_calls"], 1)
        self.assertEqual(info["text_attempts"], 1)
        self.assertEqual(info["extra_attempts"], 1)
        self.assertEqual(info["reason"], "succeeded")

    # ---------------------------------------------------------------- T-S2
    async def test_text_failure_ends_node_and_next_node_starts_with_images(self):
        def behaviour(config, request, key, index):
            if config.id == "node_a":
                raise self._safety()
            return [ImageResult(data=b"node-b")]

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider("node_a"), _provider("node_b")))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            results = await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual(
            [(call[0], call[2]) for call in _Adapter.calls],
            [("node_a", 1), ("node_a", 0), ("node_b", 1)],
        )
        self.assertEqual(results[0].data, b"node-b")
        # 每个节点各自识图一次：A 一次，B 没有安全拒绝所以不再识图。
        self.assertEqual(len(self.captioner.calls), 1)
        info = task.runtime["reference_fallback"]
        # N=1：纯文字那一次**仍然**是安全拒绝，次数用尽 ⇒ exhausted（不是 text_failed），
        # 本节点照样结束，B 重新带图（t4/F1 回归口径）。
        self.assertEqual([node["outcome"] for node in info["nodes"]], ["exhausted"])

    # ---------------------------------------------------------------- T-S3
    async def test_quota_three_gives_exactly_three_text_calls(self):
        def behaviour(config, request, key, index):
            # 每一次都被判安全拒绝：额度决定"最多撞几次"，N=3 → 恰 3 次纯文字。
            raise self._safety()

        _Adapter.behaviour = behaviour
        runtime = _runtime(
            providers=(_provider(),),
            reference_fallback=ReferenceFallbackConfig(enable=True, max_retries_per_node=3),
        )
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual([call[2] for call in _Adapter.calls], [1, 0, 0, 0])
        info = task.runtime["reference_fallback"]
        self.assertEqual(info["text_attempts"], 3)
        self.assertEqual(info["extra_attempts"], 3)
        self.assertEqual(len(self.captioner.calls), 1)
        # t4/F1 回归：3 次全为可靠安全拒绝 ⇒ 额度用尽，节点终止原因是 exhausted，
        # 不是 text_failed（后者只留给非安全失败，见 T-S26）。
        self.assertEqual([node["outcome"] for node in info["nodes"]], ["exhausted"])
        self.assertEqual(info["nodes"][0]["reason"], "exhausted")

    # ---------------------------------------------------------------- T-S4
    async def test_quota_one_gives_exactly_one_text_call(self):
        def behaviour(config, request, key, index):
            raise self._safety()

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual([call[2] for call in _Adapter.calls], [1, 0])
        self.assertEqual(task.runtime["reference_fallback"]["text_attempts"], 1)

    # ---------------------------------------------------------------- T-S5
    async def test_quota_zero_keeps_image_only_routing(self):
        def behaviour(config, request, key, index):
            raise self._safety()

        _Adapter.behaviour = behaviour
        runtime = _runtime(
            providers=(_provider(models=("m1", "m2")),),
            reference_fallback=ReferenceFallbackConfig(enable=True, max_retries_per_node=0),
        )
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        # 两个模型都只带图试过；零文字、零识图。
        self.assertEqual([(call[1], call[2]) for call in _Adapter.calls], [("m1", 1), ("m2", 1)])
        self.assertEqual(self.captioner.calls, [])
        self.assertNotIn("reference_fallback", task.runtime)

    # ---------------------------------------------------------------- T-S6
    async def test_disabled_feature_has_no_runtime_key_and_no_extra_calls(self):
        _Adapter.behaviour = lambda config, request, key, index: (_ for _ in ()).throw(self._safety())
        runtime = _runtime(
            providers=(_provider(),),
            reference_fallback=ReferenceFallbackConfig(enable=False, max_retries_per_node=3),
        )
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual([call[2] for call in _Adapter.calls], [1])
        self.assertEqual(self.captioner.calls, [])
        self.assertNotIn("reference_fallback", task.runtime)

    # ---------------------------------------------------------------- T-S7
    async def test_quota_is_per_node_and_not_refreshed_per_model(self):
        def behaviour(config, request, key, index):
            if config.id == "node_a":
                raise self._safety()
            return [ImageResult(data=b"node-b")]

        _Adapter.behaviour = behaviour
        runtime = _runtime(
            providers=(_provider("node_a", models=("m1", "m2")), _provider("node_b")),
            reference_fallback=ReferenceFallbackConfig(enable=True, max_retries_per_node=2),
        )
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            results = await scheduler._generate(task)
        finally:
            await scheduler.close()
        # A 的 m1 安全拒绝 → 2 次纯文字（额度耗尽）→ 直接进 B，既不回 m2、也不再识图。
        self.assertEqual(
            [(call[0], call[1], call[2]) for call in _Adapter.calls],
            [("node_a", "m1", 1), ("node_a", "m1", 0), ("node_a", "m1", 0), ("node_b", "model", 1)],
        )
        self.assertTrue(results)
        self.assertEqual(len(self.captioner.calls), 1)

    # ---------------------------------------------------------------- T-S9
    async def test_text_request_keeps_rejected_attempt_snapshot(self):
        def behaviour(config, request, key, index):
            raise self._safety()

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        task.request = GenerationRequest(
            "原文画面", count=2, size="1024x1024", aspect_ratio="1:1",
            references=[ImageInput(IMAGE, "image/png")], extra_params={"quality": "high"},
        )
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        rejected, text = _Adapter.requests[0], _Adapter.requests[1]
        self.assertEqual(text.count, rejected.count)
        self.assertEqual(text.size, rejected.size)
        self.assertEqual(text.aspect_ratio, rejected.aspect_ratio)
        self.assertEqual(text.extra_params, rejected.extra_params)
        self.assertEqual(text.references, ())

    # ---------------------------------------------------------------- T-S10
    async def test_config_is_read_once_per_generate(self):
        def behaviour(config, request, key, index):
            raise self._safety() if index == 0 else ProviderError("boom", status=400)

        _Adapter.behaviour = behaviour
        runtime = _runtime(
            providers=(_provider(),),
            reference_fallback=ReferenceFallbackConfig(enable=True, max_retries_per_node=2),
        )
        scheduler = self._scheduler(runtime)
        task = self._task()
        self.config_calls = 0
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual(self.config_calls, 1)

    # ---------------------------------------------------------------- T-S11
    async def test_budget_gate_skips_before_vision(self):
        _Adapter.behaviour = lambda config, request, key, index: (_ for _ in ()).throw(self._safety())
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task, remaining=54)
        finally:
            await scheduler.close()
        self.assertEqual(self.captioner.calls, [])
        self.assertEqual([call[2] for call in _Adapter.calls], [1])
        self.assertEqual(task.runtime["reference_fallback"]["reason"], "budget")

    # ---------------------------------------------------------------- T-S12
    async def test_cancelled_task_stops_the_whole_generation(self):
        _Adapter.behaviour = lambda config, request, key, index: (_ for _ in ()).throw(self._safety())
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        task.state = TaskState.CANCELLED
        try:
            with self.assertRaises(asyncio.CancelledError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        # 只有那一次带图尝试；取消后不再识图、不再发文字请求。
        self.assertEqual([call[2] for call in _Adapter.calls], [1])
        self.assertEqual(self.captioner.calls, [])
        # 取消以异常向外传播，**不写** runtime 降级记录（§8.2 E3）。
        self.assertNotIn("reference_fallback", task.runtime)

    # ---------------------------------------------------------------- T-S13
    async def test_text_prompt_is_byte_identical_across_retries(self):
        def behaviour(config, request, key, index):
            raise self._safety()

        _Adapter.behaviour = behaviour
        runtime = _runtime(
            providers=(_provider(),),
            reference_fallback=ReferenceFallbackConfig(enable=True, max_retries_per_node=2),
        )
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual(_Adapter.requests[1].prompt, _Adapter.requests[2].prompt)
        self.assertEqual(len(self.captioner.calls), 1)

    # ---------------------------------------------------------------- T-S14
    async def test_notice_respects_notify_user(self):
        def behaviour(config, request, key, index):
            if index == 0:
                raise self._safety()
            return [ImageResult(data=b"image")]

        _Adapter.behaviour = behaviour
        for notify, expected in ((True, FALLBACK_NOTICE_TEXT), (False, "")):
            _Adapter.calls = []
            _Adapter.requests = []
            self.captioner = _Captioner()
            runtime = _runtime(
                providers=(_provider(),),
                reference_fallback=ReferenceFallbackConfig(
                    enable=True, max_retries_per_node=1, notify_user=notify,
                ),
            )
            scheduler = self._scheduler(runtime)
            task = self._task()
            try:
                await scheduler._generate(task)
            finally:
                await scheduler.close()
            self.assertEqual(task.runtime["reference_fallback"]["notice"], expected)

    # ---------------------------------------------------------------- T-S15
    async def test_all_nodes_failing_raises_last_error(self):
        def behaviour(config, request, key, index):
            if index == 0:
                raise self._safety()
            raise ProviderError("text stage boom", status=400)

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider("node_a"), _provider("node_b")))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaisesRegex(ProviderError, "text stage boom"):
                await scheduler._generate(task)
        finally:
            await scheduler.close()

    # ---------------------------------------------------------------- T-S16
    async def test_successful_text_retry_is_not_polluted(self):
        def behaviour(config, request, key, index):
            if index == 0:
                raise self._safety()
            return [ImageResult(data=b"text-image")]

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            results = await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual([item.data for item in results], [b"text-image"])
        self.assertEqual(task.errors, [])
        self.assertEqual(task.runtime["provider_output_count"] if "provider_output_count" in task.runtime else None, None)

    # ---------------------------------------------------------------- T-S17
    async def test_runtime_evidence_shape(self):
        def behaviour(config, request, key, index):
            if index == 0:
                raise self._safety()
            return [ImageResult(data=b"image")]

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            await scheduler._generate(task)
        finally:
            await scheduler.close()
        info = task.runtime["reference_fallback"]
        self.assertEqual(
            set(info),
            {"attempted", "mode", "vision_calls", "text_attempts", "extra_attempts", "reason", "nodes", "notice"},
        )
        self.assertEqual(info["extra_attempts"], info["text_attempts"])
        node = info["nodes"][0]
        self.assertEqual(node["kind"], "safety")
        self.assertEqual(node["node"], "node_a")
        self.assertEqual(node["model"], "model")
        self.assertEqual(node["outcome"], "succeeded")
        self.assertEqual(node["caption_chars"], len(self.captioner.text))
        self.assertEqual(node["vision_calls"], 1)

    # ---------------------------------------------------------------- T-S18
    async def test_no_output_error_keeps_priority(self):
        def behaviour(config, request, key, index):
            if index == 0:
                raise NoOutputError("empty", status=200, block_reason="SAFETY")
            raise NoOutputError("text stage empty", status=200)

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(NoOutputError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual(task.runtime["reference_fallback"]["reason"], "text_failed")

    # ---------------------------------------------------------------- T-S19
    async def test_too_many_references_skips_with_capacity(self):
        _Adapter.behaviour = lambda config, request, key, index: (_ for _ in ()).throw(self._safety())
        refs = [ImageInput(IMAGE, "image/png") for _ in range(5)]
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task(references=refs)
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual(self.captioner.calls, [])
        self.assertEqual([call[2] for call in _Adapter.calls], [5])
        info = task.runtime["reference_fallback"]
        self.assertEqual(info["reason"], "capacity")
        self.assertEqual(info["nodes"][0]["outcome"], "skipped")

    # ---------------------------------------------------------------- T-S20
    async def test_text_prompt_drops_relation_suffix_and_position_words(self):
        injected = f"原文画面\n\n{REFERENCE_RELATION_SUFFIX}"

        def behaviour(config, request, key, index):
            if index == 0:
                raise self._safety()
            return [ImageResult(data=b"image")]

        _Adapter.behaviour = behaviour
        runtime = _runtime(
            providers=(_provider(),),
            reference_fallback=ReferenceFallbackConfig(enable=True, max_retries_per_node=1),
        )
        scheduler = self._scheduler(runtime)
        task = self._task()
        task.request = GenerationRequest(injected, references=[ImageInput(IMAGE, "image/png")])
        try:
            await scheduler._generate(task)
        finally:
            await scheduler.close()
        # 带图那次确实被注入了关系声明（延续 v1.3.0 的行为）。
        self.assertIn(REFERENCE_RELATION_SUFFIX, _Adapter.requests[0].prompt)
        text_prompt = _Adapter.requests[1].prompt
        self.assertTrue(text_prompt.startswith("原文画面"))
        self.assertIn(FALLBACK_TEXT_HEADER.strip(), text_prompt)
        self.assertNotIn(REFERENCE_RELATION_SUFFIX, text_prompt)
        self.assertNotIn("第 1 张", text_prompt)
        self.assertNotIn("Reference image(s) attached", text_prompt)
        self.assertEqual(_Adapter.requests[1].references, ())

    # ---------------------------------------------------------------- T-S21
    async def test_vision_failure_blocks_text_generation(self):
        _Adapter.behaviour = lambda config, request, key, index: (_ for _ in ()).throw(self._safety())
        self.captioner = _Captioner(error=RuntimeError("vision down"))
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual([call[2] for call in _Adapter.calls], [1])
        info = task.runtime["reference_fallback"]
        self.assertEqual(info["reason"], "vision_failed")
        self.assertEqual(info["text_attempts"], 0)
        self.assertEqual(info["vision_calls"], 1)

    # ---------------------------------------------------------------- T-S22
    async def test_empty_caption_blocks_text_generation(self):
        _Adapter.behaviour = lambda config, request, key, index: (_ for _ in ()).throw(self._safety())
        self.captioner = _Captioner(text="")
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual([call[2] for call in _Adapter.calls], [1])
        self.assertEqual(task.runtime["reference_fallback"]["reason"], "vision_failed")

    # ---------------------------------------------------------------- T-S23
    async def test_prompt_unrebuildable_skips_before_vision(self):
        _Adapter.behaviour = lambda config, request, key, index: (_ for _ in ()).throw(self._safety())
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        task.runtime.pop("original_user_prompt", None)
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual(self.captioner.calls, [])
        self.assertEqual(task.runtime["reference_fallback"]["reason"], "prompt_unrebuildable")

    # ---------------------------------------------------------------- T-S24
    async def test_no_vision_provider_skips_before_vision(self):
        _Adapter.behaviour = lambda config, request, key, index: (_ for _ in ()).throw(self._safety())
        runtime = _runtime(providers=(_provider(),), vision_provider_id="")
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual(self.captioner.calls, [])
        self.assertEqual(task.runtime["reference_fallback"]["reason"], "no_vision_provider")

    # ---------------------------------------------------------------- T-S27
    async def test_task_deadline_inside_text_loop_skips_without_degrading(self):
        clock = {"now": 1000.0}
        advancing = _Captioner(text="一名短发女性", on_call=lambda: clock.update(now=1100.0))
        # vision_timeout=5 ⇒ 进入门槛 = 5+15+20 = 40s；deadline 取 +40.5s 刚好够进。
        scheduler = self._scheduler(
            _runtime(
                providers=(_provider(),),
                reference_fallback=ReferenceFallbackConfig(
                    enable=True, max_retries_per_node=2, vision_timeout_seconds=5,
                ),
            ),
            captioner=advancing,
        )
        task = self._task()
        attempt = _provider()
        request = GenerationRequest("原文", references=(ImageInput(b"x", "image/png"),))
        with mock.patch.object(scheduler_module.time, "monotonic", lambda: clock["now"]):
            outcome = await scheduler._text_stage(
                task, scheduler.config_getter(), attempt, request,
                {"kind": "safety", "evidence": "block:SAFETY"},
                status=200, deadline=clock["now"] + 40.5, text_quota=2,
            )
        info = task.runtime["reference_fallback"]
        # 识图发过一次；余量跌到 10s（< 15s 保护线）⇒ 一次纯文字都不发起。
        self.assertEqual(info["vision_calls"], 1)
        self.assertEqual(info["text_attempts"], 0)
        self.assertEqual(_Adapter.calls, [])
        # rev3 §5.1：保护余量不足时**不降级**，正常返回空 outcome 由调用方带图继续；
        # 不抛 TimeoutError，也不打 ``timeout_guard_exhausted`` 这类人为标志位。
        self.assertIsNone(outcome.results)
        self.assertIsNone(outcome.error)
        self.assertNotIn("timeout_guard_exhausted", task.runtime)
        self.assertNotIn("timeout_kind", task.runtime)
        self.assertNotEqual(task.state, TaskState.TIMED_OUT)
        self.assertEqual(info["nodes"][-1]["reason"], "budget")
        self.assertEqual(info["nodes"][-1]["outcome"], "skipped")

    # ---------------------------------------------------------------- T-S28
    async def test_guard_skips_text_call_and_task_settles_as_failed(self):
        """t14/rev3 回归：余量 ≤ ``_TEXT_GUARD_SECONDS`` 时不发起纯文字，也**不**冒充总超时。

        与 T-S27 的区别：这里走**真实** ``_execute``+``_generate``，断言终态落到
        FAILED（真实 deadline 没到，按普通失败结算并退款），而不是 TIMED_OUT。
        """
        clock = {"now": 1000.0}
        advancing = _Captioner(text="一名短发女性", on_call=lambda: clock.update(now=1290.0))

        def behaviour(config, request, key, index):
            raise self._safety()

        _Adapter.behaviour = behaviour
        runtime = _runtime(
            providers=(_provider(),),
            generation_timeout=300,
            reference_fallback=ReferenceFallbackConfig(
                enable=True, max_retries_per_node=2, vision_timeout_seconds=5,
            ),
        )
        scheduler = self._scheduler(runtime, captioner=advancing)
        task = self._task()
        task.created_at = clock["now"]
        try:
            with mock.patch.object(scheduler_module.time, "monotonic", lambda: clock["now"]):
                await scheduler._execute(task)
        finally:
            await scheduler.close()
        # 只发起过一次带图请求；识图发生一次，但余量跌到 10s ⇒ 一次纯文字都不发起。
        self.assertEqual([call[2] for call in _Adapter.calls], [1])
        info = task.runtime["reference_fallback"]
        self.assertEqual(info["vision_calls"], 1)
        self.assertEqual(info["text_attempts"], 0)
        self.assertEqual(info["nodes"][-1]["reason"], "budget")
        self.assertEqual(info["nodes"][-1]["outcome"], "skipped")
        # 真实 deadline 没到（时钟停在 1290，预算 300，从 1000 起算）⇒ 不能凭"保护余量
        # 不足"就判 TIMED_OUT：按普通失败结算，且退款政策照旧（FAILED 退款）。
        from imago_fusion.services.quota_store import terminal_refund_amount

        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(terminal_refund_amount(task.state, 1), 1)
        self.assertNotIn("timeout_guard_exhausted", task.runtime)
        self.assertNotIn("timeout", task.errors)

    # ---------------------------------------------------------------- T-S26
    async def test_non_safety_text_failure_stops_even_with_quota_left(self):
        def behaviour(config, request, key, index):
            if index == 0:
                raise self._safety()
            raise ProviderError("upstream 500", status=500)

        _Adapter.behaviour = behaviour
        runtime = _runtime(
            providers=(_provider(), _provider("node_b")),
            reference_fallback=ReferenceFallbackConfig(enable=True, max_retries_per_node=3),
        )
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        # 500 属排除层（非安全）：不消耗剩余两次额度去撞墙，直接换下一个节点带图；
        # B 的 500 同样不是安全拒绝，因此也不会进文字阶段。
        self.assertEqual(
            [(call[0], call[2]) for call in _Adapter.calls],
            [("node_a", 1), ("node_a", 0), ("node_b", 1)],
        )
        info = task.runtime["reference_fallback"]
        self.assertEqual(info["text_attempts"], 1)
        self.assertEqual([node["outcome"] for node in info["nodes"]], ["text_failed"])
        self.assertEqual(info["nodes"][0]["reason"], "text_failed")

    # ---------------------------------------------------------------- T-S25
    async def test_attempt_errors_marks_text_attempts(self):
        def behaviour(config, request, key, index):
            if index == 0:
                raise self._safety()
            raise ProviderError("boom", status=400)

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual(
            task.runtime["attempt_errors"],
            ["node_a:model:ProviderError", "node_a:model:text:ProviderError"],
        )


if __name__ == "__main__":
    unittest.main()
