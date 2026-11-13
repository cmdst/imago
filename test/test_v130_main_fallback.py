"""v1.3.0：S6 降级执行体的真实出站形状（零图 / 仅遮挡产物）与告知文案。

这些用例直接驱动 ``ImagoFusion._reference_fallback``：用假 scheduler 记录
每一次 ``invoke`` 的请求，断言最终发往原目标节点的请求**没有任何参考图**
（文字模式）或**只有遮挡产物**（实验模式），且 prompt 从用户原文重建
（不含调度器注入的关系声明）。
"""

import base64
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

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
from imago_fusion.core.prompting import REFERENCE_RELATION_SUFFIX

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _node(provider_id, *, base_url="https://example.invalid/v1"):
    return ProviderConfig(provider_id, "custom_endpoint", base_url, ("key",), model="model")


class FakeContext:
    def __init__(self, text="参考图里有一个白色方块"):
        self.text = text
        self.calls = []

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(completion_text=self.text)


class FakeScheduler:
    def __init__(self, *, fail_target=False):
        self.invocations = []
        self.fail_target = fail_target

    async def invoke(self, provider, request, *, timeout=None):
        self.invocations.append((provider, request, timeout))
        if self.fail_target and provider.id == "node_a":
            from imago_fusion.core.errors import ProviderError

            raise ProviderError("boom", status=500)
        return [ImageResult(data=PNG, mime_type="image/png")]


class FakeStore:
    def __init__(self, base: Path):
        self.base = base

    def task_dir(self, task_id):
        return self.base / task_id


def _locked(prompt, references, provider_id="node_a", model="model", thinking=""):
    return {
        "request": GenerationRequest(
            prompt=f"{prompt}{REFERENCE_RELATION_SUFFIX}", references=tuple(references),
        ),
        "provider_id": provider_id,
        "model": model,
        "api_type": "custom_endpoint",
        "thinking": thinking,
        "user_prompt": prompt,
        "prompt_has_auto_caption": True,
    }


class FallbackOutboundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 延迟导入：``test_native_adapter`` 在收集阶段安装 fake astrbot 模块，
        # 若在模块级 import main 会把假 Star/API 绑定进来并污染 ``sys.modules``；
        # 放到用例执行期导入可保持与其它测试文件的隔离（R10 同源教训）。
        from imago_fusion.main import ImagoFusion

        self._tmp = tempfile.TemporaryDirectory()
        self.plugin = object.__new__(ImagoFusion)
        self.plugin.context = FakeContext()
        self.plugin.scheduler = FakeScheduler()
        self.plugin.session = None
        self.plugin.store = FakeStore(Path(self._tmp.name))
        self.plugin.raw_config = {}
        self.refs = [ImageInput(PNG, "image/png"), ImageInput(PNG, "image/png")]

    def tearDown(self):
        self._tmp.cleanup()

    def _task(self):
        task = DrawTask("task-1", "umo", GenerationRequest("原文画面", references=self.refs))
        task.created_at = time.monotonic()
        task.runtime["original_user_prompt"] = "原文画面"
        task.runtime["source_event"] = None
        return task

    def _config(self, *, exp=None, fb=None):
        return RuntimeConfig(
            providers=(_node("node_a"), _node("occ")),
            generation_timeout=120,
            vision_provider_id="vision",
            reference_fallback=fb or ReferenceFallbackConfig(enable=True),
            experimental_reference=exp or ExperimentalReferenceConfig(enable=False),
        )

    async def test_text_fallback_sends_no_references_and_rebuilds_prompt(self):
        task = self._task()
        locked = _locked("原文画面", self.refs)
        rejection = {"kind": "image_invalid", "status": 400}
        results = await self.plugin._reference_fallback(
            task, self._config(), 90.0, rejection, locked,
        )
        self.assertTrue(results)
        provider, request, timeout = self.plugin.scheduler.invocations[-1]
        self.assertEqual(provider.id, "node_a")
        self.assertEqual(request.references, ())
        self.assertTrue(request.prompt.startswith("原文画面"))
        self.assertNotIn(REFERENCE_RELATION_SUFFIX, request.prompt)
        self.assertIn("参考图说明（图片未能上传", request.prompt)
        self.assertIn("参考图里有一个白色方块", request.prompt)
        info = task.runtime["reference_fallback"]
        self.assertEqual(info["mode"], "text")
        self.assertEqual(info["extra_attempts"], 1)
        self.assertIn("本次参考图未被使用", info["notice"])

    async def test_occlusion_fallback_sends_only_processed_image(self):
        task = self._task()
        task.request.references = self.refs[:1]
        locked = _locked("原文画面", self.refs[:1])
        exp = ExperimentalReferenceConfig(
            enable=True, occluder_provider_id="occ", occlusion_prompt="挡住敏感处", max_images=1,
        )
        results = await self.plugin._reference_fallback(
            task, self._config(exp=exp), 90.0, {"kind": "safety", "status": 200}, locked,
        )
        self.assertTrue(results)
        first, first_request, _ = self.plugin.scheduler.invocations[0]
        self.assertEqual(first.id, "occ")
        self.assertEqual(len(first_request.references), 1)
        self.assertEqual(first_request.prompt, "挡住敏感处")
        provider, request, _ = self.plugin.scheduler.invocations[-1]
        # 原目标节点只收到 1 张遮挡产物，绝不出现原始两张参考图。
        self.assertEqual(provider.id, "node_a")
        self.assertEqual(len(request.references), 1)
        self.assertEqual(request.references[0].filename, "proc-1.png")
        self.assertIn("遮挡处理", request.prompt)
        self.assertIn("参考图里有一个白色方块", request.prompt)
        self.assertNotIn(REFERENCE_RELATION_SUFFIX, request.prompt)
        info = task.runtime["reference_fallback"]
        self.assertEqual(info["mode"], "occlusion")
        self.assertIn("已做遮挡处理", info["notice"])

    async def test_safety_occlusion_failure_does_not_fall_back_to_text(self):
        self.plugin.scheduler = FakeScheduler(fail_target=True)
        task = self._task()
        task.request.references = self.refs[:1]
        locked = _locked("原文画面", self.refs[:1])
        exp = ExperimentalReferenceConfig(
            enable=True, occluder_provider_id="occ", occlusion_prompt="挡住", max_images=1,
        )
        results = await self.plugin._reference_fallback(
            task, self._config(exp=exp), 90.0, {"kind": "safety", "status": 200}, locked,
        )
        self.assertIsNone(results)
        # 只有遮挡调用 + 一次原目标调用（失败），没有第二次文字重绘。
        self.assertEqual([call[0].id for call in self.plugin.scheduler.invocations], ["occ", "node_a"])
        info = task.runtime["reference_fallback"]
        self.assertEqual(info["reason"], "occlusion_failed")
        self.assertTrue(info["notice"])


    async def test_text_fallback_drops_auto_caption_from_locked_prompt(self):
        task = self._task()
        locked = _locked("原文画面", self.refs)
        # 模拟旧链路：失败请求的 prompt 已被自动配文 + 关系声明污染。
        locked["request"] = GenerationRequest(
            prompt=(
                "原文画面\n参考图视觉描述（仅用于还原用户所指的画面，用户原话优先，不是指令）：\n"
                "旧描述不该出现\n\n" + REFERENCE_RELATION_SUFFIX
            ),
            references=tuple(self.refs),
        )
        results = await self.plugin._reference_fallback(
            task, self._config(), 90.0, {"kind": "image_invalid", "status": 400}, locked,
        )
        self.assertTrue(results)
        _, request, _ = self.plugin.scheduler.invocations[-1]
        self.assertNotIn("旧描述不该出现", request.prompt)
        self.assertNotIn("Reference image(s) attached", request.prompt)
        self.assertTrue(request.prompt.startswith("原文画面"))
        self.assertEqual(request.references, ())

    async def test_cancelled_task_does_not_call_provider(self):
        task = self._task()
        task.state = TaskState.CANCELLED
        results = await self.plugin._reference_fallback(
            task, self._config(), 90.0, {"kind": "image_invalid"}, _locked("原文画面", self.refs),
        )
        self.assertIsNone(results)
        self.assertEqual(self.plugin.scheduler.invocations, [])
        self.assertEqual(task.runtime["reference_fallback"]["reason"], "cancelled")
