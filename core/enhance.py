"""百度「图像清晰度增强」的纯逻辑层：端点常量、本地预检、响应解析、错误分类。

本模块**不发网络请求**（那是 ``core/enhance_client.py`` 的职责），也不依赖 AstrBot 或
``aiohttp``，因此可以在纯逻辑测试环境里直接导入（与 ``core/config.py`` 同样的约束）。

端点口径（变更令 v2，见工作区 spec ``v2.3``）：本版调**图像清晰度增强**
``image_definition_enhance`` —— 对压缩后的模糊图像去噪、优化纹理细节，**输出尺寸与输入
相同**（早期草案里「长宽各放大 2 倍」的口径已作废）。同产品另有「图像无损放大」
接口，两者在百度控制台**分别**勾选供应商能力，未勾选返回
``error_code=6``；若运营方要的其实是那一个，只需换 ``ENHANCE_URL`` 常量与
:func:`plan` 的阈值，其余（token 缓存、降级、配置、日志）不变。

限制（``[文档]`` 原文）：``image`` 的 base64 ≤ 10M（参考：原图约 8M 以内）、最短边
≥ 10px、最长边 ≤ 5000px、长宽比 4:1 以内、支持 jpg/png/bmp。超限在服务端**也**算一次
计费请求，所以本地先跳过。
"""

from __future__ import annotations

import base64
import binascii
import struct
from typing import TYPE_CHECKING

from .errors import EnhanceError, EnhanceSkipped

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查导入
    from .models import EnhanceConfig

ENHANCE_URL = "https://aip.baidubce.com/rest/2.0/image-process/v1/image_definition_enhance"
TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"

# 文档：支持 jpg/png/bmp（不含 webp/gif；`url` 行与 `216201` 错误码两处交叉确认）。
SUPPORTED_MIME = frozenset({"image/png", "image/jpeg", "image/bmp"})

# 官方硬下限：最短边至少 10px。**不做配置键**（调低无意义），因此配置组恰好 8 键。
MIN_EDGE_PX = 10

# base64 上限 10 MiB 对应的**原图原始字节**上限（内部常量，不是配置键）：
# base64 按 4/3 膨胀，故 10 × 1024 × 1024 × 3 // 4 = 7 864 320 B（约 7.5 MiB）。
# ``EnhanceConfig.max_raw_bytes`` 按用户配置的 ``max_image_mb`` 现算，默认值即本常量；
# 这里保留一份常量用于「默认值是多少」的单点说明与文档对账。
MAX_RAW_BYTES_FOR_10MB_BASE64 = 10 * 1024 * 1024 * 3 // 4

# 错误码分类（``[文档]`` 错误码页 + 本插件处置，见 spec §2.7）。
#
# 判定顺序即下面的书写顺序：token → transient → input → permission → quota → config。
TOKEN_ERROR_CODES = frozenset({100, 110, 111})
TRANSIENT_ERROR_CODES = frozenset({1, 2, 4, 216401, 216630, 282000})
INPUT_ERROR_CODES = frozenset({216103, 216200, 216201, 216202, 282114})
PERMISSION_ERROR_CODES = frozenset({6, 14, 3, 216102})
QUOTA_ERROR_CODES = frozenset({17, 18, 19})
CONFIG_ERROR_CODES = frozenset({216100, 216101, 282003, 282004, 282110, 282111, 282112, 282113})

# 剩余预算门槛：剩余预算 ≤ 单张超时 + 这个余量时跳过增强（spec §3.8）。
# 出图已落盘，只剩「发给平台」的活；宁可发原图，也不要因为增强把任务拖成
# TIMED_OUT —— 超时路径**不会**发送已经生成好的图。
BUDGET_MARGIN_SECONDS = 15

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# JPEG 段里不含尺寸的 SOF 变体（DHT / JPG / DAC）。
_JPEG_NON_SOF = frozenset({0xC4, 0xC8, 0xCC})


def sniff_mime(data: bytes) -> str | None:
    """按文件魔数识别图片类型；无法识别返回 ``None``。

    ``core.network.detect_image_mime`` 覆盖 png/jpeg/webp/gif 但**不含 bmp**，而百度
    该接口支持 bmp ⇒ 这里单独补一条。刻意**不去改** ``detect_image_mime``：那是
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

    只用于**预检**（最短边 / 最长边 / 长宽比）：解析失败一律返回 ``None``（不做尺寸
    预检，交给服务端 ``216202`` 兜底），绝不因为读不出尺寸就阻断增强。

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


def plan(data: bytes, cfg: "EnhanceConfig", *, remaining: float) -> tuple[bool, str, str]:
    """发请求前的本地预检；返回 ``(是否增强, reason, detail)``。

    判定顺序即 ``reason`` 的优先级（spec 变更令 v2.5）：

    1. ``empty`` —— 0 字节；
    2. ``format`` —— 魔数不在 ``SUPPORTED_MIME``（webp/gif/未知，百度不收）；
    3. ``too_large`` —— ``len(data) > cfg.max_raw_bytes``（默认 10 MiB ⇒ 7 864 320 B）；
    4. ``edge_too_short`` —— 能读出尺寸且最短边 < ``MIN_EDGE_PX``（10px）；
    5. ``edge_too_long`` —— 能读出尺寸且最长边 > ``cfg.max_edge``（默认 5000px）；
    6. ``aspect_too_wide`` —— 能读出尺寸且长宽比 > ``cfg.max_aspect``（默认 4.0，边界 4:1 允许）；
    7. ``budget`` —— 任务剩余预算不足以再做一次增强。

    ``detail`` 只含字节数/尺寸/阈值，可直接进 DEBUG。读不出尺寸时**跳过** 4/5/6 三条，
    交服务端 ``216202`` / ``216201`` 兜底（不因读不出而阻断）。

    Args:
        data: 原图字节（此时已经落盘，``output_dir`` 里的 Path 读出来的）。
        cfg: 增强配置（阈值都带有 clamp 后的合法值）。
        remaining: 本任务剩余的总预算（秒），见 ``core/enhance_client.py``。
            **必填**（不设默认值）：漏传会静默按「预算不足」跳过，这是最难查的一类失灵。

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
        short_edge, long_edge = min(width, height), max(width, height)
        if short_edge < MIN_EDGE_PX:
            return False, "edge_too_short", f"dims={width}x{height} min_edge={MIN_EDGE_PX}"
        if long_edge > cfg.max_edge:
            return False, "edge_too_long", f"dims={width}x{height} limit={cfg.max_edge}"
        ratio = long_edge / short_edge
        if ratio > cfg.max_aspect:
            return (
                False,
                "aspect_too_wide",
                f"dims={width}x{height} ratio={ratio:.2f} limit={cfg.max_aspect:g}",
            )
    if remaining <= cfg.timeout_seconds + BUDGET_MARGIN_SECONDS:
        return False, "budget", f"remaining={remaining:.0f}s"
    return True, "", ""


def classify_error(code: int) -> str:
    """把百度 ``error_code`` 归成 ``kind``（机器可读的处置分类）。

    分类含义（处置见 ``core/enhance_client.py``）：

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
            ``<=0`` 表示不限。超出抛 :class:`EnhanceSkipped`（跳过，不是失败）。

    Returns:
        ``(图片字节, MIME)``；MIME 由魔数判定，识别不出统一给 ``image/png``。

    Raises:
        EnhanceError: ``error_code`` 非 0（分类见 :func:`classify_error`）、
            ``image`` 缺失/不是合法 base64/为空。
        EnhanceSkipped: 解出的图片超过输出上限。
    """
    if not isinstance(payload, dict):
        raise EnhanceError("unknown", "payload_not_object")
    code = error_code_of(payload)
    if code:
        raise EnhanceError(
            classify_error(code),
            str(payload.get("error_msg", "") or ""),
            code=code,
        )
    encoded = payload.get("image")
    # 文档里 ``image`` 是「否（可选）」：HTTP 200 且无 error_code 也可能没有图，
    # 必须先判空，绝不写空文件。
    if not isinstance(encoded, str) or not encoded:
        raise EnhanceError("unknown", "no_image")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise EnhanceError("unknown", "image_not_base64") from exc
    if not data:
        raise EnhanceError("unknown", "image_empty")
    if expected_max_bytes > 0 and len(data) > expected_max_bytes:
        raise EnhanceSkipped("output_oversize", f"bytes={len(data)} limit={expected_max_bytes}")
    return data, sniff_mime(data) or "image/png"
