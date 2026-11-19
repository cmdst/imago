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
    # 经过守门过滤、**真正交给适配器序列化**的图片数（主发 + 成功旁发）。
    # 它是「派发」事实，不是成员实际收件；图片任务若为 0 不得记送图成功。
    dispatched_image_count: int = 0


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
        self._side_image_count = 0
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
        self._side_send_started = True
        self._side_image_count += count_images(sanitized)
        outbound = MessageChain(chain=sanitized)
        try:
            sent = await self._context.send_message(self._outbound_session, outbound)
        except Exception as exc:
            self._side_send_error = type(exc).__name__
            raise
        if not sent:
            self._side_send_error = "PlatformNotFound"
            raise RuntimeError(self._side_send_error)
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
    ) -> SendOutcome:
        """主动发送适配：装饰链 → 提及守门 → 发送 → after 钩子。"""
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
            side_dispatched = bool(event.side_send_started and not event.side_send_error)
            return SendOutcome(
                side_dispatched,
                event.side_send_error or ("" if side_dispatched else "StoppedWithoutSend"),
                side_effects_started=True,
                side_send_started=event.side_send_started,
                side_send_error=event.side_send_error,
                dispatched_image_count=event.side_image_count,
            )
        result = event.get_result()
        # 装饰链与主发共用同一套提及策略：只保留冻结触发者的一个 At，别的 At
        # （注入/篡改/广播/重复）在这里统一拦掉；钩子删掉的提及不会被补回。
        processed = sanitize_chain(
            list(getattr(result, "chain", None) or []),
            target=event.mention_target,
            image_only=event.image_only,
            image_allowlist=event.image_allowlist,
        )
        self.debug(
            "[ImagoFusion] 主动发送装饰完成 components=%d stopped=%s",
            len(processed),
            event.is_stopped(),
        )
        error = ""
        success = True
        main_image_count = count_images(processed)
        if processed:
            event.begin_send_stage()
            try:
                success = bool(
                    await self.context.send_message(umo, MessageChain(chain=processed))
                )
                if not success:
                    error = "PlatformNotFound"
            except Exception as exc:
                success = False
                error = type(exc).__name__
                self.logger("主动消息发送异常", exc)
        else:
            # 装饰链把结果清空但未 stop_event：没有任何内容可发，不得记成功。
            success = False
            error = "EmptyAfterFilter" if event.image_only else "DecoratorSuppressed"
        await self._run_hooks(event, EventType.OnAfterMessageSentEvent)
        if event.side_send_error:
            success = False
            error = error or f"SideSend:{event.side_send_error}"
        self.debug("[ImagoFusion] 主动发送结束 success=%s error=%s", success, error or "none")
        return SendOutcome(
            success,
            error,
            side_effects_started=True,
            side_send_started=event.side_send_started,
            side_send_error=event.side_send_error,
            dispatched_image_count=main_image_count + event.side_image_count,
        )
