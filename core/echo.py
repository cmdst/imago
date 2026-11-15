"""严格回吐守卫（C-ECHO，v1.4.1 契约 §4）。

语义（一句话）：**模型把刚喂给它的参考图原样送了回来**时，这张"成品"不是新图，
既不能计入交付，也不能再花钱走增强/放大。

判定口径（v1.4.1 最终口径，**精确相等、无容差**）：

1. **同字节**：结果字节与本次 attempt 实际发出的某张参考图逐字节相同 → 拦。
2. **同像素**：EXIF 方向归一后**尺寸完全相同**，且把两侧都规范化成 RGBA 后，
   所有像素逐点完全相同 → 拦。

不满足以上两条一律放行；任何不确定（解码失败、Pillow 缺失、坏图、无法比较）
也放行，只把计数记进诊断——守卫**不能**变成新的失败源（契约 §4.3 / §4.10）。
刻意**不**使用 pHash、灰度缩略图 MAD、SSIM 之类近似度量：近似会把"人物局部微调"
误杀，而用户对"原样送回来"的判定要求是二值的。
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .models import ImageInput

try:  # pragma: no cover - 取决于运行环境
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]

#: 判定结论。
VERDICT_SAME = "same"
VERDICT_DIFFERS = "differs"
VERDICT_UNKNOWN = "unknown"

#: 唯一新增的任务级 marker（每任务最多一条，见 ``main._finish_task``）。
NO_CHANGE_MARKER = "no_change"

#: 全拦时的冻结文案（契约 §4.7，逐字节不得改写）。
ECHO_BLOCKED_TEXT = "这次没有生成新图，模型把参考图原样送了回来。请重试或换个节点。"

#: 像素级比较的资源上限（超出即 unknown 放行，不让守卫把任务拖死/吃爆内存）。
MAX_COMPARE_PIXELS = 40_000_000
MAX_COMPARE_BYTES = 64 * 1024 * 1024

#: 诊断行模板（INFO，契约 §4.9 的计数口径）。
ECHO_INFO_TEMPLATE = "回吐闸 检查={checked} 拦下={blocked} 放行={passed} 未知={unknown}"


def available() -> bool:
    """Pillow 是否可用；不可用时只能做字节级比较（像素级一律 unknown）。"""
    return Image is not None and ImageOps is not None


def _digest(data: bytes) -> bytes:
    return hashlib.sha256(data or b"").digest()


def normalized_pixels(data: bytes) -> tuple[int, int, bytes] | None:
    """把一张图规范化成 ``(宽, 高, RGBA 原始像素)``；失败返回 ``None``。

    规范化 = 取首帧 + ``exif_transpose`` + 「RGBA」+ 直接 ``tobytes()``。两侧都走
    同一条路径，因此"模式不同但视觉完全一致"（如 RGB 的 PNG vs JPEG）不会被误判为
    相同——逐像素相等要求解码后的通道值完全一致，这比"看起来一样"更严格，正是
    契约要的"精确"。
    """
    if not available() or not data:
        return None
    if len(data) > MAX_COMPARE_BYTES:
        return None
    try:
        with Image.open(io.BytesIO(data)) as opened:
            if int(getattr(opened, "n_frames", 1) or 1) > 1:
                opened.seek(0)
            if int(opened.size[0]) * int(opened.size[1]) > MAX_COMPARE_PIXELS:
                return None
            normalized = ImageOps.exif_transpose(opened)
            rgba = normalized if normalized.mode == "RGBA" else normalized.convert("RGBA")
            rgba.load()
            return (int(rgba.size[0]), int(rgba.size[1]), rgba.tobytes())
    except Exception:  # noqa: BLE001 - 坏图/不支持格式都算"不确定"
        return None


@dataclass(slots=True)
class EchoBaseline:
    """一次成功 attempt **实际发出的**参考图快照 + 懒解码缓存。

    ``references`` 必须来自 ``attempt_request.references``（压缩后的副本），而不是
    ``task.request.references`` 或 ``inputs/`` 里的原素材——基线错位会让"回吐"永远
    比不中（契约 §4.1 / §4.6）。
    """

    references: tuple[ImageInput, ...] = ()
    digests: tuple[bytes, ...] = ()
    _pixels: dict[int, tuple[int, int, bytes] | None] = field(default_factory=dict, repr=False)

    @classmethod
    def of(cls, references: Iterable[ImageInput]) -> "EchoBaseline":
        items = tuple(ref for ref in references if ref is not None)
        return cls(
            references=items,
            digests=tuple(_digest(ref.data) for ref in items),
        )

    def pixels_at(self, index: int) -> tuple[int, int, bytes] | None:
        if index not in self._pixels:
            self._pixels[index] = normalized_pixels(self.references[index].data)
        return self._pixels[index]


def classify(candidate: bytes, baseline: EchoBaseline) -> str:
    """判定一张成品图是否属于"原样回吐"。

    Returns:
        :data:`VERDICT_SAME` / :data:`VERDICT_DIFFERS` / :data:`VERDICT_UNKNOWN`。
    """
    if not baseline.references:
        return VERDICT_UNKNOWN
    if not candidate:
        return VERDICT_UNKNOWN
    digest = _digest(candidate)
    if any(digest == known for known in baseline.digests):
        return VERDICT_SAME
    if not available():
        return VERDICT_UNKNOWN

    candidate_pixels = normalized_pixels(candidate)
    if candidate_pixels is None:
        return VERDICT_UNKNOWN
    width, height, _ = candidate_pixels
    saw_decodable_reference = False
    for index in range(len(baseline.references)):
        reference = baseline.pixels_at(index)
        if reference is None:
            continue
        saw_decodable_reference = True
        if reference[0] != width or reference[1] != height:
            continue
        if reference[2] == candidate_pixels[2]:
            return VERDICT_SAME
    # 基线全部解码失败时，"不同"并不是事实——保守按不确定处理（放行 + 计数）。
    return VERDICT_DIFFERS if saw_decodable_reference else VERDICT_UNKNOWN


def dimensions_of(data: bytes) -> str:
    """诊断用尺寸串（``1024x768``）；读不出时返回 ``-``，绝不解码全图。"""
    if not available() or not data:
        return "-"
    try:
        with Image.open(io.BytesIO(data)) as opened:
            width, height = opened.size
            return f"{int(width)}x{int(height)}"
    except Exception:  # noqa: BLE001
        return "-"


def new_guard_state() -> dict:
    """``task.runtime["echo_guard"]`` 的初始形状（契约 §4.9）。"""
    return {
        "checked": 0,
        "blocked": 0,
        "unknown": 0,
        "passed": 0,
        "references_sent": 0,
        "blocked_files": [],
    }


def record(state: dict, verdict: str, *, filename: str, size: int, data: bytes = b"") -> None:
    """把一次判定写进 guard 状态（计数 + 被拦文件清单，不含任何图片内容）。"""
    state["checked"] = int(state.get("checked", 0)) + 1
    if verdict == VERDICT_SAME:
        state["blocked"] = int(state.get("blocked", 0)) + 1
        state.setdefault("blocked_files", []).append(
            {
                "file": str(filename or "")[:120],
                "size": int(size),
                "dims": dimensions_of(data),
            }
        )
    elif verdict == VERDICT_UNKNOWN:
        state["unknown"] = int(state.get("unknown", 0)) + 1
    # passed 与 INFO 行同源：一次检查要么拦下、要么放行（unknown 也算放行）。
    state["passed"] = passed_count(state)


def passed_count(state: dict) -> int:
    """放行数 = 检查 − 拦下（含"不确定"，它们都继续走后续链路）。"""
    return max(0, int(state.get("checked", 0)) - int(state.get("blocked", 0)))


def summarize(state: dict) -> str:
    """INFO 一行的最终形态。"""
    return ECHO_INFO_TEMPLATE.format(
        checked=int(state.get("checked", 0)),
        blocked=int(state.get("blocked", 0)),
        passed=passed_count(state),
        unknown=int(state.get("unknown", 0)),
    )


def blocked_all(state: dict, total_results: int) -> bool:
    """是否"本次所有成品图都被拦"（决定 no_output + 冻结文案）。"""
    blocked = int(state.get("blocked", 0))
    return blocked > 0 and blocked >= int(total_results)


__all__: Sequence[str] = (
    "ECHO_BLOCKED_TEXT",
    "ECHO_INFO_TEMPLATE",
    "NO_CHANGE_MARKER",
    "EchoBaseline",
    "VERDICT_DIFFERS",
    "VERDICT_SAME",
    "VERDICT_UNKNOWN",
    "MAX_COMPARE_BYTES",
    "MAX_COMPARE_PIXELS",
    "available",
    "blocked_all",
    "classify",
    "dimensions_of",
    "new_guard_state",
    "normalized_pixels",
    "passed_count",
    "record",
    "summarize",
)
