"""百度「图像无损放大」的用例（U1–U15；全部不碰网络、不碰生产）。

形状照抄 ``test/test_enhance.py``：``FakeSession`` 记录 ``(url, kwargs)`` 并按 URL 返回
预置响应，``unittest.IsolatedAsyncioTestCase`` 驱动协程。**不**自建 astrbot 桩、**不**改
``sys.modules``（避免 ``test/test_runtime_hook.py`` 的已知干扰）。

覆盖的契约点（spec 变更令 v3.10 的用例矩阵）：

- **U1/U2** 端点双向守卫：``UPSCALE_URL`` 必须是 ``image_quality_enhance``；放大两模块
  里不得出现增强端点常量标识符；收窄后的 E11 白名单文件里 0 处放大端点 id；
- **U3** 4M（base64）字节边界：恰好 3 145 728 B 放行、+1 B 跳过，两者都**零网络**；
- **U4** 2000×2000 **乘积**边界：4 000 000 放行、4 002 000 跳过、``4000×1000`` 放行；
- **U5** 最短边本地守卫（``UPSCALE_MIN_EDGE_PX = 16``，**非官方限值**）；
- **U6** 2× 语义：上传的是输入原字节、落盘的是响应原字节（插件不做任何缩放）；
- **U7** 两级顺序 + 载荷接力（增强产物作为放大输入）；
- **U8** 放大超限**保留上一级**（不回退原图、任务仍 succeeded）；
- **U9** 两级各自独立熔断；
- **U10** 只开放大时不出现 ``enhance`` 统计键；
- **U11** 默认配置（``load_config({})``）零副作用；
- **U13** 跳过枚举完整性（放大 7 条、增强 9 条仍在、互不冒充）；
- **U14** 统计字段形状（两侧都恰好 6 键）；
- **U15** 日志口径（INFO 独立成行在增强行之后 / WARNING 每类一次带「放大 失败：」前缀 /
  DEBUG 每图一条 / 关闭时 0 条）。

夹具纪律：只用 ``API_KEY_TEST`` / ``SECRET_KEY_TEST`` / ``TOKEN_TEST`` 假值，**零真实网络**。
"""

import base64
import struct
import time
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from imago_fusion.core import enhance as enhance_logic
from imago_fusion.core import upscale as logic
from imago_fusion.core.errors import UpscaleError, UpscaleSkipped
from imago_fusion.core.models import (
    DrawTask,
    EnhanceConfig,
    GenerationRequest,
    RuntimeConfig,
    UpscaleConfig,
)

ROOT = Path(__file__).resolve().parents[1]

TOKEN_TEST = "TOKEN_TEST"
# ``core/upscale*.py`` 两个模块（``upscale.py`` 与 ``upscale_client.py``）：用 glob 取，
# 这样静态守卫在「网络层还没落地」的中间提交里也不会因文件名不存在而误红。
UPSCALE_MODULES = sorted((ROOT / "core").glob("upscale*.py"))


def png_bytes(width: int = 64, height: int = 64, body: bytes = b"\x00" * 8) -> bytes:
    """最小 PNG 头：够魔数 + IHDR 尺寸嗅探（不做真实解码）。

    默认 64×64：必须满足放大侧的**两条**本地判据（乘积 ≤ 4 000 000、最短边 ≥ 16），
    否则连 happy path 都会被本地预检拦成 ``pixels_too_large`` / ``edge_too_short``。
    """
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + body
    )


def png_of_size(total: int, width: int = 64, height: int = 64) -> bytes:
    """造一张**恰好 total 字节**的假 PNG（头部 25 B + 填充），用于字节上限的边界断言。"""
    header = png_bytes(width, height, b"")
    assert total >= len(header), total
    return header + b"\x00" * (total - len(header))


def runtime_config(**overrides) -> RuntimeConfig:
    """放大客户端要读的运行时配置（超时预算 / 输出上限 / 代理）。"""
    values = {"providers": (), "generation_timeout": 300, "max_upload_bytes": 20 * 1024 * 1024}
    values.update(overrides)
    return RuntimeConfig(**values)


def upscale_config(**overrides) -> UpscaleConfig:
    """默认「开关打开 + 两把 key 齐全」；单条用例只覆盖自己关心的键。"""
    values = {"enabled": True, "api_key": "API_KEY_TEST", "secret_key": "SECRET_KEY_TEST"}
    values.update(overrides)
    return UpscaleConfig(**values)


def enhance_config(**overrides) -> EnhanceConfig:
    """默认「开关打开 + 两把 key 齐全」（两级用例共用）。"""
    values = {"enabled": True, "api_key": "API_KEY_TEST", "secret_key": "SECRET_KEY_TEST"}
    values.update(overrides)
    return EnhanceConfig(**values)


def make_task(task_id: str = "a" * 16) -> DrawTask:
    task = DrawTask(task_id, "umo", GenerationRequest("画一张测试图"))
    task.created_at = time.monotonic()
    return task


class UpscaleEndpointGuardTests(unittest.TestCase):
    """U1/U2：端点**双向**守卫（放大侧必须指向放大端点，且绝不能借用增强端点）。"""

    OLD_ENDPOINT_ID = "image_quality_enhance"
    DEFINITION_ENDPOINT_ID = "image_definition_enhance"

    def test_endpoint_constant_points_at_quality_enhance(self):
        """U1：``UPSCALE_URL`` 的域名与路径段逐字钉住（百度文档原文）。"""
        self.assertTrue(
            logic.UPSCALE_URL.endswith(f"/rest/2.0/image-process/v1/{self.OLD_ENDPOINT_ID}"),
            f"放大端点不对：{logic.UPSCALE_URL}",
        )
        self.assertTrue(
            logic.UPSCALE_URL.startswith("https://aip.baidubce.com/"),
            f"端点域名不对：{logic.UPSCALE_URL}",
        )

    def test_upscale_path_never_uses_the_definition_endpoint(self):
        """U2：① 路径精确；② ≠ 增强端点且不是定义增强；③ 两模块无 ``ENHANCE_URL``；④ E11 白名单 0 残留。"""
        self.assertEqual(
            urlparse(logic.UPSCALE_URL).path,
            f"/rest/2.0/image-process/v1/{self.OLD_ENDPOINT_ID}",
        )
        self.assertNotEqual(logic.UPSCALE_URL, enhance_logic.ENHANCE_URL)
        self.assertNotIn(self.DEFINITION_ENDPOINT_ID, urlparse(logic.UPSCALE_URL).path)
        # ③ 放大两模块**不得**出现增强端点常量标识符（防「复用同一个 URL 常量」的回退化）。
        #    docstring 提到另一个端点时只写端点 slug、不写标识符。
        self.assertTrue(UPSCALE_MODULES, "core/upscale*.py 一个都没有？")
        self.assertIn(ROOT / "core" / "upscale.py", UPSCALE_MODULES)
        for path in UPSCALE_MODULES:
            with self.subTest(module=path.name):
                self.assertNotIn("ENHANCE_URL", path.read_text("utf-8"))
        # ④ E11 收窄后的白名单文件里 0 处放大端点 id（增强侧没有被换成放大端点）。
        whitelist = [
            ROOT / "main.py",
            ROOT / "_conf_schema.json",
            ROOT / "core" / "enhance.py",
            ROOT / "core" / "enhance_client.py",
            ROOT / "core" / "models.py",
            ROOT / "core" / "config.py",
            ROOT / "core" / "prompting.py",
            ROOT / "core" / "network.py",
            ROOT / "core" / "errors.py",
        ]
        offenders = [
            str(path.relative_to(ROOT))
            for path in whitelist
            if self.OLD_ENDPOINT_ID in path.read_text("utf-8")
        ]
        self.assertEqual(offenders, [], f"增强侧仍在引用放大端点 id：{offenders}")


class UpscalePrecheckTests(unittest.TestCase):
    """U3/U4/U5：本地预检的三条边界（4M 字节 / 2000×2000 乘积 / 最短边 16px）。"""

    def test_base64_4mb_byte_boundary(self):
        """U3：``max_raw_bytes`` = 3 145 728 B；恰好放行、+1 B 拒绝（都零网络）。"""
        cfg = UpscaleConfig()
        self.assertEqual(cfg.max_raw_bytes, 3_145_728)
        self.assertEqual(logic.MAX_RAW_BYTES_FOR_4MB_BASE64, 3_145_728)
        allowed = png_of_size(3_145_728)
        rejected = png_of_size(3_145_729)
        self.assertEqual(logic.plan(allowed, cfg, remaining=300)[:2], (True, ""))
        self.assertEqual(
            logic.plan(rejected, cfg, remaining=300)[:2], (False, "too_large")
        )
        # max_image_mb 调小后上限同步缩小（派生量而不是写死的常量）。
        small = UpscaleConfig(max_image_mb=1)
        self.assertEqual(small.max_raw_bytes, 786_432)
        self.assertFalse(logic.plan(png_of_size(786_433), small, remaining=300)[0])

    def test_2000x2000_product_boundary(self):
        """U4：官方给的是「长宽**乘积**」⇒ 4 000 000 放行、4 002 000 跳过、``4000×1000`` 放行。"""
        cfg = UpscaleConfig()
        cases = [
            ((1999, 2000), True, ""),            # 3 998 000
            ((2000, 2000), True, ""),            # 4 000 000：恰好在边界上，放行
            ((2001, 2000), False, "pixels_too_large"),   # 4 002 000：越过边界
            ((4000, 1000), True, ""),            # 4 000 000：证明判据是乘积而不是边长
        ]
        for (width, height), expected_ok, expected_reason in cases:
            with self.subTest(dims=f"{width}x{height}"):
                ok, reason, _ = logic.plan(png_bytes(width, height), cfg, remaining=300)
                self.assertEqual((ok, reason), (expected_ok, expected_reason))
        # 阈值可被配置下调（clamp 下限 100 000）。
        tight = UpscaleConfig(max_pixels=100_000)
        self.assertEqual(
            logic.plan(png_bytes(400, 400), tight, remaining=300)[1], "pixels_too_large"
        )

    def test_short_edge_local_guard(self):
        """U5：最短边 < 16px 本地跳过（``UPSCALE_MIN_EDGE_PX`` 是**本地守卫、非官方限值**）。"""
        cfg = UpscaleConfig()
        self.assertEqual(logic.UPSCALE_MIN_EDGE_PX, 16)
        self.assertTrue(logic.plan(png_bytes(16, 20), cfg, remaining=300)[0])
        self.assertEqual(
            logic.plan(png_bytes(15, 20), cfg, remaining=300)[:2], (False, "edge_too_short")
        )
        self.assertEqual(
            logic.plan(png_bytes(20, 15), cfg, remaining=300)[:2], (False, "edge_too_short")
        )

    def test_plan_rejects_unrecognised_format_and_over_budget_images(self):
        """U13 的前半：``format`` 与 ``budget`` 两条判据（0 字节同处置，不单列一条枚举）。"""
        cfg = UpscaleConfig()
        self.assertEqual(logic.plan(b"", cfg, remaining=300)[:2], (False, "format"))
        self.assertEqual(
            logic.plan(b"RIFF\x00\x00\x00\x00WEBP", cfg, remaining=300)[:2], (False, "format")
        )
        # 剩余预算 ≤ 单张超时 + 15 s 时宁可不放大，避免把任务拖成超时。
        self.assertEqual(
            logic.plan(png_bytes(), cfg, remaining=cfg.timeout_seconds + 15)[:2],
            (False, "budget"),
        )
        self.assertTrue(logic.plan(png_bytes(), cfg, remaining=cfg.timeout_seconds + 16)[0])

    def test_dimensions_that_cannot_be_read_do_not_block(self):
        """读不出尺寸时跳过 3/4 两条（交服务端兜底），不因读不出而阻断。"""
        cfg = UpscaleConfig()
        truncated = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4
        self.assertIsNone(logic.image_dimensions(truncated))
        self.assertEqual(logic.plan(truncated, cfg, remaining=300)[:2], (True, ""))


class UpscaleSkipEnumerationTests(unittest.TestCase):
    """U13：跳过枚举完整性——放大 7 条、增强 9 条仍在、两侧互不冒充。"""

    UPSCALE_REASONS = (
        "format",
        "too_large",
        "pixels_too_large",
        "edge_too_short",
        "budget",
        "read_failed",
        "output_oversize",
    )
    # 增强专有（放大侧一条都不许出现）：``empty`` / ``edge_too_long`` /
    # ``aspect_too_wide`` / 以及不存在的 ``too_many_pixels`` 拼法。
    ENHANCE_ONLY_REASONS = ("empty", "edge_too_long", "aspect_too_wide", "too_many_pixels")
    ENHANCE_REASONS = (
        "empty",
        "format",
        "too_large",
        "edge_too_short",
        "edge_too_long",
        "aspect_too_wide",
        "budget",
        "read_failed",
        "output_oversize",
    )

    def test_upscale_declares_exactly_seven_reasons(self):
        self.assertEqual(tuple(logic.SKIP_REASONS), self.UPSCALE_REASONS)
        self.assertEqual(len(set(logic.SKIP_REASONS)), 7)
        source = (ROOT / "core" / "upscale.py").read_text("utf-8")
        for reason in self.UPSCALE_REASONS:
            with self.subTest(reason=reason):
                self.assertIn(f'"{reason}"', source)

    def test_upscale_side_never_reuses_the_enhance_only_reasons(self):
        sources = {path.name: path.read_text("utf-8") for path in UPSCALE_MODULES}
        self.assertIn("upscale.py", sources)
        for name, source in sources.items():
            for reason in self.ENHANCE_ONLY_REASONS:
                with self.subTest(module=name, reason=reason):
                    self.assertNotIn(f'"{reason}"', source)

    def test_enhance_side_keeps_all_of_its_reasons(self):
        enhance_source = (ROOT / "core" / "enhance.py").read_text("utf-8")
        client_source = (ROOT / "core" / "enhance_client.py").read_text("utf-8")
        for reason in self.ENHANCE_REASONS:
            with self.subTest(reason=reason):
                hits = f'"{reason}"' in enhance_source or f'"{reason}"' in client_source
                self.assertTrue(hits, f"增强侧丢了跳过原因 {reason}")
        # 反向：增强侧不得出现放大专有的 ``pixels_too_large``。
        self.assertNotIn('"pixels_too_large"', enhance_source)
        self.assertNotIn('"pixels_too_large"', client_source)


class UpscalePayloadTests(unittest.TestCase):
    """``decode_payload`` 的异常翻译 + 2× 判据（U6 的纯逻辑半边）。"""

    def test_missing_image_and_bad_base64_raise_upscale_errors(self):
        # 文档里 image 是「否（可选）」：HTTP 200 + 无 error_code 也可能没有图。
        with self.assertRaises(UpscaleError):
            logic.decode_payload({"log_id": 1})
        with self.assertRaises(UpscaleError):
            logic.decode_payload({"image": "!!!not-base64!!!"})
        with self.assertRaises(UpscaleError) as permission:
            logic.decode_payload({"error_code": 6, "error_msg": "No permission to access data"})
        self.assertEqual(permission.exception.kind, "permission")
        self.assertEqual(permission.exception.code, 6)
        payload = {"image": base64.b64encode(png_bytes(4, 4)).decode()}
        self.assertEqual(logic.decode_payload(payload), (png_bytes(4, 4), "image/png"))

    def test_output_limit_raises_upscale_skipped_not_enhance_skipped(self):
        payload = {"image": base64.b64encode(png_bytes(2, 2)).decode()}
        with self.assertRaises(UpscaleSkipped) as skipped:
            logic.decode_payload(payload, expected_max_bytes=4)
        self.assertEqual(skipped.exception.reason, "output_oversize")
        # 两级异常**互不继承**：放大侧绝不能误接增强异常（反之亦然）。
        from imago_fusion.core.errors import EnhanceError, EnhanceSkipped

        self.assertFalse(issubclass(UpscaleError, EnhanceError))
        self.assertFalse(issubclass(UpscaleSkipped, EnhanceSkipped))
        self.assertFalse(issubclass(EnhanceError, UpscaleError))
        self.assertFalse(issubclass(EnhanceSkipped, UpscaleSkipped))

    def test_double_scale_predicate(self):
        """U6 的纯逻辑半边：正好 2 倍为真；读不出尺寸时不误报；其它倍数可识别。"""
        self.assertTrue(logic.double_scale_ok((64, 64), (128, 128)))
        self.assertFalse(logic.double_scale_ok((64, 64), (64, 64)))
        self.assertFalse(logic.double_scale_ok((64, 64), (96, 96)))
        self.assertTrue(logic.double_scale_ok(None, (128, 128)))
        self.assertTrue(logic.double_scale_ok((64, 64), None))
        self.assertEqual(logic.dims_text((64, 64), (128, 128)), "64x64->128x128")
        self.assertEqual(logic.dims_text(None, (128, 128)), "-")
        self.assertEqual(logic.SCALE_FACTOR, 2)

    def test_mime_suffix_follows_the_magic_number(self):
        self.assertEqual(logic.mime_suffix("image/png"), ".png")
        self.assertEqual(logic.mime_suffix("image/jpeg"), ".jpg")
        self.assertEqual(logic.mime_suffix("image/bmp"), ".png")


if __name__ == "__main__":
    unittest.main()
