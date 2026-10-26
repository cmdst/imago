"""百度「图像无损放大」的纯逻辑层：端点常量、本地预检、响应解析、错误分类。

本模块**不发网络请求**（那是 ``core/upscale_client.py`` 的职责），也不依赖 AstrBot 或
``aiohttp``，因此可以在纯逻辑测试环境里直接导入（与 ``core/config.py`` 同样的约束）。

端点口径（``[文档]`` 对账结论，见工作区 spec §2.1）：本版只用**图像无损放大**
``image_quality_enhance``（长宽各放大 2 倍）。用户最初给的文档页 ``5k4i6mzqk``
其实是同产品下的「图像清晰度增强」（``image_definition_enhance``），两者**分别**
在控制台勾选供应商能力，未勾选返回 ``error_code=6``。若运营方要的其实是清晰度
增强，只需换 ``ENHANCE_URL`` 常量并放宽 :func:`plan` 的阈值，其余（token 缓存、
降级、配置、日志）不变。

限制（``[文档]`` 原文）：``image`` 的 base64 ≤ 4M、长宽乘积 ≤ 2000px × 2000px、
支持 jpg/png/bmp。超限在服务端**也**算一次计费请求，所以本地先跳过。
"""

from __future__ import annotations

import base64
import binascii
import struct
from typing import TYPE_CHECKING

from .errors import UpscaleError, UpscaleSkipped

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查导入
    from .models import UpscaleConfig

ENHANCE_URL = "https://aip.baidubce.com/rest/2.0/image-process/v1/image_quality_enhance"
TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"

# 文档：支持 jpg/png/bmp（不含 webp/gif）。
SUPPORTED_MIME = frozenset({"image/png", "image/jpeg", "image/bmp"})

# 错误码分类（``[文档]`` 错误码页 + 本插件处置，见 spec §2.7）。
#
# 判定顺序即下面的书写顺序：token → transient → input → permission → quota → config。
TOKEN_ERROR_CODES = frozenset({100, 110, 111})
TRANSIENT_ERROR_CODES = frozenset({1, 2, 4, 216401, 216630, 282000})
INPUT_ERROR_CODES = frozenset({216103, 216200, 216201, 216202, 282114})
PERMISSION_ERROR_CODES = frozenset({6, 14, 3, 216102})
QUOTA_ERROR_CODES = frozenset({17, 18, 19})
CONFIG_ERROR_CODES = frozenset({216100, 216101, 282003, 282004, 282110, 282111, 282112, 282113})

# 剩余预算门槛：剩余预算 ≤ 单张超时 + 这个余量时跳过放大（spec §3.8）。
# 出图已落盘，只剩「发给平台」的活；宁可发原图，也不要因为放大把任务拖成
# TIMED_OUT —— 超时路径**不会**发送已经生成好的图。
BUDGET_MARGIN_SECONDS = 15

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# JPEG 段里不含尺寸的 SOF 变体（DHT / JPG / DAC）。
_JPEG_NON_SOF = frozenset({0xC4, 0xC8, 0xCC})


def sniff_mime(data: bytes) -> str | None:
    """按文件魔数识别图片类型；无法识别返回 ``None``。

    ``core.network.detect_image_mime`` 覆盖 png/jpeg/webp/gif 但**不含 bmp**，而百度
    无损放大支持 bmp ⇒ 这里单独补一条。刻意**不去改** ``detect_image_mime``：那是
    参考图/结果图的公共路径，改它会影响 ``materialize_result`` 的后缀映射。

    Args:
        data: 图片字节。

    Returns:
        ``image/png`` / ``image/jpeg`` / ``image/bmp``，或 ``None``。
    """
    if not data:
        return None
    if data.startswith(_PNG_MAGIC):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    # BMP：``BM`` 魔数 + 至少够读到文件头里的尺寸字段（offset 18..26）。
    if data[:2] == b"BM" and len(data) >= 26:
        return "image/bmp"
    return None


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """扫 JPEG 段找 SOF 读尺寸；任何异常/长度不足返回 ``None``。"""
    index = 2
    length = len(data)
    while index + 4 <= length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xD9:  # EOI：没有 SOF
            return None
        segment_length = struct.unpack(">H", data[index + 2:index + 4])[0]
        if segment_length < 2:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in _JPEG_NON_SOF:
            # SOF 布局：FFCx | 长度(2) | 精度(1) | 高(2) | 宽(2)
            if index + 9 > length:
                return None
            height, width = struct.unpack(">HH", data[index + 5:index + 9])
            return int(width), int(height)
        index += 2 + segment_length
    return None


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """用纯 Python 头解析读 PNG/JPEG/BMP 的 ``(width, height)``。

    只用于**预检**（长宽乘积是否超 2000×2000）：解析失败一律返回 ``None``（不做像素
    预检，交给服务端 ``216202`` 兜底），绝不因为读不出尺寸就阻断放大。

    Args:
        data: 图片字节。

    Returns:
        ``(width, height)``；无法识别或尺寸非正时 ``None``。
    """
    try:
        dimensions: tuple[int, int] | None = None
        if data.startswith(_PNG_MAGIC):
            if len(data) >= 24 and data[12:16] == b"IHDR":
                width, height = struct.unpack(">II", data[16:24])
                dimensions = int(width), int(height)
        elif data.startswith(b"\xff\xd8\xff"):
            dimensions = _jpeg_dimensions(data)
        elif data[:2] == b"BM" and len(data) >= 26:
            width, height = struct.unpack("<ii", data[18:26])
            dimensions = abs(int(width)), abs(int(height))
    except (struct.error, ValueError, IndexError):
        return None
    if dimensions is None or dimensions[0] <= 0 or dimensions[1] <= 0:
        return None
    return dimensions


def encode_image(data: bytes) -> str:
    """把图片字节编码成百度要的 base64（**不带** ``data:image/...`` 图片头）。"""
    return base64.b64encode(data).decode("ascii")


def plan(data: bytes, cfg: "UpscaleConfig", *, remaining: float = 0.0) -> tuple[bool, str, str]:
    """发请求前的本地预检；返回 ``(是否放大, reason, detail)``。

    判定顺序即 ``reason`` 的优先级：``empty`` → ``format`` → ``too_large`` →
    ``too_many_pixels`` → ``budget``。``detail`` 只含字节数/尺寸/阈值，可直接进 DEBUG。

    Args:
        data: 原图字节（此时已经落盘，``output_dir`` 里的 Path 读出来的）。
        cfg: 放大配置（阈值都带有 clamp 后的合法值）。
        remaining: 本任务剩余的总预算（秒），见 ``core/upscale_client.py``。

    Returns:
        ``(True, "", "")`` 表示可以发请求；否则 ``(False, reason, detail)``。
    """
    if not data:
        return False, "empty", "bytes=0"
    mime = sniff_mime(data)
    if mime is None:
        # 魔数认不出（webp/gif/未知）⇒ 百度也不收，本地跳过，省一次计费。
        return False, "format", "mime=unknown"
    if cfg.max_raw_bytes and len(data) > cfg.max_raw_bytes:
        return False, "too_large", f"bytes={len(data)} limit={cfg.max_raw_bytes}"
    dimensions = image_dimensions(data)
    if dimensions is not None:
        width, height = dimensions
        if width * height > cfg.max_pixels:
            return (
                False,
                "too_many_pixels",
                f"dims={width}x{height} limit={cfg.max_pixels}",
            )
    if remaining <= cfg.timeout_seconds + BUDGET_MARGIN_SECONDS:
        return False, "budget", f"remaining={remaining:.0f}s"
    return True, "", ""


def classify_error(code: int) -> str:
    """把百度 ``error_code`` 归成 ``kind``（机器可读的处置分类）。

    分类含义（处置见 ``core/upscale_client.py``）：

    - ``token``：token 失效/无效/拉取失败 ⇒ 重取 + 恰好重试一次；
    - ``transient``：集群/内部错误 ⇒ 退避 0.5 s 重试一次；
    - ``input``：图片本身有问题（空/格式/超限）⇒ 跳过（不告警、不熔断）；
    - ``permission``：应用未勾选能力 / IAM 失败 ⇒ 不重试、告警一次、熔断；
    - ``quota``：日/QPS/总量额度用尽 ⇒ 不重试、告警一次、熔断；
    - ``config``：参数/端点错 ⇒ 不重试、失败；
    - ``unknown``：未列出的码 ⇒ 不重试、失败（DEBUG 记码）。
    """
    if code in TOKEN_ERROR_CODES:
        return "token"
    if code in TRANSIENT_ERROR_CODES:
        return "transient"
    if code in INPUT_ERROR_CODES:
        return "input"
    if code in PERMISSION_ERROR_CODES:
        return "permission"
    if code in QUOTA_ERROR_CODES:
        return "quota"
    if code in CONFIG_ERROR_CODES:
        return "config"
    return "unknown"


def _int_error_code(value: object) -> int:
    """``error_code`` 可能是数字或字符串（文档两种都给过），统一成 int。"""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def error_code_of(payload: object) -> int:
    """取响应里的 ``error_code``；没有/非法时返回 ``0``（表示「不是错误」）。"""
    if not isinstance(payload, dict):
        return 0
    raw = payload.get("error_code")
    if raw is None or raw == "" or raw is False:
        return 0
    return _int_error_code(raw)


def decode_payload(payload: object, *, expected_max_bytes: int = 0) -> tuple[bytes, str]:
    """校验百度响应并解出图片字节；返回 ``(data, mime)``。

    Args:
        payload: 响应 JSON（成功时 ``{"log_id":…, "image":"<base64>"}``）。
        expected_max_bytes: 输出体积上限（``storage_config.max_upload_mb``）；
            ``<=0`` 表示不限。超出抛 :class:`UpscaleSkipped`（跳过，不是失败）。

    Returns:
        ``(图片字节, MIME)``；MIME 由魔数判定，识别不出统一给 ``image/png``。

    Raises:
        UpscaleError: ``error_code`` 非 0（分类见 :func:`classify_error`）、
            ``image`` 缺失/不是合法 base64/为空。
        UpscaleSkipped: 解出的图片超过输出上限。
    """
    if not isinstance(payload, dict):
        raise UpscaleError("unknown", "payload_not_object")
    code = error_code_of(payload)
    if code:
        raise UpscaleError(
            classify_error(code),
            str(payload.get("error_msg", "") or ""),
            code=code,
        )
    encoded = payload.get("image")
    # 文档里 ``image`` 是「否（可选）」：HTTP 200 且无 error_code 也可能没有图，
    # 必须先判空，绝不写空文件。
    if not isinstance(encoded, str) or not encoded:
        raise UpscaleError("unknown", "no_image")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise UpscaleError("unknown", "image_not_base64") from exc
    if not data:
        raise UpscaleError("unknown", "image_empty")
    if expected_max_bytes > 0 and len(data) > expected_max_bytes:
        raise UpscaleSkipped("output_oversize", f"bytes={len(data)} limit={expected_max_bytes}")
    return data, sniff_mime(data) or "image/png"
