"""入站参考图压缩（C-COMPRESS，v1.4.1 契约 §3）。

职责边界（一句话）：**只压缩"发往 provider 的副本"**。``task.request.references``
与 ``inputs/`` 落盘的原素材逐字节不变，本模块返回的是**新的** ``ImageInput`` 列表，
调用方（``main._prepare``）把它们放进 ``task.runtime["send_references"]`` /
``["send_persona_references"]``，调度器构造 attempt 请求时优先取这两份。

设计约束（逐条对应契约）：

* **确定性**：固定尺寸阶梯 × 质量阶梯，无随机采样，同一输入两次调用结果逐字节相同。
* **不膨胀**：候选必须 ``len(candidate) <= len(original)`` 才可能被选中；全部候选都
  更大时返回原图并记 ``no_gain``（§3.5）。
* **不做无谓解码**：只有 ``min_bytes`` 一个分支在 ``Image.open`` 之前返回（体积达标的
  图仍要过尺寸/EXIF/质量档，不做 ``len<=target`` 早退），像素总数超限时只读文件头、
  不 ``load()`` 全图（§3.4）。
* **不放大**：缩放只按 ``max_edge`` 与尺寸阶梯向下缩；``max_edge == 0`` 表示**禁止
  任何缩放**、只重编码（§3.3 / §3.4）。
* **异常不外抛**：任何解码/编码/内存异常都降级为"原图直发"，且**绝不**写入
  ``task.errors``——压缩是尽力而为的优化，不能变成新的失败源（§3.8）。
* **Pillow 可选**：依赖由 AstrBot 运行时提供（实测 12.2.0）；``requirements.txt``
  不声明它，缺失时进程内只告警一次并整体降级（§3.8 / §3.9）。
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from typing import Any, Callable

from .models import ImageInput, ReferenceCompressConfig

try:  # pragma: no cover - 取决于运行环境是否带 Pillow
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - 缺失时下面所有路径都走"原图直发"
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]

JPEG_MIME_TYPE = "image/jpeg"
PNG_MIME_TYPE = "image/png"

#: 质量阶梯的下探步长与绝对下界（契约 §3.5：q、q-10、q-20，下界 ``max(40, q-30)``）。
QUALITY_FLOOR = 40
QUALITY_STEPS = (0, 10, 20)
#: 尺寸阶梯：1.0 / 0.85 / 0.7225（0.85²，与 2000→1700→1445 的定点口径一致）。
SCALE_STEPS = (1.0, 0.85, 0.7225)
#: 单张图最多编码次数（3 质量档 × 3 尺寸档，契约 §3.5）。
MAX_ENCODES = 9

#: ``skipped`` 的原因枚举（不是失败，不重试、不写 errors）。
#: 注意：**没有** ``already_small`` 这一档——只按 ``min_bytes`` 决定"不压也不解码"，
#: 超过 ``min_bytes`` 的图一律做长边检查 / EXIF 方向 / alpha 处理后再判断收益
#: （契约 rev2 §3；否则"体积已经很小但长边 8000px"的图会永远躲过压缩）。
SKIP_UNDER_MIN_BYTES = "under_min_bytes"
SKIP_TOO_MANY_PIXELS = "too_many_pixels"
SKIP_NO_GAIN = "no_gain"
#: ``failed`` 的原因枚举：``undecodable``＝解码/读头失败，``error``＝编码或其它异常。
FAIL_UNDECODABLE = "undecodable"
FAIL_ERROR = "error"

MISSING_PILLOW_WARNING = "参考图压缩不可用：Pillow 未安装，参考图按原样发送"
FALLBACK_WARNING_TEMPLATE = "参考图压缩失败，回退原图: {detail}"
INFO_TEMPLATE = "参考图压缩 in={in_bytes}B out={out_bytes}B {edge}px q{quality} {elapsed}ms"
INFO_UNMET_SUFFIX = " target_unmet"
SKIP_TEMPLATE = "参考图压缩 skip reason={reason} bytes={bytes}"

_ALPHA_MODES = frozenset({"RGBA", "LA", "PA"})
_DEFAULT_STEM = "reference"
_JPEG_SUFFIX = ".jpg"
_PNG_SUFFIX = ".png"
_MAX_DETAIL_CHARS = 120

_missing_pillow_warned = False


class _TooManyPixels(Exception):
    """像素总数超过 ``max_pixels``：只读文件头即可判定，不解码全图。"""


@dataclass(frozen=True, slots=True)
class CompressOutcome:
    """单张图的压缩结果（不可变，便于测试断言与去重缓存）。"""

    image: ImageInput
    changed: bool = False
    skipped: str = ""
    failed: str = ""
    in_bytes: int = 0
    out_bytes: int = 0
    edge: int = 0
    quality: int = 0
    target_unmet: bool = False
    elapsed_ms: int = 0
    detail: str = ""


def available() -> bool:
    """Pillow 是否可用；不可用时上层整体跳过压缩（不建 runtime 键）。"""
    return Image is not None and ImageOps is not None


def _suffix_for(filename: str) -> str:
    """取原文件名的扩展名（小写，含点）；没有则空串。"""
    name = str(filename or "").strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    dot = name.rfind(".")
    if dot <= 0:
        return ""
    return name[dot:].lower()


def output_filename(filename: str, suffix: str) -> str:
    """按输出格式生成文件名：**扩展名必须与 mime 配对**（契约 §3.7）。"""
    name = str(filename or "").strip().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = name[: name.rfind(".")] if "." in name[1:] else name
    stem = stem.strip() or _DEFAULT_STEM
    return f"{stem}{suffix}"


def _open_first_frame(data: bytes):
    """打开首帧（GIF / 动态 WebP 只取第 0 帧），不做全图解码。"""
    image = Image.open(io.BytesIO(data))
    try:
        if int(getattr(image, "n_frames", 1) or 1) > 1:
            image.seek(0)
    except Exception:  # pragma: no cover - 首帧不可 seek 时按当前帧处理
        pass
    return image


def _has_alpha(image) -> bool:
    """透明通道判定：显式 alpha 模式，或调色板自带 ``transparency`` 键。"""
    if image.mode in _ALPHA_MODES:
        return True
    try:
        return "transparency" in image.info
    except Exception:  # pragma: no cover - 极端格式取不到 info
        return False


def _flatten_on_white(image):
    """透明区域合成白底（``keep_alpha=False`` 的默认行为，契约 §3.4）。"""
    rgba = image if image.mode == "RGBA" else image.convert("RGBA")
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.split()[-1])
    return background


def _resize_to_long_edge(image, target_long_edge: int):
    """按目标长边等比缩放；**只缩不放**，长边未超过目标时原样返回。"""
    width, height = image.size
    long_edge = max(width, height)
    if target_long_edge <= 0 or long_edge <= target_long_edge:
        return image, long_edge
    ratio = target_long_edge / float(long_edge)
    new_size = (
        max(1, int(round(width * ratio))),
        max(1, int(round(height * ratio))),
    )
    if new_size == (width, height):
        return image, long_edge
    return image.resize(new_size, Image.Resampling.LANCZOS), max(new_size)


def _encode(image, image_format: str, quality: int) -> bytes | None:
    """编码为字节；失败返回 ``None``（由调用方按 ``FAIL_ERROR`` 记录）。"""
    buffer = io.BytesIO()
    if image_format == "JPEG":
        image.save(buffer, format="JPEG", quality=int(quality), optimize=True, progressive=False)
    else:
        image.save(buffer, format="PNG", optimize=True)
    payload = buffer.getvalue()
    return payload or None


def _quality_ladder(start: int) -> tuple[int, ...]:
    """质量阶梯：``max(40, q0 - 10*i)``（i=0,1,2），去重后单调不增。"""
    values: list[int] = []
    for step in QUALITY_STEPS:
        value = max(QUALITY_FLOOR, min(95, int(start) - step))
        if not values or value < values[-1]:
            values.append(value)
    return tuple(values)


def compress_reference(
    image: ImageInput,
    config: ReferenceCompressConfig,
    *,
    clock: Callable[[], float] | None = None,
) -> CompressOutcome:
    """压缩**一张**参考图，返回"要发出去的副本"与诊断数据。

    调用前必须确认 ``config.enable``；本函数不判开关（开关语义在
    :func:`compress_all`，关闭时连本函数都不会被调用，保证零解码）。
    """
    now = clock or time.perf_counter
    started = now()
    data = image.data or b""
    in_bytes = len(data)

    def _passthrough(*, skipped: str = "", failed: str = "", detail: str = "") -> CompressOutcome:
        return CompressOutcome(
            image=image,
            skipped=skipped,
            failed=failed,
            in_bytes=in_bytes,
            out_bytes=in_bytes,
            elapsed_ms=int((now() - started) * 1000),
            detail=detail[:_MAX_DETAIL_CHARS],
        )

    if not data:
        return _passthrough(skipped=SKIP_UNDER_MIN_BYTES)
    if in_bytes < int(config.min_bytes):
        return _passthrough(skipped=SKIP_UNDER_MIN_BYTES)

    try:
        opened = _open_first_frame(data)
    except _TooManyPixels:
        return _passthrough(skipped=SKIP_TOO_MANY_PIXELS)
    except Exception as exc:  # noqa: BLE001 - 任何解码失败都降级为原图直发
        return _passthrough(failed=FAIL_UNDECODABLE, detail=f"{type(exc).__name__}: {exc}")

    try:
        width, height = opened.size
        if int(width) * int(height) > int(config.max_pixels):
            try:
                opened.close()
            except Exception:  # pragma: no cover - 关闭失败无副作用
                pass
            return _passthrough(skipped=SKIP_TOO_MANY_PIXELS)
        try:
            normalized = ImageOps.exif_transpose(opened)
            normalized.load()
        except Exception as exc:  # noqa: BLE001
            return _passthrough(failed=FAIL_UNDECODABLE, detail=f"{type(exc).__name__}: {exc}")
        finally:
            try:
                if normalized is not opened:
                    opened.close()
            except Exception:  # pragma: no cover
                pass

        has_alpha = _has_alpha(normalized)
        if has_alpha and config.keep_alpha:
            base = normalized if normalized.mode == "RGBA" else normalized.convert("RGBA")
            image_format, mime_type, suffix = "PNG", PNG_MIME_TYPE, _PNG_SUFFIX
        elif has_alpha:
            base = _flatten_on_white(normalized)
            image_format, mime_type, suffix = "JPEG", JPEG_MIME_TYPE, _JPEG_SUFFIX
        else:
            base = normalized if normalized.mode == "RGB" else normalized.convert("RGB")
            image_format, mime_type, suffix = "JPEG", JPEG_MIME_TYPE, _JPEG_SUFFIX
    except Exception as exc:  # noqa: BLE001
        return _passthrough(failed=FAIL_UNDECODABLE, detail=f"{type(exc).__name__}: {exc}")

    long_edge = max(base.size)
    max_edge = max(0, int(config.max_edge))
    # 尺寸档作用在"已按 max_edge 收口后的长边"上（契约 §3.5）：先 clamp 再乘系数。
    # 若先乘再 clamp，3000px 源图的 1.0/0.85/0.7225 三档会被 max_edge 截成同一个长边，
    # 尺寸阶梯等于没生效。
    base_long = min(long_edge, max_edge) if max_edge > 0 else long_edge
    scales = SCALE_STEPS if max_edge > 0 else (1.0,)
    qualities = _quality_ladder(config.jpeg_quality) if image_format == "JPEG" else (0,)
    best: tuple[int, bytes, int, int] | None = None  # (bytes, payload, quality, edge)
    best_small: tuple[int, bytes, int, int] | None = None
    encodes = 0
    try:
        for scale in scales:
            target_long = (
                base_long if scale == 1.0 else max(1, int(round(base_long * scale)))
            )
            target_long = min(target_long, long_edge)  # 绝不放大
            candidate, candidate_edge = _resize_to_long_edge(base, target_long)
            for quality in qualities:
                if encodes >= MAX_ENCODES:
                    break
                try:
                    payload = _encode(candidate, image_format, quality)
                except Exception as exc:  # noqa: BLE001
                    # 任一编码异常立即回原图（CORE-04）：不保留前几轮已经产生的
                    # 有损候选，避免"半压缩"产物带着异常静默发出去。
                    return _passthrough(
                        failed=FAIL_ERROR, detail=f"{type(exc).__name__}: {exc}"
                    )
                encodes += 1
                if payload is None:
                    # 编码器返回空载荷同样算失败，语义与抛异常一致。
                    return _passthrough(failed=FAIL_ERROR, detail="编码结果为空")
                size = len(payload)
                if size <= in_bytes and (best is None or size < best[0]):
                    best = (size, payload, int(quality), candidate_edge)
                if size <= int(config.target_bytes) and size <= in_bytes:
                    # 只有"既达标又不膨胀"才算 small：比原图还大的候选一律不算
                    # （否则会把 10 KB 的源图换成 20 KB 的重新编码，违反 §3.5）。
                    best_small = (size, payload, int(quality), candidate_edge)
                    break
            if best_small is not None:
                break
    except MemoryError:  # pragma: no cover - 极端尺寸
        return _passthrough(failed=FAIL_ERROR, detail="MemoryError")
    except Exception as exc:  # noqa: BLE001
        return _passthrough(failed=FAIL_ERROR, detail=f"{type(exc).__name__}: {exc}")

    chosen = best_small if best_small is not None else best
    if chosen is None:
        return _passthrough(skipped=SKIP_NO_GAIN)

    size, payload, quality, edge = chosen
    target_unmet = size > int(config.target_bytes)
    return CompressOutcome(
        image=ImageInput(
            data=payload,
            mime_type=mime_type,
            filename=output_filename(image.filename, suffix),
        ),
        changed=payload != data,
        in_bytes=in_bytes,
        out_bytes=size,
        edge=int(edge),
        quality=int(quality),
        target_unmet=bool(target_unmet),
        elapsed_ms=int((now() - started) * 1000),
    )


def ingest_reference(
    data: bytes,
    mime_type: str,
    filename: str,
    config: ReferenceCompressConfig,
    *,
    clock: Callable[[], float] | None = None,
) -> tuple[bytes, str, str, CompressOutcome]:
    """**唯一公共入口**：把一份参考图字节变成"要发往 provider 的副本"。

    所有入口（事件图片、``data:``、``base64://``、http(s)、引用回复、deferred、
    Persona 磁盘图、ref-upload 之后的再次使用）最终都收敛到这一处，因此压缩行为
    只有一份实现、一份判定（契约 rev2 §4）。返回值与入参一一对应：

    Returns:
        ``(bytes, mime_type, filename, outcome)``。降级/跳过时前三项与原值逐字段相等。
    """
    outcome = compress_reference(
        ImageInput(data=data, mime_type=mime_type, filename=filename),
        config,
        clock=clock,
    )
    return outcome.image.data, outcome.image.mime_type, outcome.image.filename, outcome


def compress_all(
    images: list[ImageInput],
    config: ReferenceCompressConfig,
    *,
    info: Callable[[str], None] | None = None,
    debug: Callable[[str], None] | None = None,
    warn: Callable[[str], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[list[ImageInput], dict[str, Any] | None]:
    """对一组图（显式在前、Persona 在后）逐张压缩并汇总诊断。

    Returns:
        ``(要发出去的图, runtime 统计)``。关闭、Pillow 缺失或全部原图直发时统计为
        ``None``——**调用方不得在此情况下建 ``task.runtime["reference_compress"]``**
        （契约 §3.9 / §3.12）。
    """
    global _missing_pillow_warned
    if not config.enable:
        return list(images), None
    if not available():
        if not _missing_pillow_warned:
            _missing_pillow_warned = True
            if warn is not None:
                warn(MISSING_PILLOW_WARNING)
        return list(images), None

    stats: dict[str, Any] = {
        "in_bytes": 0,
        "out_bytes": 0,
        "compressed": 0,
        "skipped": [],
        "failed": [],
        "target_unmet": False,
    }
    outgoing: list[ImageInput] = []
    for index, image in enumerate(images, start=1):
        outcome = compress_reference(image, config, clock=clock)
        stats["in_bytes"] += int(outcome.in_bytes)
        stats["out_bytes"] += int(outcome.out_bytes)
        if outcome.changed:
            stats["compressed"] += 1
        if outcome.target_unmet:
            stats["target_unmet"] = True
        if outcome.skipped:
            stats["skipped"].append(
                {"index": index, "reason": outcome.skipped, "bytes": int(outcome.in_bytes)}
            )
            if debug is not None:
                debug(SKIP_TEMPLATE.format(reason=outcome.skipped, bytes=int(outcome.in_bytes)))
        elif outcome.failed:
            stats["failed"].append({"index": index, "reason": outcome.failed})
            if warn is not None:
                detail = outcome.detail or outcome.failed
                warn(FALLBACK_WARNING_TEMPLATE.format(detail=detail))
        if outcome.changed:
            if info is not None:
                line = INFO_TEMPLATE.format(
                    in_bytes=int(outcome.in_bytes),
                    out_bytes=int(outcome.out_bytes),
                    edge=int(outcome.edge),
                    quality=int(outcome.quality),
                    elapsed=int(outcome.elapsed_ms),
                )
                if outcome.target_unmet:
                    line += INFO_UNMET_SUFFIX
                info(line)
        outgoing.append(outcome.image)

    if stats["compressed"] == 0 and not stats["skipped"] and not stats["failed"]:
        # 全组都没有可压缩的对象（例如空列表）：不建 runtime 键。
        return outgoing, None
    return outgoing, stats
