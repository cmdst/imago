"""v1.3.0 起的调度器路由回归（真实 ``_generate`` 入口）。

只保留与"参考图降级"无关的部分：节点启停路由、全部停用时在扣额度之前拒绝、
以及 ``attempt_meta`` 的子请求/参考图快照。降级本身的执行级用例在
``test_v140_text_fallback.py``（v1.4.0 已把作用域改为逐节点）。
"""

import unittest

from imago_fusion.core.errors import ConfigurationError, ProviderError
from imago_fusion.core.models import (
    DrawTask,
    GenerationRequest,
    ImageInput,
    ImageResult,
    ProviderConfig,
    RuntimeConfig,
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
    options = {"generation_timeout": 120, "vision_provider_id": "vision"}
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


class SchedulerRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old = scheduler_module.ADAPTERS.get("custom_endpoint")
        scheduler_module.ADAPTERS["custom_endpoint"] = _AdapterBase
        _AdapterBase.calls = []
        _AdapterBase.behaviour = None

    def tearDown(self):
        if self._old is None:
            scheduler_module.ADAPTERS.pop("custom_endpoint", None)
        else:
            scheduler_module.ADAPTERS["custom_endpoint"] = self._old

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
            lambda *_: True, lambda *_: None,
        )
        task = DrawTask("t", "umo", GenerationRequest("原文", references=[explicit]))
        task.runtime["persona_references"] = persona
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
            # 快照 = 该次尝试真实采样后的集合：1 张显式 + 1 张人格（limit=2）。
            self.assertEqual(_AdapterBase.calls[0][1].references[0].data, b"e1")
            self.assertEqual(len(_AdapterBase.calls[0][1].references), 2)
            roles = [item["role"] for item in task.runtime["attempt_meta"]["references"]]
            self.assertEqual(roles, ["explicit", "persona"])
            self.assertEqual(task.runtime["attempt_meta"]["persona_count"], 1)
        finally:
            await scheduler.close()


if __name__ == "__main__":
    unittest.main()
