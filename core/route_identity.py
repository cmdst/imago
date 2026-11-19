"""路由身份冻结与群聊提及守门（v1.4.2 P0）。

AstrBot 把同一个 ``AstrMessageEvent`` 交给同一轮里的所有处理器与钩子：
``on_decorating_result`` / ``on_after_message_sent`` 钩子、后续事件与平台重入都
可以改写 ``message_obj.sender``、``sender.user_id``、``unified_msg_origin``。
因此任务归属、群聊判定、出站 @ 目标与合成主动事件必须取自**入口首次 await
之前**拍下的不可变快照，而不是发送时刻的活对象；出站链再按同一份快照做一次
提及守门（只允许冻结触发者本人，且最多一个）。

本模块不读写任何配置、不发消息、不查群成员，纯值对象 + 纯函数。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from astrbot.api.message_components import At

GROUP_MESSAGE = "GroupMessage"
FRIEND_MESSAGE = "FriendMessage"

# 已知的 OneBot 适配器平台名（AstrBot 官方适配器 = ``aiocqhttp``，其余是常见
# 别名/分支）。**只有**这些平台按 OneBot 规则校验 @ 目标（正 ASCII 十进制）；
# 其它平台的 user id 可能是不透明字符串（telegram/satori 的句柄、discord 的
# snowflake 等），不能套 QQ 的纯数字限制。
ONEBOT_PLATFORM_NAMES = frozenset(
    {
        "aiocqhttp",
        "onebot",
        "onebot11",
        "onebot_v11",
        "onebot-v11",
        "napcat",
        "llonebot",
    }
)

# 广播/全体提及：任何适配器都不得把它当作单人 @ 目标出站。
BROADCAST_TOKENS = frozenset(
    {"all", "atall", "@all", "everyone", "@everyone", "全体成员", "全体"}
)

MAX_ID_LENGTH = 128

# 提及判定原因（只用常量，不含 ID）。
MENTION_OK = "ok"
MENTION_DISABLED = "disabled"
MENTION_NOT_GROUP = "not_group"
MENTION_MISSING_SENDER = "missing_sender"
MENTION_BROADCAST = "broadcast"
MENTION_INVALID_FORMAT = "invalid_format"
MENTION_INVALID_ONEBOT_ID = "invalid_onebot_id"


def _message_type_name(value) -> str:
    """把 ``MessageType`` 成员/字符串归一成 ``"GroupMessage"`` 这样的字面值。"""
    raw = getattr(value, "value", value)
    return raw.strip() if isinstance(raw, str) else ""


def _safe_str(value) -> str:
    return str(value or "")


@dataclass(frozen=True, slots=True)
class RouteIdentity:
    """一次用户消息的不可变路由身份快照。

    ``message_type`` 取自 ``message_obj.type`` / ``session.message_type`` 的结构化
    值——**不从 ``umo`` 子串猜群**；两者都拿不到时留空，按「非群聊」保守处理。
    """

    umo: str = ""
    platform_name: str = ""
    platform_id: str = ""
    message_type: str = ""
    sender_user_id: str = ""
    sender_nickname: str = ""
    group_id: str = ""
    self_id: str = ""
    message_id: str = ""

    @property
    def is_group(self) -> bool:
        return self.message_type == GROUP_MESSAGE

    @property
    def adapter(self) -> str:
        name = self.platform_name.strip().lower()
        return "onebot" if name in ONEBOT_PLATFORM_NAMES else "opaque"

    def mention_decision(self, *, enabled: bool) -> tuple[str, str]:
        """返回 ``(出站 @ 目标, 原因)``；目标为空表示这次不加任何提及。"""
        if not enabled:
            return "", MENTION_DISABLED
        if not self.is_group:
            return "", MENTION_NOT_GROUP
        raw = self.sender_user_id
        target = raw.strip()
        if not target:
            return "", MENTION_MISSING_SENDER
        if raw != target:
            # 前后带空白：不做「悄悄 strip 后照发」——按不可信 ID 拒绝（t3 要求）。
            return "", MENTION_INVALID_FORMAT
        folded = unicodedata.normalize("NFKC", target).strip().lower()
        if folded in BROADCAST_TOKENS:
            return "", MENTION_BROADCAST
        if (
            len(target) > MAX_ID_LENGTH
            or target.startswith("@")
            or any(character.isspace() or not character.isprintable() for character in target)
        ):
            return "", MENTION_INVALID_FORMAT
        if target.isascii() and target.isdigit() and int(target) == 0:
            # 「0」不是可用的发送者 ID：宁可静默纯图，也不要发一个无效 @。
            return "", MENTION_INVALID_FORMAT
        if self.adapter == "onebot" and not (target.isascii() and target.isdigit()):
            # OneBot：只认正 ASCII 十进制 QQ 号（带符号/空白/全角/字母一律拒绝）。
            return "", MENTION_INVALID_ONEBOT_ID
        return target, MENTION_OK

    def mention_target(self, *, enabled: bool) -> str:
        return self.mention_decision(enabled=enabled)[0]


def freeze_route_identity(event) -> RouteIdentity:
    """读取一次事件身份并返回不可变快照。

    必须在各入口**首次 await 之前**调用；之后事件对象被任何钩子/后续事件改写，
    都不会再影响本次任务的归属、群聊判定、@ 目标与主动发送事件。
    """
    message_obj = getattr(event, "message_obj", None)
    session = getattr(event, "session", None)
    platform_meta = getattr(event, "platform_meta", None)

    # 平台元数据可能来自 astrbot PlatformMetadata，也可能来自测试替身。
    platform_name = _safe_str(getattr(platform_meta, "name", ""))
    platform_id = _safe_str(getattr(platform_meta, "id", ""))
    getter = getattr(event, "get_platform_name", None)
    if callable(getter):
        try:
            platform_name = platform_name or _safe_str(getter())
        except Exception:
            pass
    getter = getattr(event, "get_platform_id", None)
    if callable(getter):
        try:
            platform_id = platform_id or _safe_str(getter())
        except Exception:
            pass

    message_type = _message_type_name(getattr(message_obj, "type", None)) or _message_type_name(
        getattr(session, "message_type", None)
    )

    sender = getattr(message_obj, "sender", None)
    sender_user_id = ""
    getter = getattr(event, "get_sender_id", None)
    if callable(getter):
        try:
            sender_user_id = _safe_str(getter())
        except Exception:
            sender_user_id = ""
    if not sender_user_id:
        sender_user_id = _safe_str(getattr(sender, "user_id", ""))

    group = getattr(message_obj, "group", None)
    group_id = _safe_str(getattr(message_obj, "group_id", "")) or _safe_str(
        getattr(group, "group_id", "")
    )
    session_id = _safe_str(getattr(session, "session_id", "")) or _safe_str(
        getattr(event, "session_id", "")
    )

    umo = ""
    try:
        umo = _safe_str(getattr(event, "unified_msg_origin", ""))
    except Exception:
        umo = ""
    if not umo and platform_id and message_type and session_id:
        # 测试替身可能没有 unified_msg_origin 属性；用已冻结的三段拼出同一形状。
        umo = f"{platform_id}:{message_type}:{session_id}"

    return RouteIdentity(
        umo=umo.strip(),
        platform_name=platform_name.strip(),
        platform_id=platform_id.strip(),
        message_type=message_type,
        sender_user_id=sender_user_id.strip(),
        sender_nickname=_safe_str(getattr(sender, "nickname", "")).strip(),
        group_id=group_id.strip(),
        self_id=_safe_str(getattr(message_obj, "self_id", "")).strip(),
        message_id=_safe_str(getattr(message_obj, "message_id", "")).strip(),
    )


def sanitize_chain(chain: Iterable | None, *, target: str) -> list:
    """按冻结身份过滤出站组件：最多保留一个冻结合法触发者的 @。

    - ``target`` 为空（私聊/关 @/身份无效/无冻结身份）⇒ 移除链上所有 ``At``；
    - 非目标 ``At``（钩子注入的别人、广播 ``AtAll``、改写过的 ID）⇒ 移除；
    - 目标 ``At`` 重复出现 ⇒ 只保留第一个；
    - 钩子显式删除了目标 ``At`` ⇒ 尊重删除，不再补回。
    """
    result: list = []
    kept_target = False
    for component in chain or ():
        if isinstance(component, At):
            value = _safe_str(getattr(component, "qq", "")).strip()
            if not target or kept_target or value != target:
                continue
            kept_target = True
        result.append(component)
    return result
