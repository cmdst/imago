"""t21 wire 补件：@ 与成品必须落在**同一条**实际群消息；旁发成功不被后错覆盖。

两个 QA 已复现的真实边界：

① **宿主分帧**：OneBot 适配器只要在链上看到 ``Node``/``Nodes``/``File`` 就改成
   "逐组件派发"（``aiocqhttp_message_event.py:171-208``）。因此
   ``[At(发起人), Image(成品), Nodes([...])]`` 会被拆成三条群消息：一条只有
   ``at``（成了单独提醒）、一条只有图（**成品帧没有 @**）、一条转发。修复要求：
   发起人的唯一规范 At 与成品留在同一条群消息里；无法同帧送达的附加装饰在插件
   出口做**局部降级**（不补发、不重复出图、不改宿主）。

② **旁发记账**：hook 先旁发出本任务成品、随后一次无图文字旁发被平台拒绝，再
   ``clear`` 或 ``stop``。历史实现用"最后一次旁发没有错误"判定成功，于是已成功
   送达的成品被后一次拒绝抹掉，任务记成投递失败。修复要求：按**真正被接受的**
   成品结算（``attempted >= dispatched == 1``、终态送图成功），同时保留旁发失败
   的有界诊断；全路无图/只有辅助文本仍不得记送图成功。

全部离线：假 bot 只记录适配器**序列化后**的 wire 载荷（段序列 / at / 图片 digest
/ 转发帧），不联网；``llm_generate`` 一旦被调用即失败。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
from unittest.mock import patch

import test_v142_wire as wire


def _mods():
    return wire._mods()


class _EdgeBot(wire._FakeBot):
    """在上游假 bot 之上再记录"合并转发"帧（宿主对 Node/Nodes/File 单独派发）。"""

    def __init__(self) -> None:
        super().__init__()
        self.forwards: list = []

    async def call_action(self, action, **kwargs):
        self.forwards.append(NS(action=action, kwargs=kwargs))
        return {"message_id": 0}

    async def send_private_forward_msg(self, **kwargs):  # pragma: no cover - 私聊不触发
        self.forwards.append(NS(action="send_private_forward_msg", kwargs=kwargs))
        return {"message_id": 0}


class _Rig(wire._Rig):
    """把假 bot 换成同时记录转发帧的版本（其余装配与共享 wire 用例完全一致）。"""

    def __init__(self, root: Path, response, **kwargs) -> None:
        with patch.object(wire, "_FakeBot", _EdgeBot):
            super().__init__(root, response, **kwargs)


def _image(data: bytes):
    return _mods()["components"].Image.fromBytes(data)


def _frame_summary(frames) -> list:
    return [(frame.sequence, frame.at, len(frame.pictures)) for frame in frames]


class WireFollowUpTests(unittest.IsolatedAsyncioTestCase):
    async def _draw(self, rig, prompt="A 画一只猫"):
        return await rig.done(await rig.draw(rig.event(), prompt))

    # ------------------------------------------------------------------ ① 分帧
    async def test_forward_node_keeps_mention_in_the_product_frame(self):
        """主链：成品图 + 一个普通文本转发节点混链，@ 不得被分到另一条消息。"""

        payload = wire.png_bytes(6001)

        async def response(conf, request):
            return [_mods()["ImageResult"](data=payload)]

        def decorating(event):
            components = _mods()["components"]
            event.get_result().chain.append(
                components.Nodes(
                    [components.Node([components.Plain("QA_FORWARD_SIDECAR")], uin="888888", name="qa")]
                )
            )

        with TemporaryDirectory() as directory:
            with _Rig(Path(directory), response) as rig:
                rig.hooks.before.append(wire._Handler(decorating))
                task = await self._draw(rig)
                frames = rig.bot.out
                self.assertEqual(len(frames), 1, _frame_summary(frames))
                frame = frames[0]
                self.assertEqual((frame.kind, frame.dest), ("group", wire.GROUP_ID))
                self.assertEqual(wire._without_spacers(frame.sequence), ("at", "image"))
                self.assertEqual(frame.at, (wire.USER_A,))
                self.assertEqual(frame.pictures, (wire.digest(payload),))
                self.assertEqual(rig.bot.forwards, [])
                self.assertEqual(task.state.value, "succeeded")
                self.assertEqual(task.runtime["image_delivery_dispatched_images"], 1)
                # 局部降级必须留下有界计数（只数被丢掉的装饰段，不含内容）。
                self.assertEqual(
                    task.runtime["image_delivery_dropped_split_components"], 1
                )
                await rig.close()

    async def test_file_decoration_keeps_mention_in_the_product_frame(self):
        """``File`` 触发同一宿主分帧规则：成品帧仍必须带发起人的唯一 At。"""

        payload = wire.png_bytes(6002)

        async def response(conf, request):
            return [_mods()["ImageResult"](data=payload)]

        def decorating(event):
            components = _mods()["components"]
            event.get_result().chain.append(
                components.File(name="qa-report.txt", file="/tmp/qa-report.txt", url="")
            )

        with TemporaryDirectory() as directory:
            with _Rig(Path(directory), response) as rig:
                rig.hooks.before.append(wire._Handler(decorating))
                task = await self._draw(rig)
                frames = rig.bot.out
                self.assertEqual(len(frames), 1, _frame_summary(frames))
                self.assertEqual(wire._without_spacers(frames[0].sequence), ("at", "image"))
                self.assertEqual(frames[0].at, (wire.USER_A,))
                self.assertEqual(frames[0].pictures, (wire.digest(payload),))
                # 附件段被局部降级（不额外发一条"只有文件"的消息），计数留痕。
                self.assertEqual(rig.bot.forwards, [])
                self.assertEqual(
                    task.runtime["image_delivery_dropped_split_components"], 1
                )
                self.assertEqual(task.state.value, "succeeded")
                await rig.close()

    async def test_side_send_with_mention_and_node_stays_one_frame(self):
        """装饰旁发共用同一规则：At + 成品 + 转发节点也只能出一条带 @ 的成品帧。"""

        payload = wire.png_bytes(6003)

        async def response(conf, request):
            return [_mods()["ImageResult"](data=payload)]

        async def decorating(event):
            components = _mods()["components"]
            chain = event.get_result().chain
            product = next(item for item in chain if isinstance(item, components.Image))
            await event.send(
                _mods()["MessageChain"](
                    chain=[
                        components.At(qq=wire.USER_C),
                        product,
                        components.Nodes(
                            [components.Node([components.Plain("QA_SIDE_FORWARD")], uin="888888")]
                        ),
                    ]
                )
            )
            chain.clear()

        with TemporaryDirectory() as directory:
            with _Rig(Path(directory), response) as rig:
                rig.hooks.before.append(wire._Handler(decorating))
                task = await self._draw(rig)
                frames = rig.bot.out
                self.assertEqual(len(frames), 1, _frame_summary(frames))
                self.assertEqual(wire._without_spacers(frames[0].sequence), ("at", "image"))
                self.assertEqual(frames[0].at, (wire.USER_A,))
                self.assertEqual(frames[0].pictures, (wire.digest(payload),))
                self.assertEqual(rig.bot.forwards, [])
                self.assertTrue(task.runtime["image_delivery_success"])
                self.assertTrue(task.runtime["image_delivery_main_suppressed"])
                await rig.close()

    # ------------------------------------------------------- ② 旁发记账（后错）
    async def _late_refusal(self, *, mode: str):
        payload = wire.png_bytes(6010 if mode == "clear" else 6011)
        state = {"refused": 0}

        async def response(conf, request):
            return [_mods()["ImageResult"](data=payload)]

        async def decorating(event):
            components = _mods()["components"]
            chain = event.get_result().chain
            product = next(item for item in chain if isinstance(item, components.Image))
            await event.send(
                _mods()["MessageChain"](
                    chain=[components.At(qq=wire.USER_C), product]
                )
            )
            saved = list(rig.context.platform_manager.platform_insts)
            rig.context.platform_manager.platform_insts = []
            try:
                with self.assertRaises(RuntimeError):
                    await event.send(
                        _mods()["MessageChain"](chain=[components.Plain("QA_SECOND_REFUSED")])
                    )
                state["refused"] += 1
            finally:
                rig.context.platform_manager.platform_insts = saved
            if mode == "clear":
                chain.clear()
            else:
                event.stop_event()

        with TemporaryDirectory() as directory:
            with _Rig(Path(directory), response) as rig:
                rig.hooks.before.append(wire._Handler(decorating))
                task = await self._draw(rig)
                frames = rig.bot.out
                self.assertEqual(state["refused"], 1)
                self.assertEqual(len(frames), 1, _frame_summary(frames))
                self.assertEqual(wire._without_spacers(frames[0].sequence), ("at", "image"))
                self.assertEqual(frames[0].at, (wire.USER_A,))
                self.assertEqual(frames[0].pictures, (wire.digest(payload),))
                attempted = task.runtime["image_delivery_attempted_images"]
                dispatched = task.runtime["image_delivery_dispatched_images"]
                self.assertGreaterEqual(attempted, dispatched)
                self.assertEqual(dispatched, 1)
                self.assertTrue(task.runtime["image_delivery_success"])
                self.assertEqual(task.state.value, "succeeded")
                # 辅助失败必须保留成有界诊断，不能被"成功"抹平。
                self.assertTrue(task.runtime["image_delivery_side_send_error"])
                self.assertTrue(task.runtime["image_delivery_error"])
                self.assertEqual(
                    task.runtime["image_delivery_main_suppressed"], mode == "clear"
                )
                await rig.close()
                return task

    async def test_late_refused_side_send_then_clear_keeps_product_delivery(self):
        """旁发成品被接受 → 第二次无图旁发被拒 → clear：仍按已接受成品结算。"""

        await self._late_refusal(mode="clear")

    async def test_late_refused_side_send_then_stop_keeps_product_delivery(self):
        """同上，但钩子改用 ``stop_event``：已接受的成品仍算送达。"""

        await self._late_refusal(mode="stop")

    # ------------------------------------------------------------- 对照与回归
    async def test_text_only_side_send_after_refusal_is_still_not_image_delivery(self):
        """只发辅助文本（没有任何成品被接受）不得记送图成功。"""

        payload = wire.png_bytes(6020)

        async def response(conf, request):
            return [_mods()["ImageResult"](data=payload)]

        async def decorating(event):
            components = _mods()["components"]
            await event.send(_mods()["MessageChain"](chain=[components.Plain("QA_SIDE_TEXT")]))
            event.get_result().chain.clear()

        with TemporaryDirectory() as directory:
            with _Rig(Path(directory), response) as rig:
                rig.hooks.before.append(wire._Handler(decorating))
                task = await self._draw(rig)
                self.assertEqual(len(rig.bot.out), 1, _frame_summary(rig.bot.out))
                self.assertEqual(rig.bot.out[0].pictures, ())
                self.assertEqual(task.runtime["image_delivery_dispatched_images"], 0)
                self.assertFalse(task.runtime["image_delivery_success"])
                self.assertEqual(task.state.value, "delivery_failed")
                await rig.close()

    async def test_private_chat_with_node_decoration_still_sends_no_mention(self):
        """私聊对照：不做提及归一，转发装饰也不需要被降级。"""

        payload = wire.png_bytes(6021)

        async def response(conf, request):
            return [_mods()["ImageResult"](data=payload)]

        def decorating(event):
            components = _mods()["components"]
            event.get_result().chain.append(
                components.Nodes([components.Node([components.Plain("QA_PRIVATE_FORWARD")])])
            )

        with TemporaryDirectory() as directory:
            with _Rig(Path(directory), response) as rig:
                rig.hooks.before.append(wire._Handler(decorating))
                task = await rig.done(
                    await rig.draw(rig.event(private=True), "私聊画一只猫")
                )
                self.assertTrue(rig.bot.out)
                self.assertTrue(all(frame.kind == "private" for frame in rig.bot.out))
                self.assertTrue(all(not frame.at for frame in rig.bot.out))
                merged = [digest for frame in rig.bot.out for digest in frame.pictures]
                self.assertEqual(merged, [wire.digest(payload)])
                self.assertEqual(task.runtime["image_delivery_dispatched_images"], 1)
                await rig.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
