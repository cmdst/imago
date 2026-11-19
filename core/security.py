from __future__ import annotations

import json
import re
from pathlib import Path

# 1) 具名凭据字段（api_key/apikey/authorization/token/...）与 `key = value` 形式；
# 2) URL 查询参数里的裸 `key=`/`apiKey=`（Gemini Files API 与不少中转把凭据直接放在
#    查询串里，如 `?key=AIza...`）。旧版只认第 1 条，裸 `key=` 会原样出现在
#    `_provider_error`/`_decorate_error` 的文案里（这两处走 redact 而非 redact_debug，
#    没有整条 URL 的兜底替换）。
SENSITIVE = re.compile(
    r"(?i)(api[_-]?key|apikey|authorization|token|secret|password|\bkey)\s*[:=]\s*([^\s,;}]+)"
    r"|([?&](?:api[_-]?key|apikey|key|access_token)=)([^\s&,;}\"'<>]+)"
)
EXTRA_KEY = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,31}$")
# 除凭据/网络键外，还必须拦截会覆盖请求保留字段的键：n/model/size/prompt/count
# 可绕过 count 1-4 上限或覆盖节点配置，messages 可整体替换请求体；v1.3.0 再加图片/
# 参考图/蒙版类键（它们由任务模型与适配器管理，调用级参数不得注入）。
BLOCKED_EXTRA = {
    "url", "base_url", "authorization", "api_key", "token", "secret", "password",
    "timeout", "file", "path", "n", "model", "size", "prompt", "count", "messages",
    "image", "images", "image_url", "image_urls", "mask", "references",
    "input_image", "input_images",
}


def _mask(match: re.Match[str]) -> str:
    """掩码命中的凭据，保留参数名以便定位（``key=<redacted>`` / ``?key=<redacted>``）。

    Args:
        match: ``SENSITIVE`` 的匹配结果，命中具名凭据分支或 URL 查询参数分支。

    Returns:
        脱敏后的替换文本。
    """
    if match.group(3) is not None:
        return f"{match.group(3)}<redacted>"
    return f"{match.group(1)}=<redacted>"


def redact(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = re.sub(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+", "<image-data>", text)
    return SENSITIVE.sub(_mask, text)


def redact_debug(value: object) -> str:
    """用于 DEBUG 追踪的内容脱敏；完整保留提示词结构，不保留凭据、图片、URL 和长数字标识。"""
    text = redact(value)
    text = re.sub(r"https?://[^\s<>\]\[()\"']+", "<url>", text, flags=re.I)
    text = re.sub(r"(?<![\w.])\d{6,}(?![\w.])", "<id>", text)
    text = re.sub(r"(?:[A-Za-z]:[\\/]|/)(?:[^\s<>\"']+[\\/])+[^\s<>\"']*", "<path>", text)
    return text


_LABEL_URL = re.compile(r"https?://[^\s<>\]\[()\"']+", flags=re.I)
_LABEL_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)(?:[^\s<>\"']+[\\/])+[^\s<>\"']*")
_LABEL_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def safe_label(value: object, limit: int = 64) -> str:
    """把配置/调度标签投影成**有界、可记录**的单行文本（节点 id、模型名、api_type）。

    与 :func:`redact_debug` 的区别只在"长数字"一条规则：``redact_debug`` 会把
    ``...-20250901`` 这类版本号/模型别名替换成 ``<id>``，而成功记账必须能回答
    "这次到底是哪个模型出的图"，所以这里保留数字。其余口径一致并额外做两件事：

    1. 凭据与 ``data:image`` base64 掩码（复用 :func:`redact`），URL/本地路径替换；
    2. 控制字符压成空格、折叠连续空白、按 ``limit`` 截断——保证一条日志不会被
       换行或终端控制符切开，也不会因为超长标签污染整行。

    Args:
        value: 任意标签值；``None`` 视作空串（不写 ``None``/``null`` 到日志）。
        limit: 截断长度，默认 64（与既有 ``str(value)[:64]`` 的口径一致）。

    Returns:
        已脱敏、已截断的单行文本（可能为空串，调用方自行决定占位符）。
    """
    text = redact("" if value is None else str(value)).strip()
    text = _LABEL_URL.sub("<url>", text)
    text = _LABEL_PATH.sub("<path>", text)
    text = _LABEL_CONTROL.sub(" ", text)
    text = " ".join(text.split())
    return text[: max(0, int(limit))]


def safe_component(value: str) -> str:
    value = value.strip()
    if not value or value in {".", ".."}:
        raise ValueError("标识符为空或不安全")
    return value.encode("utf-8").hex()


def ensure_child(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("路径越界")
    return resolved


def parse_extra_params(text: str) -> dict[str, str]:
    import shlex
    tokens = shlex.split(text or "")
    if len(tokens) % 2:
        raise ValueError("附加参数必须使用 --key value")
    result: dict[str, str] = {}
    for index in range(0, len(tokens), 2):
        key_token, value = tokens[index:index + 2]
        if not key_token.startswith("--"):
            raise ValueError("附加参数必须使用 --key value")
        key = key_token[2:]
        if not EXTRA_KEY.fullmatch(key) or key.lower() in BLOCKED_EXTRA:
            raise ValueError(f"不允许的附加参数: {key}")
        result[key] = value
    return result
