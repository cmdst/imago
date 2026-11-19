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

from astrbot.api.message_components import At, Image, Plain

try:  # 合并转发组件在部分宿主/测试替身里可能不存在：缺失时按「未知容器」处理。
    from astrbot.api.message_components import Node, Nodes
except ImportError:  # pragma: no cover - 仅在精简替身中出现
    Node = None  # type: ignore[assignment]
    Nodes = None  # type: ignore[assignment]

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
    # ``sender_user_id`` 是**归一化**后的键（strip），用于查重/额度台账；
    # ``sender_user_id_raw`` 保留入口原值，只服务出站 @ 的可信性判定。二者分开，
    # 才不会让「带空白的 ID」借 strip 变成合法提及，也不会为了修 @ 放松额度。
    sender_user_id: str = ""
    sender_user_id_raw: str = ""
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
        raw = self.sender_user_id_raw if self.sender_user_id_raw != "" else self.sender_user_id
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

    raw_sender = sender_user_id
    return RouteIdentity(
        umo=umo.strip(),
        platform_name=platform_name.strip(),
        platform_id=platform_id.strip(),
        message_type=message_type,
        sender_user_id=raw_sender.strip(),
        sender_user_id_raw=raw_sender,
        sender_nickname=_safe_str(getattr(sender, "nickname", "")).strip(),
        group_id=group_id.strip(),
        self_id=_safe_str(getattr(message_obj, "self_id", "")).strip(),
        message_id=_safe_str(getattr(message_obj, "message_id", "")).strip(),
    )


_NODE_CLASSES = tuple(item for item in (Node, Nodes) if item is not None)

def image_only_reply(
    *, identity, at_trigger_user: bool, generation_success: bool
) -> bool:
    """本次回包是否必须「严格纯图」（P0 与 persona 共用的单一判定入口）。

    成立条件：本次真的有成品图、群聊、配置开启 @、但冻结身份里没有可信单人
    目标。此时主发与所有旁发都只能出本任务成品图，任何非图组件（配文、降级
    告知、人格限额提示、钩子文本）都不许出站；私聊/关 @ 或目标合法时返回
    ``False``，既有文字语义不变。
    """
    if not generation_success or not at_trigger_user:
        return False
    if not isinstance(identity, RouteIdentity) or not identity.is_group:
        return False
    return identity.mention_decision(enabled=True)[0] == ""




def _is_node(component) -> bool:
    return bool(_NODE_CLASSES) and isinstance(component, _NODE_CLASSES)


def _image_allowed(component, allowlist) -> bool:
    if allowlist is None:
        return True
    candidates = {
        _safe_str(getattr(component, "path", "")),
        _safe_str(getattr(component, "file", "")),
        _safe_str(getattr(component, "url", "")),
    }
    return bool(candidates & allowlist)


def _rebuild_node(node, *, target: str, image_only: bool, allowlist, state: dict):
    """安全复制一个 ``Node`` 并递归清理其内容；无法安全重建时返回 ``None``。"""
    content = getattr(node, "content", None) or []
    if not isinstance(content, list):
        content = [content]
    cleaned = []
    for child in content:
        value = _scrub(child, target=target, image_only=image_only, allowlist=allowlist, state=state)
        if value is not None:
            cleaned.append(value)
    if not cleaned or Node is None:
        return None
    try:
        rebuilt = Node(cleaned)
        for attribute in ("id", "name", "uin", "seq", "time"):
            if hasattr(node, attribute):
                setattr(rebuilt, attribute, getattr(node, attribute))
    except Exception:
        # 不能安全生成的容器一律拦下：宁可少发，也不把未清理的转发载荷送出去。
        return None
    return rebuilt


def _rebuild_nodes(component, *, target: str, image_only: bool, allowlist, state: dict):
    rebuilt_nodes = []
    for node in getattr(component, "nodes", None) or []:
        value = _rebuild_node(
            node, target=target, image_only=image_only, allowlist=allowlist, state=state
        )
        if value is not None:
            rebuilt_nodes.append(value)
    if not rebuilt_nodes or Nodes is None:
        return None
    try:
        return Nodes(rebuilt_nodes)
    except Exception:
        return None


def _scrub(component, *, target: str, image_only: bool, allowlist, state: dict):
    """返回允许出站的组件；``None`` 表示丢弃。"""
    if isinstance(component, At):
        state["had_mention"] = True
        if (
            target
            and not state["kept"]
            and _safe_str(getattr(component, "qq", "")).strip() == target
        ):
            state["kept"] = True
            return component
        # 非目标 / 广播 / 重复 / 冻结身份不可信：一律不进载荷。
        return None
    if image_only:
        # 严格纯图：只允许本任务已落盘的成品图；文本、表情、别人的图、
        # 以及转发容器里的非图片内容全部过滤（过滤后为空就不发，见调用方）。
        if isinstance(component, Image):
            return component if _image_allowed(component, allowlist) else None
        if _is_node(component):
            return _scrub_container(
                component, target=target, image_only=image_only, allowlist=allowlist, state=state
            )
        return None
    if isinstance(component, Plain):
        return component
    if _is_node(component):
        return _scrub_container(
            component, target=target, image_only=image_only, allowlist=allowlist, state=state
        )
    return component


def _scrub_container(component, *, target: str, image_only: bool, allowlist, state: dict):
    if Nodes is not None and isinstance(component, Nodes):
        return _rebuild_nodes(
            component, target=target, image_only=image_only, allowlist=allowlist, state=state
        )
    return _rebuild_node(
        component, target=target, image_only=image_only, allowlist=allowlist, state=state
    )


def count_images(chain: Iterable | None) -> int:
    """数出链上（含转发容器内）会被适配器序列化的图片组件数。"""
    total = 0
    for component in chain or ():
        if isinstance(component, Image):
            total += 1
        elif Nodes is not None and isinstance(component, Nodes):
            for node in getattr(component, "nodes", None) or []:
                total += count_images(getattr(node, "content", None))
        elif Node is not None and isinstance(component, Node):
            total += count_images(getattr(component, "content", None))
    return total


def sanitize_chain(
    chain: Iterable | None,
    *,
    target: str = "",
    image_only: bool = False,
    image_allowlist: Iterable | None = None,
) -> list:
    """按冻结身份过滤出站组件（主发、旁发与 after 钩子共用）。

    - ``target`` 为空（私聊/关 @/身份无效/无冻结身份）⇒ 移除链上所有 ``At``；
    - 非目标 ``At``（钩子注入的别人、广播 ``AtAll``、改写过的 ID）⇒ 移除；
    - 目标 ``At`` 重复出现 ⇒ 只保留第一个；
    - 钩子把唯一提及**改成别人/广播**（链上仍带提及）⇒ 重建一个规范的冻结
      ``At`` 并置前；钩子把提及**显式删光**（链上再无 ``At``）⇒ 尊重删除，不补；
    - ``Node``/``Nodes`` 合并转发容器递归处理：安全复制后清理嵌套提及，无法
      安全重建的容器直接拦下，不涉提及的正常内容保持原样；
    - ``image_only``（群聊开 @ 但没有可信目标、且有可用成品图）⇒ 只保留
      ``image_allowlist`` 里本任务的成品图，文本/表情/他人图片全部过滤。
    """
    allowlist = None if not image_allowlist else {_safe_str(item) for item in image_allowlist}
    state = {"had_mention": False, "kept": False}
    result: list = []
    for component in chain or ():
        value = _scrub(
            component, target=target, image_only=image_only, allowlist=allowlist, state=state
        )
        if value is not None:
            result.append(value)
    if state["had_mention"] and not state["kept"] and target and result:
        # 只有「链上本来有提及、但没有任何合法目标提及存活」时才归一化重建；
        # 纯文本/显式删光的链不会走到这里。
        result = [At(qq=target), *result]
        state["kept"] = True
    return result
