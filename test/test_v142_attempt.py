"""v1.4.2：成功 attempt 记账、任务清单与终态 INFO 的对齐（t8 专题）。

本文件只钉三件事，全部以**真实调度链 + 假传输**取证（不联网、不写生产目录）：

1. ``TaskScheduler._generate`` 只在"适配器真的返回了图"时写
   ``task.runtime["succeeded"]``，并带上调度节点/模型/模式/主后备位置；
   普通、切备用节点、切同节点备用模型、带图被拒后的文字降级四条路径共用同一份
   记录形状与同一个 helper（不各写一份）。
2. 适配器"返回 ``[]`` 而不抛错"是**无输出**：既不记成功，也不吃掉既有回退
   （普通腿继续下一个模型/节点，文字腿结束本节点、下一个节点重新带图）。
3. ``ImagoFusion._finalize_task`` 的终态 INFO 与清单 ``attempts.succeeded``
   读的是同一份 runtime 记录；同时把"Provider 返回了几张"（provider）与
   "物化/回吐后还剩几张"（usable）分列，失败/超时/取消/无结果不伪造成功者。

形状照抄 ``test/test_v140_text_fallback.py``（假适配器记录每一次真实出站形状）
与 ``test/test_v141_compress.py``（``object.__new__(ImagoFusion)`` 只跑 finalizer，
不碰 AstrBot 运行期）。
"""

import asyncio
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from imago_fusion.core.errors import NoOutputError, ProviderError, ProviderTimeout
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
from imago_fusion.services.scheduler import (
    SUCCESS_MODEL_BASIS,
    TaskScheduler,
)

IMAGE = b"\x89PNG\r\n\x1a\n-fake-bytes"


class DummySession:
    pass


def _plugin_class():
    """**惰性**导入插件主类：收集阶段不碰 ``astrbot`` 真模块（同 test_v141_compress）。"""

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


class _Outcome:
    """``_record_outcome`` 认得的假投递结果（只需要这几个属性）。"""

    def __init__(
        self,
        *,
        success=True,
        error="",
        generation_success=None,
        usable_output_count=None,
        delivery_kind="",
    ):
        self.success = success
        self.error = error
        self.generation_success = generation_success
        self.usable_output_count = usable_output_count
        self.delivery_kind = delivery_kind


class _Adapter:
    """记录 ``(node, model, 参考图数)`` 的假适配器；行为由类属性注入。"""

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
    """假识图：固定返回一句描述（用例不需要它决定成败）。"""

    def __init__(self, text="一名短发女性站在窗边，白色上衣，侧光。"):
        self.text = text
        self.calls = 0

    async def __call__(self, task, config, prompt_text, images, *, timeout, max_chars):
        self.calls += 1
        return self.text


class _BaseAttemptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old = scheduler_module.ADAPTERS.get("custom_endpoint")
        scheduler_module.ADAPTERS["custom_endpoint"] = _Adapter
        _Adapter.calls = []
        _Adapter.requests = []
        _Adapter.behaviour = None
        self.captioner = _Captioner()

    def tearDown(self):
        if self._old is None:
            scheduler_module.ADAPTERS.pop("custom_endpoint", None)
        else:
            scheduler_module.ADAPTERS["custom_endpoint"] = self._old

    # ---------------------------------------------------------------- 工具
    def _scheduler(self, runtime, runner=None):
        return TaskScheduler(
            lambda: runtime,
            DummySession(),
            (lambda *_: True) if runner is None else runner,
            lambda *_: None,
            captioner=self.captioner,
        )

    def _task(self, prompt="原文画面", references=None, umo="umo-secret-7f3a"):
        refs = references if references is not None else [ImageInput(IMAGE, "image/png")]
        task = DrawTask("task-0001", umo, GenerationRequest(prompt, references=refs))
        task.created_at = time.monotonic()
        task.runtime["original_user_prompt"] = prompt
        task.runtime["finalize"] = lambda _task: None
        return task

    @staticmethod
    def _safety():
        return ProviderError("blocked", status=400, block_reason="SAFETY")


class SuccessRecordTests(_BaseAttemptTests):
    """1) 四条成功路径共用同一份记录形状，且只记真实胜出的那次。"""

    async def test_normal_success_records_dispatch_facts(self):
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            results = await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertTrue(results)
        record = task.runtime["succeeded"]
        self.assertEqual(record["provider_id"], "node_a")
        self.assertEqual(record["model"], "model")
        self.assertEqual(record["mode"], "normal")
        self.assertEqual(record["api_type"], "custom_endpoint")
        self.assertEqual(record["route"], "primary")
        self.assertEqual(record["node_index"], 1)
        self.assertEqual(record["node_count"], 1)
        self.assertEqual(record["model_index"], 1)
        self.assertEqual(record["model_attempts"], 1)
        # 调度标签口径必须自证"不是远端实际模型"。
        self.assertEqual(record["model_basis"], SUCCESS_MODEL_BASIS)
        self.assertEqual(SUCCESS_MODEL_BASIS, "dispatch")
        for forbidden in ("actual_model", "remote_model"):
            self.assertNotIn(forbidden, record)

    async def test_same_node_backup_model_records_the_model_that_won(self):
        def behaviour(config, request, key, index):
            if config.model == "model_bad":
                raise ProviderError("bad shape", status=400)
            return [ImageResult(data=b"model-ok")]

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider(models=("model_bad", "model_ok")),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            results = await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual(results[0].data, b"model-ok")
        self.assertEqual(
            [(call[0], call[1]) for call in _Adapter.calls],
            [("node_a", "model_bad"), ("node_a", "model_ok")],
        )
        record = task.runtime["succeeded"]
        self.assertEqual(record["model"], "model_ok")
        self.assertEqual(record["mode"], "normal")
        # 同节点备用模型：route 仍是 primary，但模型序号是 2/2。
        self.assertEqual(record["route"], "primary")
        self.assertEqual(record["node_index"], 1)
        self.assertEqual(record["model_index"], 2)
        self.assertEqual(record["model_attempts"], 2)

    async def test_backup_node_records_fallback_route(self):
        def behaviour(config, request, key, index):
            if config.id == "node_a":
                raise ProviderError("node a down")
            return [ImageResult(data=b"node-b")]

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider("node_a"), _provider("node_b")))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            results = await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual(results[0].data, b"node-b")
        record = task.runtime["succeeded"]
        self.assertEqual(record["provider_id"], "node_b")
        self.assertEqual(record["route"], "fallback")
        self.assertEqual(record["node_index"], 2)
        self.assertEqual(record["node_count"], 2)
        self.assertEqual(record["model_index"], 1)

    async def test_text_fallback_success_records_mode_and_empty_reference_snapshot(self):
        def behaviour(config, request, key, index):
            if index == 0:
                raise self._safety()
            return [ImageResult(data=b"text-only")]

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            results = await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertTrue(results)
        record = task.runtime["succeeded"]
        self.assertEqual(record["mode"], "text_fallback")
        self.assertEqual(record["provider_id"], "node_a")
        self.assertEqual(record["route"], "primary")
        self.assertEqual(record["node_index"], 1)
        self.assertEqual(record["model_index"], 1)
        # 文字腿没有发任何参考图：回吐基线必须是"空快照"，不是上一轮的旧图集。
        self.assertEqual(task.runtime["delivered_references"], [])
        self.assertTrue(task.runtime["delivered_references_text"])
        self.assertEqual(task.runtime["reference_fallback"]["reason"], "succeeded")
        self.assertEqual(task.runtime["text_attempts"], 1)

    async def test_text_fallback_success_on_backup_node_records_that_node(self):
        def behaviour(config, request, key, index):
            if config.id == "node_a":
                raise self._safety()
            if request.references:
                raise self._safety()
            return [ImageResult(data=b"node-b-text")]

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider("node_a"), _provider("node_b")))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            results = await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual(results[0].data, b"node-b-text")
        record = task.runtime["succeeded"]
        self.assertEqual(record["provider_id"], "node_b")
        self.assertEqual(record["mode"], "text_fallback")
        self.assertEqual(record["route"], "fallback")
        self.assertEqual(record["node_index"], 2)
        self.assertEqual(record["node_count"], 2)


class EmptyResultTests(_BaseAttemptTests):
    """2) 适配器返回 ``[]`` 是"有依据的无输出"，不是成功、也不挡回退。"""

    async def test_empty_results_on_normal_path_do_not_claim_success(self):
        def behaviour(config, request, key, index):
            if config.model == "model_empty":
                return []
            return [ImageResult(data=b"model-good")]

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider(models=("model_empty", "model_good")),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            results = await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual(results[0].data, b"model-good")
        self.assertEqual(
            [(call[0], call[1]) for call in _Adapter.calls],
            [("node_a", "model_empty"), ("node_a", "model_good")],
        )
        # 空输出按无输出留痕，成功记录只属于真正出图的第二个模型。
        self.assertIn("node_a:model_empty:NoOutputError", task.runtime["attempt_errors"])
        record = task.runtime["succeeded"]
        self.assertEqual(record["model"], "model_good")
        self.assertEqual(record["model_index"], 2)

    async def test_only_empty_results_raises_no_output_and_records_nothing(self):
        _Adapter.behaviour = lambda *_: []
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(NoOutputError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertNotIn("succeeded", task.runtime)
        self.assertNotIn("delivered_reference_digests", task.runtime)
        self.assertIn("node_a:model:NoOutputError", task.runtime["attempt_errors"])

    async def test_empty_results_in_text_stage_do_not_block_existing_fallback(self):
        """带图被拒 → 文字腿返回 ``[]``：本节点按 text_failed 结束，下一个节点带图重试。"""

        def behaviour(config, request, key, index):
            if config.id == "node_a":
                if request.references:
                    raise self._safety()
                return []
            return [ImageResult(data=b"node-b")]

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider("node_a"), _provider("node_b")))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            results = await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertEqual(results[0].data, b"node-b")
        self.assertEqual(
            [(call[0], call[2]) for call in _Adapter.calls],
            [("node_a", 1), ("node_a", 0), ("node_b", 1)],
        )
        self.assertIn("node_a:model:text:NoOutputError", task.runtime["attempt_errors"])
        nodes = task.runtime["reference_fallback"]["nodes"]
        self.assertEqual([node["outcome"] for node in nodes], ["text_failed"])
        record = task.runtime["succeeded"]
        self.assertEqual(record["provider_id"], "node_b")
        self.assertEqual(record["mode"], "normal")


class NoFakeSuccessTests(_BaseAttemptTests):
    """3) 失败 / 节点超时 / 取消 / 全空输出都不留成功记录。"""

    async def test_plain_failure_records_no_success(self):
        _Adapter.behaviour = lambda *_: (_ for _ in ()).throw(ProviderError("upstream 500"))
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        finally:
            await scheduler.close()
        self.assertNotIn("succeeded", task.runtime)

    async def test_node_timeout_ends_failed_without_success_record(self):
        def behaviour(config, request, key, index):
            raise ProviderTimeout("节点超时 node=node_a model=model timeout=90s")

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(
            runtime,
            runner=lambda *_: _Outcome(
                success=True, generation_success=False, usable_output_count=0,
                delivery_kind="notification",
            ),
        )
        task = self._task()
        try:
            await scheduler._execute(task)
        finally:
            await scheduler.close()
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertNotIn("succeeded", task.runtime)
        self.assertTrue(task.runtime["timeouts"])

    async def test_only_empty_results_ends_no_output_not_success(self):
        _Adapter.behaviour = lambda *_: []
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(
            runtime,
            runner=lambda *_: _Outcome(
                success=True, generation_success=False, usable_output_count=0,
                delivery_kind="notification",
            ),
        )
        task = self._task()
        try:
            await scheduler._execute(task)
        finally:
            await scheduler.close()
        # 空输出 = Provider 请求已完成（可能已计费）⇒ NO_OUTPUT，不是可退款失败。
        self.assertEqual(task.state, TaskState.NO_OUTPUT)
        self.assertNotIn("succeeded", task.runtime)
        self.assertIn("node_a:model:NoOutputError", task.runtime["attempt_errors"])

    async def test_cancelled_task_never_records_success(self):
        def behaviour(config, request, key, index):
            raise asyncio.CancelledError()

        _Adapter.behaviour = behaviour
        runtime = _runtime(providers=(_provider(),))
        scheduler = self._scheduler(runtime)
        task = self._task()
        try:
            try:
                await scheduler._execute(task)
            except asyncio.CancelledError:
                pass
            else:  # pragma: no cover - 只有在"取消被吞掉"时才会走到
                self.fail("取消必须以 CancelledError 向上传播，不能被结算成成功")
        finally:
            await scheduler.close()
        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertNotIn("succeeded", task.runtime)


class _Recorder:
    """把插件模块级 ``logger`` 的三个级别收成字符串列表（``%`` 预格式化）。"""

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


class TerminalFactsTests(unittest.IsolatedAsyncioTestCase):
    """4) finalizer：终态 INFO 与清单 ``attempts.succeeded`` 同源，且生成/投递分列。"""

    def _plugin(self, store):
        plugin = object.__new__(_plugin_class())
        plugin.raw_config = {}
        plugin.store = store
        plugin.quota_store = None
        plugin.scheduler = type("S", (), {"tasks": {}, "accepting": True})()
        return plugin

    def _task(self, state):
        task = DrawTask("a" * 32, "umo-secret-7f3a", GenerationRequest("draw"))
        task.state = state
        task.stage = TaskStage.FINISHED
        task.created_at = time.monotonic()
        return task

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

    async def test_success_line_and_manifest_share_the_dispatch_record(self):
        task = self._task(TaskState.SUCCEEDED)
        record = {
            "provider_id": "node_b",
            "model": "web-gemini-image",
            "mode": "text_fallback",
            "api_type": "astrbot_native",
            "route": "fallback",
            "node_index": 2,
            "node_count": 2,
            "model_index": 1,
            "model_attempts": 2,
            "model_basis": "dispatch",
        }
        task.runtime.update(
            {
                "provider_output_count": 2,
                "usable_output_count": 2,
                "generation_success": True,
                "image_delivery_attempted": True,
                "image_delivery_success": True,
                "succeeded": dict(record),
                "attempt_errors": ["node_a:model:ProviderError"],
            }
        )
        manifest, recorder = await self._finalize(task)
        # 清单与日志读的是同一份记录。
        self.assertEqual(manifest["attempts"]["succeeded"], record)
        self.assertEqual(manifest["attempts"]["succeeded"]["provider_id"], "node_b")
        self.assertEqual(manifest["attempts"]["succeeded"]["model"], "web-gemini-image")
        line = [text for text in recorder.lines("info") if "终态" in text][0]
        self.assertIn(f"task={task.id[:8]}", line)
        self.assertIn("终态 succeeded", line)
        self.assertIn("usable=2", line)
        self.assertIn("delivery_ok=True", line)
        self.assertIn("provider=2", line)
        self.assertIn("success=node_b:web-gemini-image:text_fallback:fallback", line)
        self.assertIn("nodes=2/2", line)
        self.assertIn("models=1/2", line)
        self.assertIn("basis=dispatch", line)
        # 既有字段与顺序没被改写：读旧行的那套 grep 仍然成立。
        self.assertIn("耗时=", line)
        self.assertLess(line.index("usable="), line.index("delivery_ok="))
        self.assertLess(line.index("delivery_ok="), line.index("provider="))

    async def test_delivery_failure_keeps_generation_facts_but_is_not_success(self):
        task = self._task(TaskState.DELIVERY_FAILED)
        task.errors.append("delivery:SendError")
        task.runtime.update(
            {
                "provider_output_count": 1,
                "usable_output_count": 1,
                "generation_success": True,
                "image_delivery_attempted": True,
                "image_delivery_success": False,
                "image_delivery_error": "SendError",
                "succeeded": {
                    "provider_id": "node_a",
                    "model": "model-x",
                    "mode": "normal",
                    "route": "primary",
                    "model_basis": "dispatch",
                },
                "attempt_errors": ["node_b:model:ProviderTimeout"],
            }
        )
        manifest, recorder = await self._finalize(task)
        # 生成成功与投递失败分列：清单不会因为"有图"就把任务写成成功。
        self.assertIs(manifest["generation_success"], True)
        self.assertEqual(manifest["provider_output_count"], 1)
        self.assertEqual(manifest["usable_output_count"], 1)
        self.assertIs(manifest["image_delivery"]["attempted"], True)
        self.assertIs(manifest["image_delivery"]["success"], False)
        self.assertEqual(manifest["state"], "delivery_failed")
        line = [text for text in recorder.lines("error") if "终态" in text][0]
        self.assertIn("终态 delivery_failed", line)
        self.assertIn("provider=1", line)
        self.assertIn("usable=1", line)
        self.assertIn("success=node_a:model-x:normal:primary", line)
        self.assertNotIn("delivery_ok=True", line)

    async def test_no_output_line_reports_provider_versus_usable(self):
        task = self._task(TaskState.NO_OUTPUT)
        task.runtime.update(
            {
                "provider_output_count": 2,
                "usable_output_count": 0,
                "generation_success": False,
                "attempt_errors": ["node_a:model:ProviderError"],
            }
        )
        manifest, recorder = await self._finalize(task)
        self.assertEqual(manifest["attempts"]["succeeded"], {})
        self.assertEqual(manifest["provider_output_count"], 2)
        self.assertEqual(manifest["usable_output_count"], 0)
        line = [text for text in recorder.lines("warning") if "终态" in text][0]
        self.assertIn("终态 no_output", line)
        self.assertIn("provider=2", line)
        self.assertIn("usable=0", line)
        self.assertTrue(line.endswith("success=-"), line)

    async def test_success_facts_are_bounded_and_carry_no_identity(self):
        task = self._task(TaskState.SUCCEEDED)
        task.runtime.update(
            {
                "provider_output_count": 1,
                "usable_output_count": 1,
                "generation_success": True,
                "image_delivery_attempted": True,
                "image_delivery_success": True,
                "succeeded": {
                    "provider_id": "node " + "x" * 120,
                    "model": "sk-live?token=abc123 https://internal.example/x",
                    "mode": "normal",
                    "route": "primary",
                    "model_basis": "dispatch",
                },
                "attempt_errors": [],
            }
        )
        manifest, recorder = await self._finalize(task)
        line = [text for text in recorder.lines("info") if "终态" in text][0]
        self.assertNotIn("abc123", line)
        self.assertNotIn("internal.example", line)
        self.assertNotIn("umo-secret-7f3a", line)
        self.assertLessEqual(len(line), 400)
        succeeded = manifest["attempts"]["succeeded"]
        self.assertLessEqual(len(succeeded["provider_id"]), 64)
        self.assertLessEqual(len(succeeded["model"]), 64)
        self.assertNotIn("umo-secret-7f3a", json.dumps(manifest, ensure_ascii=False))


class SafeLabelTests(unittest.TestCase):
    """5) 标签投影：脱敏 + 单行 + 有界，但不吃掉模型名里的版本号。"""

    def test_projects_credentials_urls_paths_and_control_characters(self):
        from imago_fusion.core.security import safe_label

        label = safe_label(
            "node sk-live?token=abc123 https://internal.example/x /root/secret/a.png\nafter",
            64,
        )
        self.assertNotIn("abc123", label)
        self.assertIn("<redacted>", label)
        self.assertNotIn("https://", label)
        self.assertNotIn("/root/secret", label)
        self.assertNotIn("\n", label)
        self.assertLessEqual(len(label), 64)

    def test_keeps_model_version_digits_and_truncates_long_labels(self):
        from imago_fusion.core.security import safe_label

        # redact_debug 会把这种 8 位数字串换成 <id>，成功记账必须保留它。
        self.assertEqual(safe_label("model-20250901"), "model-20250901")
        self.assertEqual(len(safe_label("x" * 200)), 64)
        self.assertEqual(safe_label(None), "")


if __name__ == "__main__":
    unittest.main()
