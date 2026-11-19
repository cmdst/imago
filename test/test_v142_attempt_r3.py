"""IMAGO·FUSION v1.4.2 rev3 定向回归：R2-06（t20）。

真实 QA/复审现场：短模型名能正常给出"人格参考图被节点限额挤占"的提示；
106 字符的模型名写进胜出记录时被 ``safe_label`` 截到 64，``_success_reference_plan``
再按**显示字符串**去逐腿记录里找，逐字不相等 ⇒ 真挤占却静默无提示。

本文件钉死的不变量：

1. **按任务内本地标识关联**：胜出腿那条逐腿记录的 ``seq`` 由调度器写进
   ``runtime["succeeded_reference_plan_seq"]``，finalizer 只认这个标识 ——
   显示标签被截断、前缀相同、甚至撞标签都不影响判定；长（106）与短模型名在
   完全相同的真实条件下行为一致。
2. **只看胜出腿**：首腿挤占、实际胜出的是未挤占的备用节点/模型 ⇒ 不提示。
3. **既有边界不退化**：文字腿 0/0 不是挤占；缺任务信息/无输出不提示；群聊开 @
   但无可信目标（严格纯图）仍静默；私聊/关 @ 照旧可提示。
4. **不扩大观测面**：本地标识只写 runtime，不进 ``attempts.succeeded``，也不进
   终态 INFO；清单里的节点/模型标签仍按既有上限投影，长度不因本修而变长。

全程离线：真实调度链 + 合成 adapter，无网络、无真实 Provider 调用。
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from imago_fusion.core.errors import ProviderError
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
from imago_fusion.services.persona_store import PersonaStore
from imago_fusion.services.scheduler import TaskScheduler

UMO = "aiocqhttp:GroupMessage:880088"
PNG_MIME = "image/png"
PNG_BYTES = b"\x89PNG\r\n\x1a\n-fake-bytes"

#: 审阅现场用的 106 字符模型名（``safe_label`` 只留前 64）。
LONG_MODEL = "web-gemini-image-" + "x" * 89
SHORT_MODEL = "m1"
assert len(LONG_MODEL) == 106


def _plugin_class():
    """惰性导入插件主类：收集阶段不碰 ``astrbot`` 真模块（同 v141/v142 的理由）。"""

    from imago_fusion.main import ImagoFusion

    return ImagoFusion


def _provider(provider_id="node-a", *, limit=0, models=()):
    chain = tuple(models) or ("model",)
    return ProviderConfig(
        provider_id,
        "custom_endpoint",
        "https://example.invalid/v1",
        ("key",),
        model=chain[0],
        available_models=chain[1:],
        reference_image_limit=limit,
        timeout=30,
    )


def _runtime(providers):
    return RuntimeConfig(
        providers=tuple(providers),
        generation_timeout=120,
        vision_provider_id="vision",
        reference_fallback=ReferenceFallbackConfig(
            enable=True, max_retries_per_node=1
        ),
    )


class _Adapter:
    """合成 adapter：按 ``behaviour(config, request)`` 决定这一腿的返回/异常。"""

    calls: list[tuple[str, str, int]] = []
    behaviour = staticmethod(lambda config, request: [ImageResult(data=b"image")])

    def __init__(self, config):
        self.config = config

    async def generate(self, session, request, key):
        type(self).calls.append(
            (self.config.id, self.config.model, len(request.references))
        )
        return type(self).behaviour(self.config, request)


def _image(seed=0):
    return ImageInput(PNG_BYTES + bytes([seed]), PNG_MIME, f"ref-{seed}.png")


def _persona_image(seed=100):
    return ImageInput(PNG_BYTES + bytes([seed]), PNG_MIME, f"persona-{seed}.png")


async def _captioner(*args, **kwargs):
    return "一段用来触发文字降级的画面描述"


class _SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._old_adapter = scheduler_module.ADAPTERS.get("custom_endpoint")
        scheduler_module.ADAPTERS["custom_endpoint"] = _Adapter
        _Adapter.calls = []
        _Adapter.behaviour = staticmethod(
            lambda config, request: [ImageResult(data=b"image")]
        )

    def tearDown(self):
        if self._old_adapter is None:
            scheduler_module.ADAPTERS.pop("custom_endpoint", None)
        else:
            scheduler_module.ADAPTERS["custom_endpoint"] = self._old_adapter

    def _scheduler(self, runtime, *, captioner=None):
        return TaskScheduler(
            lambda: runtime,
            None,
            lambda *_: True,
            lambda *_: None,
            lambda *_a: None,
            captioner=captioner,
        )

    def _task(self, *, explicit=1, persona=1, model_prompt="画一只猫"):
        task = DrawTask(
            "7" * 32,
            UMO,
            GenerationRequest(
                model_prompt,
                references=tuple(_image(seed=index) for index in range(explicit)),
            ),
        )
        task.created_at = time.monotonic()
        task.runtime["original_user_prompt"] = model_prompt
        task.runtime["persona_references"] = [
            _persona_image(seed=100 + index) for index in range(persona)
        ]
        task.runtime["persona_library_status"] = "ok"
        task.runtime["persona_reference_pool"] = persona
        return task

    async def _run(self, task, providers, *, captioner=None):
        scheduler = self._scheduler(_runtime(providers), captioner=captioner)
        try:
            return await scheduler._generate(task)
        finally:
            await scheduler.close()


# ======================================================================
# 1. 长/短显示标签在同样真实条件下行为一致（R2-06 的主红点）
# ======================================================================
class LocalIdentityLinkTests(_SchedulerTests):
    async def test_long_model_name_still_reports_the_winning_leg_trim(self):
        task = self._task()
        results = await self._run(task, (_provider("node-a", limit=1, models=(LONG_MODEL,)),))

        self.assertEqual(len(results), 1)
        succeeded = task.runtime["succeeded"]
        # 显示标签按既有上限截断（64）—— 正是它让"按标签匹配"失效。
        self.assertEqual(succeeded["model"], LONG_MODEL[:64])
        self.assertEqual(len(succeeded["model"]), 64)
        plan = task.runtime["reference_plans"][0]
        self.assertEqual(plan["model"], LONG_MODEL, "逐腿记录保留原始标签")
        self.assertEqual(plan["reason"], "limit_exhausted_by_explicit")
        # 本地标识把胜出腿与这条记录绑在一起。
        self.assertEqual(task.runtime["succeeded_reference_plan_seq"], plan["seq"])

        plugin = _plugin_class()
        self.assertEqual(
            plugin._persona_trim_notice(task),
            plugin.PERSONA_TRIM_NOTICE_EXHAUSTED,
            "真挤占必须提示，不能因显示标签被截断而静默",
        )

    async def test_short_model_name_matches_long_model_behaviour(self):
        plugin = _plugin_class()
        notices = {}
        for model in (SHORT_MODEL, LONG_MODEL):
            task = self._task()
            await self._run(task, (_provider("node-a", limit=1, models=(model,)),))
            notices[model] = plugin._persona_trim_notice(task)
            self.assertEqual(
                task.runtime["succeeded_reference_plan_seq"],
                task.runtime["reference_plans"][0]["seq"],
            )
        self.assertEqual(
            notices[SHORT_MODEL],
            notices[LONG_MODEL],
            "同样的真实条件，长短标签必须给出同一个判定",
        )
        self.assertEqual(notices[LONG_MODEL], plugin.PERSONA_TRIM_NOTICE_EXHAUSTED)

    async def test_backup_node_winner_without_trim_is_not_mislabelled(self):
        """首腿被挤占、实际胜出的是未挤占的备用节点 ⇒ 不提示（只看胜出腿）。"""

        def behaviour(config, request):
            if config.id == "node-a":
                raise ProviderError("boom", status=500)
            return [ImageResult(data=b"image")]

        _Adapter.behaviour = staticmethod(behaviour)
        task = self._task()
        results = await self._run(
            task,
            (
                _provider("node-a", limit=1, models=(LONG_MODEL,)),
                _provider("node-b", limit=2, models=(LONG_MODEL,)),
            ),
        )
        self.assertEqual(len(results), 1)
        plans = task.runtime["reference_plans"]
        self.assertEqual([item["reason"] for item in plans], [
            "limit_exhausted_by_explicit",
            "limit_covers_pool",
        ])
        self.assertEqual(task.runtime["succeeded"]["provider_id"], "node-b")
        self.assertEqual(
            task.runtime["succeeded_reference_plan_seq"], plans[-1]["seq"]
        )
        plugin = _plugin_class()
        self.assertEqual(plugin._persona_trim_notice(task), "")

    async def test_text_fallback_leg_is_zero_and_never_a_trim(self):
        def behaviour(config, request):
            if request.references:
                raise ProviderError("blocked", status=400, block_reason="SAFETY")
            return [ImageResult(data=b"image")]

        _Adapter.behaviour = staticmethod(behaviour)
        task = self._task()
        results = await self._run(
            task,
            (_provider("node-a", limit=1, models=(LONG_MODEL,)),),
            captioner=_captioner,
        )
        self.assertEqual(len(results), 1)
        succeeded = task.runtime["succeeded"]
        self.assertEqual(succeeded["mode"], "text_fallback")
        self.assertEqual(succeeded["model"], LONG_MODEL[:64])
        text_plan = task.runtime["reference_plans"][-1]
        self.assertEqual(text_plan["kind"], "text_fallback")
        self.assertEqual((text_plan["explicit_sent"], text_plan["persona_sent"]), (0, 0))
        self.assertEqual(
            task.runtime["succeeded_reference_plan_seq"], text_plan["seq"]
        )
        plugin = _plugin_class()
        self.assertEqual(
            plugin._persona_trim_notice(task), "", "文字腿 0/0 不是挤占"
        )


# ======================================================================
# 2. 本地标识优先、不猜；旧 runtime 保持标签兼容
# ======================================================================
class ReferenceLinkSemanticsTests(unittest.TestCase):
    def _plugin(self):
        return _plugin_class()

    def _task(self, *, succeeded=None, plans=None, seq=None):
        task = DrawTask("8" * 32, UMO, GenerationRequest("draw", count=1))
        if succeeded is not None:
            task.runtime["succeeded"] = dict(succeeded)
        if plans is not None:
            task.runtime["reference_plans"] = list(plans)
        if seq is not None:
            task.runtime["succeeded_reference_plan_seq"] = seq
        return task

    @staticmethod
    def _plan(reason, *, node="node-a", model="m1", kind="normal", seq=1):
        return {
            "node": node, "model": model, "kind": kind, "reference_limit": 1,
            "explicit_sent": 1, "persona_sent": 0, "persona_pool": 1,
            "persona_budget": 0, "library": "ok", "reason": reason, "seq": seq,
            "route": "primary", "node_index": 1, "node_count": 1,
            "model_index": 1, "model_attempts": 1,
        }

    def test_missing_or_no_output_never_notifies(self):
        plugin = self._plugin()
        self.assertEqual(plugin._persona_trim_notice(self._task()), "")
        trimmed_but_no_winner = self._task(
            plans=[self._plan("limit_exhausted_by_explicit")]
        )
        self.assertEqual(plugin._persona_trim_notice(trimmed_but_no_winner), "")

    def test_seq_is_authoritative_and_a_missing_record_is_not_guessed(self):
        plugin = self._plugin()
        task = self._task(
            succeeded={"provider_id": "node-a", "model": "m1", "mode": "normal"},
            plans=[self._plan("limit_exhausted_by_explicit", seq=1)],
            seq=99,
        )
        self.assertEqual(
            plugin._persona_trim_notice(task),
            "",
            "有本地标识却不指向现存记录时不得按标签猜（可能撞标签/裁切）",
        )

    def test_legacy_runtime_without_seq_still_matches_by_display_label(self):
        plugin = self._plugin()
        task = self._task(
            succeeded={"provider_id": "node-a", "model": "m1", "mode": "normal"},
            plans=[self._plan("limit_exhausted_by_explicit", seq=1)],
        )
        self.assertEqual(
            plugin._persona_trim_notice(task), plugin.PERSONA_TRIM_NOTICE_EXHAUSTED,
            "没有本地标识的旧形状仍需按显示标签兼容（既有用例依赖）",
        )

    def test_long_label_without_seq_cannot_be_guessed_back(self):
        """旧 runtime 的长标签本来就匹配不上：宁可无提示，也不能错配到别的腿。"""

        plugin = self._plugin()
        task = self._task(
            succeeded={
                "provider_id": "node-a",
                "model": LONG_MODEL[:64],
                "mode": "normal",
            },
            plans=[self._plan("limit_exhausted_by_explicit", model=LONG_MODEL, seq=1)],
        )
        self.assertEqual(plugin._persona_trim_notice(task), "")


# ======================================================================
# 3. 边界：纯图/私聊既有策略、清单与终态 INFO 不扩长
# ======================================================================
class BoundaryAndAccountingTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self):
        return _plugin_class()

    @staticmethod
    def _plan(reason, *, node="node-a", model="m1", kind="normal", seq=1):
        return {
            "node": node, "model": model, "kind": kind, "reference_limit": 1,
            "explicit_sent": 1, "persona_sent": 0, "persona_pool": 1,
            "persona_budget": 0, "library": "ok", "reason": reason, "seq": seq,
            "route": "primary", "node_index": 1, "node_count": 1,
            "model_index": 1, "model_attempts": 1,
        }

    def test_pure_image_and_private_boundaries_still_hold_for_long_labels(self):
        from imago_fusion.core.route_identity import RouteIdentity

        plugin = self._plugin()
        task = DrawTask("9" * 32, UMO, GenerationRequest("draw", count=1))
        task.runtime["succeeded"] = {
            "provider_id": "node-a", "model": LONG_MODEL[:64], "mode": "normal",
        }
        task.runtime["reference_plans"] = [self._plan("limit_exhausted_by_explicit")]
        task.runtime["succeeded_reference_plan_seq"] = 1
        self.assertEqual(
            plugin._persona_trim_notice(task), plugin.PERSONA_TRIM_NOTICE_EXHAUSTED
        )

        cfg = SimpleNamespace(at_trigger_user=True)
        group = RouteIdentity(message_type="GroupMessage", platform_name="aiocqhttp")
        friend = RouteIdentity(
            message_type="FriendMessage", platform_name="aiocqhttp",
            sender_user_id="10001",
        )
        self.assertTrue(
            plugin._pure_image_only_reply(
                cfg=cfg, identity=group, generation_success=True
            ),
            "群聊开 @ 但无可信目标 ⇒ 严格纯图，提示必须让位",
        )
        self.assertFalse(
            plugin._pure_image_only_reply(
                cfg=cfg, identity=friend, generation_success=True
            ),
            "私聊不受纯图分支影响",
        )

    async def _finalize(self, task, directory):
        plugin = object.__new__(self._plugin())
        plugin.raw_config = {}
        plugin.session = None
        plugin.store = PersonaStore(Path(directory), lambda: 20 * 1024 * 1024)
        plugin.quota_store = None
        plugin.scheduler = type(
            "S", (), {"tasks": {}, "accepting": True, "set_stage": lambda *_: None}
        )()
        recorder = _Recorder()
        with patch("imago_fusion.main.logger", recorder):
            await plugin._finalize_task(task)
        manifest = json.loads(
            (plugin.store.task_dir(task.id) / "task.json").read_text("utf-8")
        )
        return manifest, recorder

    async def test_manifest_and_terminal_info_keep_the_bounded_labels(self):
        task = DrawTask("a1b2c3d4e5f6a7b8", UMO, GenerationRequest("draw", count=1))
        task.created_at = time.monotonic()
        task.state = TaskState.SUCCEEDED
        task.stage = TaskStage.FINISHED
        task.runtime.update(
            {
                "succeeded": {
                    "provider_id": "node-a",
                    "model": LONG_MODEL[:64],
                    "mode": "normal",
                    "api_type": "custom_endpoint",
                    "model_basis": "dispatch",
                    "route": "primary",
                    "node_index": 1,
                    "node_count": 1,
                    "model_index": 1,
                    "model_attempts": 1,
                },
                "succeeded_reference_plan_seq": 1,
                "reference_plans": [
                    self._plan("limit_exhausted_by_explicit", model=LONG_MODEL)
                ],
                "provider_output_count": 1,
                "usable_output_count": 1,
                "generation_success": True,
            }
        )
        with TemporaryDirectory() as directory:
            manifest, recorder = await self._finalize(task, directory)

        succeeded = manifest["attempts"]["succeeded"]
        self.assertEqual(succeeded["model"], LONG_MODEL[:64], "清单仍是 64 字符上限")
        self.assertNotIn("succeeded_reference_plan_seq", succeeded)
        self.assertNotIn("seq", succeeded, "本地标识只活在 runtime，不进清单")
        # 逐腿记录按清单既有上限（48）投影，本修不扩大长度。
        self.assertEqual(manifest["reference_plans"][0]["model"], LONG_MODEL[:48])
        info = [line for line in recorder.lines("info") if "终态" in line]
        self.assertEqual(len(info), 1, recorder.all_lines())
        self.assertIn("success=node-a:" + LONG_MODEL[:64], info[0])
        self.assertNotIn(LONG_MODEL, info[0], "终态行不因本修而出现完整长标签")
        self.assertIn(manifest["attempts"]["succeeded"]["model"][:16], info[0])


class _Recorder:
    """记录各层日志行，按 ``%`` 实参格式化（与既有 v141/v142 夹具同形）。"""

    def __init__(self):
        self._lines = {"info": [], "warning": [], "error": [], "debug": []}

    def __getattr__(self, level):
        if level not in self._lines:
            raise AttributeError(level)
        bucket = self._lines[level]

        def emit(message, *args):
            bucket.append(message % args if args else str(message))

        return emit

    def lines(self, level):
        return list(self._lines[level])

    def all_lines(self):
        return [line for bucket in self._lines.values() for line in bucket]


if __name__ == "__main__":
    unittest.main()
