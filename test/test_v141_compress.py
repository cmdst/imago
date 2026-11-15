"""v1.4.1 C-COMPRESS：参考图压缩的行为用例（T-C01…T-C15 + B1 真实 multipart）。

形状照抄 ``test/test_enhance.py``：纯逻辑用 :mod:`imago_fusion.core.compress`，
需要真实 ``ImageInput`` 的路径用插件实例（``object.__new__(_plugin_class())``，不碰
AstrBot 运行期）；**全程不联网、不写生产目录**。

覆盖的契约点：

* 关闭 ⇒ 零解码、零新增 runtime 键、发送字节与入站逐字节相同（§3.9）；
* ``min_bytes`` 之下零解码；``max_pixels`` 超限只读头不 ``load()``（§3.4）；
* 长边缩放、质量下限、``max_edge=0`` 禁止任何缩边、EXIF 方向归一；
* 透明白底 / ``keep_alpha`` 输出 PNG 的 mime 与文件名配对（§3.7）；
* ``target_unmet``、``no_gain``、损坏字节回退原图（§3.5 / §3.8）；
* 唯一公共入口 ``ingest_reference`` 与 ``compress_all`` 的统计形状（§3.12）；
* B1：``providers/openai_image.py::_edit_multipart`` 的 ``image[]`` 分片字节
  必须**等于压缩后的字节**（不是原字节、也不只看长度）。
"""

import io
import json
import random
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


from imago_fusion.core import compress as logic
from imago_fusion.core.models import (
    DrawTask,
    GenerationRequest,
    ImageInput,
    ReferenceCompressConfig,
    RuntimeConfig,
    TaskStage,
    TaskState,
)
from imago_fusion.providers.openai_image import OpenAIImageAdapter
from imago_fusion.core.models import ProviderConfig


def _plugin_class():
    """**惰性**导入插件主类：收集阶段不碰 ``astrbot`` 真模块。

    ``test_native_adapter.py`` 在模块导入时就把 ``astrbot.api`` 换成桩，而同进程
    里 ``test_runtime_hook.py`` 又会把桩摘掉换成真包；在本文件顶部 import
    ``imago_fusion.main`` 会让这两种状态互相污染（表现为 astrbot DB 表重复定义）。
    """

    from imago_fusion.main import ImagoFusion

    return ImagoFusion


def noise_png(width: int, height: int, seed: int = 0, mode: str = "RGB") -> bytes:
    """确定性噪声图：噪声保证"重新编码后一定还有压缩空间"。"""
    from PIL import Image

    rng = random.Random(seed)
    channels = 4 if mode == "RGBA" else 3
    payload = bytes(rng.randrange(256) for _ in range(width * height * channels))
    image = Image.frombytes(mode, (width, height), payload)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def solid_png(width: int, height: int, *, mode: str = "RGBA", color=(255, 0, 0, 0)) -> bytes:
    from PIL import Image

    image = Image.new(mode, (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def transparent_noise_png(width: int, height: int, seed: int = 0) -> bytes:
    """全透明的噪声图：既保证"重新编码一定有收益"，又能验证 alpha 处理。"""
    from PIL import Image

    rng = random.Random(seed)
    payload = bytearray(rng.randrange(256) for _ in range(width * height * 3))
    red = Image.frombytes("RGB", (width, height), bytes(payload))
    image = red.convert("RGBA")
    image.putalpha(0)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def orientation_jpeg(width: int, height: int, orientation: int = 6, seed: int = 7) -> bytes:
    """带 EXIF Orientation 的 JPEG（像素是宽 > 高，归一后应变成高 > 宽）。"""
    from PIL import Image

    rng = random.Random(seed)
    payload = bytes(rng.randrange(256) for _ in range(width * height * 3))
    image = Image.frombytes("RGB", (width, height), payload)
    exif = Image.Exif()
    exif[274] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=95, exif=exif)
    return buffer.getvalue()


def config(**overrides) -> ReferenceCompressConfig:
    options = {"enable": True, "min_bytes": 0, "target_kb": 2048}
    options.update(overrides)
    return ReferenceCompressConfig(**options)


class _NoDecode:
    """把 ``Image.open`` 换成会立刻失败的桩：用到解码的用例都不许碰它。"""

    def __init__(self):
        self.opened = 0

    def __call__(self, *args, **kwargs):
        self.opened += 1
        raise AssertionError("should not decode")


class DisabledTests(unittest.TestCase):
    def test_disabled_returns_original_without_touching_pillow(self):
        image = ImageInput(b"\x00" * 40000, "image/png", "a.png")
        calls = _NoDecode()
        with patch.object(logic, "Image") as fake:
            fake.open = calls
            outgoing, stats = logic.compress_all([image], config(enable=False))
        self.assertEqual(calls.opened, 0)
        self.assertIsNone(stats)
        self.assertEqual(len(outgoing), 1)
        self.assertIs(outgoing[0], image)

    def test_disabled_bytes_are_byte_identical(self):
        image = ImageInput(noise_png(200, 120), "image/png", "a.png")
        outgoing, stats = logic.compress_all([image], config(enable=False))
        self.assertIsNone(stats)
        self.assertEqual(outgoing[0].data, image.data)
        self.assertEqual(outgoing[0].mime_type, "image/png")
        self.assertEqual(outgoing[0].filename, "a.png")


class SkipTests(unittest.TestCase):
    def test_under_min_bytes_never_decodes(self):
        image = ImageInput(b"\x00" * 100, "image/png", "a.png")
        calls = _NoDecode()
        with patch.object(logic, "Image") as fake:
            fake.open = calls
            outgoing, stats = logic.compress_all([image], config(min_bytes=1000))
        self.assertEqual(calls.opened, 0)
        self.assertIs(outgoing[0], image)
        self.assertEqual(stats["skipped"], [{"index": 1, "reason": "under_min_bytes", "bytes": 100}])

    def test_pixel_cap_reads_header_only(self):
        class FakeOpened:
            size = (50000, 50000)

            def load(self):  # pragma: no cover - 被调用即失败
                raise AssertionError("must not load oversized image")

            def close(self):
                pass

        image = ImageInput(b"\x89PNG" + b"\x00" * 2000, "image/png", "huge.png")
        with patch.object(logic, "Image") as fake:
            fake.open = lambda *a, **k: FakeOpened()
            outgoing, stats = logic.compress_all([image], config(max_pixels=1_000_000))
        self.assertIs(outgoing[0], image)
        self.assertEqual(stats["skipped"][0]["reason"], "too_many_pixels")

    def test_corrupt_bytes_fall_back_to_original(self):
        image = ImageInput(b"\x00" * 5000, "image/png", "broken.png")
        outgoing, stats = logic.compress_all([image], config())
        self.assertIs(outgoing[0], image)
        self.assertEqual(stats["failed"][0]["reason"], "undecodable")
        self.assertEqual(stats["compressed"], 0)

    def test_no_gain_when_every_candidate_is_bigger(self):
        # 源图已经是低质量 JPEG：把质量档锁在源之上、且 max_edge=0 禁缩放，
        # 所有候选都比原图大 ⇒ 必须原样发送（不膨胀，§3.5）。
        from PIL import Image

        raw = io.BytesIO()
        Image.open(io.BytesIO(noise_png(200, 200, seed=12))).save(raw, format="JPEG", quality=25)
        image = ImageInput(raw.getvalue(), "image/jpeg", "already.jpg")
        outgoing, stats = logic.compress_all([image], config(max_edge=0, jpeg_quality=95))
        self.assertIs(outgoing[0], image)
        self.assertEqual([item["reason"] for item in stats["skipped"]], ["no_gain"])


class TransformTests(unittest.TestCase):
    def test_large_png_becomes_smaller_jpeg_with_edge_cap(self):
        source = noise_png(2400, 1600, seed=1)
        image = ImageInput(source, "image/png", "big.png")
        outgoing, stats = logic.compress_all([image], config(max_edge=1536, target_kb=2048))
        result = outgoing[0]
        self.assertLess(len(result.data), len(source))
        self.assertEqual(result.mime_type, "image/jpeg")
        self.assertTrue(result.filename.endswith(".jpg"))
        self.assertLessEqual(max(logic._open_first_frame(result.data).size), 1536)
        self.assertEqual(stats["compressed"], 1)

    def test_target_unmet_is_reported_but_image_still_smaller(self):
        source = noise_png(800, 600, seed=2)
        image = ImageInput(source, "image/png", "noise.png")
        outgoing, stats = logic.compress_all([image], config(max_edge=0, target_kb=32))
        self.assertLess(len(outgoing[0].data), len(source))
        self.assertTrue(stats["target_unmet"])
        self.assertEqual(stats["compressed"], 1)

    def test_alpha_is_flattened_on_white_by_default(self):
        source = transparent_noise_png(300, 300, seed=13)
        image = ImageInput(source, "image/png", "alpha.png")
        outgoing, _ = logic.compress_all([image], config(target_kb=2048))
        result = outgoing[0]
        self.assertEqual(result.mime_type, "image/jpeg")
        from PIL import Image

        decoded = Image.open(io.BytesIO(result.data))
        self.assertEqual(decoded.mode, "RGB")
        self.assertTrue(all(channel >= 250 for channel in decoded.getpixel((5, 5))))

    def test_keep_alpha_emits_png_with_alpha(self):
        source = transparent_noise_png(300, 300, seed=14)
        image = ImageInput(source, "image/png", "alpha.png")
        outgoing, _ = logic.compress_all([image], config(keep_alpha=True, target_kb=2048))
        result = outgoing[0]
        self.assertEqual(result.mime_type, "image/png")
        self.assertTrue(result.filename.endswith(".png"))
        from PIL import Image

        decoded = Image.open(io.BytesIO(result.data))
        self.assertEqual(decoded.mode, "RGBA")
        self.assertEqual(decoded.getpixel((5, 5))[3], 0)

    def test_exif_orientation_is_applied_before_sizing(self):
        source = orientation_jpeg(80, 40, orientation=6)
        image = ImageInput(source, "image/jpeg", "rotated.jpg")
        outgoing, _ = logic.compress_all([image], config(max_edge=1000, target_kb=2048))
        from PIL import Image

        decoded = Image.open(io.BytesIO(outgoing[0].data))
        self.assertLess(decoded.width, decoded.height)

    def test_max_edge_zero_never_scales(self):
        source = noise_png(1200, 800, seed=3)
        image = ImageInput(source, "image/png", "wide.png")
        outgoing, _ = logic.compress_all([image], config(max_edge=0, target_kb=2048))
        from PIL import Image

        decoded = Image.open(io.BytesIO(outgoing[0].data))
        self.assertEqual(decoded.size, (1200, 800))

    def test_compression_is_deterministic(self):
        source = noise_png(600, 400, seed=4)
        image = ImageInput(source, "image/png", "same.png")
        first, _ = logic.compress_all([image], config(max_edge=400, target_kb=64))
        second, _ = logic.compress_all([image], config(max_edge=400, target_kb=64))
        self.assertEqual(first[0].data, second[0].data)


class EntryTests(unittest.TestCase):
    def test_ingest_reference_pairs_mime_and_filename(self):
        payload, mime_type, filename, outcome = logic.ingest_reference(
            noise_png(400, 300, seed=5), "image/png", "shot.png", config(target_kb=2048)
        )
        self.assertEqual(mime_type, "image/jpeg")
        self.assertTrue(filename.endswith(".jpg"))
        self.assertEqual(len(payload), outcome.out_bytes)
        self.assertLessEqual(len(payload), len(noise_png(400, 300, seed=5)))

    def test_stats_shape_and_indexes(self):
        small = ImageInput(b"\x00" * 10, "image/png", "small.png")
        broken = ImageInput(b"\x01" * 4000, "image/png", "broken.png")
        good = ImageInput(noise_png(500, 400, seed=6), "image/png", "good.png")
        outgoing, stats = logic.compress_all([small, broken, good], config(min_bytes=1000))
        self.assertEqual(len(outgoing), 3)
        self.assertEqual(set(stats), {"in_bytes", "out_bytes", "compressed", "skipped", "failed", "target_unmet"})
        self.assertEqual(stats["skipped"][0]["index"], 1)
        self.assertEqual(stats["failed"][0]["index"], 2)
        self.assertEqual(stats["compressed"], 1)
        self.assertEqual(stats["in_bytes"], sum(len(item.data) for item in (small, broken, good)))


def _image_parts(form) -> list[bytes]:
    """从 aiohttp ``FormData`` 里取出所有 ``image[]`` 分片的真实字节。

    aiohttp 3.14 的 ``FormData._fields`` 是 ``(MultiDict(headers), extra, value)``
    三元组；这里按名字过滤后取第三项，得到**实际将要写进 multipart 的字节**。
    """
    parts: list[bytes] = []
    for field in form._fields:
        headers, value = field[0], field[2]
        if headers.get("name") != "image[]":
            continue
        if isinstance(value, (bytes, bytearray)):
            parts.append(bytes(value))
        elif hasattr(value, "read"):
            parts.append(bytes(value.read()))
        else:  # pragma: no cover - 目前只传 bytes
            parts.append(bytes(str(value).encode()))
    return parts


class MultipartTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_multipart_sends_compressed_bytes(self):
        """B1：真实适配器 + 真实 FormData，断言分片字节 == 压缩产物。"""

        class CaptureSession:
            def __init__(self):
                self.form = None

            def post(self, url, *, headers=None, data=None):
                self.form = data
                return _Resp()

        class _Resp:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def json(self, content_type=None):
                return {"data": [{"b64_json": "aGVsbG8="}]}

            async def text(self):
                return ""

        original = [noise_png(700, 500, seed=8), noise_png(700, 500, seed=9)]
        outgoing, _ = logic.compress_all(
            [ImageInput(payload, "image/png", f"r{index}.png") for index, payload in enumerate(original)],
            config(max_edge=512, target_kb=2048),
        )
        adapter = OpenAIImageAdapter(ProviderConfig("node", "openai_image", "https://example.invalid/v1", ("key",), model="m"))
        session = CaptureSession()
        request = GenerationRequest("draw", count=1, size="1024x1024", references=outgoing)
        await adapter._edit_multipart(session, request, "key", "https://example.invalid/v1/images/edits")
        parts = _image_parts(session.form)
        self.assertEqual([len(part) for part in parts], [len(item.data) for item in outgoing])
        self.assertEqual(parts, [item.data for item in outgoing])
        self.assertNotEqual(parts, original)
        self.assertLess(sum(len(part) for part in parts), sum(len(item) for item in original))


class PluginCompressTests(unittest.TestCase):
    def _plugin(self):
        plugin = object.__new__(_plugin_class())
        plugin.raw_config = {}
        return plugin

    def _task(self, explicit, persona):
        task = DrawTask(
            "t1", "umo", GenerationRequest("draw", references=tuple(explicit)),
        )
        task.runtime["persona_references"] = list(persona)
        return task

    def test_single_point_splits_and_dedupes(self):
        shared = noise_png(600, 400, seed=10)
        explicit = [
            ImageInput(noise_png(600, 400, seed=11), "image/png", "a.png"),
            ImageInput(shared, "image/png", "b.png"),
        ]
        persona = [ImageInput(shared, "image/png", "p.png")]
        task = self._task(explicit, persona)
        plugin = self._plugin()
        cfg = RuntimeConfig(providers=(), reference_compress=config(max_edge=512, target_kb=2048))
        plugin._apply_reference_compression(task, cfg)
        self.assertEqual(len(task.runtime["send_references"]), 2)
        self.assertEqual(len(task.runtime["send_persona_references"]), 1)
        # 原素材不被就地替换（`inputs/` 与去重仍按原字节）。
        self.assertEqual(task.request.references, tuple(explicit))
        self.assertEqual(task.runtime["persona_references"], persona)
        self.assertEqual(len(task.runtime["reference_compress_map"]), 2)
        # 同一张图（显式 b == Persona p）只压一次：两处拿到同一个对象。
        self.assertIs(
            task.runtime["send_persona_references"][0],
            task.runtime["send_references"][1],
        )

    def test_disabled_creates_no_runtime_key(self):
        task = self._task([ImageInput(noise_png(300, 200), "image/png", "a.png")], [])
        plugin = self._plugin()
        plugin._apply_reference_compression(task, RuntimeConfig(providers=(), reference_compress=config(enable=False)))
        self.assertNotIn("send_references", task.runtime)
        self.assertNotIn("reference_compress", task.runtime)


class ManifestTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalize_persists_bounded_compress_and_attempt_sections(self):
        from imago_fusion.services.persona_store import PersonaStore

        with TemporaryDirectory() as directory:
            store = PersonaStore(Path(directory), lambda: 20 * 1024 * 1024)
            plugin = object.__new__(_plugin_class())
            plugin.raw_config = {}
            plugin.store = store
            plugin.quota_store = None
            plugin.scheduler = type("S", (), {"tasks": {}, "accepting": True})()
            task = DrawTask("a" * 32, "umo", GenerationRequest("draw"))
            task.state = TaskState.NO_OUTPUT
            task.stage = TaskStage.FINISHED
            task.runtime["reference_compress"] = {
                "in_bytes": 900, "out_bytes": 300, "compressed": 1,
                "skipped": [{"index": 1, "reason": "under_min_bytes", "bytes": 10}],
                "failed": [], "target_unmet": False,
            }
            task.runtime["attempt_errors"] = ["node:model:ProviderTimeout"]
            task.runtime["attempt_diagnostics"] = [
                {"provider_id": "node", "model": "model", "phase": "upload", "elapsed": 1.2,
                 "up_bytes": 10, "timeout": 90, "error_type": "ServerDisconnectedError"}
            ]
            await plugin._finalize_task(task)
            manifest_path = store.task_dir(task.id) / "task.json"
            raw = manifest_path.read_text("utf-8")
            manifest = json.loads(raw)
        self.assertEqual(manifest["reference_compress"]["in_bytes"], 900)
        self.assertEqual(manifest["reference_compress"]["skipped"][0]["reason"], "under_min_bytes")
        self.assertEqual(manifest["attempts"]["count"], 1)
        self.assertEqual(manifest["attempts"]["diagnostics"][0]["phase"], "upload")
        self.assertNotIn("echo_guard", manifest)
        # 诊断只落计数/阶段名，不落图片内容与 base64。
        self.assertNotIn("iVBORw0KGgo", raw)


if __name__ == "__main__":
    unittest.main()
