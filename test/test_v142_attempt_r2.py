"""v1.4.2 返工：空图片包装不得记录为胜出（R1-10）。

t11 在候选 930335b 上用 ``[ImageResult(data=b"")]`` 证实：普通与文字两条腿都只
按"外部结果列表非空"判成功，于是非空容器包着**无可交付载荷**时照样写
``task.runtime["succeeded"]`` 并把 ``provider_output_count`` 记成 1；随后物化拒绝、
终态 ``no_output``，清单/终态 INFO 却仍带着"正常成功赢家"。

本文件按 R1-10 的验收逐条钉死（真实 ``TaskScheduler`` 链 + 假适配器/假会话，
不联网、不读盘、不写生产目录）：

1. 两条腿共用同一条**载荷形状**判据：``local_path`` 优先；其次是 ``data``
   （``data is not None`` 时只看字节，空字节即无载荷，即使同时带 URL 也不回落）；
   只有 ``data is None`` 时才看 ``url``（空白地址不算）。非图对象不算。
2. 空包装/空白地址/空字节 ⇒ 走既有 ``NoOutputError`` → no_output 回退：普通腿
   继续同节点下一个模型、再下一个节点；文字腿按 ``text_failed`` 结束本节点，
   下一个节点重新带原图。两条腿都**不**记赢家、不写回吐基线。
3. 判空不预取：合法 URL 不在判空阶段抓取，合法本地路径不在判空阶段 stat；
   它们后续的物化/回吐/投递失败仍由既有链路作为独立事实记录。
4. 失败/取消/无结果不伪造赢家；终态 INFO 与清单 ``attempts.succeeded`` 继续
   同源，空包装任务在两者里都必须是"没有赢家"。
"""

import asyncio
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from imago_fusion.core.errors import NoOutputError, ProviderError
from imago_fusion.core.models import (
    DrawTask,
    GenerationRequest,
    ImageInput,
    ImageResult,
    ProviderConfig,
    ReferenceFallbackConfig,
    RuntimeConfig,
    TaskStage,
    TaskState,
)
from imago_fusion.services import scheduler as scheduler_module
from imago_fusion.services.scheduler import TaskScheduler

IMAGE = b"\x89PNG\r\n\x1a\n-fake-bytes"
URL_OK = "https://ok.example/generated.png"


class DummySession:
    """假会话：本文件不允许任何网络访问，适配器也不会用它。"""


def _plugin_class():
    """**惰性**导入插件主类：收集阶段不碰 ``astrbot`` 真模块。"""

    from imago_fusion.main import ImagoFusion

    return ImagoFusion


def _provider(provider_id="node_a", *, enabled=True, models=(), limit=0):
    """节点：``models`` 的第一项当 ``model``，其余当 ``available_models``。"""

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
    """假适配器：记录每次真实出站形状，返回什么由类属性 ``behaviour`` 决定。"""

    calls: list[tuple[str, str, int]] = []
    behaviour = None

    def __init__(self, config):
        self.config = config

    async def generate(self, session, request, key):
        type(self).calls.append((self.config.id, self.config.model, len(request.references)))
        handler = type(self).behaviour
        if handler is not None:
            return handler(self.config, request, key, len(type(self).calls) - 1)
        return [ImageResult(data=b"image")]


class _Captioner:
    """假识图：固定返回一句描述（只用于让文字腿真的发一次文字请求）。"""

    def __init__(self, text="一名短发女性站在窗边，白色上衣，侧光。"):
        self.text = text
        self.calls = 0

    async def __call__(self, task, config, prompt_text, images, *, timeout, max_chars):
        self.calls += 1
        return self.text


class _Recorder:
    """把插件模块级 ``logger`` 的四个级别收成字符串列表（``%`` 预格式化）。"""

    LEVELS = ("debug", "info", "warning", "error")

    def __init__(self):
        self.records = {level: [] for level in self.LEVELS}

    def __getattr__(self, name):
        if name in type(self).LEVELS:
            def emit(message, *args):
                self.records[name].append(message % args if args else str(message))

            return emit
        raise AttributeError(name)

    def lines(self, level):
        return list(self.records[level])


class _BasePayloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old = scheduler_module.ADAPTERS.get("custom_endpoint")
        scheduler_module.ADAPTERS["custom_endpoint"] = _Adapter
        _Adapter.calls = []
        _Adapter.behaviour = None
        self.captioner = _Captioner()

    def tearDown(self):
        if self._old is None:
            scheduler_module.ADAPTERS.pop("custom_endpoint", None)
        else:
            scheduler_module.ADAPTERS["custom_endpoint"] = self._old

    # ---------------------------------------------------------------- 工具
    def _scheduler(self, runtime):
        return TaskScheduler(
            lambda: runtime,
            DummySession(),
            lambda *_: True,
            lambda *_: None,
            captioner=self.captioner,
        )

    def _task(self, prompt="原文画面", references=None):
        refs = references if references is not None else [ImageInput(IMAGE, "image/png")]
        task = DrawTask("a1b2c3d4e5f6a7b8", "umo-secret-7f3a", GenerationRequest(prompt, references=refs))
        task.created_at = time.monotonic()
        task.runtime["original_user_prompt"] = prompt
        task.runtime["finalize"] = lambda _task: None
        return task

    @staticmethod
    def _safety():
        return ProviderError("blocked", status=400, block_reason="SAFETY")

    @staticmethod
    def _errors(task):
        return list(task.runtime.get("attempt_errors", []))


class PayloadShapeUnitTests(unittest.TestCase):
    """R1-10 判据本身：与 ``materialize_result`` 的优先级逐条对齐。"""

    def test_shape_matrix_matches_materialize_precedence(self):
        from imago_fusion.core.models import has_deliverable_image

        cases = [
            (ImageResult(data=IMAGE), True),
            (ImageResult(data=b"x"), True),
            (ImageResult(data=b""), False),
            (ImageResult(data=b"", url=URL_OK), False),  # data 分支优先，空字节即无载荷
            (ImageResult(url=URL_OK), True),
            (ImageResult(url=""), False),
            (ImageResult(url="   "), False),
            (ImageResult(local_path=Path("/nonexistent/never-stat.png")), True),
            (ImageResult(), False),
            ("not-an-image", False),
            (None, False),
        ]
        for result, expected in cases:
            with self.subTest(result=result):
                self.assertEqual(has_deliverable_image(result), expected)

    def test_count_ignores_unusable_and_non_iterable(self):
        from imago_fusion.core.models import deliverable_image_count

        results = [ImageResult(data=b""), ImageResult(data=IMAGE), ImageResult(url=URL_OK)]
        self.assertEqual(deliverable_image_count(results), 2)
        self.assertEqual(deliverable_image_count([]), 0)
        self.assertEqual(deliverable_image_count(None), 0)


class NormalPayloadTests(_BasePayloadTests):
    """普通腿：空包装按无输出续走既有回退，且只记真正带图的那次。"""

    async def test_empty_bytes_wrapper_falls_back_to_next_model_same_node(self):
        runtime = _runtime(providers=(_provider(models=("model_empty", "model_good")),))
        task = self._task()

        _Adapter.behaviour = lambda config, request, key, index: (
            [ImageResult(data=b"")]
            if config.model == "model_empty"
            else [ImageResult(data=IMAGE)]
        )
        results = await self._scheduler(runtime)._generate(task)

        self.assertEqual(
            [(call[0], call[1]) for call in _Adapter.calls],
            [("node_a", "model_empty"), ("node_a", "model_good")],
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].data, IMAGE)
        record = task.runtime["succeeded"]
        self.assertEqual(record["provider_id"], "node_a")
        self.assertEqual(record["model"], "model_good")
        self.assertEqual(record["mode"], "normal")
        self.assertIn("node_a:model_empty:NoOutputError", self._errors(task))

    async def test_blank_url_wrapper_falls_back_to_next_node(self):
        runtime = _runtime(
            providers=(_provider("node_a"), _provider("node_b", models=("model_b",))),
        )
        task = self._task()

        _Adapter.behaviour = lambda config, request, key, index: (
            [ImageResult(url="   ")]
            if config.id == "node_a"
            else [ImageResult(url=URL_OK)]
        )
        results = await self._scheduler(runtime)._generate(task)

        self.assertEqual([call[0] for call in _Adapter.calls], ["node_a", "node_b"])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, URL_OK)
        record = task.runtime["succeeded"]
        self.assertEqual(record["provider_id"], "node_b")
        self.assertEqual(record["route"], "fallback")
        self.assertIn("node_a:model:NoOutputError", self._errors(task))

    async def test_empty_bytes_with_url_is_not_a_payload(self):
        runtime = _runtime(providers=(_provider(),))
        task = self._task()

        _Adapter.behaviour = lambda config, request, key, index: [
            ImageResult(data=b"", url=URL_OK)
        ]
        with self.assertRaises(NoOutputError):
            await self._scheduler(runtime)._generate(task)

        self.assertNotIn("succeeded", task.runtime)
        self.assertNotIn("delivered_reference_digests", task.runtime)
        self.assertIn("node_a:model:NoOutputError", self._errors(task))

    async def test_mixed_payloads_keep_legal_result_and_record_winner(self):
        runtime = _runtime(providers=(_provider(),))
        task = self._task()

        _Adapter.behaviour = lambda config, request, key, index: [
            ImageResult(data=b""),
            ImageResult(data=IMAGE),
        ]
        results = await self._scheduler(runtime)._generate(task)

        self.assertEqual(len(results), 2)  # 不清洗容器：空包装仍留给物化阶段留痕
        # 判据只看载荷：空包装留着，合法 data 才算得上一张（不再导入 helper，
        # 好让同一份用例在未修复的基线上也能跑出**行为**红，而不是收集期 ImportError）。
        self.assertFalse(bool(results[0].data))
        self.assertEqual(results[1].data, IMAGE)
        self.assertEqual(task.runtime["succeeded"]["model"], "model")

    async def test_legal_url_payload_is_not_prefetched(self):
        runtime = _runtime(providers=(_provider(),))
        task = self._task()

        _Adapter.behaviour = lambda config, request, key, index: [ImageResult(url=URL_OK)]
        with patch(
            "imago_fusion.core.network.fetch_reference",
            side_effect=AssertionError("judging emptiness must not fetch the URL"),
        ) as fetch:
            results = await self._scheduler(runtime)._generate(task)

        self.assertFalse(fetch.called)
        self.assertEqual(len(results), 1)
        self.assertEqual(task.runtime["succeeded"]["provider_id"], "node_a")

    async def test_legal_local_path_is_a_claim_without_touching_disk(self):
        runtime = _runtime(providers=(_provider(),))
        task = self._task()
        missing = Path("/nonexistent/never-touched-by-emptiness-check.png")

        _Adapter.behaviour = lambda config, request, key, index: [ImageResult(local_path=missing)]
        results = await self._scheduler(runtime)._generate(task)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].local_path, missing)
        self.assertEqual(task.runtime["succeeded"]["model"], "model")


class TextPayloadTests(_BasePayloadTests):
    """文字腿：与普通腿共用同一判据，且空载荷不能冒出 text_fallback 赢家。"""

    async def test_empty_text_leg_does_not_record_text_winner_then_next_node_retries(self):
        runtime = _runtime(
            providers=(_provider("node_a"), _provider("node_b", models=("model_b",))),
        )
        task = self._task()

        def behaviour(config, request, key, index):
            if index == 0:
                raise self._safety()
            if index == 1:
                return [ImageResult(data=b"")]
            return [ImageResult(data=IMAGE)]

        _Adapter.behaviour = behaviour
        results = await self._scheduler(runtime)._generate(task)

        self.assertEqual(
            _Adapter.calls,
            [("node_a", "model", 1), ("node_a", "model", 0), ("node_b", "model_b", 1)],
        )
        self.assertEqual(len(results), 1)
        record = task.runtime["succeeded"]
        self.assertEqual(record["provider_id"], "node_b")
        self.assertEqual(record["mode"], "normal")  # 空文字腿绝不记 text_fallback 赢家
        nodes = task.runtime["reference_fallback"]["nodes"]
        self.assertEqual(nodes[0]["outcome"], "text_failed")
        self.assertIn("node_a:model:text:NoOutputError", self._errors(task))

    async def test_blank_url_in_text_leg_is_no_output_without_winner(self):
        runtime = _runtime(providers=(_provider(),))
        task = self._task()

        def behaviour(config, request, key, index):
            if index == 0:
                raise self._safety()
            return [ImageResult(url="  ")]

        _Adapter.behaviour = behaviour
        with self.assertRaises(NoOutputError):
            await self._scheduler(runtime)._generate(task)

        self.assertNotIn("succeeded", task.runtime)
        self.assertNotIn("delivered_references_text", task.runtime)
        nodes = task.runtime["reference_fallback"]["nodes"]
        self.assertEqual(nodes[-1]["outcome"], "text_failed")
        self.assertIn("node_a:model:text:NoOutputError", self._errors(task))

    async def test_legal_text_payload_still_records_text_mode(self):
        runtime = _runtime(providers=(_provider(),))
        task = self._task()

        def behaviour(config, request, key, index):
            if index == 0:
                raise self._safety()
            return [ImageResult(data=IMAGE)]

        _Adapter.behaviour = behaviour
        results = await self._scheduler(runtime)._generate(task)

        self.assertEqual(len(results), 1)
        record = task.runtime["succeeded"]
        self.assertEqual(record["mode"], "text_fallback")
        self.assertTrue(task.runtime["delivered_references_text"])
        self.assertEqual(task.runtime["delivered_references"], [])


class NoWinnerAccountingTests(_BasePayloadTests):
    """失败/取消/无结果不伪造赢家；清单与终态 INFO 继续同源。"""

    async def test_failure_after_empty_payload_never_records_winner(self):
        runtime = _runtime(
            providers=(_provider("node_a"), _provider("node_b", models=("model_b",))),
        )
        task = self._task()

        def behaviour(config, request, key, index):
            if config.id == "node_a":
                return [ImageResult(data=b"")]
            raise ProviderError("boom", status=500)

        _Adapter.behaviour = behaviour
        with self.assertRaises(ProviderError):
            await self._scheduler(runtime)._generate(task)

        self.assertNotIn("succeeded", task.runtime)
        self.assertIn("node_a:model:NoOutputError", self._errors(task))

    async def test_cancel_after_empty_payload_never_records_winner(self):
        runtime = _runtime(
            providers=(_provider("node_a"), _provider("node_b", models=("model_b",))),
        )
        task = self._task()

        def behaviour(config, request, key, index):
            if config.id == "node_a":
                return [ImageResult(data=b"")]
            raise asyncio.CancelledError()

        _Adapter.behaviour = behaviour
        with self.assertRaises(asyncio.CancelledError):
            await self._scheduler(runtime)._generate(task)

        self.assertNotIn("succeeded", task.runtime)

    async def test_no_output_after_empty_payload_reports_no_winner_in_manifest_and_info(self):
        runtime = _runtime(providers=(_provider(),))
        task = self._task()

        _Adapter.behaviour = lambda config, request, key, index: [ImageResult(data=b"")]
        with self.assertRaises(NoOutputError):
            await self._scheduler(runtime)._generate(task)

        # 物化/投递从未发生：显式把任务落到 no_output 终态，再看清单与终态 INFO。
        task.state = TaskState.NO_OUTPUT
        task.stage = TaskStage.FINISHED
        manifest, recorder = await self._finalize(task)

        self.assertEqual(manifest["attempts"]["succeeded"], {})
        self.assertEqual(manifest.get("provider_output_count", 0), 0)
        line = [text for text in recorder.lines("warning") if "终态" in text][0]
        self.assertIn("终态 no_output", line)
        self.assertIn("provider=0", line)
        self.assertTrue(line.endswith("success=-"), line)

    # ---------------------------------------------------------------- finalizer 替身
    def _plugin(self, store):
        plugin = object.__new__(_plugin_class())
        plugin.raw_config = {}
        plugin.store = store
        plugin.quota_store = None
        plugin.scheduler = type("S", (), {"tasks": {}, "accepting": True})()
        return plugin

    async def _finalize(self, task):
        from imago_fusion.services.persona_store import PersonaStore

        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = PersonaStore(Path(directory.name), lambda: 20 * 1024 * 1024)
        plugin = self._plugin(store)
        recorder = _Recorder()
        from imago_fusion import main as main_module

        with patch.object(main_module, "logger", recorder):
            await plugin._finalize_task(task)
        manifest = json.loads((store.task_dir(task.id) / "task.json").read_text("utf-8"))
        return manifest, recorder


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
