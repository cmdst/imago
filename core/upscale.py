"""百度「图像无损放大」的纯逻辑层：端点常量、本地预检、响应解析。

本模块**不发网络请求**（那是 ``core/upscale_client.py`` 的职责），也不依赖 AstrBot 或
``aiohttp``，因此可以在纯逻辑测试环境里直接导入（与 ``core/enhance.py`` 同样的约束）。

端点口径（变更令 v3，见工作区 spec ``v3.3``）：本版新增**图像无损放大**
``image_quality_enhance`` —— 把图在**长宽方向各放大两倍**（面积 ×4）；与既有的
「图像清晰度增强」是同一产品下的**另一个**接口（尺寸不变、只变清晰），两者在百度
控制台**分别**勾选供应商能力，未勾选调用返回 ``error_code=6``。

与增强侧的分工（spec ``v3.3.5`` 的「分别建模」7 条硬约束）：

- **不复用**增强的 ``plan()`` / 配置对象 / 跳过枚举 / 熔断标志（各自独立，互不冒充）；
- **只复用**产品级共用的纯函数与常量，本模块把它们 import 进来后再导出：
  ``sniff_mime`` / ``image_dimensions`` / ``encode_image`` / ``classify_error`` /
  ``error_code_of`` / ``decode_payload`` / ``BUDGET_MARGIN_SECONDS``；
- 阈值判据**完全不同**：官方该页给的是「**长宽乘积** ≤ 2000×2000」与「base64 ≤ 4M」，
  没有最短边 / 最长边 / 长宽比要求。

限制（``[文档]`` 原文）：``image`` 的 base64「不超过4M，长宽乘积不超过2000p x 2000px」
（页面原文把 ``2000px`` 写成 ``2000p``，属笔误，本实现按 2000 × 2000 = 4 000 000 px
建模，代码注释同样注明「原文如此」）；格式 jpg/png/bmp（``url`` 行 + 错误码 ``216201``
交叉确认，不含 webp/gif）。超限在服务端**也**算一次计费请求，所以本地先跳过。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import enhance as _enhance
from .errors import EnhanceError, EnhanceSkipped, UpscaleError, UpscaleSkipped

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查导入
    from .models import UpscaleConfig

# 复用面：产品级共用的纯函数与常量（``[文档]`` 两接口同域名、同 token、同错误码页）。
# 只在这里 import 并再导出，**不改** ``core/enhance.py`` 的任何一行。
sniff_mime = _enhance.sniff_mime
image_dimensions = _enhance.image_dimensions
encode_image = _enhance.encode_image
classify_error = _enhance.classify_error
error_code_of = _enhance.error_code_of
BUDGET_MARGIN_SECONDS = _enhance.BUDGET_MARGIN_SECONDS

# ``[文档]`` 请求 URL（写死常量，**不做配置项**：把「换成另一个接口」做成可配的
# 开关等于给运维留一个静默换端点的破坏性选项，同 v1 的处理）。
UPSCALE_URL = "https://aip.baidubce.com/rest/2.0/image-process/v1/image_quality_enhance"

# base64 上限 4 MiB 对应的**原图原始字节**上限（内部常量，不是配置键）：
# base64 按 4/3 膨胀，故 4 × 1024 × 1024 × 3 // 4 = 3 145 728 B（约 3 MiB）。
# ``UpscaleConfig.max_raw_bytes`` 按用户配置的 ``max_image_mb`` 现算，默认值即本常量；
# 这里保留一份常量用于「默认值是多少」的单点说明与文档对账。
MAX_RAW_BYTES_FOR_4MB_BASE64 = 4 * 1024 * 1024 * 3 // 4

# 我方**本地省钱守卫**的最短边下限：官方该页**没有**最短边要求（这不是官方限值）。
# 放大只增分辨率不增信息，<16px 的边放大后仍是放大的马赛克（16 → 2× = 32px 起才有
# 意义）；做成常量、**不占配置键**，避免用户把它调到「给 1px 图付钱」。
UPSCALE_MIN_EDGE_PX = 16

# ``[文档]``「将图像在长宽方向各放大两倍」⇒ 每边 2 倍、面积 4 倍。只做观测用。
SCALE_FACTOR = 2

# 本地跳过的全部 ``reason``（**恰好 7 条**，spec ``v3.4``）：前 5 条在 :func:`plan`，
# 后 2 条在 ``core/upscale_client.BaiduUpscaler.apply`` 里判。与增强的 9 条枚举
# （``empty`` / ``edge_too_long`` / ``aspect_too_wide`` 等）**互不冒充**：本侧没有
# 那几条判据，增强侧也没有 ``pixels_too_large``。
SKIP_REASONS = (
    "format",
    "too_large",
    "pixels_too_large",
    "edge_too_short",
    "budget",
    "read_failed",
    "output_oversize",
)

def plan(data: bytes, cfg: "UpscaleConfig", *, remaining: float) -> tuple[bool, str, str]:
    """发请求前的本地预检；返回 ``(是否放大, reason, detail)``。

    判定顺序即 ``reason`` 的优先级（spec ``v3.4``，前 5 条）：

    1. ``format`` —— 魔数认不出（含 0 字节：本阶段输入必定是已落盘成品图，0 字节只能
       来自写盘异常/空响应，与「魔数不认识」同处置同成本，故**不单列**第 8 条枚举）；
    2. ``too_large`` —— ``len(data) > cfg.max_raw_bytes``（默认 4 MiB ⇒ 3 145 728 B，
       官方「base64 ≤ 4M」的原图等价判据；用原图字节数判，省一次编码）；
    3. ``pixels_too_large`` —— 能读出尺寸且 ``w * h > cfg.max_pixels``
       （默认 4 000 000 = 2000×2000；**乘积**判据，`4000×1000` 放行、`2001×2000` 跳过）；
    4. ``edge_too_short`` —— 能读出尺寸且最短边 < ``UPSCALE_MIN_EDGE_PX``（16，
       **非官方限值**，本地省钱守卫）；
    5. ``budget`` —— 任务剩余预算不足以再做一次放大（``<=`` 单张超时 + 15 s 余量，
       判据写法与 ``core/enhance.plan()`` 逐字同构）。

    ``detail`` 只含字节数/尺寸/阈值，可直接进 DEBUG。读不出尺寸时**跳过** 3/4 两条，
    交服务端 ``216202``/``216201`` 兜底（不因读不出而阻断）。

    Args:
        data: 待放大的图字节（增强产物；增强未成功时是原图）。
        cfg: 放大配置（阈值都带有 clamp 后的合法值）。
        remaining: 本任务剩余的总预算（秒）。**必填**（不设默认值）：漏传会静默按
            「预算不足」跳过，这是最难查的一类失灵。

    Returns:
        ``(True, "", "")`` 表示可以发请求；否则 ``(False, reason, detail)``。
    """
    mime = sniff_mime(data)
    if mime is None:
        # 魔数认不出（webp/gif/未知/0 字节）⇒ 百度也不收，本地跳过，省一次计费。
        return False, "format", "mime=unknown"
    if cfg.max_raw_bytes and len(data) > cfg.max_raw_bytes:
        return False, "too_large", f"bytes={len(data)} limit={cfg.max_raw_bytes}"
    dimensions = image_dimensions(data)
    if dimensions is not None:
        width, height = dimensions
        pixels = width * height
        if pixels > cfg.max_pixels:
            return (
                False,
                "pixels_too_large",
                f"dims={width}x{height} pixels={pixels} limit={cfg.max_pixels}",
            )
        if min(width, height) < UPSCALE_MIN_EDGE_PX:
            return (
                False,
                "edge_too_short",
                f"dims={width}x{height} min_edge={UPSCALE_MIN_EDGE_PX}",
            )
    if remaining <= cfg.timeout_seconds + BUDGET_MARGIN_SECONDS:
        return False, "budget", f"remaining={remaining:.0f}s"
    return True, "", ""


def dims_text(before: tuple[int, int] | None, after: tuple[int, int] | None) -> str:
    """DEBUG 行里的 ``dims=`` 段：``"64x64->128x128"``；任一侧读不出时 ``"-"``。"""
    if before is None or after is None:
        return "-"
    return f"{before[0]}x{before[1]}->{after[0]}x{after[1]}"


def double_scale_ok(before: tuple[int, int] | None, after: tuple[int, int] | None) -> bool:
    """输出是否是输入的 2 倍（长宽各 2 倍，即面积 ×4）——**只做观测，绝不判失败**。

    ``[文档]`` 语义是「长宽方向各放大两倍」，但输出尺寸文档未声明，服务端也可能因
    内部约束返回别的尺寸；此时**响应字节仍原样落盘**（信任服务端交付），只在
    ``dims=`` 段里把实际尺寸记下来供排障对账。尺寸任一侧读不出时按「不可判断」返回
    ``True``（不误报）。

    Args:
        before: 输入尺寸，``core.upscale.image_dimensions`` 的返回值。
        after: 输出尺寸，同上。

    Returns:
        True 表示「要么正好 2 倍、要么无从判断」；False 表示读出了尺寸但不是 2 倍。
    """
    if before is None or after is None:
        return True
    return after[0] == before[0] * SCALE_FACTOR and after[1] == before[1] * SCALE_FACTOR


def mime_suffix(mime: str) -> str:
    """放大结果的落盘后缀：只认 png/jpg，其余按 png（文档未声明输出格式，按魔数定）。"""
    return {"image/png": ".png", "image/jpeg": ".jpg"}.get(mime, ".png")


def decode_payload(payload: object, *, expected_max_bytes: int = 0) -> tuple[bytes, str]:
    """校验百度响应并解出图片字节；返回 ``(data, mime)``。

    实现复用 ``core/enhance.decode_payload``（产品级共用的同一份校验与 base64 解码），
    但把两类异常**翻译**成放大自己的类型：放大侧 ``except UpscaleSkipped`` 绝不能
    误接增强的异常，反之亦然（spec ``v3.9`` 的 ``core/errors.py`` 要求）。

    Args:
        payload: 响应 JSON（成功时 ``{"log_id":…, "image":"<base64>"}``）。
        expected_max_bytes: 输出体积上限；``<=0`` 表示不限（默认由
            ``BaiduUpscaler.apply`` 按 ``storage_config.max_upload_bytes`` 自己判，
            见 spec ``v3.4`` 第 7 条）。

    Returns:
        ``(图片字节, MIME)``；MIME 由魔数判定，识别不出统一给 ``image/png``。

    Raises:
        UpscaleError: ``error_code`` 非 0、``image`` 缺失/不是合法 base64/为空。
        UpscaleSkipped: 解出的图片超过 ``expected_max_bytes``。
    """
    try:
        return _enhance.decode_payload(payload, expected_max_bytes=expected_max_bytes)
    except EnhanceError as exc:
        raise UpscaleError(exc.kind, exc.detail, code=exc.code) from exc
    except EnhanceSkipped as exc:
        raise UpscaleSkipped(exc.reason, exc.detail) from exc


__all__ = [
    "BUDGET_MARGIN_SECONDS",
    "MAX_RAW_BYTES_FOR_4MB_BASE64",
    "SCALE_FACTOR",
    "SKIP_REASONS",
    "UPSCALE_MIN_EDGE_PX",
    "UPSCALE_URL",
    "classify_error",
    "decode_payload",
    "dims_text",
    "double_scale_ok",
    "encode_image",
    "error_code_of",
    "image_dimensions",
    "mime_suffix",
    "plan",
    "sniff_mime",
]
