from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger as astrbot_logger
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult, ResultContentType
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from ..core.route_identity import count_images, freeze_route_identity, sanitize_chain


@dataclass(frozen=True)
class SendOutcome:
    success: bool
    error: str = ""
    side_effects_started: bool = False
    side_send_started: bool = False
    side_send_error: str = ""
    generation_success: bool | None = None
    usable_output_count: int | None = None
    delivery_kind: str = ""
    # 守门过滤后**曾尝试**交给适配器的图片数（主发 + 旁发，含随后被适配器
    # 拒绝的那部分）。它与下面的「已提交」分开，便于排障时区分"尝试过"与
    # "真的交出去了"（t13 R2-03）。
    attempted_image_count: int = 0
    # 适配器 `send_message` 返回 True **之后**才累加的图片数（主发 + 成功旁发）。
    # 它是「已提交给平台适配器」的事实，不是成员实际收件；图片任务若为 0 不得
    # 记送图成功。
    dispatched_image_count: int = 0
    # 主链被装饰链清空/过滤为空（但没有旁发成功）：作为独立观察保留，不用它
    # 冒充"零投递"，也不据此误重发。
    main_suppressed: bool = False
    # 为让「发起人的唯一 At 与成品落在同一条群消息」而局部降级的"分帧"装饰数
    # （OneBot 遇 Node/Nodes/File 会逐组件派发，t21）。有界计数，不含内容。
    dropped_split_components: int = 0


class _ProactiveMessageEvent(AstrMessageEvent):
    """让装饰器内的 ``event.send()`` 真正委托主动发送接口。"""

    def __init__(
        self,
        *,
        context: Any,
        outbound_session: MessageSession,
        message_obj: AstrBotMessage,
        platform_meta: Any,
        session_id: str,
        before_send=None,
        mention_target: str = "",
        image_only: bool = False,
        image_allowlist=None,
    ) -> None:
        self._context = context
        self._outbound_session = outbound_session
        self._before_send = before_send
        # 冻结后的唯一合法单人 @ 目标（空 = 本次不允许任何 At 出站）。
        self._mention_target = mention_target
        # 群聊开 @ 但无可信目标、且已有可用成品图：主发与所有旁发只准出本任务成品图。
        self._image_only = bool(image_only)
        self._image_allowlist = image_allowlist
        self._send_stage_started = False
        self._side_send_started = False
        self._side_send_error = ""
        self._side_suppressed = False
        self._side_image_attempted = 0
        self._side_image_count = 0
        # 旁发上下文：只有带图片的旁发才允许归一化提及（纯文本 hook 不得被补成
        # 一条独立的 @ 提醒，t13 R2-02）。
        self._mention_needs_image = True
        # 过滤期观察（有界计数，不含内容）：本次为保持"提及与成品同帧"而局部
        # 降级掉的 Node/Nodes/File 装饰数（t21）。
        self._filter_observations: dict = {}
        super().__init__(
            message_str="",
            message_obj=message_obj,
            platform_meta=platform_meta,
            session_id=session_id,
        )

    def begin_send_stage(self) -> None:
        if self._send_stage_started:
            return
        self._send_stage_started = True
        if self._before_send:
            self._before_send()

    async def send(self, message: MessageChain) -> None:
        # 装饰器/hook 的 ``event.send()`` 旁发与主发共用同一套提及策略：
        # 只允许冻结触发者本人、最多一个；注入的人、广播与重复提及都在这里拦掉。
        # 严格纯图分支还额外只允许本任务成品图（文本/表情/别人的图会被过滤）。
        self.begin_send_stage()
        sanitized = sanitize_chain(
            list(getattr(message, "chain", None) or []),
            target=self._mention_target,
            image_only=self._image_only,
            image_allowlist=self._image_allowlist,
            mention_without_image=not self._mention_needs_image,
            observations=self._filter_observations,
        )
        if not sanitized:
            if self._image_only:
                # 严格纯图：过滤后没有任何可安全投递的内容 ⇒ 不发，也不算一次
                # 投递（不是错误：本任务主图仍会正常送出）。
                self._side_suppressed = True
                return
            # 整条旁发只剩非法提及：没有任何可安全投递的内容，不得静默算成功。
            self._side_send_error = "NoSendableComponents"
            raise RuntimeError(self._side_send_error)
        # 先记"尝试"，等适配器真的接受了整条链，再把它计入"已提交"（R2-03）：
        # `send_message` 返回 False 或抛异常时 `_side_image_count` 不得虚增。
        attempted = count_images(sanitized)
        self._side_image_attempted += attempted
        outbound = MessageChain(chain=sanitized)
        try:
            sent = await self._context.send_message(self._outbound_session, outbound)
        except Exception as exc:
            self._side_send_error = type(exc).__name__
            raise
        if not sent:
            self._side_send_error = "PlatformNotFound"
            raise RuntimeError(self._side_send_error)
        self._side_send_started = True
        self._side_image_count += attempted
        await super().send(outbound)

    @property
    def side_send_started(self) -> bool:
        return self._side_send_started

    @property
    def side_send_error(self) -> str:
        return self._side_send_error

    @property
    def mention_target(self) -> str:
        return self._mention_target

    @property
    def image_only(self) -> bool:
        return self._image_only

    @property
    def image_allowlist(self):
        return self._image_allowlist

    @property
    def side_suppressed(self) -> bool:
        return self._side_suppressed

    @property
    def side_image_count(self) -> int:
        return self._side_image_count

    @property
    def dropped_split_components(self) -> int:
        return int(self._filter_observations.get("dropped_split_components", 0) or 0)

    @property
    def side_image_attempted(self) -> int:
        return self._side_image_attempted

    @property
    def mention_needs_image(self) -> bool:
        return self._mention_needs_image

    @mention_needs_image.setter
    def mention_needs_image(self, value: bool) -> None:
        self._mention_needs_image = bool(value)


class ProactiveSender:
    """为主动消息补跑装饰/发送后 Hook，并使用 Context 完成实际发送。"""

    def __init__(self, context, logger, debug=None):
        self.context = context
        self.logger = logger
        self.debug = debug or (lambda *args, **kwargs: None)

    def _platform(self, session: MessageSession):
        getter = getattr(self.context, "get_platform_inst", None)
        if callable(getter):
            platform = getter(session.platform_id)
            if platform is not None:
                return platform
        manager = getattr(self.context, "platform_manager", None)
        return next(
            (
                item
                for item in getattr(manager, "platform_insts", [])
                if item.meta().id == session.platform_id
            ),
            None,
        )

    def build_event(
        self,
        umo: str,
        source,
        chain: list,
        before_send=None,
        identity=None,
        mention_target: str = "",
        image_only: bool = False,
        image_allowlist=None,
    ) -> _ProactiveMessageEvent:
        session = MessageSession.from_str(umo)
        platform = self._platform(session)
        if platform is None:
            raise RuntimeError("找不到会话对应的平台实例")
        # 组装主动事件时**不再**读活事件的 sender：任务创建时冻结的路由身份才是
        # 唯一来源（钩子/后续事件改写 source.message_obj.sender 不会串人）。
        if identity is None:
            identity = freeze_route_identity(source)
        message_type = session.message_type
        if identity.message_type:
            try:
                message_type = MessageType(identity.message_type)
            except ValueError:
                message_type = session.message_type
        message = AstrBotMessage()
        message.type = message_type
        message.session_id = session.session_id
        message.message_id = ""
        message.self_id = identity.self_id or "bot"
        message.sender = MessageMember(
            user_id=identity.sender_user_id,
            nickname=identity.sender_nickname or None,
        )
        message.group = (
            Group(group_id=identity.group_id or session.session_id)
            if message_type == MessageType.GROUP_MESSAGE
            else None
        )
        message.message = []
        message.message_str = ""
        message.raw_message = None

        event = _ProactiveMessageEvent(
            context=self.context,
            outbound_session=session,
            message_obj=message,
            platform_meta=platform.meta(),
            session_id=session.session_id,
            before_send=before_send,
            mention_target=mention_target,
            image_only=image_only,
            image_allowlist=image_allowlist,
        )
        event.plugins_name = getattr(source, "plugins_name", None)
        result = MessageEventResult(chain=list(chain))
        result.set_result_content_type(ResultContentType.LLM_RESULT)
        event.set_result(result)
        return event

    async def _run_hooks(self, event, event_type) -> None:
        handlers = star_handlers_registry.get_handlers_by_event_type(
            event_type,
            plugins_name=getattr(event, "plugins_name", None),
        )
        self.debug("[ImagoFusion] 主动发送 Hook event=%s handlers=%d", event_type.name, len(handlers))
        if not handlers and event_type == EventType.OnDecoratingResultEvent:
            # 一个装饰器都没命中：插件白名单（event.plugins_name）或激活状态把
            # 依赖装饰链捕获的插件（如 ChatMemory）过滤掉了，主动发送将绕过它们。
            astrbot_logger.warning(
                "[ImagoFusion] 主动发送未命中任何 on_decorating_result 处理器 "
                f"(plugins_name={event.plugins_name})，依赖装饰链的插件将无法捕获本次发送"
            )
        for handler in handlers:
            if event.is_stopped():
                break
            callback = getattr(handler, "handler", handler)
            try:
                value = callback(event)
                if inspect.isawaitable(value):
                    await value
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self.logger(f"Hook 执行异常 event={event_type.name}", exc)

    async def send(
        self,
        umo: str,
        source,
        chain: list,
        before_send=None,
        identity=None,
        mention_target: str = "",
        image_only: bool = False,
        image_allowlist=None,
        mention_needs_image: bool = False,
    ) -> SendOutcome:
        """主动发送适配：装饰链 → 提及守门 → 发送 → after 钩子。

        ``mention_needs_image`` 只作用于**主发**：任务本身出了成品图时置真——此时
        主链若被钩子改成纯文本，不得凭空补一条 @ 提醒；任务本身没有图（失败/超时/
        回吐等既有终态通知）时置假，沿用原有的"通知带 @ 触发者"语义。
        旁发（``event.send``）恒为"必须带图"，见 ``_ProactiveMessageEvent``。
        """
        try:
            event = self.build_event(
                umo,
                source,
                chain,
                before_send=before_send,
                identity=identity,
                mention_target=mention_target,
                image_only=image_only,
                image_allowlist=image_allowlist,
            )
        except Exception as exc:
            return SendOutcome(False, type(exc).__name__, False)

        await self._run_hooks(event, EventType.OnDecoratingResultEvent)
        if event.is_stopped():
            # 钩子显式 stop：只有真的发生过旁发（且旁发成功）才算送达。没有任何
            # 实际派发时不得记成功——调度器会据此判 DELIVERY_FAILED，而不是伪造
            # SUCCEEDED 或据此误重发。
            # 「已提交」只看适配器是否真的接受过一次旁发：之后因为平台实例
            # 消失等原因被拒，不能把先前已经发出去的成品图改判成零投递（t21）。
            side_dispatched = bool(event.side_send_started)
            return SendOutcome(
                side_dispatched,
                event.side_send_error or ("" if side_dispatched else "StoppedWithoutSend"),
                side_effects_started=True,
                side_send_started=event.side_send_started,
                side_send_error=event.side_send_error,
                attempted_image_count=event.side_image_attempted,
                dispatched_image_count=event.side_image_count,
                dropped_split_components=event.dropped_split_components,
            )
        result = event.get_result()
        # 装饰链与主发共用同一套提及策略：只保留冻结触发者的一个 At，别的 At
        # （注入/篡改/广播/重复）在这里统一拦掉；钩子删掉的提及不会被补回。
        event.mention_needs_image = bool(mention_needs_image)
        main_observations: dict = {}
        processed = sanitize_chain(
            list(getattr(result, "chain", None) or []),
            target=event.mention_target,
            image_only=event.image_only,
            image_allowlist=event.image_allowlist,
            mention_without_image=not event.mention_needs_image,
            observations=main_observations,
        )
        self.debug(
            "[ImagoFusion] 主动发送装饰完成 components=%d stopped=%s",
            len(processed),
            event.is_stopped(),
        )
        main_attempted = count_images(processed)
        main_sent = False
        main_suppressed = False
        error = ""
        if processed:
            event.begin_send_stage()
            try:
                main_sent = bool(
                    await self.context.send_message(umo, MessageChain(chain=processed))
                )
                if not main_sent:
                    error = "PlatformNotFound"
            except Exception as exc:
                main_sent = False
                error = type(exc).__name__
                self.logger("主动消息发送异常", exc)
        else:
            # 装饰链把结果清空但未 stop_event：主链没有任何内容可发。这是**主链
            # 抑制**，作为独立观察保留；是否算投递失败要看旁发有没有真的送出。
            main_suppressed = True
            error = "EmptyAfterFilter" if event.image_only else "DecoratorSuppressed"
        await self._run_hooks(event, EventType.OnAfterMessageSentEvent)
        side_committed = bool(event.side_send_started)
        if event.side_send_error:
            error = f"{error};SideSend:{event.side_send_error}" if error else f"SideSend:{event.side_send_error}"
        # 已提交 = 主链适配器接受，或旁发真的发出去过。真正"全路无发"才算失败：
        # 旁发已送出成品图时，主链被钩子清空不得冒充零投递（t13 R2-03）。
        success = bool(main_sent or side_committed)
        dispatched = (main_attempted if main_sent else 0) + event.side_image_count
        self.debug("[ImagoFusion] 主动发送结束 success=%s error=%s", success, error or "none")
        return SendOutcome(
            success,
            error,
            side_effects_started=True,
            side_send_started=event.side_send_started,
            side_send_error=event.side_send_error,
            attempted_image_count=main_attempted + event.side_image_attempted,
            dispatched_image_count=dispatched,
            main_suppressed=main_suppressed,
            dropped_split_components=(
                int(main_observations.get("dropped_split_components", 0) or 0)
                + event.dropped_split_components
            ),
        )
