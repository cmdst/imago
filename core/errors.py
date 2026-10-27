class ImagoFusionFusionError(Exception):
    code = "internal_error"


class ConfigurationError(ImagoFusionFusionError):
    code = "configuration_missing"


class PersonaError(ImagoFusionFusionError):
    code = "persona_invalid"


class ReferenceImageError(ImagoFusionFusionError):
    code = "reference_invalid"


class QuotaError(ImagoFusionFusionError):
    code = "quota_denied"


class ProviderError(ImagoFusionFusionError):
    code = "provider_error"

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class ProviderTimeout(ProviderError):
    code = "provider_timeout"


class UnsupportedResponse(ProviderError):
    code = "unsupported_response"


class NoOutputError(UnsupportedResponse):
    """Provider 请求已完成，但响应中没有可用图片。"""

    code = "no_output"


class SendError(ImagoFusionFusionError):
    code = "send_failed"


class DuplicateImage(ImagoFusionFusionError):
    code = "duplicate_image"


class DuplicateRequest(ImagoFusionFusionError):
    """同一条用户消息重复调用绘图工具，或缺少消息 ID 时的同内容重复提交。"""

    code = "duplicate_request"

    def __init__(self, message: str = "刚刚已创建过相同画面，请稍后再试"):
        super().__init__(message)


class EnhanceError(Exception):
    """增强失败的分类错误；``kind`` 机器可读，``code`` 为百度 ``error_code``（可能为 0）。

    刻意不继承 :class:`ImagoFusionFusionError`：增强失败**不是**绘图失败，绝不能被
    ``main.py`` 的 ``task.errors`` 收下（否则用户会看到「部分图片绘制失败。」，
    任务状态也会被降级成 ``PARTIAL_SUCCESS``）。见 ``core/enhance_client.py`` 的降级语义。
    """

    def __init__(self, kind: str, detail: str = "", *, code: int = 0) -> None:
        super().__init__(detail or kind)
        self.kind = kind  # token | transient | input | permission | quota | config | unknown
        self.detail = detail
        self.code = code


class EnhanceSkipped(Exception):
    """本地预检判定「不增强」（不是失败）：``reason`` 见 ``core/enhance.plan()``。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


# 创建阶段允许透出的固定文案：前缀白名单（冒号结尾的条目允许后跟数字等参数，
# 如 "绘图额度不足: 3"、"不允许的附加参数: n"），不带冒号的只允许完全一致。
_ALLOWED_ERROR_MESSAGES = (
    "提示词不能为空",
    "未配置有效图片节点",
    "附加参数必须使用 --key value",
    "不允许的附加参数:",
    "当前 AstrBot 不支持 Persona 正式解析",
    "Persona 不存在或 prompt 为空",
    "任务准备超时",
    "引用消息图片无法获取",
    "插件正在关闭",
    "你当前无法使用绘图功能",
    "绘图额度不足:",
    "绘图额度不足：",
    "无法识别用户 ID",
    "用户 ID 无效",
    "额度不能小于 0",
    "请提供用户 ID 和额度整数",
    "额度必须是整数",
    "外观摘要不能为空",
    "请在同一条消息中附带图片",
    "刚刚已创建过相同画面，请稍后再试",
)

# 参考图/SSRF 相关的固定文案：必须与已知文案完全一致才透出，
# 防止拼接了 URL/路径/平台信息的变体被前缀误放行。
_REFERENCE_ERROR_MESSAGES = frozenset((
    "参考图重定向次数过多",
    "参考图重定向目标不安全",
    "参考图重定向缺少 Location",
    "参考图过大",
    "参考图无法获取",
    "远程响应不是图片",
    "base64 图片无效",
    "base64 图片格式无法识别",
    "data URL 无效",
    "data URL 图片格式无法识别",
    "本地参考图不存在",
    "本地参考图格式不支持",
    "图片格式或大小不符合要求",
    "不允许访问私网或本地地址",
    "无法解析远程主机",
    "只允许无内嵌凭据的 HTTP/HTTPS URL",
))


def safe_creation_error_message(exc: BaseException) -> str:
    """只向用户和主 LLM 返回任务创建阶段的可公开原因。"""
    from .security import redact

    message = redact(str(exc)).strip()
    for prefix in _ALLOWED_ERROR_MESSAGES:
        if message == prefix or (
            prefix.endswith((":", "：")) and message.startswith(prefix)
        ):
            return message
    if message in _REFERENCE_ERROR_MESSAGES:
        return message
    # “参考图 HTTP <3 位状态码>” 只含状态码，不包含 URL/路径/平台信息。
    if message.startswith("参考图 HTTP "):
        status = message[len("参考图 HTTP "):]
        if len(status) == 3 and status.isdigit():
            return message
    if isinstance(exc, (TypeError, ValueError, OverflowError)):
        return "任务参数无效"
    return "插件暂时无法创建任务"
