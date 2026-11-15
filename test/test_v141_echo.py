"""v1.4.1 C-ECHO：严格同像素回吐守卫（T-E01…T-E07 + 真 ``_finish_task``）。

判定口径（contract rev2 §4.2/§4.3，队长 2026-11-15 复核）：

* 命中 = 候选与参考图**字节完全相同**，或 EXIF 方向归一后**同尺寸**且
  规范化 RGBA ``tobytes()`` **逐字节相等**（保留 alpha，不做灰度/网格/MAD 近似）；
* 判不了（损坏、超像素上限、无 Pillow）记 ``unknown`` 并**放行**；
* 比较集只能是成功 attempt 实际发出的 ``ImageInput`` 快照（含压缩副本）；
* 被拦的 provider 原件保留在 ``outputs/``，但不进 ``output_paths``/manifest
  ``outputs[]``，也不触发增强/放大；全拦 ⇒ ``no_output`` + ``no_change`` + 冻结文案，
  混合 ⇒ 只发有效图并落 ``PARTIAL_SUCCESS``。
"""

import io
import json
import random
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from imago_fusion.core import echo as echo_guard
from imago_fusion.core.models import (
    DrawTask,
    GenerationRequest,
    ImageInput,
    ImageResult,
    TaskState,
)
from imago_fusion.services.persona_store import PersonaStore

def _plugin_class():
    """**惰性**导入插件主类：收集阶段不碰 ``astrbot`` 真模块。

    ``test_native_adapter.py`` 在模块导入时就把 ``astrbot.api`` 换成桩，而同进程
    里 ``test_runtime_hook.py`` 又会把桩摘掉换成真包；在本文件顶部 import
    ``imago_fusion.main`` 会让这两种状态互相污染（表现为 astrbot DB 表重复定义）。
    """

    from imago_fusion.main import ImagoFusion

    return ImagoFusion


PNG_MIME = "image/png"


def _pattern(width=320, height=240, seed=0, mode="RGB"):
    """噪声图：保证"重新编码后一定还有压缩空间"，避免小图直接走 ``no_gain``。"""
    channels = 4 if mode == "RGBA" else 3
    rng = random.Random(seed)
    payload = bytes(rng.randrange(256) for _ in range(width * height * channels))
    return Image.frombytes(mode, (width, height), payload)


def png_bytes(width=64, height=48, seed=0, mode="RGB", **save):
    buffer = io.BytesIO()
    _pattern(width, height, seed, mode).save(buffer, format="PNG", **save)
    return buffer.getvalue()


def reencode(payload, *, format="PNG", **save):
    with Image.open(io.BytesIO(payload)) as image:
        image.load()
        buffer = io.BytesIO()
        target = image if format != "JPEG" else image.convert("RGB")
        target.save(buffer, format=format, **save)
        return buffer.getvalue()


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.baseline = echo_guard.EchoBaseline.of([ImageInput(png_bytes(), PNG_MIME, "ref.png")])

    def test_identical_bytes_is_same(self):
        verdict = echo_guard.classify(png_bytes(), self.baseline)
        self.assertEqual(verdict, echo_guard.VERDICT_SAME)

    def test_same_pixels_different_encoding_is_same(self):
        verdict = echo_guard.classify(reencode(png_bytes(), compress_level=1), self.baseline)
        self.assertEqual(verdict, echo_guard.VERDICT_SAME)

    def test_same_pixels_but_jpeg_lossy_differs(self):
        verdict = echo_guard.classify(reencode(png_bytes(), format="JPEG", quality=90), self.baseline)
        self.assertEqual(verdict, echo_guard.VERDICT_DIFFERS)

    def test_different_pixels_same_size_differs(self):
        verdict = echo_guard.classify(png_bytes(seed=1), self.baseline)
        self.assertEqual(verdict, echo_guard.VERDICT_DIFFERS)

    def test_different_size_differs(self):
        verdict = echo_guard.classify(png_bytes(width=32, height=24), self.baseline)
        self.assertEqual(verdict, echo_guard.VERDICT_DIFFERS)

    def test_alpha_is_not_flattened(self):
        transparent = png_bytes(mode="RGBA", seed=3)
        baseline = echo_guard.EchoBaseline.of([ImageInput(transparent, PNG_MIME, "a.png")])
        with Image.open(io.BytesIO(transparent)) as image:
            flat = image.convert("RGB")
        buffer = io.BytesIO()
        flat.save(buffer, format="PNG")
        # 丢掉 alpha 后像素会变成"白底"，必须判成 DIFFERS，不能靠 convert("RGB") 命中。
        self.assertEqual(echo_guard.classify(buffer.getvalue(), baseline), echo_guard.VERDICT_DIFFERS)

    def test_undecodable_candidate_is_unknown(self):
        verdict = echo_guard.classify(b"\x89PNG\r\n\x1a\nbroken", self.baseline)
        self.assertEqual(verdict, echo_guard.VERDICT_UNKNOWN)

    def test_all_baselines_undecodable_is_unknown(self):
        baseline = echo_guard.EchoBaseline.of([ImageInput(b"not-an-image", PNG_MIME, "x.png")])
        verdict = echo_guard.classify(png_bytes(), baseline)
        self.assertEqual(verdict, echo_guard.VERDICT_UNKNOWN)

    def test_guard_state_counts_and_summary(self):
        state = echo_guard.new_guard_state()
        for verdict, size in (
            (echo_guard.VERDICT_SAME, 100),
            (echo_guard.VERDICT_DIFFERS, 200),
            (echo_guard.VERDICT_UNKNOWN, 300),
        ):
            echo_guard.record(
                state, verdict, filename=f"output-{size}.png", size=size,
                data=png_bytes() if verdict != echo_guard.VERDICT_UNKNOWN else b"",
            )
        self.assertEqual(state["checked"], 3)
        self.assertEqual(state["blocked"], 1)
        self.assertEqual(state["unknown"], 1)
        self.assertEqual(state["blocked_files"][0]["file"], "output-100.png")
        self.assertEqual(echo_guard.passed_count(state), 2)
        summary = echo_guard.summarize(state)
        self.assertIn("检查=3", summary)
        self.assertIn("拦下=1", summary)
        self.assertIn("未知=1", summary)


class _Scheduler:
    def __init__(self):
        self.accepting = True
        self.stages = []
        self.tasks = {}

    def set_stage(self, task, stage):
        task.stage = stage
        self.stages.append(stage)


class _Counter:
    def __init__(self):
        self.calls = 0

    async def apply(self, task, value, output_dir, config, state):
        self.calls += 1
        return value


class _Outcome:
    """``SendOutcome`` 的最小替身：不 import ``integrations.active_send``，避免把
    astrbot 运行期真实模块拉进测试进程（同进程混跑会与既有桩互相污染）。"""

    def __init__(self, success=True, error=""):
        self.success = success
        self.error = error
        self.side_effects_started = True
        self.side_send_started = True
        self.side_send_error = ""


class _Sender:
    def __init__(self):
        self.chain = None

    async def send(self, umo, event, chain, before_send=None):
        if before_send is not None:
            before_send()
        self.chain = list(chain)
        return _Outcome(True)


class _Event:
    def get_sender_id(self):
        return "1"


class FinishTaskTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, directory, *, delivered):
        plugin = object.__new__(_plugin_class())
        plugin.raw_config = {}
        plugin.session = None
        plugin.store = PersonaStore(Path(directory), lambda: 20 * 1024 * 1024)
        plugin.scheduler = _Scheduler()
        plugin.enhancer = _Counter()
        plugin.upscaler = _Counter()
        plugin.sender = _Sender()
        plugin.quota_store = None
        plugin._delivered = delivered
        return plugin

    def _task(self, plugin, *, references):
        task = DrawTask("c" * 32, "umo", GenerationRequest("draw", count=2))
        task.owner_user_id = "1"
        task.created_at = __import__("time").monotonic()
        task.runtime["source_event"] = _Event()
        task.runtime["original_user_prompt"] = "draw"
        task.runtime["delivered_references"] = list(references)
        return task

    async def test_all_echoed_blocks_every_image(self):
        payload = png_bytes()
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory, delivered=[ImageInput(payload, PNG_MIME, "ref.png")])
            task = self._task(plugin, references=[ImageInput(payload, PNG_MIME, "ref.png")])
            outcome = await plugin._finish_task(
                task, [ImageResult(data=payload, mime_type=PNG_MIME), ImageResult(data=png_bytes(), mime_type=PNG_MIME)],
            )
            state = task.runtime["echo_guard"]
            outputs = plugin.store.task_dir(task.id) / "outputs"
            kept = [path for path in outputs.iterdir() if path.is_file()]
        self.assertFalse(outcome.generation_success)
        self.assertEqual(outcome.usable_output_count, 0)
        self.assertEqual(task.errors, ["no_change"])
        self.assertEqual((state["checked"], state["blocked"], state["unknown"]), (2, 2, 0))
        self.assertTrue(state["no_change"])
        # 一次都不花钱：增强/放大都没被调用。
        self.assertEqual(plugin.enhancer.calls, 0)
        self.assertEqual(plugin.upscaler.calls, 0)
        self.assertEqual([type(item).__name__ for item in plugin.sender.chain], ["Plain"])
        self.assertIn("这次没有生成新图", plugin.sender.chain[0].text)
        # provider 原件仍留在 outputs/，但一条也没进 output_paths。
        self.assertEqual(len(kept), 2)

    async def test_mixed_echo_delivers_only_valid_images(self):
        payload = png_bytes()
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory, delivered=[ImageInput(payload, PNG_MIME, "ref.png")])
            task = self._task(plugin, references=[ImageInput(payload, PNG_MIME, "ref.png")])
            outcome = await plugin._finish_task(
                task, [ImageResult(data=payload, mime_type=PNG_MIME), ImageResult(data=png_bytes(seed=9), mime_type=PNG_MIME)],
            )
            state = task.runtime["echo_guard"]
            manifest = json.loads((plugin.store.task_dir(task.id) / "task.json").read_text("utf-8")) \
                if (plugin.store.task_dir(task.id) / "task.json").exists() else {}
        self.assertTrue(outcome.generation_success)
        self.assertEqual(outcome.usable_output_count, 1)
        self.assertEqual(task.errors, ["no_change"])
        self.assertEqual((state["checked"], state["blocked"]), (2, 1))
        self.assertEqual(plugin.enhancer.calls, 1)
        self.assertEqual(plugin.upscaler.calls, 1)
        self.assertNotIn("unchanged", [type(item).__name__ for item in plugin.sender.chain])
        self.assertIsInstance(manifest, dict)

    async def test_different_pixels_are_not_blocked(self):
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory, delivered=[ImageInput(png_bytes(), PNG_MIME, "ref.png")])
            task = self._task(plugin, references=[ImageInput(png_bytes(), PNG_MIME, "ref.png")])
            outcome = await plugin._finish_task(task, [ImageResult(data=png_bytes(seed=5), mime_type=PNG_MIME)])
        self.assertTrue(outcome.generation_success)
        self.assertEqual(task.errors, [])
        self.assertEqual(task.runtime["echo_guard"]["blocked"], 0)

    async def test_undecodable_candidate_is_passed_through(self):
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory, delivered=[ImageInput(png_bytes(), PNG_MIME, "ref.png")])
            task = self._task(plugin, references=[ImageInput(png_bytes(), PNG_MIME, "ref.png")])
            outcome = await plugin._finish_task(task, [ImageResult(data=b"\x00not-an-image", mime_type=PNG_MIME)])
        # 判不了 ⇒ 记 unknown 并放行（原件照常交付），绝不因为"解码失败"拦图。
        self.assertTrue(outcome.generation_success)
        state = task.runtime["echo_guard"]
        self.assertEqual(state.get("blocked", 0), 0)
        self.assertEqual(state.get("unknown", 1), 1)

    async def test_no_delivered_snapshot_means_no_echo_work(self):
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory, delivered=[])
            task = self._task(plugin, references=[])
            task.runtime.pop("delivered_references")
            outcome = await plugin._finish_task(task, [ImageResult(data=png_bytes(), mime_type=PNG_MIME)])
        self.assertTrue(outcome.generation_success)
        self.assertNotIn("echo_guard", task.runtime)
        self.assertNotIn("_echo_baseline", task.runtime)

    async def test_compressed_reference_snapshot_is_the_baseline(self):
        """基线是成功 attempt 实际发出的**压缩副本**字节：回吐压缩副本也算命中。"""
        from imago_fusion.core import compress as logic
        from imago_fusion.core.models import ReferenceCompressConfig

        source = png_bytes(320, 240, seed=4)
        outgoing, _ = logic.compress_all(
            [ImageInput(source, PNG_MIME, "ref.png")],
            ReferenceCompressConfig(max_edge=80, jpeg_quality=60, target_kb=64),
        )
        sent = outgoing[0]
        self.assertNotEqual(sent.data, source)
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory, delivered=[sent])
            task = self._task(plugin, references=[sent])
            outcome = await plugin._finish_task(task, [ImageResult(data=sent.data, mime_type=sent.mime_type)])
        self.assertFalse(outcome.generation_success)
        self.assertEqual(task.errors, ["no_change"])


class DiagnosticsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    """echo 结论既进 INFO，也持久化进 ``task.json``（契约 §4.1/§4.6，t9 纠偏）。"""

    async def test_echo_summary_is_logged_and_persisted(self):
        from unittest.mock import patch

        payload = png_bytes()
        lines = []

        class _Logger:
            def info(self, message, *args):
                lines.append(message % args if args else message)

            def warning(self, message, *args):  # pragma: no cover
                pass

            def error(self, message, *args):  # pragma: no cover
                pass

            def debug(self, message, *args):  # pragma: no cover
                pass

        with TemporaryDirectory() as directory:
            plugin = object.__new__(_plugin_class())
            plugin.raw_config = {}
            plugin.session = None
            plugin.store = PersonaStore(Path(directory), lambda: 20 * 1024 * 1024)
            plugin.scheduler = _Scheduler()
            plugin.enhancer = _Counter()
            plugin.upscaler = _Counter()
            plugin.sender = _Sender()
            plugin.quota_store = None
            task = DrawTask("e" * 32, "umo", GenerationRequest("draw", count=1))
            task.runtime["usable_output_count"] = 0
            task.owner_user_id = "1"
            task.created_at = __import__("time").monotonic()
            task.runtime["source_event"] = _Event()
            task.runtime["original_user_prompt"] = "draw"
            task.runtime["delivered_references"] = [ImageInput(payload, PNG_MIME, "ref.png")]
            with patch("imago_fusion.main.logger", _Logger()):
                await plugin._finish_task(task, [ImageResult(data=payload, mime_type=PNG_MIME)])
                # 闸门不改终态（契约 §4.4）：终态由调度器按"是否有可用图"给出。
                self.assertEqual(task.runtime["usable_output_count"], 0)
                task.state = TaskState.NO_OUTPUT
                await plugin._finalize_task(task)
            manifest = json.loads((plugin.store.task_dir(task.id) / "task.json").read_text("utf-8"))
        summary = [line for line in lines if "回吐闸" in line]
        self.assertTrue(summary)
        self.assertIn("检查=1", summary[0])
        self.assertIn("拦下=1", summary[0])
        self.assertIn("未知=0", summary[0])
        guard = manifest["echo_guard"]
        self.assertEqual((guard["checked"], guard["blocked"], guard["unknown"]), (1, 1, 0))
        self.assertTrue(guard["no_change"])
        self.assertEqual(len(guard["blocked_files"]), 1)
        self.assertEqual(manifest["state"], TaskState.NO_OUTPUT.value)
        self.assertIn("no_change", manifest["errors"])
        # 只落相对文件名与体积/尺寸，不落图片内容与绝对路径。
        self.assertNotIn(str(Path(plugin.store.task_dir(task.id))), json.dumps(guard, ensure_ascii=False))
        self.assertNotIn("iVBORw0KGgo", json.dumps(manifest, ensure_ascii=False))

    async def test_passed_and_references_sent_are_persisted(self):
        """``passed = checked - blocked``；``references_sent`` 区分"不需要比"（验收 9/10）。"""
        from unittest.mock import patch

        first, second = png_bytes(seed=0), png_bytes(seed=1)

        class _Logger:
            def __init__(self):
                self.lines = []

            def info(self, message, *args):
                self.lines.append(message % args if args else message)

            def warning(self, message, *args):  # pragma: no cover
                pass

            def error(self, message, *args):  # pragma: no cover
                pass

            def debug(self, message, *args):  # pragma: no cover
                pass

        logger = _Logger()
        with TemporaryDirectory() as directory:
            plugin = object.__new__(_plugin_class())
            plugin.raw_config = {}
            plugin.session = None
            plugin.store = PersonaStore(Path(directory), lambda: 20 * 1024 * 1024)
            plugin.scheduler = _Scheduler()
            plugin.enhancer = _Counter()
            plugin.upscaler = _Counter()
            plugin.sender = _Sender()
            plugin.quota_store = None
            task = DrawTask("f" * 32, "umo", GenerationRequest("draw", count=2))
            task.runtime["source_event"] = _Event()
            task.runtime["original_user_prompt"] = "draw"
            task.runtime["delivered_references"] = [ImageInput(first, PNG_MIME, "ref.png")]
            with patch("imago_fusion.main.logger", logger):
                # 一张回吐（拦下）、一张真实新图（放行）⇒ checked=2, blocked=1, passed=1。
                await plugin._finish_task(
                    task,
                    [
                        ImageResult(data=first, mime_type=PNG_MIME),
                        ImageResult(data=second, mime_type=PNG_MIME),
                    ],
                )
                task.state = TaskState.PARTIAL_SUCCESS
                await plugin._finalize_task(task)
            manifest = json.loads((plugin.store.task_dir(task.id) / "task.json").read_text("utf-8"))
        guard = manifest["echo_guard"]
        self.assertEqual(guard["checked"], 2)
        self.assertEqual(guard["blocked"], 1)
        self.assertEqual(guard["passed"], 1)
        self.assertEqual(guard["passed"], guard["checked"] - guard["blocked"])
        self.assertEqual(guard["references_sent"], 1)
        summary = [line for line in logger.lines if "回吐闸" in line][0]
        self.assertIn("放行=1", summary)
        self.assertIn("未知=0", summary)

    async def test_text_only_snapshot_logs_and_persists_zero_references(self):
        """纯文字降级：闸门跑了但没发参考图 ⇒ ``references_sent=0``，仍要留痕（验收 10）。"""

        from unittest.mock import patch

        class _Logger:
            def __init__(self):
                self.lines = []

            def info(self, message, *args):
                self.lines.append(message % args if args else message)

            def warning(self, message, *args):  # pragma: no cover
                pass

            def error(self, message, *args):  # pragma: no cover
                pass

            def debug(self, message, *args):  # pragma: no cover
                pass

        logger = _Logger()
        payload = png_bytes()
        with TemporaryDirectory() as directory:
            plugin = object.__new__(_plugin_class())
            plugin.raw_config = {}
            plugin.session = None
            plugin.store = PersonaStore(Path(directory), lambda: 20 * 1024 * 1024)
            plugin.scheduler = _Scheduler()
            plugin.enhancer = _Counter()
            plugin.upscaler = _Counter()
            plugin.sender = _Sender()
            plugin.quota_store = None
            task = DrawTask("c" * 32, "umo", GenerationRequest("draw", count=1))
            task.runtime["source_event"] = _Event()
            task.runtime["original_user_prompt"] = "draw"
            # 文字降级成功的哨兵：显式空快照，不借旧图硬拦。
            task.runtime["delivered_references"] = []
            task.runtime["delivered_references_text"] = True
            with patch("imago_fusion.main.logger", logger):
                await plugin._finish_task(task, [ImageResult(data=payload, mime_type=PNG_MIME)])
                task.state = TaskState.SUCCEEDED
                await plugin._finalize_task(task)
            manifest = json.loads((plugin.store.task_dir(task.id) / "task.json").read_text("utf-8"))
        guard = manifest["echo_guard"]
        self.assertEqual(guard["checked"], 0)
        self.assertEqual(guard["blocked"], 0)
        self.assertEqual(guard["passed"], 0)
        self.assertEqual(guard["references_sent"], 0)
        summary = [line for line in logger.lines if "回吐闸" in line]
        self.assertTrue(summary)
        self.assertIn("references_sent=0", summary[0])
        # 空快照不能反过来把真实成品拦掉。
        self.assertEqual(task.runtime["usable_output_count"], 1)
        self.assertNotIn("no_change", manifest["errors"])


if __name__ == "__main__":
    unittest.main()
