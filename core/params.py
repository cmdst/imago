"""节点参数目录（``custom_params``）与质量/思考控制的唯一事实源。

本模块只做**纯逻辑**：不读配置、不发请求、不 import ``providers``。两套管理界面
（AstrBot schema 面板与插件 WebUI）都从这里取字段表，避免前端硬编码。

三类参数严格分离（v1.3.0 契约 F3/F4/F5、字典 §3.4）：

- ``quality`` / ``image_size`` / ``resolution`` 属于**出图质量或分辨率档**，存放在
  节点的 ``custom_params`` 文本里，按协议落到各自请求容器；
- ``thinking`` 是**节点一级键**，独立通道，永不与 ``custom_params`` 同名键合并
  （写了 ``reasoning_effort`` 时以 ``thinking`` 为准）；
- 未知键在 ``typed``/``free`` 协议下原样透传，在 ``ignored`` 下不发送。

``API_TYPE_ORDER`` 是下拉框与 ``build_catalog()`` 的唯一顺序来源；``core.config``
的 ``API_TYPES`` 是它的集合形式，``providers/__init__.py`` 的 ``ADAPTERS`` 必须与
它逐项相等（门禁守卫）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ``custom_params`` 的键名规则与保留键（字典 §2）。保留键写进来会被跳过并记
# ``reserved_key``；``size`` **不在**保留表里，因为它是合法参数字段。
CUSTOM_KEY = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")
RESERVED_PARAM_KEYS = frozenset({
    "url", "base_url", "authorization", "api_key", "apikey", "token", "secret",
    "password", "timeout", "file", "path", "n", "model", "prompt", "count",
    "messages", "thinking",
    # v1.3.0 rev3 追加：图片/参考图/蒙版类键由适配器与任务模型管理，节点参数不得覆盖。
    "image", "images", "image_url", "image_urls", "mask", "references",
    "input_image", "input_images",
})

# 七种接口类型：六个旧类型顺序不变，``xai_image`` 追加在末尾（契约 C1）。
API_TYPE_ORDER = (
    "openai_image",
    "openai_chat",
    "gemini_official",
    "dashscope_multimodal",
    "custom_endpoint",
    "astrbot_native",
    "xai_image",
)

THINKING_VALUES = ("", "off", "low", "medium", "high", "max")
THINKING_LABELS = {
    "": "跟随默认（不发送）",
    "off": "关闭",
    "low": "低",
    "medium": "中",
    "high": "高",
    "max": "最高（协议支持时映射到 xhigh）",
}

# 字段 dict 的固定形状：两套界面都按这 9 个键渲染，不随协议变化（契约 §12）。
_FIELD_KEYS = (
    "key", "label", "control", "type", "default", "options", "placeholder", "help", "max_length",
)

_QUALITY_OPTIONS = ["", "auto", "low", "medium", "high", "xhigh", "max"]
_IMAGE_SIZE_OPTIONS = ["", "512", "1K", "2K", "4K"]
_RESOLUTION_OPTIONS = ["", "1k", "1.5k", "2k"]
_EDIT_MODE_OPTIONS = ["json", "multipart"]


def _field(
    key: str,
    label: str,
    control: str,
    value_type: str,
    default: Any = "",
    *,
    options: list[str] | None = None,
    placeholder: str = "",
    help_text: str = "",
    max_length: int | None = None,
) -> dict[str, Any]:
    """构造一个字段描述（固定 9 键，未用项给空值）。"""
    return {
        "key": key,
        "label": label,
        "control": control,
        "type": value_type,
        "default": default,
        "options": list(options or []),
        "placeholder": placeholder,
        "help": help_text,
        "max_length": max_length,
    }


# 协议字段表：``control`` 决定界面控件与值类型转换（text|select|bool3|int|float）。
PARAM_FIELDS: dict[str, list[dict[str, Any]]] = {
    "openai_image": [
        _field(
            "size", "尺寸 / 比例", "text", "str", "",
            placeholder="1024x1024 / 1:1 / auto / 留空",
            help_text="原样透传到 OpenAI images 的 size；留空表示不发送，交给上游默认值。",
        ),
        _field(
            "quality", "生成质量", "select", "str", "",
            options=_QUALITY_OPTIONS,
            help_text="OpenAI images 的 quality；留空不发送。与「思考控制」无关，quality=high 不会改变推理档位。",
        ),
    ],
    "openai_chat": [],
    "gemini_official": [
        _field(
            "size", "尺寸 / 比例", "text", "str", "",
            placeholder="16:9 / 1:1 / 1024x1024 / 留空",
            help_text="像素尺寸按最大公约数折算成比例，写入 generationConfig.imageConfig.aspectRatio；留空不写。",
        ),
        _field(
            "image_size", "输出分辨率", "select", "str", "",
            options=_IMAGE_SIZE_OPTIONS,
            help_text="写入 generationConfig.imageConfig.imageSize（512/1K/2K/4K）。这是分辨率档，不是质量。",
        ),
    ],
    "dashscope_multimodal": [
        _field(
            "size", "尺寸", "text", "str", "",
            placeholder="1024*1024 / 留空",
            help_text="百炼 parameters.size；原样透传，百炼要求用星号（1024*1024）。",
        ),
        _field(
            "prompt_extend", "提示词智能改写", "bool3", "bool", "",
            help_text="百炼 parameters.prompt_extend；留空表示用插件默认（开启）。这是提示词改写，不是思考控制。",
        ),
        _field(
            "watermark", "添加水印", "bool3", "bool", "",
            help_text="百炼 parameters.watermark；留空不发送。",
        ),
        _field(
            "seed", "随机种子", "int", "int", "",
            help_text="百炼 parameters.seed，整数 0-2147483647；越界或非整数会跳过该行并记 param_errors。",
        ),
        _field(
            "negative_prompt", "反向提示词", "text", "str", "",
            placeholder="不希望出现的元素",
            help_text="百炼 parameters.negative_prompt；最长 500 字符。",
            max_length=500,
        ),
    ],
    "custom_endpoint": [],
    "astrbot_native": [],
    "xai_image": [
        _field(
            "size", "尺寸 / 比例", "text", "str", "",
            placeholder="3:4 / 1024x1024 / auto / 留空",
            help_text="比例串原样写入 aspect_ratio；像素尺寸按最大公约数约分；auto 直传；留空不发送。",
        ),
        _field(
            "resolution", "输出分辨率", "select", "str", "",
            options=_RESOLUTION_OPTIONS,
            help_text="写入 xAI 的 resolution（1k/1.5k/2k）。这是分辨率档，不是质量。",
        ),
        _field(
            "edit_mode", "参考图提交方式", "select", "str", "json",
            options=_EDIT_MODE_OPTIONS,
            help_text="有参考图时的提交方式：json=官方 images/edits JSON（单图 image、多图 images）；multipart=兼容只收表单的中转。",
        ),
    ],
}

# 协议元信息：``param_mode`` 决定未知键如何处理；``capabilities`` 给界面显示真实承载名。
# ``quality`` 取值语义（t8 rev3）：``native`` = 协议有一等质量字段；``undocumented`` =
# 协议可能有但官方未文档化（控件不给，用户显式写则透传）；``none`` = 协议无此概念。
# ``thinking`` 用真实承载字段名（complexity / reasoning_effort / thinking_level / none）；
# ``references`` 表示该协议是否接受参考图。
API_TYPE_META: dict[str, dict[str, Any]] = {
    "openai_image": {
        "label": "OpenAI 图像接口",
        "param_mode": "typed",
        "capabilities": {"quality": "native", "thinking": "complexity", "references": True},
    },
    "openai_chat": {
        "label": "OpenAI Chat 接口",
        "param_mode": "typed",
        "capabilities": {"quality": "none", "thinking": "reasoning_effort", "references": True},
    },
    "gemini_official": {
        "label": "Gemini 官方接口",
        "param_mode": "typed",
        "capabilities": {"quality": "undocumented", "thinking": "thinking_level", "references": True},
    },
    "dashscope_multimodal": {
        "label": "百炼多模态",
        "param_mode": "typed",
        "capabilities": {"quality": "none", "thinking": "none", "references": True},
    },
    "custom_endpoint": {
        "label": "自定义 JSON 接口",
        "param_mode": "free",
        "capabilities": {"quality": "none", "thinking": "none", "references": True},
    },
    "astrbot_native": {
        "label": "AstrBot 原生 Provider",
        "param_mode": "ignored",
        "capabilities": {"quality": "none", "thinking": "none", "references": True},
    },
    "xai_image": {
        "label": "xAI 图像接口",
        "param_mode": "typed",
        "capabilities": {"quality": "undocumented", "thinking": "none", "references": True},
    },
}

# Gemini 图像模型的思考能力**显式声明表**（能力未列出的模型一律不发送）。
# 禁止用模型名前缀/正则猜测能力（AC-25b/AC-P10）；新增模型必须显式加行并附联调证据。
GEMINI_THINKING_CAPS: dict[str, dict[str, Any]] = {
    "gemini-3.1-flash-lite-image": {
        "levels": ("MINIMAL", "HIGH"),
        "map": {"low": "MINIMAL", "medium": "HIGH", "high": "HIGH", "max": "HIGH"},
        "disable_allowed": False,
    },
}
GEMINI_THINKING_DEFAULT_DENY = True

_OPENAI_IMAGE_THINKING = {"off": "low", "low": "low", "medium": "medium", "high": "high", "max": "high"}
_OPENAI_CHAT_THINKING = {"off": "none", "low": "low", "medium": "medium", "high": "high", "max": "xhigh"}

# 整数字段的合法区间（越界按非法值跳过该行）。
_INT_RANGES: dict[str, tuple[int, int]] = {"seed": (0, 2_147_483_647)}

_TYPED_API_TYPES = frozenset({
    "openai_image", "openai_chat", "gemini_official", "dashscope_multimodal", "xai_image",
})


@dataclass(frozen=True, slots=True)
class ThinkingDecision:
    """一次思考控制的解析结果。

    Attributes:
        carrier: 承载字段名（``complexity`` / ``reasoning_effort`` /
            ``thinking_level``）；空串表示不发送。
        value: 要发送的值（如 ``high`` / ``MINIMAL`` / ``xhigh``）。
        warnings: 需要写 WARNING 的稳定 code（``gemini_thinking_cannot_disable`` 等）。
        debug: 需要写 DEBUG 的整行文案（已含脱敏后的字段）。
    """

    carrier: str = ""
    value: str = ""
    warnings: tuple[str, ...] = ()
    debug: tuple[str, ...] = ()

    @property
    def applied(self) -> bool:
        """是否有字段要写进请求。"""
        return bool(self.carrier)


def capabilities(api_type: str) -> dict[str, Any]:
    """返回某协议的 ``capabilities``（未知类型给全 none）。"""
    meta = API_TYPE_META.get(api_type)
    if meta is None:
        return {"quality": "none", "thinking": "none", "references": False}
    return dict(meta["capabilities"])


def param_mode(api_type: str) -> str:
    """返回某协议的参数模式（typed/free/ignored；未知按 ignored）。"""
    meta = API_TYPE_META.get(api_type)
    return str(meta["param_mode"]) if meta else "ignored"


def build_catalog() -> dict[str, Any]:
    """构造 WebUI 的字段目录（``version=1``，顺序 = ``API_TYPE_ORDER``）。"""
    api_types = []
    for api_type in API_TYPE_ORDER:
        meta = API_TYPE_META[api_type]
        api_types.append({
            "value": api_type,
            "label": meta["label"],
            "param_mode": meta["param_mode"],
            "fields": [dict(field) for field in PARAM_FIELDS.get(api_type, [])],
            "capabilities": dict(meta["capabilities"]),
        })
    return {"version": 1, "api_types": api_types}


def _separator_index(text: str) -> int:
    """返回首个 ``=`` 或 ``:`` 的位置；都没有返回 -1。"""
    positions = [index for index in (text.find("="), text.find(":")) if index >= 0]
    return min(positions) if positions else -1


def _coerce(field: dict[str, Any], text: str) -> tuple[Any, str]:
    """按字段 ``control`` 转换取值。

    Returns:
        ``(value, reason)``；``reason`` 非空表示该行非法（调用方跳过该行）。
        ``value`` 为 ``None`` 或空串表示"显式空值"，调用方不写入参数表。
    """
    control = str(field.get("control", "text"))
    stripped = text.strip()
    if control == "bool3":
        lowered = stripped.lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True, ""
        if lowered in {"false", "0", "no", "off"}:
            return False, ""
        return None, "invalid_value"
    if control == "int":
        try:
            value = int(stripped)
        except (TypeError, ValueError):
            return None, "invalid_value"
        low, high = _INT_RANGES.get(str(field.get("key")), (None, None))
        if low is not None and high is not None and not (low <= value <= high):
            return None, "invalid_value"
        return value, ""
    if control == "float":
        try:
            return float(stripped), ""
        except (TypeError, ValueError):
            return None, "invalid_value"
    if control == "select":
        if stripped == "":
            return None, ""
        if stripped not in field.get("options", []):
            return None, "invalid_value"
        return stripped, ""
    max_length = field.get("max_length")
    if max_length is not None and len(stripped) > int(max_length):
        return None, "invalid_value"
    return stripped, ""


def _param_error(line: int, key: str, reason: str, message: str) -> dict[str, Any]:
    """构造一条 ``param_errors`` 记录（形状供 WebUI 直接渲染）。"""
    return {"line": line, "key": key, "reason": reason, "message": message}


def parse_custom_params(raw: str, api_type: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """解析 ``custom_params`` 文本。

    规则（字典 §2）：每行 ``key=value`` 或 ``key:value``；空行与 ``#`` 注释忽略；
    键名须匹配 ``^[a-zA-Z][a-zA-Z0-9_-]{0,31}$``；重复键保留首次出现的值；保留键、
    非法键、非法取值**只跳过该行**并记 ``param_errors``，绝不抛异常或让节点失效。

    Args:
        raw: 节点配置里的原始文本。
        api_type: 节点协议；决定字段表与未知键的标注方式。

    Returns:
        ``(params, errors)``：``params`` 已按字段类型转换（bool3→bool、int→int，
        其余 str；未知键原样 str 透传，不丢）；``errors`` 每项含
        ``line/key/reason/message``。
    """
    text = str(raw or "")
    declared = {str(field["key"]): field for field in PARAM_FIELDS.get(api_type, [])}
    mode = param_mode(api_type)
    params: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        separator = _separator_index(stripped)
        if separator < 0:
            errors.append(_param_error(index, stripped[:40], "invalid_key", "缺少 = 或 : 分隔符"))
            continue
        key = stripped[:separator].strip()
        value_text = stripped[separator + 1:].strip()
        if not CUSTOM_KEY.fullmatch(key):
            errors.append(_param_error(
                index, key[:40], "invalid_key",
                "键名需以字母开头，只含字母数字 _ -，长度 1-32",
            ))
            continue
        if key.lower() in RESERVED_PARAM_KEYS:
            errors.append(_param_error(index, key, "reserved_key", "保留键不能写在 custom_params 里"))
            continue
        if key in params:
            errors.append(_param_error(index, key, "duplicate_key", "重复键，保留首次出现的值"))
            continue
        field = declared.get(key)
        if field is None:
            params[key] = value_text
            if mode == "typed":
                errors.append(_param_error(
                    index, key, "unknown_for_type",
                    f"{api_type} 未声明该参数，按原始文本透传",
                ))
            continue
        value, reason = _coerce(field, value_text)
        if reason:
            errors.append(_param_error(index, key, reason, "取值不符合字段类型或范围，已跳过该行"))
            continue
        if value is None or (isinstance(value, str) and not value):
            continue
        params[key] = value
    return params, errors


def effective_params(
    custom: dict[str, Any],
    extra: dict[str, Any] | None = None,
    *,
    reserved: frozenset[str] | set[str] = frozenset(),
    drop_empty: bool = True,
) -> dict[str, Any]:
    """合并节点参数与调用级参数（后者覆盖前者），并按需丢弃空值与保留键。

    Args:
        custom: ``parse_custom_params`` 的结果。
        extra: 调用级 ``extra_params``（来自 LLM 工具）。
        reserved: 额外不允许被覆盖的键（``model``/``prompt`` 等由各适配器传入）。
        drop_empty: 为 True 时丢弃 ``""``/``None``（"留空不发"）。

    Returns:
        合并后的参数字典。
    """
    merged: dict[str, Any] = dict(custom)
    if extra:
        merged.update(extra)
    for key in reserved:
        merged.pop(key, None)
    if not drop_empty:
        return merged
    return {key: value for key, value in merged.items() if value not in ("", None)}


def resolve_thinking(api_type: str, model: str, value: str) -> ThinkingDecision:
    """把节点的 ``thinking`` 值映射成协议字段（F5、能力表 §4.1）。

    - 空值：什么都不发；
    - 协议不支持（xAI/百炼/原生/自定义）：不发送，只给 DEBUG；
    - Gemini：只有 ``GEMINI_THINKING_CAPS`` 显式声明的模型才发送 ``thinkingLevel``，
      ``off`` 一律不发并 WARN（图像模型思考不可关闭）；
    - OpenAI：``max`` 在 Chat 侧映射 ``xhigh``、Images 侧映射 ``high``。

    Returns:
        :class:`ThinkingDecision`；``carrier`` 为空表示不发送。
    """
    text = str(value or "").strip().lower()
    if not text:
        return ThinkingDecision()
    if text not in THINKING_VALUES[1:]:
        return ThinkingDecision(debug=(
            f"思考控制 忽略 api_type={api_type} value={text} reason=invalid_value",
        ))
    if api_type == "openai_image":
        return ThinkingDecision("complexity", _OPENAI_IMAGE_THINKING[text])
    if api_type == "openai_chat":
        return ThinkingDecision("reasoning_effort", _OPENAI_CHAT_THINKING[text])
    if api_type == "gemini_official":
        if text == "off":
            return ThinkingDecision(
                warnings=("gemini_thinking_cannot_disable",),
                debug=(f"思考控制 忽略 api_type={api_type} value=off reason=cannot_disable",),
            )
        caps = GEMINI_THINKING_CAPS.get(str(model or "").strip())
        if caps is None:
            return ThinkingDecision(debug=(
                f"思考控制 忽略 api_type={api_type} model={model} value={text} "
                "reason=model_capability_undeclared",
            ))
        level = str(caps["map"][text])
        debug: tuple[str, ...] = ()
        if level != text.upper():
            debug = (f"思考控制 粗映射 value={text} sent={level}",)
        return ThinkingDecision("thinking_level", level, (), debug)
    return ThinkingDecision(debug=(
        f"思考控制 忽略 api_type={api_type} value={text} reason=unsupported",
    ))


def gemini_aspect_ratio(size: str) -> str:
    """把尺寸归一成 Gemini 的 ``aspectRatio``；无法识别返回空串。

    规则与 ``providers.gemini`` 既有实现一致（吸收上游 v1.3.0 F2）：比例串原样，
    像素尺寸按最大公约数约分，``auto``/空/乱码不产生值。
    """
    text = str(size or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"\d+(?:\.\d+)?:\d+(?:\.\d+)?", text):
        return text
    match = re.fullmatch(r"(\d+)\s*[x*]\s*(\d+)", text, re.IGNORECASE)
    if match is None:
        return ""
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return ""
    divisor = _gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def _gcd(left: int, right: int) -> int:
    """整数最大公约数（避免为纯逻辑模块引入 math 之外的东西）。"""
    while right:
        left, right = right, left % right
    return left


def xai_aspect_ratio(size: str) -> str:
    """把尺寸归一成 xAI 的 ``aspect_ratio``；无法识别返回空串。

    与 Gemini 的区别：``auto`` 是 xAI 的合法值，原样直传。
    """
    text = str(size or "").strip()
    if not text:
        return ""
    if text.lower() == "auto":
        return "auto"
    return gemini_aspect_ratio(text)


def format_param(value: Any) -> str:
    """把参数值序列化为请求字段（bool→``true``/``false``，其余 ``str``）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
