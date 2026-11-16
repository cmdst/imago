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

import base64
import contextlib
import hashlib
import io
import json
import os
import random
import time
import unittest
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import aiohttp


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



def photo_like_png(width: int = 3000, height: int = 2000, amp: int = 9, block: int = 4) -> bytes:
    """T-C04 夹具：3000×2000、约 0.8 MB 的"照片感" PNG。

    纯噪声 PNG 会到十几 MB 且 JPEG 压不进 128 KB（``target_unmet`` 恒为 1）；
    纯渐变又小到触发 ``no_gain``。平滑渐变 + 块状纹理让源图落在几百 KB 量级，
    缩边到 1536px 再转码后又确实能过 128 KB，正好覆盖"大图一次到位"这条路径。
    """
    from PIL import Image, ImageChops

    gradient = Image.linear_gradient("L").resize((width, height), Image.Resampling.BILINEAR)
    base = Image.merge(
        "RGB",
        (gradient, gradient.rotate(90), gradient.transpose(Image.Transpose.FLIP_LEFT_RIGHT)),
    )
    noise = Image.effect_noise((width // block, height // block), amp).convert("L")
    noise = noise.resize((width, height), Image.Resampling.NEAREST)
    merged = Image.merge("RGB", [ImageChops.add(channel, noise, scale=2) for channel in base.split()])
    buffer = io.BytesIO()
    merged.save(buffer, format="PNG")
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


class _Resp:
    """最小 aiohttp 响应替身：只实现适配器会用到的那几项。"""

    status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return {"data": [{"b64_json": "aGVsbG8="}]}

    async def text(self):
        return ""


class _CaptureWriter:
    """接住 multipart 写入的最小 writer（aiohttp 的 ``MultipartWriter`` 只要求这两个）。"""

    def __init__(self):
        self.buffer = bytearray()

    async def write(self, chunk):
        self.buffer += bytes(chunk)
        return len(chunk)

    async def write_eof(self, data=b""):
        self.buffer += bytes(data)


async def _serialized_parts(form) -> list[tuple[str, bytes]]:
    """把 ``FormData`` 真的序列化成 multipart 字节流，再从**接收端**解析回来。

    ``FormData._gen_form_data()`` 就是 aiohttp 发请求时走的那条路径；解析用标准库
    ``email``，因此拿到的是"线上那几个 part 的真实载荷"，而不是 ``_fields`` 里的对象。
    """
    import aiohttp
    from email.parser import BytesParser
    from email.policy import default as email_policy

    writer = form._gen_form_data()
    capture = _CaptureWriter()
    await writer.write(capture)
    content_type = writer.headers.get(aiohttp.hdrs.CONTENT_TYPE, "multipart/form-data")
    raw = (
        b"Content-Type: " + str(content_type).encode("utf-8")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + bytes(capture.buffer)
    )
    message = BytesParser(policy=email_policy).parsebytes(raw)
    parts: list[tuple[str, bytes]] = []
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition") or ""
        parts.append((str(name), part.get_payload(decode=True) or b""))
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

    async def test_receiver_parses_exactly_the_compressed_bytes(self):
        """P3 集成：发送副本 → 真实适配器 → 接收端逐 part 比对压缩后字节。"""

        class CaptureSession:
            def __init__(self):
                self.form = None

            def post(self, url, *, headers=None, data=None):
                self.form = data
                return _Resp()

        original = [photo_like_png(900, 700, amp=9), noise_png(700, 500, seed=31)]
        outgoing, stats = logic.compress_all(
            [ImageInput(payload, "image/png", f"src{index}.png") for index, payload in enumerate(original)],
            ReferenceCompressConfig(),
        )
        self.assertEqual(stats["compressed"], 2)
        adapter = OpenAIImageAdapter(
            ProviderConfig("node", "openai_image", "https://example.invalid/v1", ("key",), model="m")
        )
        session = CaptureSession()
        request = GenerationRequest("draw", count=1, size="1024x1024", references=outgoing)
        await adapter._edit_multipart(session, request, "key", "https://example.invalid/v1/images/edits")
        parts = await _serialized_parts(session.form)
        images = [payload for name, payload in parts if name == "image[]"]
        fields = {name: payload for name, payload in parts if name != "image[]"}
        self.assertEqual(images, [item.data for item in outgoing])
        self.assertNotEqual(images, original)
        self.assertEqual(fields["model"], b"m")
        self.assertEqual(fields["prompt"], b"draw")
        self.assertEqual(fields["n"], b"1")
        # 压缩后更小：这条断言同时证明发出去的不是原图。
        self.assertLess(sum(len(item) for item in images), sum(len(item) for item in original))

    async def test_disabled_reference_compress_sends_original_bytes(self):
        """``enable=false``：part 字节逐字节等于入站原图（§3.9）。"""

        class CaptureSession:
            def __init__(self):
                self.form = None

            def post(self, url, *, headers=None, data=None):
                self.form = data
                return _Resp()

        plugin = object.__new__(_plugin_class())
        plugin.raw_config = {}
        originals = [
            ImageInput(noise_png(800, 600, seed=41), "image/png", "a.png"),
            ImageInput(noise_png(800, 600, seed=42), "image/png", "b.png"),
        ]
        task = DrawTask("t9", "umo", GenerationRequest("draw", references=tuple(originals)))
        task.runtime["persona_references"] = []
        plugin._apply_reference_compression(
            task, RuntimeConfig(providers=(), reference_compress=config(enable=False))
        )
        self.assertNotIn("send_references", task.runtime)
        adapter = OpenAIImageAdapter(
            ProviderConfig("node", "openai_image", "https://example.invalid/v1", ("key",), model="m")
        )
        session = CaptureSession()
        request = GenerationRequest("draw", count=1, references=tuple(originals))
        await adapter._edit_multipart(session, request, "key", "https://example.invalid/v1/images/edits")
        parts = await _serialized_parts(session.form)
        images = [payload for name, payload in parts if name == "image[]"]
        self.assertEqual(images, [item.data for item in originals])


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


class PillowMissingTests(unittest.TestCase):
    """Pillow 不可用 ⇒ 全量降级原图、只告警一次、不新增任务失败（契约 §3.8 / T-C15）。"""

    def test_missing_pillow_degrades_to_originals_once(self):
        from unittest.mock import patch

        from imago_fusion.core import compress as logic

        payload = noise_png(400, 300, seed=21)
        images = [ImageInput(payload, "image/png", "a.png")]
        warnings = []
        with patch.object(logic, "Image", None), patch.object(logic, "_missing_pillow_warned", False):
            outgoing, stats = logic.compress_all(
                images, config(), warn=warnings.append, debug=lambda *a: None,
            )
            # 第二次不再重复告警（进程内一次）。
            logic.compress_all(images, config(), warn=warnings.append)
        self.assertIsNone(stats)
        self.assertEqual([item.data for item in outgoing], [payload])
        self.assertEqual(warnings, [logic.MISSING_PILLOW_WARNING])

    def test_missing_pillow_leaves_task_untouched(self):
        from unittest.mock import patch

        from imago_fusion.core import compress as logic

        payload = noise_png(400, 300, seed=22)
        task = DrawTask("t2", "umo", GenerationRequest("draw", references=(ImageInput(payload, "image/png", "a.png"),)))
        task.runtime["persona_references"] = []
        plugin = object.__new__(_plugin_class())
        plugin.raw_config = {}
        with patch.object(logic, "Image", None), patch.object(logic, "_missing_pillow_warned", False):
            plugin._apply_reference_compression(task, RuntimeConfig(providers=(), reference_compress=config()))
        # 没有统计就没有 runtime 键：任务照常把原图发出去，errors 不受影响。
        self.assertNotIn("reference_compress", task.runtime)
        self.assertNotIn("send_references", task.runtime)
        self.assertEqual(task.request.references[0].data, payload)
        self.assertEqual(task.errors, [])


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


class LargePhotoTests(unittest.TestCase):
    """T-C04：3000×2000 的大图在默认 7 键下必须**一次**压到 ≤131072 B。"""

    def test_t_c04_large_photo_meets_target(self):
        source = photo_like_png()
        self.assertGreater(len(source), 500_000, "夹具必须够大，否则测不到缩边+转码链路")
        outcome = logic.compress_reference(
            ImageInput(source, "image/png", "photo.png"), ReferenceCompressConfig()
        )
        self.assertEqual(outcome.skipped, "")
        self.assertEqual(outcome.failed, "")
        self.assertTrue(outcome.changed)
        self.assertLessEqual(len(outcome.image.data), 128 * 1024)
        self.assertFalse(outcome.target_unmet)
        self.assertLess(len(outcome.image.data), len(source))
        self.assertLessEqual(max(logic._open_first_frame(outcome.image.data).size), 1536)
        self.assertEqual(outcome.image.mime_type, "image/jpeg")
        self.assertTrue(outcome.image.filename.endswith(".jpg"))

    def test_size_ladder_works_on_max_edge_clamped_long_edge(self):
        """尺寸档必须作用在 ``max_edge`` 收口后的长边（t14 验收 3）。

        先乘系数再 clamp 会让三档都被 ``max_edge`` 截成同一个长边，尺寸阶梯
        形同虚设；这里用"必须继续降尺寸才达标"的场景钉住顺序。
        """
        source = photo_like_png(1600, 1200, amp=9)
        outcome = logic.compress_reference(
            ImageInput(source, "image/png", "photo.png"),
            config(max_edge=1024, jpeg_quality=95, target_kb=32),
        )
        self.assertTrue(outcome.changed)
        self.assertLess(outcome.edge, 1024, "第二/三档必须真的把长边压到 1024 以下")
        self.assertGreaterEqual(outcome.edge, 700)
        self.assertLess(len(outcome.image.data), len(source))
        self.assertLessEqual(max(logic._open_first_frame(outcome.image.data).size), 1024)


class EncodeFailureTests(unittest.TestCase):
    """CORE-04：任一编码异常**立即**回原图，不保留前几轮产生的有损候选。"""

    def test_encode_error_rolls_back_to_original(self):
        source = noise_png(600, 400, seed=21)
        image = ImageInput(source, "image/png", "r.png")
        records = {"info": [], "debug": [], "warn": []}
        with patch.object(logic, "_encode", side_effect=OSError("encoder exploded")):
            outgoing, stats = logic.compress_all(
                [image],
                config(max_edge=512, target_kb=32),
                info=records["info"].append,
                debug=records["debug"].append,
                warn=records["warn"].append,
            )
        self.assertIs(outgoing[0], image)
        self.assertEqual(stats["compressed"], 0)
        self.assertEqual(stats["failed"], [{"index": 1, "reason": logic.FAIL_ERROR}])
        self.assertEqual(stats["skipped"], [])
        self.assertEqual(len(records["warn"]), 1)
        self.assertIn("回退原图", records["warn"][0])
        self.assertIn("OSError", records["warn"][0])
        self.assertEqual(records["info"], [])

    def test_encode_error_detail_is_truncated_and_redacted(self):
        source = noise_png(600, 400, seed=22)
        image = ImageInput(source, "image/png", "r.png")
        long_message = "boom " + "x" * 500 + " https://user:pass@example.invalid/secret"
        with patch.object(logic, "_encode", side_effect=RuntimeError(long_message)):
            outcome = logic.compress_reference(image, config(max_edge=512))
        self.assertEqual(outcome.failed, logic.FAIL_ERROR)
        self.assertFalse(outcome.changed)
        self.assertIs(outcome.image, image)
        self.assertLessEqual(len(outcome.detail), 120)
        self.assertNotIn("pass@", outcome.detail)

    def test_short_credentials_at_message_start_are_redacted_before_truncation(self):
        """R3-03：短消息（<120 字符）里的 URL/token 也必须先脱敏。

        既有 ``test_encode_error_detail_is_truncated_and_redacted`` 用的是 500 字符
        长消息，截断本身就把凭据切掉了，覆盖不到"短消息原样进 WARNING"这条真实
        路径（url 与 token 都在开头）。
        """

        source = noise_png(600, 400, seed=24)
        image = ImageInput(source, "image/png", "short.png")
        canary = "SYNTHETIC_CANARY_R303"
        message = (
            f"https://user:pass@example.invalid/ref.png?token={canary} "
            f"token={canary} boom"
        )
        self.assertLess(len(message), 120)
        records = {"warn": []}
        with patch.object(logic, "_encode", side_effect=RuntimeError(message)):
            outgoing, stats = logic.compress_all(
                [image],
                config(max_edge=512),
                info=lambda *a, **k: None,
                debug=lambda *a, **k: None,
                warn=records["warn"].append,
            )
        self.assertIs(outgoing[0], image)
        self.assertEqual(outgoing[0].mime_type, "image/png")
        self.assertEqual(outgoing[0].filename, "short.png")
        self.assertEqual(stats["failed"], [{"index": 1, "reason": logic.FAIL_ERROR}])
        self.assertEqual(len(records["warn"]), 1)
        warning = records["warn"][0]
        self.assertIn("回退原图", warning)
        for secret in (canary, "example.invalid", "https://", "user:pass"):
            self.assertNotIn(secret, warning)

    def test_no_gain_is_not_a_failure(self):
        from PIL import Image

        raw = io.BytesIO()
        Image.open(io.BytesIO(noise_png(200, 200, seed=23))).save(raw, format="JPEG", quality=25)
        image = ImageInput(raw.getvalue(), "image/jpeg", "already.jpg")
        outcome = logic.compress_reference(image, config(max_edge=0, jpeg_quality=95))
        self.assertEqual(outcome.skipped, logic.SKIP_NO_GAIN)
        self.assertEqual(outcome.failed, "")
        self.assertIs(outcome.image, image)


class InputsPreservedTests(unittest.TestCase):
    """T-C11：``inputs/`` 落的是**原字节**，运行期发送副本才是压缩产物。"""

    def test_inputs_keep_original_bytes_while_send_copy_shrinks(self):
        from imago_fusion.services.persona_store import PersonaStore

        source = noise_png(1200, 800, seed=51)
        image = ImageInput(source, "image/png", "big.png")
        plugin = object.__new__(_plugin_class())
        plugin.raw_config = {}
        with TemporaryDirectory() as tmp:
            store = PersonaStore(Path(tmp), lambda: 20 * 1024 * 1024)
            plugin.store = store
            plugin.quota_store = None
            import hashlib

            records = store.persist_task_inputs("a" * 32, [image])
            task = DrawTask("a" * 32, "umo", GenerationRequest("draw", references=(image,)))
            task.runtime["persona_references"] = []
            plugin._apply_reference_compression(
                task,
                RuntimeConfig(
                    providers=(),
                    reference_compress=config(max_edge=768, target_kb=2048),
                ),
            )
            send = task.runtime["send_references"][0]
            self.assertLess(len(send.data), len(source))
            # inputs/ 仍是原字节：大小与 sha256 都对得上。
            self.assertEqual(records[0]["size"], len(source))
            self.assertEqual(records[0]["sha256"], hashlib.sha256(source).hexdigest())
            written = (store.task_dir("a" * 32) / "inputs" / records[0]["file"]).read_bytes()
            self.assertEqual(written, source)
            # 去重键用的也是原字节。
            key = hashlib.sha256(source).hexdigest()
            self.assertIn(key, task.runtime["reference_compress_map"])
            self.assertEqual(task.runtime["reference_compress_map"][key]["bytes"], len(send.data))


class SourceCoverageTests(unittest.IsolatedAsyncioTestCase):
    """契约 rev2 §4：不同来源的**同一张图**必须收敛到同一个发送副本。

    事件图片 / Persona 磁盘图本身就是 ``ImageInput``；``data:`` 与 ``base64://``
    由 :func:`imago_fusion.core.network.fetch_reference` 解出来后也进同一个
    :func:`ingest_reference`。http(s) 走同一条 ``fetch_reference`` 分支，但需要
    网络栈，这里不覆盖（由 ``/ref-upload`` 与离线集成测试兜底）。
    """

    async def test_data_and_base64_sources_reuse_the_same_ingest(self):
        import base64

        from imago_fusion.core.network import fetch_reference

        payload = photo_like_png(900, 700, amp=9)
        cfg = ReferenceCompressConfig()
        direct_bytes, direct_mime, direct_name, direct_outcome = logic.ingest_reference(
            payload, "image/png", "shot.png", cfg
        )
        self.assertTrue(direct_outcome.changed)

        data_url = "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
        fetched = await fetch_reference(
            None, data_url, max_bytes=20 * 1024 * 1024, block_private=False
        )
        self.assertEqual(fetched.data, payload)  # 入站仍是原字节
        bytes_b, mime_b, name_b, outcome_b = logic.ingest_reference(
            fetched.data, fetched.mime_type, fetched.filename, cfg
        )
        # 字节与 mime 必须一致；文件名保留来源差异（data: 没有原始文件名）。
        self.assertEqual((bytes_b, mime_b), (direct_bytes, direct_mime))
        self.assertTrue(name_b.endswith(".jpg"))

        inline = "base64://" + base64.b64encode(payload).decode("ascii")
        fetched_base64 = await fetch_reference(
            None, inline, max_bytes=20 * 1024 * 1024, block_private=False
        )
        bytes_c, mime_c, name_c, _ = logic.ingest_reference(
            fetched_base64.data, fetched_base64.mime_type, "persona.png", cfg
        )
        # 同一份字节 ⇒ 完全相同的压缩产物（与来源、文件名无关）。
        self.assertEqual((bytes_c, mime_c), (direct_bytes, direct_mime))
        self.assertTrue(name_c.endswith(".jpg"))


# ── CORE-13：入口级 e2e（真实 loopback HTTP 接收端，不用 CaptureSession 替身）─────


class Image:
    """轻量 ``Image`` 组件替身：``core/references.py`` 按类名匹配（含 "image"）。"""

    def __init__(self, path="", url="", file="", converter=None):
        self.path = path
        self.url = url
        self.file = file
        self._converter = converter

    async def convert_to_file_path(self):
        if self._converter is None:
            raise ValueError("no converter")
        return await self._converter()


class Text:
    """轻量文字组件替身：``core/references.py`` 从 ``.text`` 里抽 http(s) 来源。"""

    def __init__(self, text=""):
        self.text = text


class _EditServer:
    """真实 loopback HTTP 接收端：解析 multipart，逐 ``image[]`` part 记录原始字节。

    契约 rev3 §7.2 B1/CORE-13 要求接收端按真实网络栈读 part（``request.multipart()``），
    不能用 ``CaptureSession`` + email 解析替代——那样测不到 aiohttp 真正发出的字节。
    """

    def __init__(self, ref_payload: bytes = b""):
        self.ref_payload = ref_payload
        self.parts: list[tuple[str, str, bytes]] = []
        self.edits = 0
        self.ref_hits = 0
        self.port = 0

    async def __aenter__(self):
        from aiohttp import web

        async def edit(request):
            self.edits += 1
            reader = await request.multipart()
            async for part in reader:
                if part.name in {"image[]", "image"}:
                    self.parts.append((
                        part.filename or "",
                        part.headers.get("Content-Type", ""),
                        await part.read(decode=False),
                    ))
            body = noise_png(80, 60, seed=99)
            return web.json_response(
                {"data": [{"b64_json": base64.b64encode(body).decode("ascii")}]}
            )

        async def ref(request):
            self.ref_hits += 1
            return web.Response(body=self.ref_payload, content_type="image/png")

        app = web.Application()
        app.router.add_post("/v1/images/edits", edit)
        app.router.add_get("/ref.png", ref)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        await self._runner.cleanup()
        return False


@contextlib.asynccontextmanager
async def _netguard_allow(port: int):
    """门禁 netguard 只放行**显式许可**的 loopback 端口，用完立即还原。"""

    previous = os.environ.get("IMAGO_NETGUARD_ALLOW")
    entry = f"127.0.0.1:{port}"
    os.environ["IMAGO_NETGUARD_ALLOW"] = f"{previous},{entry}" if previous else entry
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("IMAGO_NETGUARD_ALLOW", None)
        else:
            os.environ["IMAGO_NETGUARD_ALLOW"] = previous


class EntryE2ETests(unittest.IsolatedAsyncioTestCase):
    """CORE-13：真实入口 → 真实 adapter → 真实 HTTP 接收端，逐 part 比对字节。"""

    async def asyncSetUp(self):
        self._tmpdir = TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=5)
        )

    async def asyncTearDown(self):
        await self.session.close()
        self._tmpdir.cleanup()

    # ── 装配 ────────────────────────────────────────────────────────────
    def _plugin(self, port: int, *, compress_enabled: bool = True, max_edge: int = 768):
        from imago_fusion.core.config import load_config
        from imago_fusion.services.persona_store import PersonaStore
        from imago_fusion.services.scheduler import TaskScheduler

        plugin = object.__new__(_plugin_class())
        plugin.raw_config = {
            "providers": [
                {
                    "id": "local",
                    "api_type": "openai_image",
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "api_keys": ["API_KEY_TEST"],
                    "model": "test-model",
                    "default_size": "1024x1024",
                    "timeout": 30,
                }
            ],
            "storage_config": {"max_upload_mb": 20, "block_private_networks": False},
            "reference_compress_config": {
                "enable": compress_enabled,
                "max_edge": max_edge,
                "jpeg_quality": 85,
                "target_kb": 128,
                "min_bytes": 0,
                "keep_alpha": False,
                "max_pixels": 40000000,
            },
        }
        plugin.session = self.session
        plugin.store = PersonaStore(self._tmp, lambda: 20 * 1024 * 1024)
        plugin.quota_store = None

        async def runner(task, chain=None):
            return None

        plugin.scheduler = TaskScheduler(
            lambda: load_config(plugin.raw_config),
            self.session,
            runner,
            lambda provider, exc, task=None: None,
            lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
            warn=lambda *args, **kwargs: None,
        )
        return plugin

    async def _run_entry(self, plugin, components, *, prompt="画一只猫"):
        """真实入口：组件 → ``_plan_task_references`` → ``_prepare`` → ``_generate``。"""

        local, deferred = await plugin._plan_task_references(components)
        task = DrawTask(
            uuid.uuid4().hex,
            "umo://test",
            # 与 `_submit` 一致：`_resolve_deferred_references` 会 append 到该列表。
            GenerationRequest(prompt, references=list(local)),
            created_at=time.monotonic(),
        )
        task.runtime["source_event"] = None
        task.runtime["deferred_references"] = list(deferred)
        await plugin._prepare(task)
        results = await plugin.scheduler._generate(task)
        return task, results

    # ── 用例 ────────────────────────────────────────────────────────────
    async def test_local_entry_sends_exactly_the_compressed_bytes(self):
        payload = photo_like_png(1400, 1000)
        path = self._tmp / "entry.png"
        path.write_bytes(payload)

        async with _EditServer() as server, _netguard_allow(server.port):
            plugin = self._plugin(server.port)
            task, results = await self._run_entry(plugin, [Image(path=str(path))])

        sent = task.runtime["send_references"]
        self.assertEqual(len(sent), 1)
        self.assertNotEqual(sent[0].data, payload)  # 确实压过
        self.assertLess(len(sent[0].data), len(payload))
        # 接收端逐 part 字节 == 压缩后的 ImageInput.data（不是长度、不是原字节）
        self.assertEqual(server.edits, 1)
        self.assertEqual([part[2] for part in server.parts], [sent[0].data])
        self.assertEqual(server.parts[0][1], sent[0].mime_type)
        self.assertEqual(server.parts[0][0], sent[0].filename)
        self.assertEqual(len(results), 1)

    async def test_disabled_sends_original_bytes_and_inputs_untouched(self):
        payload = photo_like_png(1400, 1000)
        path = self._tmp / "raw.png"
        path.write_bytes(payload)

        async with _EditServer() as server, _netguard_allow(server.port):
            plugin = self._plugin(server.port, compress_enabled=False)
            task, _ = await self._run_entry(plugin, [Image(path=str(path))])

        self.assertNotIn("send_references", task.runtime)
        self.assertNotIn("reference_compress", task.runtime)
        self.assertEqual([part[2] for part in server.parts], [payload])
        records = task.runtime["input_records"]
        self.assertEqual(records[0]["size"], len(payload))
        self.assertEqual(
            (plugin.store.task_dir(task.id) / "inputs" / records[0]["file"]).read_bytes(),
            payload,
        )

    async def test_attempt_meta_matches_sent_bytes_and_differs_from_inputs(self):
        payload = photo_like_png(1600, 1100)
        path = self._tmp / "meta.png"
        path.write_bytes(payload)

        async with _EditServer() as server, _netguard_allow(server.port):
            plugin = self._plugin(server.port)
            task, _ = await self._run_entry(plugin, [Image(path=str(path))])

        sent = task.runtime["send_references"]
        meta = task.runtime["attempt_meta"]
        records = task.runtime["input_records"]
        # attempt_meta 记的是**发出去**的字节；inputs/ 记的是**收到**的字节。
        self.assertEqual([item["bytes"] for item in meta["references"]], [len(sent[0].data)])
        self.assertEqual([item["role"] for item in meta["references"]], ["explicit"])
        self.assertEqual(records[0]["size"], len(payload))
        self.assertNotEqual(meta["references"][0]["bytes"], records[0]["size"])
        digest = hashlib.sha256(payload).hexdigest()
        self.assertEqual(task.runtime["reference_compress_map"][digest]["bytes"], len(sent[0].data))
        self.assertEqual(
            (plugin.store.task_dir(task.id) / "inputs" / records[0]["file"]).read_bytes(),
            payload,
        )

    async def test_deferred_text_url_entry_is_compressed_and_sent(self):
        """R3-05：引用/文字里带的 http(s) 来源走 deferred 路径，仍过唯一压缩点。

        这是"带内容"的 deferred 执行用例：``plan()`` 只把 URL 记成 deferred，
        ``_prepare`` 里 ``_resolve_deferred_references`` 真去 loopback 拉图，随后
        同一个压缩单点产物进 ``send_references``，最终 multipart 逐字节相等。
        """

        payload = photo_like_png(1400, 1000)
        async with _EditServer(ref_payload=payload) as server:
            async with _netguard_allow(server.port):
                plugin = self._plugin(server.port, max_edge=768)
                url = f"http://127.0.0.1:{server.port}/ref.png"
                task, results = await self._run_entry(plugin, [Text(f"画一只猫 {url}")])
                sent = task.runtime.get("send_references") or []
                stats = task.runtime.get("reference_compress") or {}
                parts = list(server.parts)
                ref_hits = server.ref_hits
                records = list(task.runtime.get("input_records") or [])
        self.assertEqual(ref_hits, 1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(stats.get("compressed"), 1)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0][2], sent[0].data)
        self.assertLess(len(sent[0].data), len(payload))
        self.assertEqual(records[0]["size"], len(payload))
        self.assertEqual(results, [results[0]])

    async def test_every_source_entry_converges_to_the_same_sent_bytes(self):
        payload = photo_like_png(1200, 900)
        path = self._tmp / "shared.png"
        path.write_bytes(payload)
        encoded = base64.b64encode(payload).decode("ascii")

        async with _EditServer(ref_payload=payload) as server, _netguard_allow(server.port):
            entries = {
                "local": Image(path=str(path)),
                "data": Image(url="data:image/png;base64," + encoded),
                "base64": Image(url="base64://" + encoded),
                "http": Image(url=f"http://127.0.0.1:{server.port}/ref.png"),
            }
            sent_by_source = {}
            for name, component in entries.items():
                plugin = self._plugin(server.port)
                task, _ = await self._run_entry(plugin, [component])
                sent_by_source[name] = task.runtime["send_references"][0].data

        # 同一份入站字节 ⇒ 四个入口产出**逐字节相同**的发送副本。
        self.assertEqual(len(set(sent_by_source.values())), 1, sent_by_source.keys())
        self.assertEqual(server.ref_hits, 1)  # http 入口真的取过图
        self.assertEqual(len(server.parts), len(entries))
        self.assertTrue(all(part[2] == sent_by_source["local"] for part in server.parts))
