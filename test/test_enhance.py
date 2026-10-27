"""百度「图像清晰度增强」的行为用例（E1–E11；全部不碰网络、不碰生产）。

形状照抄 ``test/test_scheduler.py``：``FakeSession`` 记录 ``(url, kwargs)`` 并按 URL
返回预置响应，``unittest.IsolatedAsyncioTestCase`` 驱动协程。**不**放进
``test_native_adapter.py``（那个文件自建 astrbot 桩，会互相干扰）。

覆盖的契约点（spec 变更令 v2.10）：

- E1 正常增强：新文件落盘、原图保留、token/增强两次调用的参数形状与端点；
- E2–E5 本地预检超限（10MiB / 最长边 5000px / 最短边 10px / 长宽比 4:1）跳过，且**零网络**；
- E6 开关开着但 key 不全 ⇒ 完全静默（连 token 都不取），告警只打一次；
- E7 开关关闭 ⇒ 零网络、零 runtime 键、零日志、状态对象一点没动；
- E8 网络失败 / 无 ``image`` 字段 ⇒ 降级原图 + 本任务熔断 + **绝不写 task.errors**；
- E9 token 过期（110）⇒ 失效重取 + 恰好重试一次（调用序列恰好 4 次）；
- E10 ``error_code=6``（应用未勾选「图像清晰度增强」）⇒ 不重试、告警一次、熔断；
- E10 之后：输出超过 storage 上限 ⇒ 跳过、不落盘（spec 未给单独编号，随 E8/E10 的降级语义一并覆盖）；
- E11 端点守卫（见**本文件** ``EnhanceEndpointGuardTests``）：``ENHANCE_URL`` 必须是
  ``image_definition_enhance``，且 ``main.py`` / ``core/*.py`` / ``_conf_schema.json``
  里 0 处旧端点 id。
"""

import asyncio
import base64
import struct
import time
import unittest
from collections import deque
from pathlib import Path
from tempfile import TemporaryDirectory

import aiohttp

from imago_fusion.core import enhance as logic
from imago_fusion.core.models import DrawTask, GenerationRequest, RuntimeConfig, TaskState, EnhanceConfig
from imago_fusion.core.enhance_client import BaiduEnhancer, EnhanceState

ROOT = Path(__file__).resolve().parents[1]

TOKEN_TEST = "TOKEN_TEST"


def png_bytes(width: int = 64, height: int = 64, body: bytes = b"\x00" * 8) -> bytes:
    """最小 PNG 头：够魔数 + IHDR 尺寸嗅探（不做真实解码）。

    默认 64×64：必须同时满足官方三项几何限值（最短边 ≥10px、最长边 ≤5000px、
    长宽比 ≤4:1），否则连 happy path 都会被本地预检拦成 ``edge_too_short``。
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
    """增强客户端要读的运行时配置（超时预算 / 输出上限 / 代理）。"""
    values = {"providers": (), "generation_timeout": 300, "max_upload_bytes": 20 * 1024 * 1024}
    values.update(overrides)
    return RuntimeConfig(**values)


def enhance_config(**overrides) -> EnhanceConfig:
    """默认「开关打开 + 两把 key 齐全」；单条用例只覆盖自己关心的键。"""
    values = {"enabled": True, "api_key": "API_KEY_TEST", "secret_key": "SECRET_KEY_TEST"}
    values.update(overrides)
    return EnhanceConfig(**values)


def make_task(task_id: str = "a" * 16) -> DrawTask:
    task = DrawTask(task_id, "umo", GenerationRequest("画一张测试图"))
    task.created_at = time.monotonic()
    return task


def recorder(sink: list):
    """把 ``(message, args)`` 收进列表的 **WARNING** 回调（``warn=``）。"""

    def _record(message, *args):
        sink.append((message, args))

    return _record


def debug_recorder(sink: list):
    """把 ``(message, args)`` 收进列表的 **DEBUG** 回调（``debug=``）。

    ``main.py`` 注入的调试回调用的是 ``_scheduler_debug(task, message, *args)``
    形状（第一个参数是任务），这里保持同形，免得测试与生产签名漂移。
    """

    def _record(task, message, *args):
        sink.append((message, args))

    return _record


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


def _status_sequence(value) -> tuple[deque, int]:
    """把 ``200`` / ``[401, 200]`` 归一成 ``(队列, 末位默认值)``；``None`` ⇒ 200。

    与 ``FakeSession._take()`` 配合：队列里多于一个时按调用顺序 pop，剩最后一个时
    重复使用（最后一条状态就是「后续都这样」）。
    """
    values = list(value) if isinstance(value, (list, tuple)) else [value]
    values = [200 if item is None else int(item) for item in values]
    return deque(values), (values[-1] if values else 200)


class _PostContext:
    """``session.post(...)`` 的异步上下文管理器（延迟/异常在这里发生）。"""

    def __init__(self, session: "FakeSession", url: str) -> None:
        self.session = session
        self.url = url

    async def __aenter__(self):
        session = self.session
        if self.url.startswith(logic.TOKEN_URL):
            if session.token_delay:
                await asyncio.sleep(session.token_delay)
            if session.token_exc is not None:
                raise session.token_exc
            return FakeResponse(
                session._take(session.token_payloads, session.token_default),
                session._take(session.token_statuses, session.token_status_default),
            )
        if session.enhance_delay:
            await asyncio.sleep(session.enhance_delay)
        if session.enhance_exc is not None:
            raise session.enhance_exc
        return FakeResponse(
            session._take(session.enhance_payloads, session.enhance_default),
            session._take(session.enhance_statuses, session.enhance_status_default),
        )

    async def __aexit__(self, *_exc):
        return False


class FakeSession:
    """记录 ``(url, kwargs)``；按 URL 前缀返回预置响应；**不作任何真实 IO**。"""

    def __init__(
        self,
        *,
        token=None,
        enhance=None,
        token_status=None,
        enhance_status=None,
        token_exc: BaseException | None = None,
        enhance_exc: BaseException | None = None,
        token_delay: float = 0.0,
        enhance_delay: float = 0.0,
    ) -> None:
        token_list = list(token) if isinstance(token, (list, tuple)) else [token]
        enhance_list = list(enhance) if isinstance(enhance, (list, tuple)) else [enhance]
        self.token_payloads = deque(token_list)
        self.enhance_payloads = deque(enhance_list)
        self.token_default = token_list[-1] if token_list else None
        self.enhance_default = enhance_list[-1] if enhance_list else None
        self.token_statuses, self.token_status_default = _status_sequence(token_status)
        self.enhance_statuses, self.enhance_status_default = _status_sequence(enhance_status)
        self.token_exc = token_exc
        self.enhance_exc = enhance_exc
        self.token_delay = token_delay
        self.enhance_delay = enhance_delay
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def _take(queue: deque, default):
        if not queue:
            return default
        return queue.popleft() if len(queue) > 1 else queue[0]

    @property
    def token_calls(self) -> list[tuple[str, dict]]:
        return [call for call in self.calls if call[0].startswith(logic.TOKEN_URL)]

    @property
    def enhance_calls(self) -> list[tuple[str, dict]]:
        return [call for call in self.calls if call[0].startswith(logic.ENHANCE_URL)]

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _PostContext(self, url)


def token_payload(token: str = TOKEN_TEST) -> dict:
    return {"access_token": token, "expires_in": 2592000}


def enhance_payload(width: int = 4, height: int = 4, *, log_id: int = 7) -> dict:
    return {"log_id": log_id, "image": base64.b64encode(png_bytes(width, height, b"\x00" * 16)).decode()}


class EnhanceHappyPathTests(unittest.IsolatedAsyncioTestCase):
    """E1：开关打开 + key 齐全 ⇒ 交付图是增强结果（尺寸不变），原图保留。"""

    async def test_happy_path_writes_a_new_file_and_keeps_the_original(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            original = out_dir / "origin.png"
            original.write_bytes(png_bytes())
            enhanced = png_bytes(4, 4, b"\x00" * 16)
            session = FakeSession(
                token=token_payload(),
                enhance={"log_id": 7, "image": base64.b64encode(enhanced).decode()},
            )
            debugs: list = []
            warns: list = []
            enhancer = BaiduEnhancer(
                session, lambda: runtime_config(), debug=debug_recorder(debugs), warn=recorder(warns)
            )
            task = make_task()
            state = EnhanceState()

            value = await enhancer.apply(task, original, out_dir, enhance_config(), state)

            # ① 返回新 Path（≠ 入参）② 内容就是增强结果 ③ 原图仍在
            self.assertIsInstance(value, Path)
            self.assertNotEqual(value, original)
            self.assertEqual(value.read_bytes(), enhanced)
            self.assertTrue(original.is_file(), "原图必须保留（兜底/对账）")
            self.assertEqual(value.parent, out_dir, "增强图必须落在任务缓存目录")
            # ④ 统计
            self.assertEqual((state.attempted, state.succeeded), (1, 1))
            self.assertEqual(task.runtime["enhance"]["succeeded"], 1)
            self.assertEqual(task.errors, [])
            # ⑤ 两次调用：token 在前、增强在后，access_token 走查询参数
            self.assertEqual([url for url, _ in session.calls], [logic.TOKEN_URL, logic.ENHANCE_URL])
            _, enhance_kwargs = session.calls[1]
            self.assertTrue(
                session.calls[1][0].endswith("/rest/2.0/image-process/v1/image_definition_enhance"),
                f"端点必须换成官方 image_definition_enhance：{session.calls[1][0]}",
            )
            self.assertEqual(enhance_kwargs["params"]["access_token"], TOKEN_TEST)
            # ⑥ 表单里的 base64 不带图片头，且与原图字节一一对应
            encoded = enhance_kwargs["data"]["image"]
            self.assertFalse(encoded.startswith("data:"))
            self.assertEqual(base64.b64decode(encoded), original.read_bytes())
            # ⑦ 日志：一条 DEBUG（含 in/out 字节与 log_id），无 WARNING
            self.assertEqual(len(debugs), 1)
            self.assertIn("增强 完成", debugs[0][0])
            self.assertEqual(warns, [])

    async def test_proxy_is_passed_only_when_enabled_and_configured(self):
        """代理只在 `enable_proxy` 且顶层 `proxy_url` 非空时逐请求传（不改 session）。"""
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            original = out_dir / "origin.png"
            original.write_bytes(png_bytes())
            proxy_url = "http://127.0.0.1:7890"

            proxied = FakeSession(token=token_payload(), enhance=enhance_payload())
            enhancer = BaiduEnhancer(
                proxied,
                lambda: runtime_config(proxy_url=proxy_url),
                debug=debug_recorder([]),
                warn=recorder([]),
            )
            proxied_task = make_task()
            await enhancer.apply(proxied_task, original, out_dir, enhance_config(enable_proxy=True), EnhanceState())
            self.assertEqual([kwargs.get("proxy") for _, kwargs in proxied.calls], [proxy_url, proxy_url])
            self.assertEqual(proxied_task.errors, [])

            direct = FakeSession(token=token_payload(), enhance=enhance_payload())
            enhancer = BaiduEnhancer(
                direct,
                lambda: runtime_config(proxy_url=proxy_url),
                debug=debug_recorder([]),
                warn=recorder([]),
            )
            # 开着代理但顶层地址为空 ⇒ 直连（不能把空串当代理传给 aiohttp）
            empty = FakeSession(token=token_payload(), enhance=enhance_payload())
            enhancer_empty = BaiduEnhancer(
                empty, lambda: runtime_config(), debug=debug_recorder([]), warn=recorder([])
            )
            await enhancer_empty.apply(make_task(), original, out_dir, enhance_config(enable_proxy=True), EnhanceState())
            self.assertEqual([kwargs.get("proxy") for _, kwargs in empty.calls], [None, None])
            # 开关没开 ⇒ 即使配了 proxy_url 也不传
            await enhancer.apply(make_task(), original, out_dir, enhance_config(), EnhanceState())
            self.assertEqual([kwargs.get("proxy") for _, kwargs in direct.calls], [None, None])


class EnhancePrecheckTests(unittest.IsolatedAsyncioTestCase):
    """E2–E5：本地预检按官方限值跳过，绝**不**发无效请求（省一次计费）。"""

    async def _run(self, image: bytes, **cfg_overrides):
        """把一张图跑一遍 apply；返回 (value, original, session, state, debugs)。"""
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        out_dir = Path(directory.name)
        original = out_dir / "input.png"
        original.write_bytes(image)
        session = FakeSession(token=token_payload(), enhance=enhance_payload())
        debugs: list = []
        enhancer = BaiduEnhancer(
            session, lambda: runtime_config(), debug=debug_recorder(debugs), warn=recorder([])
        )
        state = EnhanceState()
        value = await enhancer.apply(make_task(), original, out_dir, enhance_config(**cfg_overrides), state)
        return value, original, session, state, debugs

    async def test_too_large_image_is_skipped_without_any_request(self):
        # E2：base64 ≤10M ⇒ 原图字节上限 = 10 MiB × 3/4 = 7 864 320 B
        limit = logic.MAX_RAW_BYTES_FOR_10MB_BASE64
        self.assertEqual(limit, 7_864_320)

        value, original, session, state, _ = await self._run(png_of_size(limit + 1))
        self.assertEqual(value, original, "降级必须原样返回入参")
        self.assertEqual(session.calls, [], "预检跳过必须零网络")
        self.assertEqual(state.skipped, ["too_large"])
        self.assertEqual(sorted(path.name for path in original.parent.iterdir()), ["input.png"])

        # 边界：恰好等于上限必须放行（百度口径是「不超过」）
        value, original, session, state, _ = await self._run(png_of_size(limit))
        self.assertNotEqual(value, original)
        self.assertEqual(state.attempted, 1)
        self.assertEqual(state.skipped, [])

    async def test_long_edge_over_limit_is_skipped_but_the_boundary_is_allowed(self):
        # E3：最长边 ≤ 5000px
        value, original, session, state, _ = await self._run(png_bytes(6000, 600))
        self.assertEqual(value, original)
        self.assertEqual(session.calls, [])
        self.assertEqual(state.skipped, ["edge_too_long"])

        # 边界：最长边恰好 5000 放行。注意必须同时满足长宽比 ≤4:1，故用 5000×1250
        # （spec v2.10 E3 写的 5000×1000 实际是 5:1，会被 aspect_too_wide 拦下）。
        value, original, session, state, _ = await self._run(png_bytes(5000, 1250))
        self.assertNotEqual(value, original)
        self.assertEqual(state.attempted, 1)
        self.assertEqual(state.skipped, [])

    async def test_short_edge_under_limit_is_skipped(self):
        # E4：最短边 ≥ 10px（硬下限，非配置键）
        value, original, session, state, _ = await self._run(png_bytes(3000, 8))
        self.assertEqual(value, original)
        self.assertEqual(session.calls, [])
        self.assertEqual(state.skipped, ["edge_too_short"])

        # 边界：最短边恰好 10 放行（用 40×10 恰好 4:1；3000×10 会撞长宽比）
        value, original, session, state, _ = await self._run(png_bytes(40, 10))
        self.assertNotEqual(value, original)
        self.assertEqual(state.attempted, 1)
        self.assertEqual(state.skipped, [])

    async def test_aspect_ratio_over_limit_is_skipped_but_the_boundary_is_allowed(self):
        # E5：长宽比 ≤ 4:1
        value, original, session, state, _ = await self._run(png_bytes(5000, 1000))  # 5:1
        self.assertEqual(value, original)
        self.assertEqual(session.calls, [])
        self.assertEqual(state.skipped, ["aspect_too_wide"])

        # 边界：恰好 4:1 允许
        value, original, session, state, _ = await self._run(png_bytes(4000, 1000))
        self.assertNotEqual(value, original)
        self.assertEqual(state.attempted, 1)
        self.assertEqual(state.skipped, [])

    async def test_precheck_table_covers_every_limit_format_and_empty(self):
        """表驱动：四条限值 + 格式 + 空图，逐条断言「不请求、不落盘、DEBUG 记 reason」。"""
        limit = logic.MAX_RAW_BYTES_FOR_10MB_BASE64
        cases = (
            ("empty", b"", "empty"),
            ("format_webp", b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16, "format"),
            ("too_large", png_of_size(limit + 1), "too_large"),
            ("edge_too_short", png_bytes(3000, 8), "edge_too_short"),
            ("edge_too_long", png_bytes(6000, 600), "edge_too_long"),
            ("aspect_too_wide", png_bytes(5000, 1000), "aspect_too_wide"),
            ("allowed", png_bytes(64, 64), None),
        )
        for name, image, reason in cases:
            with self.subTest(case=name):
                value, original, session, state, debugs = await self._run(image)
                if reason is None:
                    self.assertNotEqual(value, original)
                    self.assertEqual(state.attempted, 1)
                    self.assertEqual(state.skipped, [])
                    continue
                self.assertEqual(value, original)
                self.assertEqual(session.calls, [], "预检跳过必须零网络")
                self.assertEqual(state.attempted, 0)
                self.assertEqual(state.skipped, [reason])
                # DEBUG 是 %-模板 + 位置参数（main.py 的 _scheduler_debug 会脱敏参数），
                # 所以按「模板含 reason=%s 且参数里确实有该 reason」判定。
                self.assertTrue(
                    any("跳过" in message and reason in args for message, args in debugs),
                    f"DEBUG 必须记下 reason={reason}：{debugs}",
                )
                self.assertEqual(sorted(path.name for path in original.parent.iterdir()), ["input.png"])


class EnhanceGatingTests(unittest.IsolatedAsyncioTestCase):
    """E6 / E7：未配置与关闭两条「零副作用」路径。"""

    async def test_enabled_without_keys_is_silent_and_warns_only_once(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            original = out_dir / "origin.png"
            original.write_bytes(png_bytes())
            session = FakeSession(token=token_payload(), enhance=enhance_payload())
            warns: list = []
            enhancer = BaiduEnhancer(session, lambda: runtime_config(), debug=debug_recorder([]), warn=recorder(warns))
            task = make_task()
            cfg = enhance_config(api_key="")

            value = await enhancer.apply(task, original, out_dir, cfg, EnhanceState())
            self.assertEqual(value, original)
            self.assertEqual(session.calls, [], "key 不全时连 token 接口都不能调")
            self.assertNotIn("enhance", task.runtime, "未配置＝完全静默（不建 runtime 键）")
            self.assertEqual(len(warns), 1)
            self.assertIn("API Key", warns[0][0])

            # 再跑一次任务：去重后不再告警
            await enhancer.apply(task, original, out_dir, cfg, EnhanceState())
            self.assertEqual(len(warns), 1)

    async def test_disabled_switch_touches_nothing(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            original = out_dir / "origin.png"
            original.write_bytes(png_bytes())
            session = FakeSession(token=token_payload(), enhance=enhance_payload())
            debugs: list = []
            warns: list = []
            enhancer = BaiduEnhancer(session, lambda: runtime_config(), debug=debug_recorder(debugs), warn=recorder(warns))
            task = make_task()
            state = EnhanceState()

            value = await enhancer.apply(task, original, out_dir, EnhanceConfig(), state)

            self.assertEqual(value, original)
            self.assertEqual(session.calls, [], "开关关闭必须零网络")
            self.assertNotIn("enhance", task.runtime)
            self.assertEqual(state, EnhanceState(), "关闭路径不能碰统计对象")
            self.assertEqual(debugs, [])
            self.assertEqual(warns, [])


class EnhanceTokenTests(unittest.IsolatedAsyncioTestCase):
    """E9：token 失效重取（恰好一次）与并发单飞。"""

    async def test_expired_token_refreshes_once_and_retries_once(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            original = out_dir / "origin.png"
            original.write_bytes(png_bytes())
            session = FakeSession(
                token=[token_payload("A"), token_payload("B")],
                enhance=[{"error_code": 110, "error_msg": "Access token invalid or no longer valid"}, enhance_payload()],
            )
            enhancer = BaiduEnhancer(session, lambda: runtime_config(), debug=debug_recorder([]), warn=recorder([]))
            task = make_task()
            state = EnhanceState()

            value = await enhancer.apply(task, original, out_dir, enhance_config(), state)

            self.assertNotEqual(value, original, "重取 token 后重试一次应当成功")
            self.assertEqual(
                [url for url, _ in session.calls],
                [logic.TOKEN_URL, logic.ENHANCE_URL, logic.TOKEN_URL, logic.ENHANCE_URL],
                "调用序列必须恰好 4 次（不多不少）",
            )
            self.assertEqual(session.enhance_calls[0][1]["params"]["access_token"], "A")
            self.assertEqual(session.enhance_calls[1][1]["params"]["access_token"], "B")
            self.assertEqual(state.succeeded, 1)
            self.assertEqual(state.failures, [])

            # 缓存里现在是 B：下一张图不再取 token
            second = out_dir / "second.png"
            second.write_bytes(png_bytes())
            calls_before = len(session.calls)
            await enhancer.apply(task, second, out_dir, enhance_config(), EnhanceState())
            self.assertEqual(len(session.token_calls), 2, "缓存命中后不得再打 token 接口")
            self.assertEqual(len(session.calls) - calls_before, 1)

    async def test_concurrent_enhances_share_one_token_request(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            session = FakeSession(
                token=token_payload(), enhance=enhance_payload(), token_delay=0.05
            )
            enhancer = BaiduEnhancer(session, lambda: runtime_config(), debug=debug_recorder([]), warn=recorder([]))

            files = []
            for index in range(5):
                path = out_dir / f"image_{index}.png"
                path.write_bytes(png_bytes())
                files.append(path)

            async def run(path: Path):
                return await enhancer.apply(
                    make_task(f"{index:032x}"), path, out_dir, enhance_config(), EnhanceState()
                )

            results = await asyncio.gather(*(run(path) for path in files))

            self.assertEqual(len(session.token_calls), 1, "并发任务只允许打一次 token 接口")
            self.assertEqual(len(session.enhance_calls), 5)
            for path, value in zip(files, results):
                self.assertNotEqual(value, path, "5 次增强都应成功")
            tokens = {kwargs["params"]["access_token"] for _, kwargs in session.enhance_calls}
            self.assertEqual(tokens, {TOKEN_TEST})


class EnhanceFailureTests(unittest.IsolatedAsyncioTestCase):
    """E8 / E10：失败与跳过的降级语义（**不碰 task.errors**）。

    含「输出超过 storage 上限」的跳过路径（spec 未单列编号）与 ``error_code=6`` 的
    不重试/熔断路径。
    """

    async def test_network_failure_degrades_and_opens_the_circuit(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            first = out_dir / "first.png"
            second = out_dir / "second.png"
            first.write_bytes(png_bytes())
            second.write_bytes(png_bytes())
            session = FakeSession(
                token=token_payload(), enhance=enhance_payload(), enhance_exc=aiohttp.ClientError("boom")
            )
            enhancer = BaiduEnhancer(session, lambda: runtime_config(), debug=debug_recorder([]), warn=recorder([]))
            task = make_task()
            state = EnhanceState()

            value = await enhancer.apply(task, first, out_dir, enhance_config(), state)
            self.assertEqual(value, first)
            self.assertTrue(state.circuit_open, "网络失败后本任务必须熔断")

            value = await enhancer.apply(task, second, out_dir, enhance_config(), state)
            self.assertEqual(value, second)
            self.assertEqual(len(session.calls), 2, "熔断后第二张图零网络（1 token + 1 增强）")
            self.assertEqual(task.errors, [], "增强失败绝不能写 task.errors")
            failures = task.runtime["enhance"]["failures"]
            self.assertEqual(len(failures), 1)
            self.assertTrue(failures[0].startswith("transient"), failures)
            self.assertEqual(sorted(path.name for path in out_dir.iterdir()), ["first.png", "second.png"])

    async def test_failure_never_degrades_the_task_state_or_quota(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            original = out_dir / "origin.png"
            original.write_bytes(png_bytes())
            session = FakeSession(
                token=token_payload(),
                enhance={"error_code": 216401, "error_msg": "internal error"},
            )
            enhancer = BaiduEnhancer(session, lambda: runtime_config(), debug=debug_recorder([]), warn=recorder([]))
            task = make_task()
            task.runtime["quota_charged"] = 1

            await enhancer.apply(task, original, out_dir, enhance_config(), EnhanceState())

            # 增强失败对任务唯一的痕迹是 runtime["enhance"]。
            self.assertEqual(task.errors, [], "task.errors 一非空，用户就会看到『部分图片绘制失败。』")
            self.assertEqual(task.state, TaskState.CREATED, "增强不得改任务状态")
            self.assertEqual(task.runtime["quota_charged"], 1)
            self.assertEqual(task.runtime["enhance"]["circuit_open"], True)

    async def test_permission_error_warns_once_and_does_not_retry(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            original = out_dir / "origin.png"
            original.write_bytes(png_bytes())
            session = FakeSession(
                token=token_payload(),
                enhance={"error_code": 6, "error_msg": "No permission to access data"},
            )
            warns: list = []
            enhancer = BaiduEnhancer(session, lambda: runtime_config(), debug=debug_recorder([]), warn=recorder(warns))
            task = make_task()
            state = EnhanceState()

            value = await enhancer.apply(task, original, out_dir, enhance_config(), state)

            self.assertEqual(value, original)
            self.assertEqual(len(session.enhance_calls), 1, "无权限不重试（重试也不会变对）")
            self.assertEqual(len(warns), 1)
            self.assertIn("图像清晰度增强", warns[0][0], "告警文案必须点名控制台未勾选的能力")
            self.assertTrue(state.circuit_open)
            self.assertEqual(state.failures, ["permission:6"])

    async def test_oversized_output_is_skipped_without_writing_a_file(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            original = out_dir / "origin.png"
            original.write_bytes(png_bytes())
            session = FakeSession(token=token_payload(), enhance=enhance_payload(8, 8))
            debugs: list = []
            enhancer = BaiduEnhancer(
                session,
                lambda: runtime_config(max_upload_bytes=16),
                debug=debug_recorder(debugs),
                warn=recorder([]),
            )
            task = make_task()
            state = EnhanceState()

            value = await enhancer.apply(task, original, out_dir, enhance_config(), state)

            self.assertEqual(value, original)
            self.assertIn("output_oversize", state.skipped)
            self.assertEqual(sorted(path.name for path in out_dir.iterdir()), ["origin.png"])
            self.assertEqual(state.succeeded, 0)


class EnhanceHttpStatusTests(unittest.IsolatedAsyncioTestCase):
    """E9 的 HTTP 状态码分支（执行级）：401 重取一次 / 500 只重试一次后降级。

    与 ``EnhanceTokenTests``（响应体里的 ``error_code=110``）互补——这两条走的是
    ``_exchange`` 的**状态码**分支：401 ⇒ token 族错误 ⇒ 失效重取后恰好重试一次；
    HTTP ≥400 ⇒ transient ⇒ 退避 0.5 s 重试一次，仍失败则降级发原图。
    """

    async def test_http_401_refreshes_token_once_and_retries(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            original = out_dir / "origin.png"
            original.write_bytes(png_bytes())
            enhanced = png_bytes(4, 4, b"\x00" * 16)
            session = FakeSession(
                token=[token_payload("A"), token_payload("B")],
                enhance={"log_id": 7, "image": base64.b64encode(enhanced).decode()},
                enhance_status=[401, 200],
            )
            debugs: list = []
            enhancer = BaiduEnhancer(
                session, lambda: runtime_config(), debug=debug_recorder(debugs), warn=recorder([])
            )
            task = make_task()
            state = EnhanceState()

            value = await enhancer.apply(task, original, out_dir, enhance_config(), state)

            self.assertEqual(
                [url for url, _ in session.calls],
                [logic.TOKEN_URL, logic.ENHANCE_URL, logic.TOKEN_URL, logic.ENHANCE_URL],
                "HTTP 401 ⇒ 失效重取 token 一次 + 重试增强一次（不多不少）",
            )
            self.assertEqual(session.enhance_calls[0][1]["params"]["access_token"], "A")
            self.assertEqual(session.enhance_calls[1][1]["params"]["access_token"], "B")
            # 执行级：最终真的拿到增强结果，而不是降级原图
            self.assertIsInstance(value, Path)
            self.assertNotEqual(value, original)
            self.assertEqual(value.read_bytes(), enhanced)
            self.assertEqual(state.succeeded, 1)
            self.assertEqual(state.failures, [])
            self.assertEqual(task.errors, [])
            self.assertEqual(task.state, TaskState.CREATED)

    async def test_http_500_twice_degrades_without_a_retry_loop(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            original = out_dir / "origin.png"
            original.write_bytes(png_bytes())
            session = FakeSession(
                token=token_payload(),
                enhance=enhance_payload(),
                enhance_status=[500, 500],
            )
            debugs: list = []
            enhancer = BaiduEnhancer(
                session, lambda: runtime_config(), debug=debug_recorder(debugs), warn=recorder([])
            )
            task = make_task()
            state = EnhanceState()

            value = await enhancer.apply(task, original, out_dir, enhance_config(), state)

            # 失败后降级：只重试一次（transient 族），不会无限重试
            self.assertEqual(
                [url for url, _ in session.calls],
                [logic.TOKEN_URL, logic.ENHANCE_URL, logic.ENHANCE_URL],
                "HTTP 500 连续两次 ⇒ token + 增强 + 一次重试，然后放弃",
            )
            self.assertIs(value, original, "降级必须原样返回入参")
            self.assertEqual(sorted(path.name for path in out_dir.iterdir()), ["origin.png"])
            # 硬约束：不写 task.errors、不改 task.state（否则会误报「部分图片绘制失败」）
            self.assertEqual(task.errors, [])
            self.assertEqual(task.state, TaskState.CREATED)
            self.assertEqual(task.runtime["enhance"]["failures"], ["transient:500"])
            self.assertTrue(state.circuit_open, "transient 失败后本任务熔断，后续图不再尝试")
            self.assertTrue(
                any("失败" in message for message, _ in debugs),
                f"必须留在 DEBUG 失败行：{debugs}",
            )


class EnhanceDefaultConfigTests(unittest.IsolatedAsyncioTestCase):
    """默认配置（新装的真实起点）下绝不产生任何百度调用。

    E7 用手写的 ``EnhanceConfig()``；这里改用 ``load_config({})`` 的**真实解析
    结果**，防止「schema 默认值被改成 true」或「load_config 把组读错」这类改动绕过
    零副作用承诺。
    """

    async def test_default_config_never_calls_baidu(self):
        from imago_fusion.core.config import load_config

        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            original = out_dir / "origin.png"
            original.write_bytes(png_bytes())
            session = FakeSession(token=token_payload(), enhance=enhance_payload())
            runtime_cfg = load_config({})  # 空配置 = 用户没填过任何东西
            enhancer = BaiduEnhancer(
                session, lambda: runtime_cfg, debug=debug_recorder([]), warn=recorder([])
            )
            task = make_task()

            value = await enhancer.apply(task, original, out_dir, runtime_cfg.enhance, EnhanceState())

            self.assertFalse(runtime_cfg.enhance.enabled, "默认必须是关闭")
            self.assertEqual(value, original)
            self.assertEqual(session.calls, [], "默认配置下不允许任何百度网络调用")
            self.assertNotIn("enhance", task.runtime, "默认配置下不允许出现增强 runtime 键")
            self.assertEqual(sorted(path.name for path in out_dir.iterdir()), ["origin.png"])


class EnhanceLogicTests(unittest.TestCase):
    """纯逻辑层：编码/嗅探/预检/错误分类（不涉及 session）。"""

    def test_sniff_and_dimensions_cover_the_three_supported_formats(self):
        self.assertEqual(logic.sniff_mime(png_bytes(3, 5)), "image/png")
        self.assertEqual(logic.image_dimensions(png_bytes(3, 5)), (3, 5))
        jpeg = b"\xff\xd8\xff" + b"\xe0" + struct.pack(">H", 4) + b"\x00\x00" + b"\xff\xc0" + struct.pack(">H", 11) + b"\x08" + struct.pack(">HH", 480, 640) + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
        self.assertEqual(logic.sniff_mime(jpeg), "image/jpeg")
        self.assertEqual(logic.image_dimensions(jpeg), (640, 480))
        bmp = b"BM" + b"\x00" * 16 + struct.pack("<ii", 120, 80) + b"\x00" * 8
        self.assertEqual(logic.sniff_mime(bmp), "image/bmp")
        self.assertEqual(logic.image_dimensions(bmp), (120, 80))
        self.assertIsNone(logic.sniff_mime(b"RIFF\x00\x00\x00\x00WEBP"))
        self.assertIsNone(logic.image_dimensions(b"not-an-image"))

    def test_error_code_classification_matches_the_documented_handling(self):
        cases = {
            110: "token",
            111: "token",
            100: "token",
            1: "transient",
            2: "transient",
            216401: "transient",
            216201: "input",
            216202: "input",
            6: "permission",
            17: "quota",
            18: "quota",
            19: "quota",
            216100: "config",
            999999: "unknown",
        }
        for code, expected in cases.items():
            self.assertEqual(logic.classify_error(code), expected, f"error_code={code}")
        # error_code 可能是字符串
        self.assertEqual(logic.error_code_of({"error_code": "110"}), 110)
        self.assertEqual(logic.error_code_of({"log_id": 1}), 0)

    def test_plan_rejects_empty_unrecognised_and_over_budget_images(self):
        cfg = EnhanceConfig()
        self.assertEqual(logic.plan(b"", cfg, remaining=300)[1], "empty")
        self.assertEqual(logic.plan(b"RIFF\x00\x00\x00\x00WEBP", cfg, remaining=300)[1], "format")
        self.assertEqual(logic.plan(png_bytes(), cfg, remaining=300)[0], True)
        # 剩余预算不足（≤ 单张超时 + 15 s）时宁可不增强，避免把任务拖成超时
        self.assertEqual(logic.plan(png_bytes(), cfg, remaining=cfg.timeout_seconds + 15)[1], "budget")

    def test_payload_decoding_reports_missing_image_and_bad_base64(self):
        from imago_fusion.core.errors import EnhanceError, EnhanceSkipped

        # 文档里 image 是「否（可选）」：HTTP 200 + 无 error_code 也可能没有图
        with self.assertRaises(EnhanceError):
            logic.decode_payload({"log_id": 1})
        with self.assertRaises(EnhanceError):
            logic.decode_payload({"image": "!!!not-base64!!!"})
        with self.assertRaises(EnhanceError) as permission:
            logic.decode_payload({"error_code": 6, "error_msg": "No permission to access data"})
        self.assertEqual(permission.exception.kind, "permission")
        self.assertEqual(permission.exception.code, 6)
        # 输出超过 storage 上限 ⇒ 跳过（不是失败）
        with self.assertRaises(EnhanceSkipped) as oversized:
            logic.decode_payload(
                {"image": base64.b64encode(png_bytes(2, 2)).decode()}, expected_max_bytes=4
            )
        self.assertEqual(oversized.exception.reason, "output_oversize")
        data, mime = logic.decode_payload({"image": base64.b64encode(png_bytes(2, 2)).decode()})
        self.assertEqual((data, mime), (png_bytes(2, 2), "image/png"))


class EnhanceEndpointGuardTests(unittest.TestCase):
    """E11：端点常量守卫（防回退到「图像无损放大」）+ 代码/配置里 0 残留。

    本版把百度接口从「图像无损放大」改判为「图像清晰度增强」。这条守卫钉住两件事：
    ① ``ENHANCE_URL`` 必须指向 ``image_definition_enhance``；
    ② ``main.py`` / ``core/*.py`` / ``_conf_schema.json`` 里不得再出现旧端点 id
    （README / CHANGELOG 的历史说明与 spec 文档不在扫描范围内）。
    """

    # 把旧端点 id 拆成两段拼接：本文件自己不是命中源，日后把 test/ 加进扫描面也不会自伤。
    OLD_ENDPOINT_ID = "image_quality_" + "enhance"

    def test_endpoint_constant_points_at_definition_enhance(self):
        self.assertTrue(
            logic.ENHANCE_URL.endswith("/rest/2.0/image-process/v1/image_definition_enhance"),
            f"端点必须换成 image_definition_enhance：{logic.ENHANCE_URL}",
        )
        self.assertTrue(
            logic.ENHANCE_URL.startswith("https://aip.baidubce.com/"),
            f"端点域名不对：{logic.ENHANCE_URL}",
        )

    def test_repo_never_mentions_the_old_quality_endpoint(self):
        targets = [
            ROOT / "main.py",
            ROOT / "_conf_schema.json",
            *sorted((ROOT / "core").glob("*.py")),
        ]
        offenders = [
            str(path.relative_to(ROOT))
            for path in targets
            if self.OLD_ENDPOINT_ID in path.read_text("utf-8")
        ]
        self.assertEqual(offenders, [], f"仍在引用旧端点 id：{offenders}")
        self.assertIn(
            "image_definition_enhance",
            (ROOT / "core" / "enhance.py").read_text("utf-8"),
        )
