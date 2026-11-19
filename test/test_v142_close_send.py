"""v1.4.2 收尾：三个发送边界的真序列化回归（t24）。

全部用例都走**真实命令/工具入口 → 真 ``TaskScheduler`` → 真 OneBot 适配器
序列化 → 假 bot**，断言的是平台真正会收到的分段（``at`` / ``image`` 的字节
digest / ``text``），不是 ``context.outbound`` 里的组件对象。

三个边界（与派单逐条对应）：

1. **成品归属**：同一条路径上的字节被钩子换掉后，不能把别人的图当成（或发性
   化成）本任务成品；也不能因为"适配器返回 True"就记成功。用任务局部的原始
   成品字节校验，最终序列化的就是校验过的那份字节。
2. **严格纯图子集**：纯图分支只取钩子**保留下来的**成品；删一张不得回灌整单，
   删光或只剩外来图时不得恢复整单。
3. **提及豁免不外溢**：无图终态主通知保留自己的 @ 语义，但 after 钩子的旁发
   不能继承这份豁免，纯文本旁发不得凭空补出一个只有提醒的消息。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import test_v142_wire as wire


def _handler(callback):
    return wire._Handler(callback)


async def _draw_tool(rig, event, prompt: str, count: int = 1):
    """走真实 LLM 工具入口创建任务（需要 ``count`` > 1 时用）。"""

    before = set(rig.plugin.scheduler.tasks) | set(rig.plugin.scheduler.recent)
    await rig.plugin.generate_fusion_image(event, prompt=prompt, count=count)
    fresh = (set(rig.plugin.scheduler.tasks) | set(rig.plugin.scheduler.recent)) - before
    mine = [
        task_id
        for task_id in fresh
        if getattr(getattr(rig._task_entry(task_id)[0], "request", None), "prompt", None) == prompt
    ]
    if len(mine) != 1:  # pragma: no cover - 入口被改坏时给出可读失败
        raise AssertionError(f"工具入口没有为 {prompt!r} 创建唯一任务；新任务={sorted(fresh)}")
    return rig._task_entry(mine[0])


def _product_paths(event) -> list[Path]:
    mods = wire._mods()
    paths = []
    for item in list(event.get_result().chain or []):
        if isinstance(item, mods["components"].Image):
            path = getattr(item, "path", None)
            if path:
                paths.append(Path(path))
    return paths


class ProductIntegrityTests(unittest.IsolatedAsyncioTestCase):
    """① 成品归属：路径上的字节被换掉，也不能把别人的图当成本任务成品。"""

    async def test_strict_mode_ignores_hook_swapped_bytes_at_the_product_path(self):
        """严格纯图：钩子把成品文件覆盖成别的图，线上仍必须是本任务成品字节。"""

        product = wire.png_bytes(901)
        other = wire.png_bytes(902)

        async def response(conf, request):
            return [wire._mods()["ImageResult"](data=product)]

        async def decorating(event):
            for path in _product_paths(event):
                path.write_bytes(other)

        with TemporaryDirectory() as directory, wire._Rig(Path(directory), response) as rig:
            rig.hooks.before.append(_handler(decorating))
            task = await rig.done(await rig.draw(rig.event(wire.USER_A + " "), "A 画一只猫"))

            self.assertEqual(len(rig.bot.out), 1)
            frame = rig.bot.out[0]
            self.assertEqual(frame.pictures, (wire.digest(product),))
            self.assertNotIn(wire.digest(other), frame.pictures)
            self.assertEqual(_without_spacers(frame.sequence), ("image",))
            self.assertEqual(frame.at, ())
            self.assertEqual(task.runtime["image_delivery_dispatched_images"], 1)

    async def test_normal_mode_serializes_frozen_product_bytes(self):
        """普通主发：同一路径被换字节，出站的仍应是本任务成品，且算 1 张。"""

        product = wire.png_bytes(903)
        other = wire.png_bytes(904)

        async def response(conf, request):
            return [wire._mods()["ImageResult"](data=product)]

        async def decorating(event):
            for path in _product_paths(event):
                path.write_bytes(other)

        with TemporaryDirectory() as directory, wire._Rig(Path(directory), response) as rig:
            rig.hooks.before.append(_handler(decorating))
            task = await rig.done(await rig.draw(rig.event(wire.USER_A), "A 画一只猫"))

            self.assertEqual(len(rig.bot.out), 1)
            frame = rig.bot.out[0]
            self.assertEqual(frame.pictures, (wire.digest(product),))
            self.assertNotIn(wire.digest(other), frame.pictures)
            self.assertEqual(frame.at, (wire.USER_A,))
            self.assertEqual(task.runtime["image_delivery_dispatched_images"], 1)

    async def test_hook_side_send_of_the_product_path_uses_frozen_bytes(self):
        """旁发：钩子按同路径重读文件时，出站的必须是校验过的成品字节。"""

        product = wire.png_bytes(905)
        other = wire.png_bytes(906)

        async def response(conf, request):
            return [wire._mods()["ImageResult"](data=product)]

        async def decorating(event):
            mods = wire._mods()
            paths = _product_paths(event)
            self.assertTrue(paths)
            paths[0].write_bytes(other)
            await event.send(
                mods["MessageChain"]([mods["components"].Image.fromFileSystem(str(paths[0]))])
            )

        with TemporaryDirectory() as directory, wire._Rig(Path(directory), response) as rig:
            rig.hooks.before.append(_handler(decorating))
            task = await rig.done(await rig.draw(rig.event(wire.USER_A), "A 画一只猫"))

            self.assertEqual(len(rig.bot.out), 2)
            side, main = rig.bot.out
            self.assertEqual(side.pictures, (wire.digest(product),))
            self.assertNotIn(wire.digest(other), side.pictures)
            self.assertEqual(main.pictures, (wire.digest(product),))
            # 两次实际提交的是同一份成品：都算本任务成品，且没有把别的图算进去。
            self.assertEqual(task.runtime["image_delivery_dispatched_images"], 2)


class StrictSubsetTests(unittest.IsolatedAsyncioTestCase):
    """② 严格纯图只取钩子保留的成品子集：不补全、不整单恢复。"""

    async def test_strict_mode_keeps_only_the_surviving_product(self):
        """两张成品删掉一张：线上只发剩下的那张，不得回灌被删的那张。"""

        first = wire.png_bytes(911)
        second = wire.png_bytes(912)

        async def response(conf, request):
            return [
                wire._mods()["ImageResult"](data=first),
                wire._mods()["ImageResult"](data=second),
            ]

        async def decorating(event):
            mods = wire._mods()
            chain = list(event.get_result().chain or [])
            kept = []
            removed = False
            for item in chain:
                if isinstance(item, mods["components"].Image) and not removed:
                    removed = True
                    continue
                kept.append(item)
            self.assertTrue(removed)
            event.get_result().chain = kept

        with TemporaryDirectory() as directory, wire._Rig(Path(directory), response) as rig:
            rig.hooks.before.append(_handler(decorating))
            task = await rig.done(
                await _draw_tool(rig, rig.event(wire.USER_A + " "), "A 画两只猫", count=2)
            )

            self.assertEqual(len(rig.bot.out), 1)
            frame = rig.bot.out[0]
            self.assertEqual(frame.pictures, (wire.digest(second),))
            self.assertNotIn(wire.digest(first), frame.pictures)
            self.assertEqual(_without_spacers(frame.sequence), ("image",))
            self.assertEqual(task.runtime["image_delivery_dispatched_images"], 1)

    async def test_strict_mode_does_not_restore_product_set_for_foreign_image(self):
        """只剩外来图：不得按整单恢复成品，也不得把外来图算成本任务成品。"""

        product = wire.png_bytes(913)
        foreign = wire.png_bytes(914)

        async def response(conf, request):
            return [wire._mods()["ImageResult"](data=product)]

        async def decorating(event):
            mods = wire._mods()
            event.get_result().chain = [mods["components"].Image.fromBytes(foreign)]

        with TemporaryDirectory() as directory, wire._Rig(Path(directory), response) as rig:
            rig.hooks.before.append(_handler(decorating))
            task = await rig.done(await rig.draw(rig.event(wire.USER_A + " "), "A 画一只猫"))

            self.assertEqual(rig.bot.out, [])
            self.assertEqual(task.runtime["image_delivery_dispatched_images"], 0)
            self.assertEqual(task.runtime["image_delivery_success"], False)
            self.assertEqual(task.state.value, "delivery_failed")


class NoticeMentionTests(unittest.IsolatedAsyncioTestCase):
    """③ 无图终态通知的 @ 豁免不得外溢到 after 钩子的旁发。"""

    async def test_after_hook_text_side_send_does_not_inherit_notice_exemption(self):
        """主通知带 @ 是既有语义；旁发的纯文本不得因此独发一条 @。"""

        async def response(conf, request):
            return []

        async def after(event):
            mods = wire._mods()
            await event.send(
                mods["MessageChain"](
                    [mods["components"].At(qq=wire.USER_C), mods["components"].Plain("side text")]
                )
            )

        with TemporaryDirectory() as directory, wire._Rig(Path(directory), response) as rig:
            rig.hooks.after.append(_handler(after))
            task = await rig.done(await rig.draw(rig.event(wire.USER_A), "A 画一只猫"))

            self.assertEqual(len(rig.bot.out), 2)
            main, side = rig.bot.out
            # 既有语义：无图终态主通知仍然 @ 发起人（不变）。
            self.assertEqual(main.at, (wire.USER_A,))
            self.assertTrue(any("绘制失败" in text for text in main.text))
            # 旁发是纯文本：不能继承主通知的豁免凭空补一个只含提醒的 @。
            self.assertEqual(side.at, ())
            self.assertEqual(side.text, ("side text",))
            # 无图任务走通知分支：既没有图片投递，也不该留下"图片已投递"的假记录。
            self.assertEqual(
                int(task.runtime.get("notification_delivery_dispatched_images", 0) or 0), 0
            )
            self.assertNotIn("image_delivery_dispatched_images", task.runtime)

    async def test_after_hook_at_only_side_send_creates_no_frame(self):
        """旁发只剩一个 At：不补任何消息，也不留下独立提醒。"""

        async def response(conf, request):
            return []

        async def after(event):
            mods = wire._mods()
            await event.send(mods["MessageChain"]([mods["components"].At(qq=wire.USER_C)]))

        with TemporaryDirectory() as directory, wire._Rig(Path(directory), response) as rig:
            rig.hooks.after.append(_handler(after))
            await rig.done(await rig.draw(rig.event(wire.USER_A), "A 画一只猫"))

            self.assertEqual(len(rig.bot.out), 1)
            self.assertEqual(rig.bot.out[0].at, (wire.USER_A,))


def _without_spacers(sequence: tuple) -> tuple:
    return wire._without_spacers(sequence)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
