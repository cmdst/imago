"""v1.3.0：调度器启停路由、拒绝快照与 S6 条件闸门（真实 _generate 入口）。"""

import unittest

from imago_fusion.core.errors import ConfigurationError, ProviderError
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
from imago_fusion.services import scheduler as scheduler_module
from imago_fusion.services.scheduler import TaskScheduler


class DummySession:
    pass


def _provider(provider_id="node_a", *, enabled=True, base_url="https://example.invalid/v1"):
    return ProviderConfig(
        provider_id, "custom_endpoint", base_url, ("key",), model="model", enabled=enabled,
    )


def _runtime(**kwargs):
    options = {
        "generation_timeout": 120,
        "vision_provider_id": "vision",
        "reference_fallback": ReferenceFallbackConfig(enable=True),
        "experimental_reference": ExperimentalReferenceConfig(enable=False),
    }
    options.update(kwargs)
    return RuntimeConfig(**options)


class _AdapterBase:
    """记录请求与节点顺序的假适配器；通过类属性注入行为。"""

    calls: list[tuple[str, GenerationRequest]] = []
    behaviour = None

    def __init__(self, config):
        self.config = config

    async def generate(self, session, request, key):
        type(self).calls.append((self.config.id, request))
        handler = type(self).behaviour
        if handler is not None:
            return handler(self.config, request, key)
        return [ImageResult(data=b"image")]


class SchedulerV130Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old = scheduler_module.ADAPTERS.get("custom_endpoint")
        scheduler_module.ADAPTERS["custom_endpoint"] = _AdapterBase
        _AdapterBase.calls = []
        _AdapterBase.behaviour = None
        self.rescued = []

    def tearDown(self):
        if self._old is None:
            scheduler_module.ADAPTERS.pop("custom_endpoint", None)
        else:
            scheduler_module.ADAPTERS["custom_endpoint"] = self._old

    async def _rescue(self, task, config, remaining, rejection, locked):
        self.rescued.append((rejection, locked, remaining))
        return [ImageResult(data=b"rescued")]

    async def test_disabled_node_is_not_routed(self):
        providers = (_provider("off", enabled=False), _provider("on"))
        scheduler = TaskScheduler(lambda: _runtime(providers=providers), DummySession(),
                                  lambda *_: True, lambda *_: None)
        try:
            await scheduler._generate(DrawTask("t", "umo", GenerationRequest("x")))
            self.assertEqual([call[0] for call in _AdapterBase.calls], ["on"])
        finally:
            await scheduler.close()

    async def test_all_nodes_disabled_raises_before_any_call(self):
        providers = (_provider("off", enabled=False),)
        scheduler = TaskScheduler(lambda: _runtime(providers=providers), DummySession(),
                                  lambda *_: True, lambda *_: None)
        try:
            with self.assertRaisesRegex(ConfigurationError, "没有可用的绘图节点"):
                await scheduler._generate(DrawTask("t", "umo", GenerationRequest("x")))
            self.assertEqual(_AdapterBase.calls, [])
        finally:
            await scheduler.close()

    async def test_rejection_records_meta_and_locks_request_snapshot(self):
        images = [ImageInput(b"one", "image/png"), ImageInput(b"two", "image/png")]

        def reject(config, request, key):
            raise ProviderError("Invalid image format", status=400)

        _AdapterBase.behaviour = reject
        providers = (_provider("node_a"),)
        scheduler = TaskScheduler(lambda: _runtime(providers=providers), DummySession(),
                                  lambda *_: True, lambda *_: None, rescue=self._rescue)
        task = DrawTask("t", "umo", GenerationRequest("原文", references=images))
        task.runtime["original_user_prompt"] = "原文（用户）"
        try:
            await scheduler._generate(task)
            self.assertEqual(task.runtime["attempt_meta"]["references"][0]["role"], "explicit")
            self.assertEqual(task.runtime["attempt_meta"]["explicit_count"], 2)
            locked = task.runtime["fallback_request"]
            # 快照 = 实际失败请求（含调度器注入的关系声明）；用户原文单独保存。
            self.assertTrue(locked["request"].prompt.startswith("原文"))
            self.assertIn("Reference image(s) attached", locked["request"].prompt)
            self.assertEqual(len(locked["request"].references), 2)
            self.assertEqual(locked["user_prompt"], "原文（用户）")
            self.assertTrue(task.runtime["fallback_locked"])
            self.assertEqual(len(self.rescued), 1)
        finally:
            await scheduler.close()

    async def test_later_rejection_does_not_overwrite_locked_snapshot(self):
        first = [ImageInput(b"first", "image/png")]
        second = [ImageInput(b"second", "image/png"), ImageInput(b"second2", "image/png")]

        def reject(config, request, key):
            if config.id == "node_a":
                raise ProviderError("Invalid image format", status=400)
            raise ProviderError("blocked by safety", status=400, block_reason="SAFETY")

        _AdapterBase.behaviour = reject
        providers = (_provider("node_a"), _provider("node_b"))
        scheduler = TaskScheduler(lambda: _runtime(providers=providers), DummySession(),
                                  lambda *_: True, lambda *_: None, rescue=self._rescue)
        task = DrawTask("t", "umo", GenerationRequest("原文", references=first))
        task.runtime["original_user_prompt"] = "原文"
        try:
            await scheduler._generate(task)
            locked = task.runtime["fallback_request"]
            self.assertEqual(locked["provider_id"], "node_a")
            self.assertEqual([item.data for item in locked["request"].references], [b"first"])
            self.assertEqual(task.runtime["reference_rejection"]["kind"], "image_invalid")
        finally:
            await scheduler.close()

    async def test_not_convertible_rejection_does_not_rescue(self):
        def reject(config, request, key):
            raise ProviderError("Invalid API key", status=401)

        _AdapterBase.behaviour = reject
        providers = (_provider("node_a"),)
        scheduler = TaskScheduler(lambda: _runtime(providers=providers), DummySession(),
                                  lambda *_: True, lambda *_: None, rescue=self._rescue)
        task = DrawTask("t", "umo", GenerationRequest("x", references=[ImageInput(b"i", "image/png")]))
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
            self.assertEqual(self.rescued, [])
            # 401 属排除层：不产生 reference_rejection，因此是 no_rejection（不是可降级）。
            self.assertEqual(task.runtime["reference_fallback"]["reason"], "no_rejection")
        finally:
            await scheduler.close()

    async def test_small_budget_skips_fallback(self):
        def reject(config, request, key):
            raise ProviderError("Invalid image format", status=400)

        _AdapterBase.behaviour = reject
        providers = (_provider("node_a"),)
        scheduler = TaskScheduler(lambda: _runtime(providers=providers), DummySession(),
                                  lambda *_: True, lambda *_: None, rescue=self._rescue)
        task = DrawTask("t", "umo", GenerationRequest("x", references=[ImageInput(b"i", "image/png")]))
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task, remaining=10)
            self.assertEqual(task.runtime["reference_fallback"]["reason"], "budget")
        finally:
            await scheduler.close()


    async def test_attempt_meta_records_sampled_persona_snapshot(self):
        persona = [ImageInput(b"p1", "image/png"), ImageInput(b"p2", "image/png")]
        explicit = ImageInput(b"e1", "image/png")
        node = ProviderConfig(
            "node_a", "custom_endpoint", "https://example.invalid/v1", ("key",),
            model="model", reference_image_limit=2,
        )

        def reject(config, request, key):
            raise ProviderError("Invalid image format", status=400)

        _AdapterBase.behaviour = reject
        scheduler = TaskScheduler(
            lambda: _runtime(providers=(node,)), DummySession(),
            lambda *_: True, lambda *_: None, rescue=self._rescue,
        )
        task = DrawTask("t", "umo", GenerationRequest("原文", references=[explicit]))
        task.runtime["persona_references"] = persona
        try:
            await scheduler._generate(task)
            locked = task.runtime["fallback_request"]
            # 快照 = 该次尝试真实采样后的集合：1 张显式 + 1 张人格（limit=2）。
            self.assertEqual(len(locked["request"].references), 2)
            self.assertEqual(locked["request"].references[0].data, b"e1")
            roles = [item["role"] for item in task.runtime["attempt_meta"]["references"]]
            self.assertEqual(roles, ["explicit", "persona"])
            self.assertEqual(task.runtime["attempt_meta"]["persona_count"], 1)
        finally:
            await scheduler.close()

    async def test_safety_with_capacity_shortage_stops_with_notice(self):
        def reject(config, request, key):
            raise ProviderError("blocked", status=400, block_reason="SAFETY")

        _AdapterBase.behaviour = reject
        providers = (_provider("node_a"), _provider("occluder"))
        exp = ExperimentalReferenceConfig(enable=True, occluder_provider_id="occluder", max_images=1)
        scheduler = TaskScheduler(
            lambda: _runtime(providers=providers, experimental_reference=exp),
            DummySession(), lambda *_: True, lambda *_: None, rescue=self._rescue,
        )
        task = DrawTask("t", "umo", GenerationRequest(
            "x", references=[ImageInput(b"1", "image/png"), ImageInput(b"2", "image/png")],
        ))
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
            info = task.runtime["reference_fallback"]
            self.assertEqual(info["reason"], "capacity_safety")
            self.assertIn("未改按文字重绘", info["notice"])
            self.assertEqual(self.rescued, [])
        finally:
            await scheduler.close()

    async def test_capacity_over_caption_stops_with_notice(self):
        def reject(config, request, key):
            raise ProviderError("Invalid image format", status=400)

        _AdapterBase.behaviour = reject
        providers = (_provider("node_a"),)
        refs = [ImageInput(bytes([index]), "image/png") for index in range(5)]
        scheduler = TaskScheduler(lambda: _runtime(providers=providers), DummySession(),
                                  lambda *_: True, lambda *_: None, rescue=self._rescue)
        task = DrawTask("t", "umo", GenerationRequest("x", references=refs))
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
            info = task.runtime["reference_fallback"]
            self.assertEqual(info["reason"], "capacity_over_caption")
            self.assertIn("降级上限", info["notice"])
        finally:
            await scheduler.close()

    async def test_missing_vision_provider_skips_fallback(self):
        def reject(config, request, key):
            raise ProviderError("Invalid image format", status=400)

        _AdapterBase.behaviour = reject
        providers = (_provider("node_a"),)
        scheduler = TaskScheduler(
            lambda: _runtime(providers=providers, vision_provider_id=""),
            DummySession(), lambda *_: True, lambda *_: None, rescue=self._rescue,
        )
        task = DrawTask("t", "umo", GenerationRequest("x", references=[ImageInput(b"i", "image/png")]))
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
            self.assertEqual(task.runtime["reference_fallback"]["reason"], "no_vision_provider")
        finally:
            await scheduler.close()

    async def test_cancelled_task_skips_fallback(self):
        def reject(config, request, key):
            raise ProviderError("Invalid image format", status=400)

        _AdapterBase.behaviour = reject
        providers = (_provider("node_a"),)
        scheduler = TaskScheduler(lambda: _runtime(providers=providers), DummySession(),
                                  lambda *_: True, lambda *_: None, rescue=self._rescue)
        task = DrawTask("t", "umo", GenerationRequest("x", references=[ImageInput(b"i", "image/png")]))
        task.state = TaskState.CANCELLED
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
            self.assertEqual(task.runtime["reference_fallback"]["reason"], "cancelled")
        finally:
            await scheduler.close()


if __name__ == "__main__":
    unittest.main()
