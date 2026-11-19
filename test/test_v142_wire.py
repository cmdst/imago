"""v1.4.2 P0 / t11 R1-06 端到端：真实入口 → 真调度器 → 真 OneBot 适配器序列化。

t11 指出的并发归属问题，只有把整条链路接起来才证得完整：

1. **真实入口**：``ImagoFusion.draw_command``（不是手工 ``_finish_task``）；
2. **真实调度**：``TaskScheduler.submit`` / ``_execute``，两个任务确实同时在途；
3. **真实 OneBot 序列化**：``AiocqhttpAdapter`` 把链交给 ``_parse_onebot_json`` 后
   调 ``bot.send_group_msg``，本文件在**假 bot** 侧记录这份 wire 载荷（``at`` /
   ``image`` 的 base64 / 文本），因此断言的是"平台真正会收到的 JSON"；
4. **屏障**：A 的 provider 调用挂起期间，把 A 的**源事件**改成别人的 sender 与
   UMO；B 正常入队并先完成。放开 A 后要求两条 wire 载荷仍然是"各自的图 + 各自
   的冻结 @ 与 UMO"。

对照：没有改写、顺序完成的独立事件，先后顺序与 @ 归属都按原语义（不因本修复
改变正常路径）。全程离线：适配器被替换成假实现，``llm_generate`` 一旦被调用即
失败，``Metric.upload`` 被摘掉。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import itertools
import random
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import patch

_cache: dict = {}


def _ensure_real_astrbot() -> None:
    """保证用到的是宿主真模块（同类文件也这么做，见 test_v142_at.py）。"""

    import astrbot.api  # noqa: F401  — 可能拿到别的测试文件装的桩

    if getattr(sys.modules["astrbot.api"], "logger", None) is not None:
        return
    for name in [key for key in sys.modules if key == "astrbot" or key.startswith("astrbot.")]:
        del sys.modules[name]
    import astrbot.api as real_api

    if getattr(real_api, "logger", None) is None:  # pragma: no cover - 环境损坏
        raise AssertionError("astrbot.api.logger 不可用：无法验证 OneBot 出站载荷")


def _mods():
    """惰性导入：收集阶段不碰 astrbot 真模块。"""

    if not _cache:
        _ensure_real_astrbot()
        from astrbot.core.message import components
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.core.platform.astr_message_event import AstrMessageEvent
        from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
        from astrbot.core.platform.message_type import MessageType
        from astrbot.core.platform.platform_metadata import PlatformMetadata
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
            AiocqhttpAdapter,
        )
        from astrbot.core.star.context import Context
        from astrbot.core.star.star_handler import EventType
        from astrbot.core.utils.metrics import Metric

        import imago_fusion.main as plugin
        from imago_fusion.core.config import load_config
        from imago_fusion.core.models import ImageResult
        from imago_fusion.integrations import active_send
        from imago_fusion.services import scheduler as sched
        from imago_fusion.services.persona_store import PersonaStore
        from imago_fusion.services.quota_store import QuotaStore

        _cache.update(
            MessageChain=MessageChain,
            components=components,
            AstrMessageEvent=AstrMessageEvent,
            AstrBotMessage=AstrBotMessage,
            Group=Group,
            MessageMember=MessageMember,
            MessageType=MessageType,
            PlatformMetadata=PlatformMetadata,
            AiocqhttpAdapter=AiocqhttpAdapter,
            Context=Context,
            EventType=EventType,
            Metric=Metric,
            plugin=plugin,
            load_config=load_config,
            ImageResult=ImageResult,
            active_send=active_send,
            sched=sched,
            PersonaStore=PersonaStore,
            QuotaStore=QuotaStore,
        )
    return _cache


USER_A = "710001"
USER_B = "720002"
USER_C = "730003"
GROUP_ID = "880088"
META_SEQ = itertools.count(1)


def png_bytes(seed: int, size: tuple[int, int] = (64, 48)) -> bytes:
    from PIL import Image as Pixels

    width, height = size
    rng = random.Random(seed)
    payload = bytes(rng.randrange(256) for _ in range(width * height * 3))
    buffer = io.BytesIO()
    Pixels.frombytes("RGB", (width, height), payload).save(buffer, format="PNG")
    return buffer.getvalue()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _without_spacers(sequence: tuple) -> tuple:
    """OneBot 适配器会在 At 与后续段之间插入纯空格 Plain，属平台排版，不是我们的内容。"""

    return tuple(item for item in sequence if item != "text")


class _FakeBot:
    """只记录 OneBot 适配器**序列化后**的 wire 载荷，不联网、不碰协议端。"""

    def __init__(self) -> None:
        self.out: list = []

    async def send_group_msg(self, *, group_id, message, **kwargs):
        self._record("group", group_id, message)

    async def send_private_msg(self, *, user_id, message, **kwargs):
        self._record("private", user_id, message)

    def _record(self, kind: str, dest, message) -> None:
        sequence = tuple(item["type"] for item in message)
        mentions = tuple(
            str(item["data"]["qq"]) for item in message if item["type"] == "at"
        )
        pictures = tuple(
            digest(base64.b64decode(item["data"]["file"].removeprefix("base64://"), validate=True))
            for item in message
            if item["type"] == "image"
        )
        text = tuple(
            item["data"]["text"]
            for item in message
            if item["type"] == "text" and item["data"]["text"] != " "
        )
        self.out.append(
            NS(
                kind=kind,
                dest=str(dest),
                sequence=sequence,
                at=mentions,
                pictures=pictures,
                text=text,
            )
        )


class _Logger:
    def __init__(self) -> None:
        self.rows: list = []

    def __getattr__(self, level):
        def row(message, *args, **kwargs):
            self.rows.append((level, message % args if args else str(message)))

        return row


class _Hooks:
    def __init__(self) -> None:
        self.before: list = []
        self.after: list = []

    def get_handlers_by_event_type(self, event_type, **kwargs):
        mods = _mods()
        if event_type == mods["EventType"].OnDecoratingResultEvent:
            return self.before
        return self.after


class _Rig:
    def __init__(self, root: Path, response, *, at_trigger_user: bool = True) -> None:
        mods = _mods()
        plugin = mods["plugin"]
        self.bot = _FakeBot()
        self.logs = _Logger()
        self.hooks = _Hooks()
        self.calls: list = []
        self.patched_llm = 0
        self.meta = mods["PlatformMetadata"](
            name="aiocqhttp", description="offline wire test", id="wire-onebot"
        )

        platform = mods["AiocqhttpAdapter"].__new__(mods["AiocqhttpAdapter"])
        platform.metadata = self.meta
        platform.bot = self.bot
        context = mods["Context"].__new__(mods["Context"])
        context.platform_manager = NS(platform_insts=[platform])
        context._config = {}
        context.astrbot_config_mgr = NS(get_conf=lambda umo: {})

        async def no_model_call(*args, **kwargs):
            self.patched_llm += 1
            raise AssertionError("本用例不应调用宿主模型/识图")

        context.llm_generate = no_model_call
        self.context = context

        self.plugin = plugin.ImagoFusion.__new__(plugin.ImagoFusion)
        self.plugin.context = context
        self.plugin.session = None
        self.plugin.store = mods["PersonaStore"](Path(root))
        self.plugin.quota_store = mods["QuotaStore"](Path(root), lambda: "2026-01-01")
        self.plugin.enhancer = None
        self.plugin.upscaler = None
        self.plugin.raw_config = {
            "providers": [
                {
                    "id": "wire-node",
                    "api_type": "custom_endpoint",
                    "base_url": "https://offline.invalid",
                    "api_keys": "WIRE_FAKE_KEY",
                    "model": "wire-model",
                    "timeout": 40,
                }
            ],
            "task_config": {
                "at_trigger_user": bool(at_trigger_user),
                "llm_caption": False,
                "generation_timeout": 60,
                "max_concurrent_tasks": 2,
            },
            "optimizer_config": {"enable_optimizer": False},
            "quota_config": {},
            "reference_fallback_config": {"enable": False},
            "reference_compress_config": {"enable": False},
        }

        async def picture_caption(*args, **kwargs):
            raise AssertionError("本用例不应生成配文")

        self.plugin._build_caption = picture_caption

        class _Adapter:
            """替身 provider：只记录调用并交给用例提供的响应函数。"""

            def __init__(adapter, conf):
                adapter.conf = conf

            async def generate(adapter, session, request, key):
                calls = self.calls
                calls.append(NS(node=adapter.conf.id, prompt=request.prompt))
                return await response(adapter.conf, request)

        async def captioner(*args, **kwargs):  # pragma: no cover - 本用例不触发
            raise AssertionError("本用例不应调用识图摘要")

        self.plugin.scheduler = mods["sched"].TaskScheduler(
            lambda: mods["load_config"](self.plugin.raw_config),
            self.plugin.session,
            self.plugin._finish_task,
            self.plugin._provider_error,
            self.plugin._scheduler_debug,
            captioner=captioner,
            info=self.plugin._scheduler_info,
        )
        self.plugin.sender = mods["active_send"].ProactiveSender(
            context,
            self.plugin._decorate_error,
            self.plugin._debug,
        )

        async def noop(*args, **kwargs):
            return None

        self._patches = [
            patch.dict(mods["sched"].ADAPTERS, {"custom_endpoint": _Adapter}),
            patch.object(plugin, "logger", self.logs),
            patch.object(mods["active_send"], "astrbot_logger", self.logs),
            patch.object(mods["active_send"], "star_handlers_registry", self.hooks),
            patch.object(mods["Metric"], "upload", noop),
        ]

    def __enter__(self):
        for item in self._patches:
            item.start()
        return self

    def __exit__(self, *error):
        for item in reversed(self._patches):
            item.stop()
        return False

    def event(self, user: str = USER_A, *, private: bool = False, room: str = GROUP_ID):
        mods = _mods()
        message = mods["AstrBotMessage"]()
        message.type = (
            mods["MessageType"].FRIEND_MESSAGE if private else mods["MessageType"].GROUP_MESSAGE
        )
        message.self_id = "888888"
        message.message_id = f"wire-message-{next(META_SEQ)}"
        message.session_id = room
        message.sender = mods["MessageMember"](user_id=user, nickname="wire-sender")
        message.group = None if private else mods["Group"](group_id=room)
        message.message_str = ""
        message.message = []
        message.raw_message = None
        return mods["AstrMessageEvent"]("", message, self.meta, room)

    def blocking_image(self, entered: asyncio.Event, release: asyncio.Event, path: Path):
        """一个能让入口在"参考图接管"await 处停住的 Image 形态组件。

        它不是 mock 插件内部方法：``_plan_task_references`` → ``ReferencePlanner``
        对裸文件名/no-source 的 Image 组件本来就会 ``await convert_to_file_path()``，
        这里只是让这个真实 await 点停在屏障上。
        """

        class _BarrierImage:
            path = None
            url = None
            file = "opaque-wire-image"

            async def convert_to_file_path(_self):
                entered.set()
                await asyncio.wait_for(release.wait(), 10)
                return str(path)

        return _BarrierImage()

    def _task_entry(self, task_id: str):
        if task_id in self.plugin.scheduler.tasks:
            return self.plugin.scheduler.tasks[task_id]
        task, _ = self.plugin.scheduler.recent[task_id]
        handle = asyncio.get_running_loop().create_future()
        handle.set_result(None)
        return task, handle

    async def draw(self, event, prompt: str):
        """走真实命令入口；返回调度器里本次真正新增的那个任务与其 handle。

        并发用例里可能有别的任务同时在途，因此按 prompt 归属挑新任务，而不是
        只看"新增了几个"。
        """

        before = set(self.plugin.scheduler.tasks) | set(self.plugin.scheduler.recent)
        async for _ in self.plugin.draw_command(event, prompt):
            pass
        fresh = (set(self.plugin.scheduler.tasks) | set(self.plugin.scheduler.recent)) - before
        mine = []
        for task_id in fresh:
            task = self._task_entry(task_id)[0]
            if getattr(getattr(task, "request", None), "prompt", None) == prompt:
                mine.append(task_id)
        if len(mine) != 1:  # pragma: no cover - 入口被改坏时给出可读失败
            detail = [
                (task_id[:8], self._task_entry(task_id)[0].owner_user_id, self._task_entry(task_id)[0].umo)
                for task_id in fresh
            ]
            tail = [row for level, row in self.logs.rows if level in ("error", "warning")][-3:]
            raise AssertionError(f"入口没有为 {prompt!r} 创建唯一任务；新任务={detail}；错误尾：{tail}")
        return self._task_entry(mine[0])

    @staticmethod
    async def done(task_handle, timeout: float = 10.0):
        task, handle = task_handle
        await asyncio.wait_for(asyncio.shield(handle), timeout)
        return task

    async def close(self) -> None:
        await self.plugin.scheduler.close()


class WireConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    """R1-06：两个任务同时在途，B 先完成；A 的源事件在候等期间被改写。"""

    async def test_provider_stage_mutation_keeps_frozen_wire_identity(self):
        payload_a = png_bytes(101)
        payload_b = png_bytes(202)
        digest_a = digest(payload_a)
        digest_b = digest(payload_b)
        entered_a = asyncio.Event()
        release_a = asyncio.Event()

        async def response(conf, request):
            if "A" in request.prompt:
                entered_a.set()
                await asyncio.wait_for(release_a.wait(), 10)
            return [_mods()["ImageResult"](data=payload_a if "A" in request.prompt else payload_b)]

        with TemporaryDirectory() as directory, _Rig(Path(directory), response) as rig:
            event_a = rig.event(USER_A)
            event_b = rig.event(USER_B)
            handle_a = await rig.draw(event_a, "A 画一只猫")
            await asyncio.wait_for(entered_a.wait(), 10)  # A 已在 provider 调用里挂起

            # 屏障期间把「活事件」改成别人的身份（t1 的现场形状）。
            event_a.message_obj.sender.user_id = USER_C
            event_a.unified_msg_origin = "wire-onebot:GroupMessage:999988"

            handle_b = await rig.draw(event_b, "B 画一只狗")
            task_b = await rig.done(handle_b)

            # B 先完成：只有一条 wire 载荷，且是 B 的图 + B 的 @。
            self.assertEqual(len(rig.bot.out), 1)
            first = rig.bot.out[0]
            self.assertEqual((first.kind, first.dest), ("group", GROUP_ID))
            self.assertEqual(first.pictures, (digest_b,))
            self.assertEqual(first.at, (USER_B,))
            self.assertEqual(_without_spacers(first.sequence), ("at", "image"))
            self.assertEqual(first.text, ())
            self.assertEqual(task_b.owner_user_id, USER_B)

            release_a.set()
            task_a = await rig.done(handle_a)

            self.assertEqual(len(rig.bot.out), 2)
            second = rig.bot.out[1]
            # A 后完成，但帧里仍是 A 自己的图与冻结的 A 提及（不是改写后的 C）。
            self.assertEqual(second.pictures, (digest_a,))
            self.assertEqual(second.at, (USER_A,))
            self.assertEqual(_without_spacers(second.sequence), ("at", "image"))
            self.assertEqual(second.text, ())  # 没有任何真实文本泄漏到出站
            self.assertEqual(task_a.owner_user_id, USER_A)
            self.assertEqual(task_a.umo, "wire-onebot:GroupMessage:880088")
            self.assertEqual(rig.patched_llm, 0)
        await rig.close()

    async def test_entry_await_mutation_keeps_frozen_wire_identity(self):
        """入口 await 期间改写源事件：任务身份、UMO 与出站 @ 仍属于原事件。

        这是 t1 指出的最早风险点（``_submit`` 里的参考图接管 await）。改写在
        任务创建**之前**落地，所以基线会把它写进 ``owner_user_id``/``umo``；
        冻结后必须仍取入口最初读到的值。
        """

        payload_a = png_bytes(505)
        payload_b = png_bytes(606)
        digest_a = digest(payload_a)
        digest_b = digest(payload_b)
        entered_a = asyncio.Event()
        release_a = asyncio.Event()
        explicit = None

        async def response(conf, request):
            side = "A" if "A" in request.prompt else "B"
            return [_mods()["ImageResult"](data=payload_a if side == "A" else payload_b)]

        with TemporaryDirectory() as directory, _Rig(Path(directory), response) as rig:
            explicit = Path(directory) / "explicit.png"
            explicit.write_bytes(png_bytes(707))

            event_a = rig.event(USER_A)
            event_a.message_obj.message = [rig.blocking_image(entered_a, release_a, explicit)]
            pending_a = asyncio.create_task(rig.draw(event_a, "A 用参考图画一只猫"))
            await asyncio.wait_for(entered_a.wait(), 10)  # A 停在入口的参考图 await

            # 候等期间把「活事件」改成别人的 sender 与别的会话。
            event_a.message_obj.sender.user_id = USER_C
            event_a.unified_msg_origin = "wire-onebot:GroupMessage:999988"

            handle_b = await rig.draw(rig.event(USER_B), "B 画一只狗")
            await rig.done(handle_b)

            # B 先完成：此时 A 的任务还没被创建，wire 里只有 B。
            self.assertEqual(len(rig.bot.out), 1)
            self.assertEqual(rig.bot.out[0].at, (USER_B,))
            self.assertEqual(rig.bot.out[0].pictures, (digest_b,))

            release_a.set()
            task_a = await rig.done(await asyncio.wait_for(pending_a, 10))

            self.assertEqual(len(rig.bot.out), 2)
            second = rig.bot.out[1]
            # 身份、会话与提及全部来自入口最初读到的事件，不是改写后的 C/999988。
            self.assertEqual(task_a.owner_user_id, USER_A)
            self.assertEqual(task_a.umo, "wire-onebot:GroupMessage:880088")
            self.assertEqual(second.dest, GROUP_ID)
            self.assertEqual(second.at, (USER_A,))
            self.assertEqual(second.pictures, (digest_a,))
            self.assertEqual(_without_spacers(second.sequence), ("at", "image"))
            self.assertEqual(second.text, ())
            self.assertEqual(rig.patched_llm, 0)
        await rig.close()

    async def test_independent_events_keep_natural_order_and_own_mention(self):
        """对照：不改写、顺序完成时，先后顺序与 @ 归属保持原语义。"""

        payload_a = png_bytes(303)
        payload_b = png_bytes(404)
        outputs = {
            "A": payload_a,
            "B": payload_b,
        }

        async def response(conf, request):
            side = "A" if request.prompt.startswith("A") else "B"
            return [_mods()["ImageResult"](data=outputs[side])]

        with TemporaryDirectory() as directory, _Rig(Path(directory), response) as rig:
            task_a = await rig.done(await rig.draw(rig.event(USER_A), "A 画一只猫"))
            task_b = await rig.done(await rig.draw(rig.event(USER_B), "B 画一只狗"))

            self.assertEqual(len(rig.bot.out), 2)
            self.assertEqual(rig.bot.out[0].pictures, (digest(payload_a),))
            self.assertEqual(rig.bot.out[0].at, (USER_A,))
            self.assertEqual(rig.bot.out[1].pictures, (digest(payload_b),))
            self.assertEqual(rig.bot.out[1].at, (USER_B,))
            self.assertEqual({task_a.owner_user_id, task_b.owner_user_id}, {USER_A, USER_B})
            self.assertTrue(task_a.state.is_terminal)
            self.assertTrue(task_b.state.is_terminal)
        await rig.close()



class WireGuardTests(unittest.IsolatedAsyncioTestCase):
    """t13 R2-01/02/03：真 OneBot 序列化后的成品字节、提及与投递记账。

    这些用例一律走**真实命令入口 → 真调度器 → 真适配器序列化 → 假 bot**，
    断言的是平台真正会收到的 JSON 段序列（``at`` / ``image`` / ``text`` 与图片
    字节 digest），不是 ``context.outbound`` 里的组件对象。
    """

    async def _draw(self, rig, event, prompt="A 画一只猫"):
        return await rig.done(await rig.draw(event, prompt))

    async def test_strict_mode_ignores_hook_rewritten_image_url(self):
        """R2-01 ①：钩子只改 ``Image.url`` 也换不掉实际出站的成品字节。

        身份不可信（sender 带边缘空白）⇒ 严格纯图分支；钩子把成品图的 ``url``
        指向另一张合成图，同时保留 ``path``/``file``。真 OneBot 用 ``url or file``
        序列化，历史实现会把别人的图发出去；修复后只按冻结本地路径重建顶层
        ``Image``，线上一律是本任务成品。
        """

        product = png_bytes(501)
        other = png_bytes(502)
        foreign = "data:image/png;base64," + base64.b64encode(other).decode()

        async def response(conf, request):
            return [_mods()["ImageResult"](data=product)]

        async def decorating(event):
            mods = _mods()
            chain = list(event.get_result().chain or [])
            for item in chain:
                if isinstance(item, mods["components"].Image):
                    item.url = foreign
            event.get_result().chain = chain

        with TemporaryDirectory() as directory, _Rig(Path(directory), response) as rig:
            rig.hooks.before.append(_Handler(decorating))
            event = rig.event(USER_A + " ")
            task = await self._draw(rig, event)

            self.assertEqual(len(rig.bot.out), 1)
            frame = rig.bot.out[0]
            self.assertEqual(frame.sequence, ("image",))
            self.assertEqual(frame.at, ())
            self.assertEqual(frame.pictures, (digest(product),))
            self.assertEqual(frame.text, ())
            self.assertEqual(task.runtime["mention_reason"], "invalid_format")
            self.assertTrue(task.runtime["silent_image_only"])
        await rig.close()

    async def test_strict_mode_does_not_forward_node_envelopes(self):
        """R2-01 ②：钩子把成品塞进 ``Node``/``Nodes`` 也不得走转发封套。"""

        product = png_bytes(503)
        other = png_bytes(504)

        async def response(conf, request):
            return [_mods()["ImageResult"](data=product)]

        async def decorating(event):
            mods = _mods()
            components = mods["components"]
            images = [
                item
                for item in event.get_result().chain or []
                if isinstance(item, components.Image)
            ]
            images.append(components.Image.fromBytes(other))
            event.get_result().chain = [
                components.Nodes(
                    nodes=[
                        components.Node(
                            content=[
                                components.At(qq="all"),
                                *images,
                                components.Plain("forward text"),
                            ]
                        )
                    ]
                )
            ]

        with TemporaryDirectory() as directory, _Rig(Path(directory), response) as rig:
            rig.hooks.before.append(_Handler(decorating))
            task = await self._draw(rig, rig.event(USER_A + " "))

            self.assertEqual(len(rig.bot.out), 1)
            frame = rig.bot.out[0]
            # 只有顶层成品图：没有转发容器、没有文本、没有提及、没有别人的图。
            self.assertEqual(frame.sequence, ("image",))
            self.assertEqual(frame.at, ())
            self.assertEqual(frame.pictures, (digest(product),))
            self.assertEqual(frame.text, ())
            self.assertTrue(task.runtime["silent_image_only"])
        await rig.close()

    async def test_mention_moved_after_image_is_rebuilt_in_front(self):
        """R2-02：正确的 At 被挪到图后，也要收敛成"新建 At、恰好一个、置前"。"""

        payload = png_bytes(505)

        async def response(conf, request):
            return [_mods()["ImageResult"](data=payload)]

        async def decorating(event):
            mods = _mods()
            components = mods["components"]
            chain = list(event.get_result().chain or [])
            mentions = [item for item in chain if isinstance(item, components.At)]
            rest = [item for item in chain if not isinstance(item, components.At)]
            event.get_result().chain = [*rest, *mentions]

        with TemporaryDirectory() as directory, _Rig(Path(directory), response) as rig:
            rig.hooks.before.append(_Handler(decorating))
            await self._draw(rig, rig.event(USER_A))

            self.assertEqual(len(rig.bot.out), 1)
            frame = rig.bot.out[0]
            self.assertEqual(_without_spacers(frame.sequence), ("at", "image"))
            self.assertEqual(frame.at, (USER_A,))
            self.assertEqual(frame.pictures, (digest(payload),))
        await rig.close()

    async def test_mention_with_trailing_blank_is_rebuilt_canonically(self):
        """R2-02：钩子把 ``At.qq`` 改成「A + 尾空白」也不得原样透给 wire。"""

        payload = png_bytes(506)

        async def response(conf, request):
            return [_mods()["ImageResult"](data=payload)]

        async def decorating(event):
            mods = _mods()
            for item in event.get_result().chain or []:
                if isinstance(item, mods["components"].At):
                    item.qq = USER_A + " "

        with TemporaryDirectory() as directory, _Rig(Path(directory), response) as rig:
            rig.hooks.before.append(_Handler(decorating))
            await self._draw(rig, rig.event(USER_A))

            self.assertEqual(len(rig.bot.out), 1)
            frame = rig.bot.out[0]
            self.assertEqual(frame.at, (USER_A,))
            self.assertEqual(_without_spacers(frame.sequence), ("at", "image"))
        await rig.close()

    async def test_text_only_hook_side_send_creates_no_mention_or_extra_frame(self):
        """R2-02：钩子纯文本旁发不得被补成独立的 @ 提醒（一条旁发帧、无 at）。"""

        payload = png_bytes(507)

        async def response(conf, request):
            return [_mods()["ImageResult"](data=payload)]

        async def decorating(event):
            mods = _mods()
            await event.send(
                mods["MessageChain"](
                    chain=[
                        mods["components"].At(qq="999999"),
                        mods["components"].Plain("side text"),
                    ]
                )
            )

        with TemporaryDirectory() as directory, _Rig(Path(directory), response) as rig:
            rig.hooks.before.append(_Handler(decorating))
            await self._draw(rig, rig.event(USER_A))

            self.assertEqual(len(rig.bot.out), 2)
            side, main = rig.bot.out
            self.assertEqual(side.at, ())
            self.assertEqual(side.pictures, ())
            self.assertEqual(side.text, ("side text",))
            self.assertEqual(main.at, (USER_A,))
            self.assertEqual(main.pictures, (digest(payload),))
        await rig.close()

    async def test_rejected_main_send_reports_zero_dispatched_images(self):
        """R2-03：适配器拒绝（返回 False）时不得把"尝试"记成"已提交"。"""

        payload = png_bytes(508)

        async def response(conf, request):
            return [_mods()["ImageResult"](data=payload)]

        async def refuse(*args, **kwargs):
            return False

        with TemporaryDirectory() as directory, _Rig(Path(directory), response) as rig:
            with patch.object(rig.plugin.context, "send_message", new=refuse):
                task = await self._draw(rig, rig.event(USER_A))

            self.assertEqual(rig.bot.out, [])
            self.assertEqual(task.runtime["image_delivery_dispatched_images"], 0)
            self.assertFalse(task.runtime["image_delivery_success"])
            self.assertEqual(task.state.value, "delivery_failed")

        await rig.close()

    async def test_side_image_after_main_chain_clear_is_not_zero_delivery(self):
        """R2-03 反向：旁发已送出成品图，主链被清空不得冒充零投递。"""

        payload = png_bytes(509)
        side_payload = png_bytes(510)

        async def response(conf, request):
            return [_mods()["ImageResult"](data=payload)]

        async def decorating(event):
            mods = _mods()
            await event.send(
                mods["MessageChain"](
                    chain=[mods["components"].Image.fromBytes(side_payload)]
                )
            )
            event.get_result().chain = []

        with TemporaryDirectory() as directory, _Rig(Path(directory), response) as rig:
            rig.hooks.before.append(_Handler(decorating))
            task = await self._draw(rig, rig.event(USER_A))

            self.assertEqual(len(rig.bot.out), 1)
            self.assertEqual(rig.bot.out[0].pictures, (digest(side_payload),))
            self.assertEqual(task.runtime["image_delivery_dispatched_images"], 1)
            self.assertTrue(task.runtime["image_delivery_success"])
            self.assertTrue(task.runtime["image_delivery_main_suppressed"])
        await rig.close()

    async def test_text_only_main_chain_gets_no_mention(self):
        """R2-02：任务出了图、钩子却把主链换成纯文本 ⇒ 不补 @，也不算送图成功。"""

        payload = png_bytes(512)

        async def response(conf, request):
            return [_mods()["ImageResult"](data=payload)]

        async def decorating(event):
            mods = _mods()
            event.get_result().chain = [mods["components"].Plain("textonly")]

        with TemporaryDirectory() as directory, _Rig(Path(directory), response) as rig:
            rig.hooks.before.append(_Handler(decorating))
            task = await self._draw(rig, rig.event(USER_A))

            self.assertEqual(len(rig.bot.out), 1)
            frame = rig.bot.out[0]
            self.assertEqual(frame.at, ())
            self.assertEqual(frame.pictures, ())
            self.assertEqual(frame.text, ("textonly",))
            self.assertEqual(task.runtime["image_delivery_dispatched_images"], 0)
            self.assertFalse(task.runtime["image_delivery_success"])
            self.assertEqual(task.state.value, "delivery_failed")
        await rig.close()

    async def test_every_path_empty_is_still_a_delivery_failure(self):
        """R2-03：主链清空且全路无发，仍必须失败（不得记成功）。"""

        payload = png_bytes(511)

        async def response(conf, request):
            return [_mods()["ImageResult"](data=payload)]

        async def decorating(event):
            event.get_result().chain = []

        with TemporaryDirectory() as directory, _Rig(Path(directory), response) as rig:
            rig.hooks.before.append(_Handler(decorating))
            task = await self._draw(rig, rig.event(USER_A))

            self.assertEqual(rig.bot.out, [])
            self.assertEqual(task.runtime["image_delivery_dispatched_images"], 0)
            self.assertFalse(task.runtime["image_delivery_success"])
            self.assertEqual(task.state.value, "delivery_failed")
        await rig.close()


class _Handler:
    """最小 handler 替身：``_run_hooks`` 只取 ``.handler`` 并 await 其返回值。"""

    def __init__(self, handler):
        self.handler = handler


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
