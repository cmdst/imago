"""v1.4.2 P0：任务身份冻结、群图 @、主动发送与装饰钩子守门。

t1 的只读取证证明两件事（固定基线 ``feat/v1.4.1@a1056441``）：

1. ``_submit`` 在**建任务之前**会先 await（参考图/引用消息解析，photo 入口更早：
   人格解析）；这段挂起期间事件对象的 ``message_obj.sender`` / ``unified_msg_origin``
   一旦被改写，任务归属就落到后一个人身上（首个错误点在 ``main.py:639``）。
2. 主动发送（``integrations/active_send.py``）在组装合成事件时**重读活 sender**，
   装饰钩子又可以在主发之外追加 ``event.send()``：于是 ``on_decorating_result``
   / ``on_after_message_sent`` 注入、篡改、删除或重复提及都能把 @ 发给错的人；
   钩子 stop 掉且没有任何实际派发时还会被记成"发送成功"。

本文件用「屏障 + 冻结前后身份变化 + 假 bot 出站载荷」把这两条钉死，断言的是
**真正交给平台适配器的链**（``Context.send_message`` 收到的 ``MessageChain``），
不联网、不查群成员、不读活配置。
"""

from __future__ import annotations

import io
import random
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

_MISSING = object()
_cache: dict = {}


def _ensure_real_astrbot() -> None:
    """保证 astrbot 真模块在场。

    ``test_native_adapter.py`` 在收集阶段就把 ``astrbot.api`` 换成只有
    ``message_components.Image`` 的桩；本文件的用例需要真实的
    ``AstrMessageEvent`` / ``MessageChain`` / ``At``。若拿到桩，摘掉重导入
    （与 ``test_runtime_hook.py`` 同一手法）。
    """

    import astrbot.api  # noqa: F401  — 可能拿到桩，下面判定

    if getattr(sys.modules["astrbot.api"], "logger", None) is not None:
        return
    for name in [key for key in sys.modules if key == "astrbot" or key.startswith("astrbot.")]:
        del sys.modules[name]
    import astrbot.api as real_api

    if getattr(real_api, "logger", None) is None:  # pragma: no cover - 环境损坏
        raise AssertionError("astrbot.api.logger 不可用：无法验证 P0 提及/发送契约")


def _mods():
    """惰性导入插件模块（收集阶段不碰 astrbot 真模块）。"""
    if not _cache:
        _ensure_real_astrbot()
        import astrbot.api.message_components as components
        import imago_fusion.core.route_identity as route
        import imago_fusion.integrations.active_send as active_send
        import imago_fusion.main as main_module
        from imago_fusion.core.models import DrawTask, GenerationRequest, ImageResult
        from imago_fusion.services.persona_store import PersonaStore

        _cache.update(
            components=components,
            route=route,
            active_send=active_send,
            main=main_module,
            DrawTask=DrawTask,
            GenerationRequest=GenerationRequest,
            ImageResult=ImageResult,
            PersonaStore=PersonaStore,
        )
    return _cache


def png_bytes(seed: int = 0, size: tuple[int, int] = (48, 32)) -> bytes:
    """不同 seed 的噪声 PNG：用来区分「这是谁那张图」。"""
    from PIL import Image

    width, height = size
    rng = random.Random(seed)
    payload = bytes(rng.randrange(256) for _ in range(width * height * 3))
    buffer = io.BytesIO()
    Image.frombytes("RGB", (width, height), payload).save(buffer, format="PNG")
    return buffer.getvalue()


# ── 事件替身（只在字段层面模仿 AstrBot，冻结函数按鸭子类型读取） ──────────────


class _Meta:
    def __init__(self, id: str, name: str) -> None:
        self.id = id
        self.name = name


class _Sender:
    def __init__(self, user_id: str, nickname: str = "someone") -> None:
        self.user_id = user_id
        self.nickname = nickname


class _Group:
    def __init__(self, group_id: str) -> None:
        self.group_id = group_id


class _Message:
    def __init__(self, *, sender: _Sender, message_type: str, group_id: str, self_id: str, message_id: str) -> None:
        self.type = message_type
        self.sender = sender
        self.group_id = group_id
        self.group = _Group(group_id)
        self.self_id = self_id
        self.message_id = message_id
        self.message = []


class _Session:
    def __init__(self, *, platform_id: str, message_type: str, session_id: str) -> None:
        self.platform_name = platform_id
        self.platform_id = platform_id
        self.message_type = message_type
        self.session_id = session_id


class _FakeEvent:
    """最小入站事件；``mutate()`` 模拟 t1 的屏障（挂起期间事件被改写）。"""

    def __init__(
        self,
        *,
        sender: str = "1001",
        umo: str = "aiocqhttp:GroupMessage:g1",
        message_type: str = "GroupMessage",
        platform_id: str = "inst1",
        platform_name: str = "aiocqhttp",
        message_id: str = "m1",
        group_id: str = "g1",
        self_id: str = "bot",
        messages=None,
        nickname: str = "A",
    ) -> None:
        self.unified_msg_origin = umo
        self.platform_meta = _Meta(platform_id, platform_name)
        self.session = _Session(platform_id=platform_id, message_type=message_type, session_id=group_id)
        self.message_obj = _Message(
            sender=_Sender(sender, nickname),
            message_type=message_type,
            group_id=group_id,
            self_id=self_id,
            message_id=message_id,
        )
        self._messages = list(messages or [])
        self.plugins_name = None

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_sender_name(self):
        return self.message_obj.sender.nickname

    def get_platform_id(self):
        return self.platform_meta.id

    def get_platform_name(self):
        return self.platform_meta.name

    def get_group_id(self):
        return self.message_obj.group_id

    def get_messages(self):
        return list(self._messages)

    def plain_result(self, text):
        """入口成功/失败分支只 yield 文本结果；这里只做最小替身。"""
        return ("plain", text)

    def mutate(self, *, sender: str = _MISSING, umo: str = _MISSING, platform_id: str = _MISSING):
        """改写「活事件」身份：模拟屏障挂起期间被替换/被后续事件覆盖。"""
        if sender is not _MISSING:
            self.message_obj.sender = _Sender(sender, "hijacked")
            self.message_obj.sender.user_id = sender
        if umo is not _MISSING:
            self.unified_msg_origin = umo
        if platform_id is not _MISSING:
            self.platform_meta = _Meta(platform_id, self.platform_meta.name)


# ── 假 bot：记录真正交给平台适配器的链 ───────────────────────────────────────


class _Platform:
    def __init__(self, meta: _Meta) -> None:
        self._meta = meta

    def meta(self) -> _Meta:
        return self._meta


class _Context:
    """``ProactiveSender`` 只需要 ``get_platform_inst`` + ``send_message``。"""

    def __init__(self, platform_name: str = "aiocqhttp", platform_id: str = "inst1") -> None:
        self.platform = _Platform(_Meta(platform_id, platform_name))
        self.outbound: list = []
        self.sessions: list = []

    def get_platform_inst(self, platform_id):
        return self.platform

    async def send_message(self, session, chain):
        self.sessions.append(session)
        self.outbound.append(list(getattr(chain, "chain", chain)))
        return True


class _Registry:
    def __init__(self, handlers=None) -> None:
        self.handlers = dict(handlers or {})

    def get_handlers_by_event_type(self, event_type, plugins_name=None):
        return list(self.handlers.get(event_type, []))


class _Handler:
    def __init__(self, callback) -> None:
        self.handler = callback


def _at_targets(chain) -> list:
    """出站链里所有 At 的 ``qq``（``AtAll`` 也会出现在这里，值是 ``"all"``）。"""
    components = _mods()["components"]
    # ``At`` 会把纯数字 ``qq`` 归一成 int；出站比较统一按字符串看。
    return [
        str(getattr(item, "qq", None))
        for item in chain
        if isinstance(item, components.At)
    ]


class RouteIdentityTests(unittest.TestCase):
    """身份快照与提及判定（纯函数层）。"""

    def setUp(self) -> None:
        self.route = _mods()["route"]

    def _identity(self, **kwargs):
        base = dict(
            umo="aiocqhttp:GroupMessage:g1",
            platform_name="aiocqhttp",
            platform_id="inst1",
            message_type="GroupMessage",
            sender_user_id="1001",
            group_id="g1",
        )
        base.update(kwargs)
        return self.route.RouteIdentity(**base)

    def test_group_is_decided_by_structured_type_not_umo_substring(self):
        """群类型只看结构化 ``message_type``：UMO 里有 ":GroupMessage:" 也不算群。"""

        spoofed = self._identity(umo="aiocqhttp:GroupMessage:g1", message_type="")
        self.assertFalse(spoofed.is_group)
        self.assertEqual(spoofed.mention_decision(enabled=True), ("", self.route.MENTION_NOT_GROUP))

        # 反向：UMO 不含群标记，但结构化类型是群 ⇒ 仍然要 @（不能按子串漏掉）。
        structured = self._identity(umo="aiocqhttp:FriendMessage:u1", message_type="GroupMessage")
        self.assertTrue(structured.is_group)
        self.assertEqual(structured.mention_decision(enabled=True)[0], "1001")

    def test_private_chat_and_disabled_never_mention(self):
        friend = self._identity(message_type="FriendMessage", umo="aiocqhttp:FriendMessage:u1")
        self.assertEqual(friend.mention_decision(enabled=True), ("", self.route.MENTION_NOT_GROUP))
        group = self._identity()
        self.assertEqual(group.mention_decision(enabled=False), ("", self.route.MENTION_DISABLED))

    def test_onebot_accepts_only_positive_ascii_decimal(self):
        cases = {
            "1001": self.route.MENTION_OK,
            "0": self.route.MENTION_INVALID_FORMAT,
            "00": self.route.MENTION_INVALID_FORMAT,
            " 1001": self.route.MENTION_INVALID_FORMAT,
            "1001 ": self.route.MENTION_INVALID_FORMAT,
            "10 01": self.route.MENTION_INVALID_FORMAT,
            "+1001": self.route.MENTION_INVALID_ONEBOT_ID,
            "-1001": self.route.MENTION_INVALID_ONEBOT_ID,
            "１００１": self.route.MENTION_INVALID_ONEBOT_ID,
            "1001a": self.route.MENTION_INVALID_ONEBOT_ID,
            "@1001": self.route.MENTION_INVALID_FORMAT,
            "9" * 200: self.route.MENTION_INVALID_FORMAT,
        }
        for sender, reason in cases.items():
            with self.subTest(sender=sender):
                target, actual = self._identity(sender_user_id=sender).mention_decision(enabled=True)
                self.assertEqual(actual, reason)
                self.assertEqual(target, "" if reason != self.route.MENTION_OK else sender)

    def test_broadcast_tokens_never_leave_as_single_mention(self):
        for sender in ("all", "ALL", "@all", "atall", "everyone", "@everyone", "全体成员", "全体"):
            with self.subTest(sender=sender):
                target, reason = self._identity(sender_user_id=sender).mention_decision(enabled=True)
                self.assertEqual((target, reason), ("", self.route.MENTION_BROADCAST))

    def test_missing_sender_is_silent(self):
        for sender in ("", "   ", "\t"):
            with self.subTest(sender=repr(sender)):
                target, reason = self._identity(sender_user_id=sender).mention_decision(enabled=True)
                self.assertEqual((target, reason), ("", self.route.MENTION_MISSING_SENDER))

    def test_opaque_platform_keeps_non_numeric_ids(self):
        """非 OneBot 适配器：不把 QQ 的纯数字规则套到不透明 ID 上。"""

        opaque = self._identity(
            platform_name="telegram",
            umo="telegram:GroupMessage:-100123456",
            sender_user_id="-100123456",
        )
        self.assertEqual(opaque.adapter, "opaque")
        self.assertEqual(opaque.mention_decision(enabled=True)[0], "-100123456")

        handle = self._identity(
            platform_name="wechatpadpro",
            umo="wechatpadpro:GroupMessage:abc@chatroom",
            sender_user_id="wxid_abc123@im.wechat",
        )
        self.assertEqual(handle.mention_decision(enabled=True)[0], "wxid_abc123@im.wechat")

        # 不透明 ID 也要拦掉广播/空白/@ 前缀。
        for sender in ("all", "全体成员", "two words", "@handle"):
            with self.subTest(sender=sender):
                decision = self._identity(
                    platform_name="telegram", sender_user_id=sender
                ).mention_decision(enabled=True)
                self.assertEqual(decision[0], "")
                self.assertIn(
                    decision[1], {self.route.MENTION_BROADCAST, self.route.MENTION_INVALID_FORMAT}
                )

    def test_freeze_snapshot_survives_live_event_mutation(self):
        event = _FakeEvent(sender="1001", umo="aiocqhttp:GroupMessage:g1")
        identity = self.route.freeze_route_identity(event)
        event.mutate(sender="2002", umo="aiocqhttp:GroupMessage:g2", platform_id="inst2")
        # 屏障挂起期间事件被替换：快照仍是原发起人/原会话。
        self.assertEqual(identity.sender_user_id, "1001")
        self.assertEqual(identity.umo, "aiocqhttp:GroupMessage:g1")
        self.assertEqual(identity.platform_id, "inst1")
        self.assertTrue(identity.is_group)

    def test_freeze_tolerates_minimal_stand_in(self):
        """老测试/最小替身只有 ``get_sender_id``：不崩、不猜群、不加 @。"""

        class _Bare:
            def get_sender_id(self):
                return "1"

        identity = self.route.freeze_route_identity(_Bare())
        self.assertEqual(identity.sender_user_id, "1")
        self.assertFalse(identity.is_group)
        self.assertEqual(identity.mention_target(enabled=True), "")

    def test_sanitize_chain_matrix(self):
        components = _mods()["components"]
        At, Plain = components.At, components.Plain
        chain = [At(qq="1001"), At(qq="999"), At(qq="1001"), components.AtAll(), Plain("hi")]

        kept = self.route.sanitize_chain(chain, target="1001")
        self.assertEqual(_at_targets(kept), ["1001"])
        self.assertEqual([type(item).__name__ for item in kept], ["At", "Plain"])

        # 没有可信目标 ⇒ 一个 At 都不许出站，但正文保留。
        stripped = self.route.sanitize_chain(chain, target="")
        self.assertEqual(_at_targets(stripped), [])
        self.assertEqual([type(item).__name__ for item in stripped], ["Plain"])

        # 钩子删掉了目标提及：尊重删除，不补回。
        self.assertEqual(_at_targets(self.route.sanitize_chain([At(qq="999")], target="1001")), [])


class MentionOutboundTests(unittest.IsolatedAsyncioTestCase):
    """``_finish_task`` → 真 ``ProactiveSender`` → 假 bot 出站载荷。"""

    def setUp(self) -> None:
        mods = _mods()
        self.route = mods["route"]
        self.active_send = mods["active_send"]
        self.main = mods["main"]
        self.ImageResult = mods["ImageResult"]
        self._registry = self.active_send.star_handlers_registry
        self.addCleanup(setattr, self.active_send, "star_handlers_registry", self._registry)

    def _plugin(self, directory, *, context=None, at_trigger_user=True):
        mods = _mods()
        plugin = object.__new__(self.main.ImagoFusion)
        plugin.raw_config = {
            "task_config": {"at_trigger_user": at_trigger_user, "llm_caption": False},
            "providers": [
                {
                    "id": "node",
                    "api_type": "openai_image",
                    "base_url": "https://example.invalid/v1",
                    "api_keys": "key_test",
                }
            ],
        }
        plugin.session = None
        plugin.store = mods["PersonaStore"](Path(directory), lambda: 20 * 1024 * 1024)
        plugin.scheduler = _Scheduler()
        plugin.enhancer = None
        plugin.upscaler = None
        plugin.context = context or _Context()
        plugin.sender = self.active_send.ProactiveSender(
            plugin.context,
            lambda *args, **kwargs: None,
            lambda *args, **kwargs: None,
        )
        plugin.quota_store = None
        return plugin

    def _task(self, plugin, identity, *, count=1):
        mods = _mods()
        task = mods["DrawTask"]("d" * 32, identity.umo, mods["GenerationRequest"]("draw", count=count))
        task.owner_user_id = identity.sender_user_id
        task.bot_instance_id = identity.platform_id
        task.runtime["source_event"] = _FakeEvent()
        task.runtime["original_user_prompt"] = "draw"
        task.runtime["route_identity"] = identity
        task.created_at = time.monotonic()
        return task

    def _hooks(self, decorating=None, after=None):
        event_type = self.active_send.EventType
        handlers = {}
        if decorating is not None:
            handlers[event_type.OnDecoratingResultEvent] = [_Handler(decorating)]
        if after is not None:
            handlers[event_type.OnAfterMessageSentEvent] = [_Handler(after)]
        self.active_send.star_handlers_registry = _Registry(handlers)

    def _group_identity(self, *, sender="1001", platform_name="aiocqhttp"):
        return self.route.RouteIdentity(
            umo=f"{platform_name}:GroupMessage:g1",
            platform_name=platform_name,
            platform_id="inst1",
            message_type="GroupMessage",
            sender_user_id=sender,
            sender_nickname="A",
            group_id="g1",
        )

    # ── 主发链：群图只 @ 冻结触发者 ────────────────────────────────────────

    async def test_group_image_mentions_frozen_sender_once(self):
        payload = png_bytes(1)
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory)
            identity = self._group_identity()

            async def decorating(event):
                # 钩子注入别的 At + 重复目标 + 广播：出站前必须被同一策略收敛。
                components = _mods()["components"]
                event.get_result().chain = [
                    components.At(qq="999"),
                    components.At(qq="1001"),
                    components.AtAll(),
                    *list(event.get_result().chain),
                    components.At(qq="1001"),
                ]

            self._hooks(decorating=decorating)
            task = self._task(plugin, identity)
            outcome = await plugin._finish_task(
                task, [self.ImageResult(data=payload, mime_type="image/png")]
            )

            outbound = plugin.context.outbound
            chain = outbound[0] if outbound else []

        self.assertTrue(outcome.success, outcome.error)
        self.assertTrue(outcome.generation_success)
        self.assertEqual(len(outbound), 1)
        self.assertEqual(_at_targets(chain), ["1001"])
        self.assertEqual(type(chain[0]).__name__, "At")
        self.assertEqual(task.runtime["mention_target"], "1001")
        self.assertEqual(task.runtime["mention_reason"], self.route.MENTION_OK)
        self.assertEqual(sum(1 for item in chain if type(item).__name__ == "Image"), 1)

    async def test_hook_rewriting_source_sender_cannot_steal_mention(self):
        """装饰钩子改写**活事件** sender：合成事件与 @ 都取冻结身份。"""

        payload = png_bytes(2)
        seen = {}
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory)
            identity = self._group_identity(sender="1001")
            source = _FakeEvent(sender="1001")

            async def decorating(event):
                source.mutate(sender="7777", umo="aiocqhttp:GroupMessage:g2")
                seen["sender"] = event.get_sender_id()
                seen["type"] = event.get_message_type()
                seen["group"] = event.get_group_id()

            self._hooks(decorating=decorating)
            task = self._task(plugin, identity)
            task.runtime["source_event"] = source
            outcome = await plugin._finish_task(
                task, [self.ImageResult(data=payload, mime_type="image/png")]
            )
            chain = plugin.context.outbound[0]

        self.assertTrue(outcome.success, outcome.error)
        self.assertEqual(seen["sender"], "1001")
        self.assertEqual(getattr(seen["type"], "value", seen["type"]), "GroupMessage")
        self.assertEqual(seen["group"], "g1")
        self.assertEqual(_at_targets(chain), ["1001"])

    async def test_two_senders_same_group_keep_their_own_mention(self):
        """同群 A/B 各自成图，B 先出图：谁也不串到谁（t1 的对照必须保持正确）。"""

        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory)
            alice = self.route.RouteIdentity(
                umo="aiocqhttp:GroupMessage:g1",
                platform_name="aiocqhttp",
                platform_id="inst1",
                message_type="GroupMessage",
                sender_user_id="1001",
                group_id="g1",
            )
            bob = self.route.RouteIdentity(
                umo="aiocqhttp:GroupMessage:g1",
                platform_name="aiocqhttp",
                platform_id="inst1",
                message_type="GroupMessage",
                sender_user_id="2002",
                group_id="g1",
            )
            task_a = self._task(plugin, alice)
            task_b = self._task(plugin, bob)
            for task, seed in ((task_a, 11), (task_b, 22)):
                task.id = f"{seed:032x}"
            # B 先完成（t1 的逆序场景）。
            outcome_b = await plugin._finish_task(
                task_b, [self.ImageResult(data=png_bytes(22), mime_type="image/png")]
            )
            outcome_a = await plugin._finish_task(
                task_a, [self.ImageResult(data=png_bytes(11), mime_type="image/png")]
            )
            outbound = plugin.context.outbound

        self.assertTrue(outcome_b.success and outcome_a.success)
        self.assertEqual([_at_targets(chain) for chain in outbound], [["2002"], ["1001"]])

    async def test_private_chat_never_gains_mention(self):
        payload = png_bytes(3)
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory)
            identity = self.route.RouteIdentity(
                umo="aiocqhttp:FriendMessage:u1",
                platform_name="aiocqhttp",
                platform_id="inst1",
                message_type="FriendMessage",
                sender_user_id="1001",
            )

            async def decorating(event):
                components = _mods()["components"]
                event.get_result().chain = [components.At(qq="1001"), *list(event.get_result().chain)]

            self._hooks(decorating=decorating)
            task = self._task(plugin, identity)
            outcome = await plugin._finish_task(
                task, [self.ImageResult(data=payload, mime_type="image/png")]
            )
            chain = plugin.context.outbound[0]

        self.assertTrue(outcome.success, outcome.error)
        self.assertEqual(_at_targets(chain), [])
        self.assertEqual(task.runtime["mention_reason"], self.route.MENTION_NOT_GROUP)

    async def test_disabled_at_config_never_mentions(self):
        payload = png_bytes(4)
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory, at_trigger_user=False)

            async def decorating(event):
                components = _mods()["components"]
                event.get_result().chain = [
                    components.At(qq="1001"),
                    *list(event.get_result().chain),
                ]

            self._hooks(decorating=decorating)
            task = self._task(plugin, self._group_identity())
            outcome = await plugin._finish_task(
                task, [self.ImageResult(data=payload, mime_type="image/png")]
            )
            chain = plugin.context.outbound[0]

        self.assertTrue(outcome.success, outcome.error)
        self.assertEqual(_at_targets(chain), [])
        self.assertEqual(task.runtime["mention_reason"], self.route.MENTION_DISABLED)

    async def test_untrusted_target_still_delivers_image(self):
        """拿不到可信单人目标 ⇒ 静默纯图：既不补文字，也不因 @ 失败丢图。"""

        payload = png_bytes(5)
        for sender, reason in (
            ("all", self.route.MENTION_BROADCAST),
            ("0", self.route.MENTION_INVALID_FORMAT),
            ("", self.route.MENTION_MISSING_SENDER),
            ("abc", self.route.MENTION_INVALID_ONEBOT_ID),
        ):
            with self.subTest(sender=sender), TemporaryDirectory() as directory:
                plugin = self._plugin(directory)
                task = self._task(plugin, self._group_identity(sender=sender))
                outcome = await plugin._finish_task(
                    task, [self.ImageResult(data=payload, mime_type="image/png")]
                )
                chain = plugin.context.outbound[0]
                texts = [str(getattr(item, "text", "")) for item in chain]
                self.assertTrue(outcome.success, outcome.error)
                self.assertTrue(outcome.generation_success)
                self.assertEqual(_at_targets(chain), [])
                self.assertEqual(task.runtime["mention_reason"], reason)
                self.assertEqual(sum(1 for item in chain if type(item).__name__ == "Image"), 1)
                # 有图成功回包不额外补文字解释（尤其是"没 @ 到"这类噪音）。
                self.assertEqual([text for text in texts if text], [])

    async def test_task_without_frozen_identity_does_not_guess_group(self):
        payload = png_bytes(6)
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory)
            task = self._task(plugin, self._group_identity())
            # 老任务/替身可能没有冻结身份：只剩 UMO 子串（含 :GroupMessage:）。
            del task.runtime["route_identity"]
            outcome = await plugin._finish_task(
                task, [self.ImageResult(data=payload, mime_type="image/png")]
            )
            chain = plugin.context.outbound[0]

        self.assertTrue(outcome.success, outcome.error)
        self.assertEqual(_at_targets(chain), [])
        self.assertEqual(task.runtime["mention_reason"], self.route.MENTION_NOT_GROUP)

    # ── 装饰/after 钩子的旁发与 stop 语义 ────────────────────────────────

    async def test_side_send_from_hook_is_sanitized(self):
        payload = png_bytes(7)
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory)

            async def decorating(event):
                components = _mods()["components"]
                await event.send(
                    self.active_send.MessageChain(
                        chain=[components.At(qq="6666"), components.Plain("side")]
                    )
                )

            self._hooks(decorating=decorating)
            task = self._task(plugin, self._group_identity())
            outcome = await plugin._finish_task(
                task, [self.ImageResult(data=payload, mime_type="image/png")]
            )
            outbound = plugin.context.outbound

        self.assertTrue(outcome.success, outcome.error)
        self.assertTrue(outcome.side_send_started)
        self.assertEqual(len(outbound), 2)
        self.assertEqual(_at_targets(outbound[0]), [])
        self.assertEqual(_at_targets(outbound[1]), ["1001"])

    async def test_stop_without_any_send_is_not_success(self):
        payload = png_bytes(8)
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory)

            async def decorating(event):
                event.stop_event()

            self._hooks(decorating=decorating)
            task = self._task(plugin, self._group_identity())
            outcome = await plugin._finish_task(
                task, [self.ImageResult(data=payload, mime_type="image/png")]
            )
            outbound = plugin.context.outbound

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.error, "StoppedWithoutSend")
        self.assertTrue(outcome.generation_success)
        self.assertEqual(outbound, [])
        self.assertFalse(task.runtime["image_delivery_success"])
        self.assertEqual(task.runtime["image_delivery_error"], "StoppedWithoutSend")
        self.assertTrue(task.runtime["runner_send_completed"])

    async def test_stop_after_real_side_send_counts_as_delivered(self):
        payload = png_bytes(9)
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory)

            async def decorating(event):
                components = _mods()["components"]
                await event.send(
                    self.active_send.MessageChain(chain=[components.Plain("early")])
                )
                event.stop_event()

            self._hooks(decorating=decorating)
            task = self._task(plugin, self._group_identity())
            outcome = await plugin._finish_task(
                task, [self.ImageResult(data=payload, mime_type="image/png")]
            )
            outbound = plugin.context.outbound

        self.assertTrue(outcome.success, outcome.error)
        self.assertEqual(len(outbound), 1)
        self.assertTrue(outcome.side_send_started)

    async def test_after_hook_side_send_is_sanitized_too(self):
        payload = png_bytes(10)
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory)

            async def after(event):
                components = _mods()["components"]
                await event.send(
                    self.active_send.MessageChain(
                        chain=[components.At(qq="all"), components.Plain("after")]
                    )
                )

            self._hooks(after=after)
            task = self._task(plugin, self._group_identity())
            outcome = await plugin._finish_task(
                task, [self.ImageResult(data=payload, mime_type="image/png")]
            )
            outbound = plugin.context.outbound

        self.assertTrue(outcome.success, outcome.error)
        self.assertEqual(len(outbound), 2)
        self.assertEqual(_at_targets(outbound[1]), [])


    async def test_hook_removing_all_mentions_is_respected(self):
        """钩子显式删掉提及但保留图：不补 @，也不为 @ 恢复被删的内容。"""

        payload = png_bytes(11)
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory)

            async def decorating(event):
                components = _mods()["components"]
                kept = [
                    item
                    for item in event.get_result().chain
                    if not isinstance(item, components.At)
                ]
                event.get_result().chain = kept

            self._hooks(decorating=decorating)
            task = self._task(plugin, self._group_identity())
            outcome = await plugin._finish_task(
                task, [self.ImageResult(data=payload, mime_type="image/png")]
            )
            chain = plugin.context.outbound[0]

        self.assertTrue(outcome.success, outcome.error)
        self.assertTrue(outcome.generation_success)
        self.assertEqual(_at_targets(chain), [])
        self.assertEqual(sum(1 for item in chain if type(item).__name__ == "Image"), 1)
        # 主发链自己那张 @ 被删掉了：不因为"该有 @ "再发第二条消息。
        self.assertEqual(len(plugin.context.outbound), 1)

    async def test_hook_clearing_chain_then_stop_sends_nothing(self):
        """clear + stop：不为了 @ 恢复整条消息，也不记成功。"""

        payload = png_bytes(12)
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory)

            async def decorating(event):
                event.get_result().chain = []
                event.stop_event()

            self._hooks(decorating=decorating)
            task = self._task(plugin, self._group_identity())
            outcome = await plugin._finish_task(
                task, [self.ImageResult(data=payload, mime_type="image/png")]
            )
            outbound = plugin.context.outbound

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.error, "StoppedWithoutSend")
        self.assertEqual(outbound, [])

    async def test_cleared_chain_without_stop_is_not_success(self):
        """clear 但没 stop：没有任何内容可发 ⇒ DecoratorSuppressed，不是成功。"""

        payload = png_bytes(13)
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory)

            async def decorating(event):
                event.get_result().chain = []

            self._hooks(decorating=decorating)
            task = self._task(plugin, self._group_identity())
            outcome = await plugin._finish_task(
                task, [self.ImageResult(data=payload, mime_type="image/png")]
            )
            outbound = plugin.context.outbound

        self.assertFalse(outcome.success)
        self.assertEqual(outcome.error, "DecoratorSuppressed")
        self.assertEqual(outbound, [])

    async def test_plain_text_side_send_gains_no_mention(self):
        """纯文本旁发：合法群 @ 目标在场也不凭空加 @（只守门、不造提及）。"""

        payload = png_bytes(14)
        with TemporaryDirectory() as directory:
            plugin = self._plugin(directory)

            async def after(event):
                components = _mods()["components"]
                await event.send(
                    self.active_send.MessageChain(chain=[components.Plain("再提醒一次")])
                )

            self._hooks(after=after)
            task = self._task(plugin, self._group_identity())
            outcome = await plugin._finish_task(
                task, [self.ImageResult(data=payload, mime_type="image/png")]
            )
            outbound = plugin.context.outbound

        self.assertTrue(outcome.success, outcome.error)
        self.assertEqual(len(outbound), 2)
        self.assertEqual(_at_targets(outbound[0]), ["1001"])
        self.assertEqual(_at_targets(outbound[1]), [])
        self.assertEqual([type(item).__name__ for item in outbound[1]], ["Plain"])

class _Decision:
    def __init__(self, allowed=True, charged=0, reason="") -> None:
        self.allowed = allowed
        self.charged = charged
        self.reason = reason


class _QuotaStore:
    def __init__(self) -> None:
        self.consumed: list = []

    def can_consume(self, user_id, amount, policy):
        return _Decision()

    def consume(self, user_id, amount, policy):
        self.consumed.append((user_id, amount))
        return _Decision(charged=amount)


class _Scheduler:
    def __init__(self) -> None:
        self.accepting = True
        self.submitted: list = []
        self.stages: list = []

    def set_stage(self, task, stage):
        self.stages.append((task.id, stage))

    def submit(self, task):
        self.submitted.append(task)

    def query(self, *, umo, owner_user_id, bot_instance_id):
        return [
            task
            for task in self.submitted
            if task.umo == umo
            and task.owner_user_id == owner_user_id
            and task.bot_instance_id == bot_instance_id
        ]


class EntryFreezeTests(unittest.IsolatedAsyncioTestCase):
    """六个入口：首次 await 之前冻结；屏障期间身份改写不影响任务归属。"""

    def setUp(self) -> None:
        mods = _mods()
        self.route = mods["route"]
        self.main = mods["main"]
        self.modules = mods

    def _plugin(self, directory, *, mutate):
        mods = self.modules
        plugin = object.__new__(self.main.ImagoFusion)
        plugin.raw_config = {
            "task_config": {"at_trigger_user": True, "llm_caption": False},
            "providers": [
                {
                    "id": "node",
                    "api_type": "openai_image",
                    "base_url": "https://example.invalid/v1",
                    "api_keys": "key_test",
                }
            ],
        }
        plugin.session = None
        plugin.store = mods["PersonaStore"](Path(directory), lambda: 20 * 1024 * 1024)
        plugin.scheduler = _Scheduler()
        plugin.quota_store = _QuotaStore()
        plugin.enhancer = None
        plugin.upscaler = None
        plugin.sender = None
        plugin._delivered = None

        async def plan_task_references(components):
            # 屏障：t1 在 `_submit` 的参考图解析（main.py:617/624）处挂起事件。
            mutate()
            return [], []

        plugin._plan_task_references = plan_task_references
        return plugin

    async def test_draw_entry_freezes_before_reference_barrier(self):
        event = _FakeEvent(sender="1001", umo="aiocqhttp:GroupMessage:g1")
        with TemporaryDirectory() as directory:
            plugin = self._plugin(
                directory,
                mutate=lambda: event.mutate(sender="2002", umo="aiocqhttp:GroupMessage:g2"),
            )
            async for _ in plugin.draw_command(event, "画一只猫"):
                pass
            submitted = plugin.scheduler.submitted
        self.assertEqual(len(submitted), 1)
        task = submitted[0]
        self.assertEqual(task.owner_user_id, "1001")
        self.assertEqual(task.umo, "aiocqhttp:GroupMessage:g1")
        self.assertEqual(task.bot_instance_id, "inst1")
        self.assertEqual(task.runtime["route_identity"].sender_user_id, "1001")

    async def test_persona_entries_freeze_before_persona_resolution(self):
        """photo/工具入口的人格解析早于建任务：冻结必须在它之前。"""

        with TemporaryDirectory() as directory:
            event = _FakeEvent(sender="1001", umo="aiocqhttp:GroupMessage:g1")
            plugin = self._plugin(directory, mutate=lambda: None)
            frozen = []

            async def resolve_persona(inner_event, identity=None):
                # 人格解析挂起期间事件被改写（t1 的 :2082→:534 路径）。
                frozen.append((identity.sender_user_id, identity.umo))
                inner_event.mutate(sender="2002", umo="aiocqhttp:GroupMessage:g2")
                return "persona", "prompt"

            plugin._resolve_persona = resolve_persona
            async for _ in plugin.photo_command(event, "拍一张"):
                pass
            submitted = plugin.scheduler.submitted

        self.assertEqual(frozen, [("1001", "aiocqhttp:GroupMessage:g1")])
        self.assertEqual(submitted[0].owner_user_id, "1001")
        self.assertEqual(submitted[0].umo, "aiocqhttp:GroupMessage:g1")

    async def test_photo_entry_freezes_before_first_await(self):
        """photo 入口：身份在**人格解析之前**冻结，解析期间改事件也改不了归属。"""

        with TemporaryDirectory() as directory:
            event = _FakeEvent(sender="1001", umo="aiocqhttp:GroupMessage:g1")
            plugin = self._plugin(directory, mutate=lambda: event.mutate(sender="2002"))
            resolved = []

            async def resolve_persona(inner_event, identity=None):
                resolved.append(identity.sender_user_id)
                inner_event.mutate(sender="2002", umo="aiocqhttp:GroupMessage:g2")
                return "persona", "prompt"

            plugin._resolve_persona = resolve_persona
            async for _ in plugin.photo_command(event, "拍一张"):
                pass
            submitted = plugin.scheduler.submitted

        self.assertEqual(resolved, ["1001"])
        self.assertEqual(submitted[0].owner_user_id, "1001")
        self.assertEqual(submitted[0].umo, "aiocqhttp:GroupMessage:g1")

    async def test_shortcuts_inherit_command_freeze(self):
        for entry, method in (("shortcut-draw", "draw_shortcut"), ("shortcut-photo", "photo_shortcut")):
            with self.subTest(entry=entry), TemporaryDirectory() as directory:
                event = _FakeEvent(sender="1001", umo="aiocqhttp:GroupMessage:g1")
                plugin = self._plugin(directory, mutate=lambda: event.mutate(sender="2002"))

                async def resolve_persona(inner_event, identity=None):
                    return "persona", "prompt"

                plugin._resolve_persona = resolve_persona
                async for _ in getattr(plugin, method)(event, "画一只猫"):
                    pass
                submitted = plugin.scheduler.submitted

                self.assertEqual(len(submitted), 1)
                self.assertEqual(submitted[0].owner_user_id, "1001")
                self.assertEqual(submitted[0].umo, "aiocqhttp:GroupMessage:g1")

    async def test_llm_tool_entries_freeze_before_submit(self):
        for entry in ("tool-image", "tool-persona"):
            with self.subTest(entry=entry), TemporaryDirectory() as directory:
                event = _FakeEvent(sender="1001", umo="aiocqhttp:GroupMessage:g1")
                plugin = self._plugin(directory, mutate=lambda: event.mutate(sender="2002"))

                async def resolve_persona(inner_event, identity=None):
                    return "persona", "prompt"

                plugin._resolve_persona = resolve_persona
                if entry == "tool-image":
                    await plugin.generate_fusion_image(event, prompt="画一只猫")
                else:
                    await plugin.generate_fusion_persona_image(event, action="自拍")
                submitted = plugin.scheduler.submitted

                self.assertEqual(len(submitted), 1)
                self.assertEqual(submitted[0].owner_user_id, "1001")
                self.assertEqual(submitted[0].umo, "aiocqhttp:GroupMessage:g1")



if __name__ == "__main__":  # pragma: no cover
    unittest.main()
