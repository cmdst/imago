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

import asyncio
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
from imago_fusion.core.config import load_config
from imago_fusion.core.enhance_client import BaiduEnhancer, EnhanceState
from imago_fusion.core.errors import UpscaleError, UpscaleSkipped
from imago_fusion.core.models import (
    DrawTask,
    EnhanceConfig,
    GenerationRequest,
    ImageResult,
    ProviderConfig,
    RuntimeConfig,
    TaskStage,
    TaskState,
    UpscaleConfig,
)
from imago_fusion.core.network import materialize_result
from imago_fusion.core.upscale_client import BaiduUpscaler, UpscaleState
from imago_fusion.services import scheduler as scheduler_module
from imago_fusion.services.scheduler import TaskScheduler

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
        self.assertEqual(logic.SCALE_FACTOR, 2)
        # DEBUG 行里 ``dims=`` 段的两种写法（``64x64->128x128`` / 读不出时 ``-``）由
        # ``UpscaleLoggingTests`` 的逐字格式串钉住。

    def test_mime_suffix_follows_the_magic_number(self):
        self.assertEqual(logic.mime_suffix("image/png"), ".png")
        self.assertEqual(logic.mime_suffix("image/jpeg"), ".jpg")
        self.assertEqual(logic.mime_suffix("image/bmp"), ".png")


# --------------------------------------------------------------------------- 网络层夹具


class FakeResponse:
    def __init__(self, payload, status: int = 200) -> None:
        self.payload = payload
        self.status = status

    async def json(self, content_type=None):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


def _payload_queue(value) -> tuple[deque, object]:
    """把单值/列表归一成 ``(队列, 末位默认值)``：队列剩一个时重复使用（「后续都这样」）。"""
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    return deque(values), (values[-1] if values else None)


def _take(queue: deque, default):
    if not queue:
        return default
    return queue.popleft() if len(queue) > 1 else queue[0]


class _PostContext:
    """``session.post(...)`` 的异步上下文管理器（延迟/异常在这里发生）。"""

    def __init__(self, session: "FakeSession", url: str) -> None:
        self.session = session
        self.url = url

    async def __aenter__(self):
        session = self.session
        if self.url.startswith(enhance_logic.TOKEN_URL):
            if session.token_exc is not None:
                raise session.token_exc
            return FakeResponse(
                _take(session.token_payloads, session.token_default),
                _take(session.token_statuses, session.token_status_default),
            )
        # 先判放大端点：它是本文件的主角，且必须与增强端点互斥（U2）。
        if self.url.startswith(logic.UPSCALE_URL):
            if session.upscale_delay:
                await asyncio.sleep(session.upscale_delay)
            if session.upscale_exc is not None:
                raise session.upscale_exc
            return FakeResponse(
                _take(session.upscale_payloads, session.upscale_default),
                _take(session.upscale_statuses, session.upscale_status_default),
            )
        if session.enhance_delay:
            await asyncio.sleep(session.enhance_delay)
        if session.enhance_exc is not None:
            raise session.enhance_exc
        return FakeResponse(
            _take(session.enhance_payloads, session.enhance_default),
            _take(session.enhance_statuses, session.enhance_status_default),
        )

    async def __aexit__(self, *_exc):
        return False


class FakeSession:
    """记录 ``(url, kwargs)``；按 URL 前缀返回预置响应；**零真实 IO**。

    三条前缀互不重叠：token（``oauth/2.0/token``）/ 放大（``UPSCALE_URL``）/ 增强
    （``ENHANCE_URL``）。记录里同时留了 ``kwargs``，因此「端点、载荷、token 走查询参数、
    没有手写 Content-Type」都能被断言。
    """

    def __init__(
        self,
        *,
        token=None,
        enhance=None,
        upscale=None,
        token_status=None,
        enhance_status=None,
        upscale_status=None,
        token_exc: BaseException | None = None,
        enhance_exc: BaseException | None = None,
        upscale_exc: BaseException | None = None,
        enhance_delay: float = 0.0,
        upscale_delay: float = 0.0,
    ) -> None:
        self.token_payloads, self.token_default = _payload_queue(token)
        self.enhance_payloads, self.enhance_default = _payload_queue(enhance)
        self.upscale_payloads, self.upscale_default = _payload_queue(upscale)
        self.token_statuses, self.token_status_default = _payload_queue(
            200 if token_status is None else token_status
        )
        self.enhance_statuses, self.enhance_status_default = _payload_queue(
            200 if enhance_status is None else enhance_status
        )
        self.upscale_statuses, self.upscale_status_default = _payload_queue(
            200 if upscale_status is None else upscale_status
        )
        self.token_exc = token_exc
        self.enhance_exc = enhance_exc
        self.upscale_exc = upscale_exc
        self.enhance_delay = enhance_delay
        self.upscale_delay = upscale_delay
        self.calls: list[tuple[str, dict]] = []

    @property
    def token_calls(self):
        return [call for call in self.calls if call[0].startswith(enhance_logic.TOKEN_URL)]

    @property
    def upscale_calls(self):
        return [call for call in self.calls if call[0].startswith(logic.UPSCALE_URL)]

    @property
    def enhance_calls(self):
        return [call for call in self.calls if call[0].startswith(enhance_logic.ENHANCE_URL)]

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _PostContext(self, url)

    def endpoints(self) -> list[str]:
        """调用序列的可读前缀：``token`` / ``quality``（放大）/ ``definition``（增强）。"""
        result = []
        for url, _ in self.calls:
            if url.startswith(enhance_logic.TOKEN_URL):
                result.append("token")
            elif url.startswith(logic.UPSCALE_URL):
                result.append("quality")
            else:
                result.append("definition")
        return result


class DummySession:
    """给 ``materialize_result`` 用的占位会话：结果图带 ``data`` 时它不会发任何请求。"""


def recorder(sink: list):
    """把 ``(message, args)`` 收进列表的 **WARNING** 回调（``warn=``）。"""

    def _record(message, *args):
        sink.append((message, args))

    return _record


def debug_recorder(sink: list):
    """把 ``(message, args)`` 收进列表的 **DEBUG** 回调（``debug=``），形状同 ``main.py``。"""

    def _record(task, message, *args):
        sink.append((message, args))

    return _record


def token_payload(token: str = TOKEN_TEST) -> dict:
    return {"access_token": token, "expires_in": 2592000}


def image_payload(width: int, height: int, *, log_id: int = 7) -> dict:
    """成功响应：``{"log_id", "image": <base64>}``（``image`` 不带 ``data:`` 头）。"""
    return {"log_id": log_id, "image": base64.b64encode(png_bytes(width, height, b"\x00" * 16)).decode()}


def upscaler_of(session, *, debug=None, warn=None, runtime=None) -> "BaiduUpscaler":
    return BaiduUpscaler(
        session,
        lambda: runtime_config() if runtime is None else runtime,
        debug=debug,
        warn=warn,
    )


def enhancer_of(session, *, debug=None, warn=None, runtime=None) -> "BaiduEnhancer":
    return BaiduEnhancer(
        session,
        lambda: runtime_config() if runtime is None else runtime,
        debug=debug,
        warn=warn,
    )


# --------------------------------------------------------------------------- 单级行为


class UpscaleApplyTests(unittest.IsolatedAsyncioTestCase):
    """U6/U3/U11/U14：放大单级的载荷、边界、默认零副作用与统计形状。"""

    async def _apply(self, *, data: bytes, session: FakeSession, cfg=None, state=None):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out_dir = Path(directory.name)
        original = out_dir / "origin.png"
        original.write_bytes(data)
        task = make_task()
        state = state or UpscaleState()
        upscaler = upscaler_of(session, debug=debug_recorder([]), warn=recorder([]))
        value = await upscaler.apply(task, original, out_dir, cfg or upscale_config(), state)
        return task, state, original, value, out_dir

    async def test_upscale_uploads_the_raw_input_and_stores_the_raw_response(self):
        """U6（2× 语义）：输入原字节上传、响应原字节落盘，插件不做任何缩放/转码。"""
        data = png_bytes(64, 64)
        enlarged = png_bytes(128, 128, b"\x11" * 24)
        session = FakeSession(token=token_payload(), upscale={"log_id": 11, "image": base64.b64encode(enlarged).decode()})
        task, state, original, value, out_dir = await self._apply(data=data, session=session)
        # ① 上传的载荷 == base64(输入原字节)：我们没有缩放输入
        self.assertEqual(len(session.upscale_calls), 1)
        url, kwargs = session.upscale_calls[0]
        self.assertTrue(url.startswith(logic.UPSCALE_URL))
        # 端点 = 执行级断言：抓**实际发出的 URL** 的 host 与 path（不是只看常量）
        parsed = urlparse(url)
        self.assertEqual(parsed.netloc, "aip.baidubce.com")
        self.assertEqual(parsed.path, "/rest/2.0/image-process/v1/image_quality_enhance")
        # 放大路径全程不得出现增强端点（防「改回错误端点」的静默回退化）
        self.assertEqual(
            [url_ for url_, _ in session.calls if "image_definition_enhance" in url_], []
        )
        self.assertEqual([url_ for url_, _ in session.calls if "image_quality_enhance" not in url_ and "oauth" not in url_], [])
        self.assertEqual(kwargs["data"], {"image": base64.b64encode(data).decode()})
        # token 走查询参数、没有手写 Content-Type、不是 json=
        self.assertEqual(kwargs["params"], {"access_token": TOKEN_TEST})
        self.assertNotIn("headers", kwargs)
        self.assertNotIn("json", kwargs)
        # ② 落盘字节 == 响应解码字节；③ 尺寸恰为 2 倍（image_dimensions 读出 128×128）
        self.assertIsInstance(value, Path)
        self.assertNotEqual(value, original)
        self.assertEqual(value.read_bytes(), enlarged)
        self.assertEqual(logic.image_dimensions(value.read_bytes()), (128, 128))
        self.assertTrue(logic.double_scale_ok(logic.image_dimensions(data), (128, 128)))
        # ④ 后缀按响应魔数
        self.assertEqual(value.suffix, ".png")
        # 统计：成功 +1、log_id 落 runtime、恰好 6 键（U14）、绝不写 task.errors
        self.assertEqual(state.succeeded, 1)
        self.assertEqual(state.log_id, 11)
        self.assertEqual(
            set(task.runtime["upscale"]),
            {"attempted", "succeeded", "skipped", "failures", "circuit_open", "log_id"},
        )
        self.assertEqual(task.errors, [])
        # 原图保留（最坏情况 count=1 时目录里 2 个文件：原图 + 放大图）
        self.assertEqual(sorted(path.name for path in out_dir.iterdir()), sorted([original.name, value.name]))

    async def test_base64_4mb_boundary_is_enforced_without_any_request(self):
        """U3 的执行级半边：恰好 3 145 728 B 放行；+1 B 跳过且**零网络**。"""
        cfg = upscale_config()
        # 恰好到边界：本地判据放行（随后请求照常发），证明 `>` 而不是 `>=`
        session_ok = FakeSession(token=token_payload(), upscale=image_payload(128, 128))
        task, state, _, value, _ = await self._apply(
            data=png_of_size(3_145_728), session=session_ok, cfg=cfg
        )
        self.assertEqual(len(session_ok.upscale_calls), 1)
        self.assertEqual(state.succeeded, 1)
        self.assertEqual(task.runtime["upscale"]["skipped"], [])
        # 越过 1 字节：连 token 都不取
        session_over = FakeSession(token=token_payload(), upscale=image_payload(128, 128))
        task, state, original, value, _ = await self._apply(
            data=png_of_size(3_145_729), session=session_over, cfg=cfg
        )
        self.assertEqual(session_over.calls, [])
        self.assertIs(value, original)
        self.assertEqual(state.skipped, ["too_large"])
        self.assertEqual(state.succeeded, 0)
        self.assertEqual(task.runtime["upscale"]["skipped"], ["too_large"])
        self.assertEqual(task.errors, [])

    async def test_default_config_has_zero_side_effects(self):
        """U11：``load_config({})`` 真实解析 ⇒ 零网络、零新文件、无 runtime 键、零日志、返回同对象。"""
        raw_cfg = load_config({})
        self.assertFalse(raw_cfg.upscale.enabled)
        session = FakeSession(token=token_payload(), upscale=image_payload(128, 128))
        debug, warns = [], []
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out_dir = Path(directory.name)
        original = out_dir / "origin.png"
        original.write_bytes(png_bytes())
        task = make_task()
        state = UpscaleState()
        upscaler = BaiduUpscaler(
            session, lambda: raw_cfg, debug=debug_recorder(debug), warn=recorder(warns)
        )
        value = await upscaler.apply(task, original, out_dir, raw_cfg.upscale, state)
        self.assertIs(value, original)
        self.assertEqual(session.calls, [])                      # Z0-U1 零请求
        self.assertEqual([path.name for path in out_dir.iterdir()], [original.name])  # Z0-U2
        self.assertNotIn("upscale", task.runtime)                # Z0-U3
        self.assertEqual((debug, warns), ([], []))               # Z0-U4
        self.assertEqual(task.errors, [])                        # Z0-U6
        self.assertIs(task.state, TaskState.CREATED)
        self.assertIs(task.stage, TaskStage.QUEUED)

    async def test_output_oversize_is_a_skip_that_keeps_the_previous_stage(self):
        """U14 的另一半：输出超 ``storage_config.max_upload_bytes`` ⇒ 跳过（不是失败）。"""
        session = FakeSession(token=token_payload(), upscale=image_payload(128, 128))
        tight = runtime_config(max_upload_bytes=10)
        task = make_task()
        state = UpscaleState()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out_dir = Path(directory.name)
        original = out_dir / "origin.png"
        original.write_bytes(png_bytes())
        upscaler = BaiduUpscaler(session, lambda: tight, debug=debug_recorder([]), warn=recorder([]))
        value = await upscaler.apply(task, original, out_dir, upscale_config(), state)
        self.assertIs(value, original)
        self.assertEqual(state.skipped, ["output_oversize"])
        self.assertEqual(state.failures, [])
        self.assertEqual(task.runtime["upscale"]["skipped"], ["output_oversize"])

    async def test_upgrade_is_skipped_when_keys_are_missing(self):
        """``enable=true`` 但 key 不全 ⇒ 零网络 + 整个生命周期只 WARNING 一次（marker 独立）。"""
        session = FakeSession(token=token_payload(), upscale=image_payload(128, 128))
        warns: list = []
        upscaler = upscaler_of(session, debug=debug_recorder([]), warn=recorder(warns))
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out_dir = Path(directory.name)
        original = out_dir / "origin.png"
        original.write_bytes(png_bytes())
        half = upscale_config(secret_key="")
        task = make_task()
        state = UpscaleState()
        for _ in range(2):
            self.assertIs(await upscaler.apply(task, original, out_dir, half, state), original)
        self.assertEqual(session.calls, [])
        self.assertEqual(len(warns), 1)
        self.assertIn("放大 已在配置里开启", warns[0][0])
        self.assertNotIn("upscale", task.runtime)

    async def test_error_code_6_opens_only_the_upscale_circuit(self):
        """U9 的单级半边：``error_code=6`` ⇒ 不重试、告警一次（带「放大 失败：」前缀）、熔断。"""
        session = FakeSession(
            token=token_payload(),
            upscale={"error_code": 6, "error_msg": "No permission to access data"},
        )
        debug, warns = [], []
        task = make_task()
        state = UpscaleState()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out_dir = Path(directory.name)
        original = out_dir / "origin.png"
        original.write_bytes(png_bytes())
        upscaler = upscaler_of(session, debug=debug_recorder(debug), warn=recorder(warns))
        value = await upscaler.apply(task, original, out_dir, upscale_config(), state)
        self.assertIs(value, original)
        self.assertEqual(len(session.upscale_calls), 1)          # permission 不重试
        self.assertEqual(state.failures, ["permission:6"])
        self.assertTrue(state.circuit_open)
        self.assertEqual(len(warns), 1)
        self.assertTrue(warns[0][0].startswith("放大 失败："))
        self.assertIn("图像无损放大", warns[0][0])
        self.assertIn("error_code=6", warns[0][0])
        self.assertEqual(task.errors, [])
        # 熔断后第二张图不再尝试（连 token 都不取）
        calls_before = len(session.calls)
        self.assertIs(await upscaler.apply(task, original, out_dir, upscale_config(), state), original)
        self.assertEqual(len(session.calls), calls_before)


# --------------------------------------------------------------------------- 两级链路


def chain_runner(*, session, out_dir, cfg, enhancer, upscaler, enhance_state, upscale_state, delivered: list):
    """镜像 ``main.py._finish_task`` 的两级插入点：materialize → 增强 → 放大。"""

    async def runner(task, results):
        for result in results:
            value = await materialize_result(
                session, result, out_dir,
                max_bytes=cfg.max_upload_bytes, block_private=cfg.block_private_networks,
            )
            if enhancer is not None:
                value = await enhancer.apply(task, value, out_dir, cfg.enhance, enhance_state)
            if upscaler is not None:
                value = await upscaler.apply(task, value, out_dir, cfg.upscale, upscale_state)
            delivered.append(value)
        return True

    return runner


class _ChainHarness(unittest.IsolatedAsyncioTestCase):
    """用**真实** ``TaskScheduler`` 跑一遍「适配器 → 两级链路」，验任务终态与交付物。"""

    async def run_chain(
        self,
        *,
        results: int,
        session: FakeSession,
        enhance_cfg: EnhanceConfig | None,
        upscale_cfg: UpscaleConfig | None,
        source_data: bytes | None = None,
        output_dir: Path | None = None,
    ):
        provider = ProviderConfig(
            "node", "custom_endpoint", "https://example.invalid", ("key",), model="model",
        )
        payload = source_data or png_bytes()
        delivered: list = []
        debug, warns = [], []

        class Adapter:
            def __init__(self, config): self.config = config

            async def generate(self, request_session, request, key):
                return [ImageResult(data=payload) for _ in range(results)]

        cfg = RuntimeConfig(
            providers=(provider,),
            generation_timeout=300,
            enhance=enhance_cfg or EnhanceConfig(),
            upscale=upscale_cfg or UpscaleConfig(),
        )
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out_dir = output_dir or Path(directory.name)
        enhancer = enhancer_of(session, debug=debug_recorder(debug), warn=recorder(warns), runtime=cfg)
        upscaler = upscaler_of(session, debug=debug_recorder(debug), warn=recorder(warns), runtime=cfg)
        enhance_state = EnhanceState()
        upscale_state = UpscaleState()
        runner = chain_runner(
            session=session, out_dir=out_dir, cfg=cfg, enhancer=enhancer, upscaler=upscaler,
            enhance_state=enhance_state, upscale_state=upscale_state, delivered=delivered,
        )
        finished = asyncio.Event()

        async def wrapped(task, task_results):
            try:
                return await runner(task, task_results)
            finally:
                finished.set()

        old = scheduler_module.ADAPTERS.get("custom_endpoint")
        scheduler_module.ADAPTERS["custom_endpoint"] = Adapter
        scheduler = TaskScheduler(
            lambda: cfg, DummySession(), wrapped, lambda *_: None,
            lambda task, message, *args: debug.append((message, args)),
        )
        task = DrawTask("c" * 16, "umo", GenerationRequest("x", count=results))
        try:
            scheduler.submit(task)
            await asyncio.wait_for(finished.wait(), 5)
            await asyncio.sleep(0)  # 让调度器把终态写回
        finally:
            await scheduler.close()
            if old is None:
                scheduler_module.ADAPTERS.pop("custom_endpoint", None)
            else:
                scheduler_module.ADAPTERS["custom_endpoint"] = old
        return task, delivered, out_dir, enhance_state, upscale_state, debug, warns


class UpscaleChainTests(_ChainHarness):
    """U7/U8/U9/U10：两级顺序、载荷接力、保留上一级、独立熔断、只开一级。"""

    async def test_enhance_runs_before_upscale_and_the_payload_is_relayed(self):
        """U7：调用序 token→definition→token→quality；放大吃的是增强产物；目录恰 3 个文件。"""
        enhanced = png_bytes(1000, 1000, b"\x22" * 16)
        enlarged = png_bytes(2000, 2000, b"\x33" * 16)
        session = FakeSession(
            token=token_payload(),
            enhance={"log_id": 7, "image": base64.b64encode(enhanced).decode()},
            upscale={"log_id": 21, "image": base64.b64encode(enlarged).decode()},
        )
        task, delivered, out_dir, enhance_state, upscale_state, debug, _ = await self.run_chain(
            results=1, session=session, enhance_cfg=enhance_config(), upscale_cfg=upscale_config(),
        )
        # ① 端点序列：先增强、后放大（两级各有自己的 token 请求，缓存**不共享**）
        self.assertEqual(session.endpoints(), ["token", "definition", "token", "quality"])
        # ② 载荷接力：放大上传的正是增强响应字节
        self.assertEqual(
            session.upscale_calls[0][1]["data"], {"image": base64.b64encode(enhanced).decode()}
        )
        # ③ 交付物是放大产物；原图与中间产物都留在目录里（恰 3 个文件）
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].read_bytes(), enlarged)
        self.assertEqual(len(list(out_dir.iterdir())), 3)
        # ④ 两级统计各自 +1，任务成功、无错误
        self.assertEqual(enhance_state.succeeded, 1)
        self.assertEqual(upscale_state.succeeded, 1)
        self.assertEqual(task.runtime["enhance"]["succeeded"], 1)
        self.assertEqual(task.runtime["upscale"]["succeeded"], 1)
        # U14 的另一半：增强侧口径**未变**，仍是同样 6 个键（多一个键 = 统计口径漂移）
        self.assertEqual(
            set(task.runtime["enhance"]),
            {"attempted", "succeeded", "skipped", "failures", "circuit_open", "log_id"},
        )
        self.assertEqual(
            set(task.runtime["upscale"]),
            {"attempted", "succeeded", "skipped", "failures", "circuit_open", "log_id"},
        )
        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(task.errors, [])

    async def test_upscale_over_the_pixel_limit_keeps_the_enhanced_output(self):
        """U8：增强成功、放大 ``pixels_too_large`` ⇒ 交付**增强产物**（不回退原图）、任务 succeeded。"""
        # 增强产物 2001×2000 = 4 002 000 px > 4 000 000 ⇒ 放大本地跳过
        session = FakeSession(token=token_payload(), enhance=image_payload(2001, 2000))
        task, delivered, out_dir, enhance_state, upscale_state, _, _ = await self.run_chain(
            results=1, session=session, enhance_cfg=enhance_config(), upscale_cfg=upscale_config(),
        )
        self.assertEqual(session.endpoints(), ["token", "definition"])  # 只 2 笔：token + 增强
        self.assertEqual(upscale_state.skipped, ["pixels_too_large"])
        self.assertEqual(upscale_state.succeeded, 0)
        self.assertEqual(upscale_state.attempted, 0)
        self.assertEqual(len(delivered), 1)
        enhanced_path = delivered[0]
        self.assertEqual(logic.image_dimensions(enhanced_path.read_bytes()), (2001, 2000))
        # 交付的是增强产物：不是原图（原图 64×64），也不是放大图
        self.assertNotEqual(logic.image_dimensions(enhanced_path.read_bytes()), (64, 64))
        self.assertEqual(len(list(out_dir.iterdir())), 2)  # 原图 + 增强产物
        self.assertEqual(task.runtime["upscale"]["skipped"], ["pixels_too_large"])
        self.assertEqual(task.runtime["enhance"]["succeeded"], 1)
        # 任务仍 succeeded：放大跳过**不是**失败（不写 errors、不改 state）
        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(task.errors, [])

    async def test_upscale_over_the_byte_limit_also_keeps_the_enhanced_output(self):
        """U8 的字节半边：增强产物 3 145 729 B > 4M 的原图等价上限 ⇒ 放大 ``too_large`` 跳过。"""
        big = png_of_size(3_145_729, 1000, 1000)   # 恰好越过 4M base64 的等价原图上限
        session = FakeSession(
            token=token_payload(),
            enhance={"log_id": 9, "image": base64.b64encode(big).decode()},
        )
        task, delivered, out_dir, enhance_state, upscale_state, _, _ = await self.run_chain(
            results=1, session=session, enhance_cfg=enhance_config(), upscale_cfg=upscale_config(),
        )
        # 增强级的判据是 10M（3.1 MB 远未到），放大级的判据是 4M ⇒ 只有放大被跳过
        self.assertEqual(enhance_state.succeeded, 1)
        self.assertEqual(session.endpoints(), ["token", "definition"])
        self.assertEqual(upscale_state.skipped, ["too_large"])
        self.assertEqual(upscale_state.attempted, 0)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0].read_bytes(), big)      # 交付增强产物，不回退原图
        self.assertEqual(task.runtime["upscale"]["skipped"], ["too_large"])
        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(task.errors, [])

    async def test_circuits_are_independent_between_the_two_stages(self):
        """U9：第 1 张放大 ``error_code=6`` ⇒ 放大熔断、增强继续；两图都交付增强产物。"""
        session = FakeSession(
            token=token_payload(),
            enhance=image_payload(1000, 1000),
            upscale={"error_code": 6, "error_msg": "No permission to access data"},
        )
        task, delivered, out_dir, enhance_state, upscale_state, _, warns = await self.run_chain(
            results=2, session=session, enhance_cfg=enhance_config(), upscale_cfg=upscale_config(),
        )
        # 序列：token(增强) → 增强#1 → token(放大) → 放大#1(失败) → 增强#2（放大不再尝试）
        self.assertEqual(
            session.endpoints(), ["token", "definition", "token", "quality", "definition"]
        )
        self.assertEqual(upscale_state.attempted, 1)
        self.assertEqual(upscale_state.failures, ["permission:6"])
        self.assertTrue(upscale_state.circuit_open)
        self.assertFalse(enhance_state.circuit_open)   # 增强的熔断标志一点没动
        self.assertEqual(enhance_state.succeeded, 2)
        self.assertFalse(task.runtime["enhance"]["circuit_open"])
        # 两图都交付**增强产物**（放大失败保留上一级），尺寸都是 1000×1000
        self.assertEqual(len(delivered), 2)
        for path in delivered:
            self.assertEqual(logic.image_dimensions(path.read_bytes()), (1000, 1000))
        self.assertEqual(len(list(out_dir.iterdir())), 4)  # 2 原图 + 2 增强产物
        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(task.errors, [])
        self.assertTrue(any("图像无损放大" in message for message, _ in warns))

    async def test_upscale_alone_leaves_no_enhance_report(self):
        """U10：增强关闭、放大开启 ⇒ 只 1 级调用，runtime 只有 ``upscale`` 键。"""
        session = FakeSession(token=token_payload(), upscale=image_payload(128, 128))
        task, delivered, out_dir, enhance_state, upscale_state, _, _ = await self.run_chain(
            results=1, session=session, enhance_cfg=None, upscale_cfg=upscale_config(),
        )
        self.assertEqual(session.endpoints(), ["token", "quality"])
        self.assertEqual(len(delivered), 1)
        self.assertEqual(upscale_state.succeeded, 1)
        self.assertEqual(enhance_state.succeeded, 0)
        self.assertIn("upscale", task.runtime)
        self.assertNotIn("enhance", task.runtime)
        self.assertEqual(len(list(out_dir.iterdir())), 2)  # 原图 + 放大图
        self.assertEqual(task.state, TaskState.SUCCEEDED)


class UpscaleWiringGuardTests(unittest.TestCase):
    """U7/U15 的**源序**半边：main.py 的两级插入点与 INFO 汇总行逐字钉住。

    为什么是静态断言：这两处都在 ``ImagoFusion._finish_task`` 的循环体与终态日志里，
    要执行它们得先造一个完整的 AstrBot 插件实例（本文件按 v3.10.1 明令"不自建 astrbot
    桩"）。所以「运行期行为」由 ``UpscaleChainTests`` 保证，「插入点顺序」与「INFO 汇总
    行的**整段调用**（含实参顺序）」由本节按源码位置钉住——与既有 E11 端点守卫同一套路。

    「只断言格式串存在」不够：把 ``upscale_stats.get("succeeded", 0)`` 与
    ``len(upscale_stats.get("skipped") or [])`` 两个实参对调，格式串与出现顺序都不变，
    线上 INFO 行的「成功/跳过」却会静默错位（评审 t4 的 mutation F 就是这样漏过的）。
    因此 ``EXPECTED_UPSCALE_INFO`` 把整段调用（含每个实参一行一项、顺序在内）写死。
    """

    # `logger.info` 整段调用（规范化：每行 strip 后逐行比对）。任何一行被改写、两个数字
    # 实参被对调、或多/少一个实参，都会让本列表与源码不等 ⇒ 门禁必红。
    EXPECTED_UPSCALE_INFO = [
        'upscale_stats = task.runtime.get("upscale")',
        "if upscale_stats:",
        "logger.info(",
        '"%s task=%s 放大 成功=%d 跳过=%d 失败=%d",',
        "prefix, task.id[:8],",
        'upscale_stats.get("succeeded", 0),',
        'len(upscale_stats.get("skipped") or []),',
        'len(upscale_stats.get("failures") or []),',
        ")",
    ]

    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "main.py").read_text("utf-8")

    def test_upscale_info_call_is_pinned_verbatim_with_its_argument_order(self):
        """U15 的 INFO 半边：整段 ``logger.info``（5 行有效代码 + 实参顺序）逐字比对。"""
        start = self.source.index('upscale_stats = task.runtime.get("upscale")')
        lines = [line.strip() for line in self.source[start:].splitlines()]
        end = lines.index(")") + 1
        self.assertEqual(
            lines[:end],
            self.EXPECTED_UPSCALE_INFO,
            "放大 INFO 汇总行的调用被改写（实参顺序/文本变了）："
            "冻结口径是 prefix, task.id[:8], succeeded, len(skipped), len(failures)",
        )

    def test_materialize_then_enhance_then_upscale_in_the_same_loop(self):
        source = self.source
        materialize = source.index("value = await materialize_result(")
        enhance = source.index("value = await self.enhancer.apply(")
        upscale = source.index("value = await self.upscaler.apply(")
        append = source.index("if isinstance(value, Path): output_paths.append(value)")
        self.assertLess(materialize, enhance, "增强必须在 materialize 之后")
        self.assertLess(enhance, upscale, "放大必须在增强之后（顺序不可交换）")
        self.assertLess(upscale, append, "放大必须在 output_paths 收集之前（否则替换不了交付物）")
        self.assertIn("upscale_cfg = result_cfg.upscale", source)
        self.assertIn("upscale_state = UpscaleState()", source)
        self.assertIn("self.upscaler = BaiduUpscaler(", source)

    def test_upscale_info_summary_is_its_own_line_right_after_the_enhance_one(self):
        source = self.source
        enhance_line = '"%s task=%s 增强 成功=%d 跳过=%d 失败=%d"'
        upscale_line = '"%s task=%s 放大 成功=%d 跳过=%d 失败=%d"'
        self.assertIn(enhance_line, source)
        self.assertIn(upscale_line, source)
        self.assertLess(source.index(enhance_line), source.index(upscale_line))
        # 只在本次任务真的碰过放大时才打（关闭时 runtime 无该键 ⇒ 零新增 INFO 行）
        gate = source.index('upscale_stats = task.runtime.get("upscale")')
        self.assertLess(gate, source.index(upscale_line))
        self.assertIn("if upscale_stats:", source[gate:source.index(upscale_line) + 200])


class UpscaleLoggingTests(unittest.IsolatedAsyncioTestCase):
    """U15：WARNING 每类一次 + 「放大 失败：」前缀；DEBUG 每图一条三条格式；关闭时 0 条。"""

    async def _run(self, *, session: FakeSession, cfg=None, data: bytes | None = None):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out_dir = Path(directory.name)
        original = out_dir / "origin.png"
        original.write_bytes(data or png_bytes())
        debug, warns = [], []
        task = make_task()
        state = UpscaleState()
        upscaler = upscaler_of(session, debug=debug_recorder(debug), warn=recorder(warns))
        value = await upscaler.apply(task, original, out_dir, cfg or upscale_config(), state)
        return task, value, original, debug, warns

    async def test_debug_lines_are_one_per_image_with_the_frozen_formats(self):
        session = FakeSession(token=token_payload(), upscale=image_payload(128, 128, log_id=33))
        task, value, _, debug, _ = await self._run(session=session)
        self.assertEqual(len(debug), 1)
        message, args = debug[0]
        self.assertEqual(
            message,
            "放大 完成 index=%d in_bytes=%d out_bytes=%d dims=%dx%d->%dx%d cost=%.1fs log_id=%s",
        )
        self.assertEqual(args[0], 1)                       # index = state.succeeded
        self.assertEqual((args[3], args[4], args[5], args[6]), (64, 64, 128, 128))
        self.assertEqual(args[-1], 33)
        # 读不出尺寸时该段写 dims=-（另一条逐字格式）
        session = FakeSession(
            token=token_payload(),
            upscale={"log_id": 5, "image": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4).decode()},
        )
        task, value, _, debug, _ = await self._run(session=session)
        self.assertEqual(len(debug), 1)
        self.assertEqual(
            debug[0][0],
            "放大 完成 index=%d in_bytes=%d out_bytes=%d dims=- cost=%.1fs log_id=%s",
        )
        # 跳过：``放大 跳过 index=%d reason=%s %s``
        session = FakeSession(token=token_payload(), upscale=image_payload(128, 128))
        task, value, original, debug, _ = await self._run(
            session=session, data=png_of_size(3_145_729)
        )
        self.assertEqual(debug[0][0], "放大 跳过 index=%d reason=%s %s")
        self.assertEqual(debug[0][1][1], "too_large")

    async def test_failure_line_and_warning_are_emitted_once(self):
        session = FakeSession(
            token=token_payload(),
            upscale={"error_code": 17, "error_msg": "Open api daily request limit reached"},
        )
        debug, warns = [], []
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out_dir = Path(directory.name)
        original = out_dir / "origin.png"
        original.write_bytes(png_bytes())
        task = make_task()
        state = UpscaleState()
        upscaler = upscaler_of(session, debug=debug_recorder(debug), warn=recorder(warns))
        for _ in range(2):
            await upscaler.apply(task, original, out_dir, upscale_config(), state)
        # quota 族：不重试、告警一次、熔断；第二次连 token 都不取
        self.assertEqual(len(warns), 1)
        self.assertTrue(warns[0][0].startswith("放大 失败："))
        self.assertIn("额度已用尽", warns[0][0])
        self.assertIn("17", str(warns[0][1]))
        self.assertEqual(debug[0][0], "放大 失败 index=%d kind=%s code=%s detail=%s")
        self.assertEqual(debug[0][1][1], "quota")

    async def test_switch_off_emits_no_log_at_all(self):
        session = FakeSession(token=token_payload(), upscale=image_payload(128, 128))
        task, value, original, debug, warns = await self._run(
            session=session, cfg=UpscaleConfig()
        )
        self.assertIs(value, original)
        self.assertEqual((debug, warns), ([], []))
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
