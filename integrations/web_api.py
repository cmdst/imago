from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from typing import Any

from quart import jsonify, request

from ..core.config import API_TYPES, load_config
from ..core.errors import DuplicateImage

try:  # v1.3.0 共享形状源（后端独占实现）；旧库上导入失败时降级为只读空目录。
    from ..core import params as core_params
except ImportError:  # pragma: no cover - 仅在未同步 v1.3.0 core 时触发
    core_params = None


def _ok(message: str = "ok", **data):
    return jsonify({"success": True, "ok": True, "message": message, **data})


def _err(
    message: str,
    status: int = 400,
    *,
    code: str = "",
    field: str = "",
    index: int | None = None,
):
    error: dict[str, Any] = {"code": code or "invalid_request", "message": message}
    if field:
        error["field"] = field
    if index is not None:
        error["index"] = index
    return jsonify({"success": False, "ok": False, "message": message, "error": error}), status


# 单次上传 Persona 参考图的数量上限（每张仍有 max_upload_bytes 大小限制）。
MAX_UPLOAD_IMAGES = 20

_MISSING = object()

# 节点条目的已知键（schema 形状）。保存时按“缺席=保留 / 出现=按值写入”处理，
# 旧条目里不在表内的键原样保留，避免升级/降级时吞字段。
PROVIDER_FIELDS = (
    "id",
    "api_type",
    "enabled",
    "base_url",
    "api_keys",
    "astrbot_provider_id",
    "model",
    "available_models",
    "reference_image_limit",
    "default_size",
    "timeout",
    "stream",
    "enable_proxy",
    "fallback_order",
    "thinking",
    "custom_params",
)

# 遮挡提示词的默认文案（字典 §6）；配置里存空串时由运行时回退到它。
DEFAULT_OCCLUSION_PROMPT = (
    "在你认为的敏感部位用滑稽表情挡住；只做不透明覆盖，不要描述或还原被遮挡内容。"
)

# 两个新配置组的展示/校验描述。字段表是 WebUI 与 Web API 的唯一形状源
# （catalog.groups 由它生成），前端不复制任何字段名。
GROUP_DEFS: dict[str, dict[str, Any]] = {
    "reference_fallback_config": {
        "payload": "reference_fallback",
        "label": "参考图文字降级",
        "description": "当节点明确拒绝参考图时，改用一次纯文字请求重绘；默认关闭，关闭时不产生任何额外调用。",
        "fields": [
            {
                "key": "enable",
                "label": "参考图被拒时改走文字重绘",
                "control": "switch",
                "type": "bool",
                "default": False,
                "help": "只在节点明确拒绝图片输入时触发（400/413/415/422 + 图片相关特征）；超时、429、401、未知 400 都不触发。",
            },
            {
                "key": "max_extra_attempts",
                "label": "每任务最多额外生成次数",
                "control": "number",
                "type": "int",
                "default": 1,
                "min": 0,
                "max": 1,
                "help": "硬上限 1；0 = 只记录拒绝事件，不再重试。",
            },
            {
                "key": "caption_max_chars",
                "label": "描述长度上限（字符）",
                "control": "number",
                "type": "int",
                "default": 200,
                "min": 80,
                "max": 400,
                "help": "与“外观摘要”的 400 字上限相互独立。",
            },
            {
                "key": "vision_timeout_seconds",
                "label": "识图超时（秒）",
                "control": "number",
                "type": "int",
                "default": 20,
                "min": 5,
                "max": 60,
                "help": "单次识图调用上限，且必须小于任务剩余预算。",
            },
            {
                "key": "notify_user",
                "label": "降级时在结果消息里告知用户",
                "control": "switch",
                "type": "bool",
                "default": True,
                "help": "默认告知，避免用户误以为参考图仍然生效。",
            },
        ],
    },
    "experimental_reference_config": {
        "payload": "experimental_reference",
        "label": "实验：参考图被拒后先遮挡再重绘",
        "description": (
            "拒绝后的降级阶段，会把参考原图发给所选遮挡节点做遮挡编辑，再把遮挡产物与对应描述提交给原目标节点；"
            "只有遮挡产物和对应描述会提交给原目标。遮挡编辑是额外的一次生图调用（费用另计）；"
            "不做任何本地图像处理。依赖「参考图文字降级」开启。"
        ),
        "depends_on": {
            "payload": "reference_fallback",
            "field": "enable",
            "message": "实验遮挡依赖「参考图文字降级」：请先开启该组，实验功能才会生效。",
        },
        "fields": [
            {
                "key": "enable",
                "label": "实验：参考图被拒后先遮挡再重绘",
                "control": "switch",
                "type": "bool",
                "default": False,
                "help": "关闭时代码路径不可达（零文件、零额外调用）；开启但降级关闭 → 不生效。",
            },
            {
                "key": "occluder_provider_id",
                "label": "遮挡编辑节点",
                "control": "provider_select",
                "type": "str",
                "default": "",
                "help": "只列「已启用且配置完整」的节点；为空、不存在或已停用时不编辑，也不猜、不换节点。",
            },
            {
                "key": "occlusion_prompt",
                "label": "遮挡提示词",
                "control": "textarea",
                "type": "str",
                "default": DEFAULT_OCCLUSION_PROMPT,
                "max_length": 500,
                "help": "实际发送时会追加固定后缀「只输出图片，不要文字说明。」；清空 = 使用默认提示词。",
            },
            {
                "key": "max_images",
                "label": "最多处理图片数（实验成本上限）",
                "control": "number",
                "type": "int",
                "default": 1,
                "min": 1,
                "max": 3,
                "help": "这是实验遮挡的成本上限，不是删图许可：实际参考图数量超过该值时不调用遮挡编辑（技术性拒绝改走纯文字降级）。",
            },
            {
                "key": "timeout_seconds",
                "label": "单张遮挡编辑超时（秒）",
                "control": "number",
                "type": "int",
                "default": 20,
                "min": 5,
                "max": 60,
                "help": "实际取 min(本值, 任务剩余预算 - 15 秒)。",
            },
            {
                "key": "caption_max_chars",
                "label": "产物摘要长度上限（字符）",
                "control": "number",
                "type": "int",
                "default": 200,
                "min": 80,
                "max": 400,
                "help": "摘要只描述产物中未被遮挡的部分。",
            },
        ],
    },
}


def _line_items(value) -> list[str]:
    values = value if isinstance(value, list) else str(value or "").splitlines()
    return [str(item).strip() for item in values if str(item).strip()]


def _safe_int(value, default: int, minimum: int | None = None) -> int:
    """配置值转整数并兜底：手工改坏（非数字/null）时回退默认，不抛 500。"""
    try:
        result = int(value or default)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    return result


def _as_bool(value, default: bool) -> tuple[bool, bool]:
    """布尔化：返回 ``(值, 是否合法)``；``"false"``/``"0"`` 等字符串按假处理。"""
    if isinstance(value, bool):
        return value, True
    if value is None:
        return default, False
    if isinstance(value, (int, float)):
        return bool(value), True
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True, True
        if text in {"false", "0", "no", "off", ""}:
            return False, True
    return default, False


def _int_checked(value, minimum: int | None = None, maximum: int | None = None) -> tuple[int | None, bool]:
    if isinstance(value, bool):
        return None, False
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None, False
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result, True


def _text(value) -> str:
    return "" if value is None else str(value)


def _catalog() -> dict[str, Any]:
    """把后端 ``core.params`` 的共享形状整理成 GET 的 ``catalog``。"""
    catalog: dict[str, Any] = {"version": 1, "api_types": []}
    if core_params is None:
        catalog["thinking_values"] = []
        catalog["thinking_labels"] = {}
        catalog["groups"] = _groups_meta()
        return catalog
    builder = getattr(core_params, "build_catalog", None)
    if callable(builder):
        try:
            built = builder()
            if isinstance(built, Mapping):
                catalog.update(built)
        except Exception:  # pragma: no cover - 共享形状损坏时不让页面整体 500
            pass
    catalog.setdefault("version", 1)
    catalog.setdefault("api_types", [])
    catalog["thinking_values"] = [str(item) for item in getattr(core_params, "THINKING_VALUES", ())]
    catalog["thinking_labels"] = dict(getattr(core_params, "THINKING_LABELS", {}) or {})
    catalog["groups"] = _groups_meta()
    return catalog


def _api_type_entries() -> list[dict[str, Any]]:
    return [entry for entry in _catalog().get("api_types", []) if isinstance(entry, Mapping)]


def _api_type_set() -> set[str]:
    values = {str(entry.get("value", "")) for entry in _api_type_entries()}
    return {value for value in values if value} or set(API_TYPES)


def _api_type_meta(api_type: str) -> dict[str, Any]:
    for entry in _api_type_entries():
        if str(entry.get("value", "")) == api_type:
            return dict(entry)
    return {"value": api_type, "label": api_type, "param_mode": "ignored", "fields": [],
            "capabilities": {"quality": "none", "thinking": "none", "references": "none"}}


def _parse_params(text: str, api_type: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if core_params is None:
        return {}, []
    parser = getattr(core_params, "parse_custom_params", None)
    if not callable(parser):
        return {}, []
    try:
        params, errors = parser(text, api_type)
    except Exception:  # pragma: no cover - 解析器异常不应让 GET 失败
        return {}, [{"line": 0, "key": "", "reason": "parse_failed", "message": "参数解析失败"}]
    return dict(params or {}), [dict(item) for item in (errors or []) if isinstance(item, Mapping)]


def _groups_meta() -> list[dict[str, Any]]:
    groups = []
    for name, definition in GROUP_DEFS.items():
        meta = {
            "key": name,
            "payload": definition["payload"],
            "label": definition["label"],
            "description": definition["description"],
            "fields": [dict(field) for field in definition["fields"]],
        }
        if definition.get("depends_on"):
            meta["depends_on"] = dict(definition["depends_on"])
        groups.append(meta)
    return groups


def _group_by_payload(payload: str) -> tuple[str, dict[str, Any]] | tuple[None, None]:
    for name, definition in GROUP_DEFS.items():
        if definition["payload"] == payload:
            return name, definition
    return None, None


def _coerce_group_value(
    field: dict[str, Any],
    value,
    *,
    strict: bool,
    provider_ids: set[str] | None = None,
) -> tuple[Any, bool]:
    """按字段声明转换组值；strict=True 时非法值返回 ok=False（字段级拒绝）。

    ``provider_select`` 字段额外要求：空串允许；非空时必须是本次保存后
    「已启用且配置完整」的节点 id，否则字段级拒绝并保留原值。
    """
    kind = field.get("type", "str")
    if kind == "bool":
        converted, ok = _as_bool(value, bool(field.get("default", False)))
        return (converted, True) if ok or not strict else (None, False)
    if kind == "int":
        converted, ok = _int_checked(value, field.get("min"), field.get("max"))
        return (converted, True) if ok else (None, False)
    if kind == "float":
        try:
            converted = float(value)
        except (TypeError, ValueError):
            return (None, False) if strict else (field.get("default"), True)
        if field.get("min") is not None:
            converted = max(float(field["min"]), converted)
        if field.get("max") is not None:
            converted = min(float(field["max"]), converted)
        return converted, True
    text = _text(value)
    limit = field.get("max_length")
    if limit:
        text = text[: int(limit)]
    options = field.get("options")
    if options and text not in options:
        return (None, False) if strict else (str(field.get("default", "")), True)
    if field.get("control") == "provider_select" and text and strict:
        if provider_ids is not None and text not in provider_ids:
            return None, False
    return text, True


def _group_values_from(raw: Mapping[str, Any] | None, definition: Mapping[str, Any]) -> dict[str, Any]:
    stored = dict(raw or {})
    values: dict[str, Any] = {}
    for field in definition["fields"]:
        value, ok = _coerce_group_value(field, stored.get(field["key"], field["default"]), strict=False)
        values[field["key"]] = value if ok else field["default"]
    return values


def _group_warnings(
    name: str,
    definition: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    provider_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    warnings = []
    for field in definition["fields"]:
        key = field["key"]
        if key not in payload:
            continue
        _value, ok = _coerce_group_value(field, payload[key], strict=True, provider_ids=provider_ids)
        if not ok:
            warnings.append(
                {
                    "code": "param_invalid_value",
                    "key": f"{name}.{key}",
                    "message": f"{definition['label']}的「{field['label']}」取值无效，已保留原值。",
                }
            )
    return warnings


def _provider_complete(item: Mapping[str, Any]) -> bool:
    """节点是否配置完整（与 ``core.config.load_config`` 的判据一致）。"""
    if not str(item.get("id", "")).strip():
        return False
    api_type = str(item.get("api_type", ""))
    if api_type == "astrbot_native":
        return bool(str(item.get("astrbot_provider_id", "")).strip())
    return bool(str(item.get("base_url", "")).strip() and _line_items(item.get("api_keys", [])))


def _provider_enabled(item: Mapping[str, Any]) -> bool:
    value, ok = _as_bool(item.get("enabled", True), True)
    return value if ok else True


class PageAPI:
    def __init__(self, plugin):
        self.plugin = plugin

    # ------------------------------------------------------------------ 人设
    async def personas(self):
        try:
            return _ok(personas=[{"id": value} for value in await self.plugin.list_persona_ids()])
        except Exception:
            return _err("无法读取 Persona 列表", 500)

    async def detail(self):
        persona_id = str(request.args.get("persona_id", "")).strip()
        if not persona_id or len(persona_id) > 256:
            return _err("persona_id 无效")
        try:
            prompt = await self.plugin.get_persona_prompt(persona_id)
            if not prompt:
                return _err("Persona 不存在或 prompt 为空", 404)
            item = self.plugin.store.get_summary(persona_id, prompt)
            return _ok(
                persona_id=persona_id,
                prompt=prompt,
                summary=(item or {}).get("summary", ""),
                summary_manual=bool((item or {}).get("manual", False)),
                references=self.plugin.store.list_references(persona_id),
            )
        except Exception:
            return _err("无法读取 Persona 素材", 500)

    async def upload(self):
        try:
            body = await request.get_json(force=True)
            persona_id = str((body or {}).get("persona_id", "")).strip()
            if not await self.plugin.get_persona_prompt(persona_id):
                return _err("Persona 不存在", 404)
            images = (body or {}).get("images", [])
            if not isinstance(images, list) or not images:
                return _err("请选择至少一张图片")
            if len(images) > MAX_UPLOAD_IMAGES:
                return _err(f"单次最多上传 {MAX_UPLOAD_IMAGES} 张图片")
            added = []
            for image in images:
                data = base64.b64decode(str(image.get("data", "")), validate=True)
                added.append(self.plugin.store.add_reference(persona_id, data, str(image.get("mime_type", ""))))
            return _ok("上传成功", references=added)
        except DuplicateImage as exc:
            return _err(str(exc), 409)
        except (ValueError, binascii.Error):
            return _err("上传数据或图片格式无效")
        except Exception:
            return _err("上传失败", 500)

    async def delete(self):
        try:
            body = await request.get_json(force=True)
            self.plugin.store.delete_reference(str((body or {}).get("persona_id", "")), str((body or {}).get("filename", "")))
            return _ok("已删除")
        except (ValueError, FileNotFoundError):
            return _err("文件不存在或请求不安全", 404)
        except Exception:
            return _err("删除失败", 500)

    async def preview(self):
        try:
            path = self.plugin.store.reference_path(str(request.args.get("persona_id", "")), str(request.args.get("filename", "")))
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}.get(path.suffix.lower(), "image/png")
            encoded = base64.b64encode(path.read_bytes()).decode()
            return _ok(mime_type=mime, base64_data=encoded, data_url=f"data:{mime};base64,{encoded}")
        except (ValueError, FileNotFoundError):
            return _err("图片不存在", 404)
        except Exception:
            return _err("无法读取图片", 500)

    async def rebuild(self):
        try:
            body = await request.get_json(force=True)
            persona_id = str((body or {}).get("persona_id", "")).strip()
            reference_names = (body or {}).get("reference_names", [])
            if not isinstance(reference_names, list):
                return _err("参考图选择格式无效")
            summary = await self.plugin.rebuild_summary(persona_id, reference_names)
            return _ok("已重建", summary=summary)
        except Exception:
            return _err("无法重建外观摘要；请确认 Persona Prompt 和副脑 Provider 已配置", 500)

    async def set_summary(self):
        try:
            body = await request.get_json(force=True)
            persona_id = str((body or {}).get("persona_id", "")).strip()
            summary = str((body or {}).get("summary", "")).strip()
            prompt = await self.plugin.get_persona_prompt(persona_id)
            if not prompt:
                return _err("Persona 不存在", 404)
            item = self.plugin.store.set_summary(persona_id, prompt, summary, manual=True)
            return _ok("手工外观摘要已保存", summary=item["summary"])
        except ValueError as exc:
            return _err(str(exc))
        except Exception:
            return _err("无法保存外观摘要", 500)

    # ------------------------------------------------------------------ 节点
    def _chat_providers(self) -> list[dict[str, str]]:
        items = []
        for provider in self.plugin.context.get_all_providers():
            try:
                meta = provider.meta()
                items.append({"id": str(meta.id), "model": str(meta.model or ""), "type": str(meta.type)})
            except Exception:
                continue
        return items

    def _snapshot(self) -> dict[str, Any]:
        raw_items = self.plugin.raw_config.get("providers", []) or []
        configured: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(raw_items):
            if not isinstance(item, Mapping):
                continue
            provider_id = str(item.get("id", "")).strip()
            if not provider_id:
                provider_id = f"未命名节点 {index + 1}"
            api_type = str(item.get("api_type", ""))
            meta = _api_type_meta(api_type)
            raw_params = str(item.get("custom_params", "") or "")
            params, param_errors = _parse_params(raw_params, api_type)
            keys = _line_items(item.get("api_keys", []))
            enabled = _provider_enabled(item)
            configured.append(
                {
                    "id": provider_id,
                    "api_type": api_type,
                    "enabled": enabled,
                    "thinking": str(item.get("thinking", "") or ""),
                    "custom_params": raw_params,
                    "params": params,
                    "param_errors": param_errors,
                    "param_mode": str(meta.get("param_mode", "ignored")),
                    "capabilities": dict(meta.get("capabilities") or {}),
                    "model": str(item.get("model", "")),
                    "reference_image_limit": _safe_int(item.get("reference_image_limit"), 0, 0),
                    "available_models": [str(value) for value in (item.get("available_models", []) or [])],
                    "base_url": str(item.get("base_url", "")),
                    "api_keys_configured": bool(keys),
                    "default_size": str(item.get("default_size", "1024x1024")),
                    "timeout": _safe_int(item.get("timeout"), 180, 10),
                    "astrbot_provider_id": str(item.get("astrbot_provider_id", "")).strip(),
                    "stream": bool(item.get("stream", False)),
                    "enable_proxy": bool(item.get("enable_proxy", False)),
                    "fallback_order": _safe_int(item.get("fallback_order"), 0),
                    "complete": _provider_complete(item),
                    "primary": False,
                    "primary_disabled": False,
                }
            )
            seen.add(provider_id)

        stored_primary = str(self.plugin.store.get_primary_provider_id() or "").strip()
        candidates = sorted(
            enumerate(configured),
            key=lambda pair: (_safe_int(pair[1].get("fallback_order"), 0), pair[0]),
        )
        usable = [entry for _index, entry in candidates if entry["enabled"] and entry["complete"]]
        usable_ids = {entry["id"] for entry in usable}
        # 冻结语义（captain 纠偏）：存储主节点仍然有效时优先；只有失效才按回退顺序补位。
        if stored_primary and stored_primary in usable_ids:
            effective = stored_primary
        else:
            effective = usable[0]["id"] if usable else ""
        for entry in configured:
            entry["primary"] = entry["id"] == effective
            entry["primary_disabled"] = bool(stored_primary and entry["id"] == stored_primary and stored_primary != effective)

        chat_providers = self._chat_providers()
        optimizer = self.plugin.raw_config.get("optimizer_config", {}) or {}
        vision_provider_id = str(optimizer.get("vision_provider_id", "")).strip()
        chat_ids = {item["id"] for item in chat_providers}
        fallback = _group_values_from(
            self.plugin.raw_config.get("reference_fallback_config"),
            GROUP_DEFS["reference_fallback_config"],
        )
        fallback["vision_provider_id"] = vision_provider_id
        fallback["vision_provider_ready"] = bool(vision_provider_id and vision_provider_id in chat_ids)
        experimental = _group_values_from(
            self.plugin.raw_config.get("experimental_reference_config"),
            GROUP_DEFS["experimental_reference_config"],
        )
        occluder = str(experimental.get("occluder_provider_id", "")).strip()
        experimental["occluder_ready"] = bool(occluder and occluder in usable_ids)
        # 派生：实验依赖普通降级开启；遮挡节点无效时即使开关打开也不会生效。
        experimental["dependency_ready"] = bool(fallback.get("enable") and experimental["occluder_ready"])
        return {
            "providers": configured,
            "catalog": _catalog(),
            "reference_fallback": fallback,
            "experimental_reference": experimental,
            "primary_provider_id": effective,
            "stored_primary_provider_id": stored_primary,
            "chat_providers": chat_providers,
            "optimizer_provider_id": str(optimizer.get("optimizer_provider_id", "")),
            "vision_provider_id": vision_provider_id,
        }

    async def providers(self):
        snapshot = self._snapshot()
        return _ok(data=snapshot, **snapshot)

    async def save_providers(self):
        try:
            body = await request.get_json(force=True)
        except Exception:
            return _err("请求体不是有效 JSON")
        if not isinstance(body, Mapping):
            return _err("节点配置格式无效")
        incoming = body.get("providers", _MISSING)
        if incoming is not _MISSING and not isinstance(incoming, list):
            return _err("节点配置格式无效")
        old_items = [item for item in (self.plugin.raw_config.get("providers", []) or []) if isinstance(item, Mapping)]
        old_by_id: dict[str, Mapping[str, Any]] = {}
        for item in old_items:
            provider_id = str(item.get("id", "")).strip()
            if provider_id:
                old_by_id[provider_id] = item
        warnings: list[dict[str, Any]] = []
        saved: list[dict[str, Any]] | None = None

        if incoming is not _MISSING:
            saved = []
            seen: set[str] = set()
            known_types = _api_type_set()
            thinking_values = {str(value) for value in getattr(core_params, "THINKING_VALUES", ())} if core_params else {"", "off", "low", "medium", "high", "max"}
            for index, item in enumerate(incoming):
                if not isinstance(item, Mapping):
                    return _err("节点配置格式无效", 400, code="invalid_request", index=index)
                provider_id = str(item.get("id", "")).strip()
                if not provider_id:
                    return _err("节点 ID 不能为空", 400, code="missing_field", field="id", index=index)
                if provider_id in seen:
                    return _err(f"节点 ID「{provider_id}」重复", 400, code="duplicate_id", field="id", index=index)
                old = old_by_id.get(provider_id, {})
                old_api_type = str(old.get("api_type", "")).strip()
                if "api_type" in item:
                    api_type = str(item.get("api_type", "")).strip()
                else:
                    api_type = old_api_type
                if api_type not in known_types:
                    return _err(
                        f"节点「{provider_id}」的接口类型无效",
                        400,
                        code="invalid_api_type",
                        field="api_type",
                        index=index,
                    )
                native = api_type == "astrbot_native"

                def take(key: str, fallback, *, as_text: bool = False):
                    if key not in item:
                        return fallback
                    value = item.get(key)
                    if as_text:
                        return _text(value)
                    return value

                def warn(code: str, key: str, message: str):
                    warnings.append({"code": code, "key": key, "node": provider_id, "message": message})

                # 布尔字段：出现即写入；非法值字段级拒绝（保留旧值）。
                enabled, ok = _as_bool(take("enabled", _provider_enabled(old)), _provider_enabled(old))
                if "enabled" in item and not ok:
                    warn("param_invalid_value", "enabled", f"节点「{provider_id}」的启用开关取值无效，已保留原值。")
                    enabled = _provider_enabled(old)
                enable_proxy, ok = _as_bool(take("enable_proxy", bool(old.get("enable_proxy", False))), bool(old.get("enable_proxy", False)))
                if "enable_proxy" in item and not ok:
                    warn("param_invalid_value", "enable_proxy", f"节点「{provider_id}」的代理开关取值无效，已保留原值。")
                    enable_proxy = bool(old.get("enable_proxy", False))
                if native:
                    stream, ok = _as_bool(take("stream", bool(old.get("stream", False))), bool(old.get("stream", False)))
                    if "stream" in item and not ok:
                        warn("param_invalid_value", "stream", f"节点「{provider_id}」的流式开关取值无效，已保留原值。")
                        stream = bool(old.get("stream", False))
                else:
                    # 切离原生：流式字段归零（不残留）。
                    stream = False

                # 整数字段：坏值字段级拒绝；越界按钳制表夹紧。
                ref_limit, ok = _int_checked(take("reference_image_limit", _safe_int(old.get("reference_image_limit"), 0, 0)), 0, None)
                if not ok:
                    if "reference_image_limit" in item:
                        warn("param_invalid_value", "reference_image_limit", f"节点「{provider_id}」的参考图上限不是有效整数，已保留原值。")
                    ref_limit = _safe_int(old.get("reference_image_limit"), 0, 0)
                timeout, ok = _int_checked(take("timeout", _safe_int(old.get("timeout"), 180, 10)), 10, None)
                if not ok:
                    if "timeout" in item:
                        warn("param_invalid_value", "timeout", f"节点「{provider_id}」的超时不是有效整数，已保留原值。")
                    timeout = _safe_int(old.get("timeout"), 180, 10)
                if "fallback_order" in item:
                    order, ok = _int_checked(item.get("fallback_order"))
                    fallback_order = order if ok else 0
                else:
                    fallback_order = _safe_int(old.get("fallback_order"), 0)

                thinking = str(old.get("thinking", "") or "")
                if "thinking" in item:
                    candidate = _text(item.get("thinking")).strip()
                    if candidate in thinking_values:
                        thinking = candidate
                    else:
                        warn("param_invalid_value", "thinking", f"节点「{provider_id}」的思考档位无效，已保留原值。")
                custom_params = _text(old.get("custom_params", ""))
                if "custom_params" in item:
                    candidate = item.get("custom_params")
                    if isinstance(candidate, str):
                        custom_params = candidate
                    else:
                        warn("param_invalid_value", "custom_params", f"节点「{provider_id}」的高级参数必须是文本，已保留原值。")

                astrbot_provider_id = str(item.get("astrbot_provider_id", old.get("astrbot_provider_id", ""))).strip() if native else ""
                if native and not astrbot_provider_id:
                    return _err(
                        f"原生节点「{provider_id}」必须选择 AstrBot Provider",
                        400,
                        code="native_missing_provider",
                        field="astrbot_provider_id",
                        index=index,
                    )

                submitted_keys = _line_items(item.get("api_keys", []))
                stored_keys = _line_items(old.get("api_keys", []))
                clear_keys, _clear_ok = _as_bool(item.get("clear_api_keys", False), False)
                if clear_keys:
                    api_keys: list[str] = []
                elif native:
                    api_keys = []
                elif submitted_keys:
                    api_keys = submitted_keys
                else:
                    api_keys = stored_keys

                extra = {key: value for key, value in old.items() if key not in PROVIDER_FIELDS and key != "__template_key"}
                entry = {
                    **extra,
                    "id": provider_id,
                    "api_type": api_type,
                    "enabled": enabled,
                    "base_url": str(take("base_url", old.get("base_url", ""), as_text=True)).strip(),
                    "api_keys": api_keys,
                    "model": str(take("model", old.get("model", ""), as_text=True)).strip(),
                    "available_models": _line_items(item["available_models"]) if "available_models" in item else _line_items(old.get("available_models", [])),
                    "reference_image_limit": ref_limit,
                    "default_size": str(take("default_size", old.get("default_size", "1024x1024"), as_text=True)).strip(),
                    "timeout": timeout,
                    "astrbot_provider_id": astrbot_provider_id,
                    "stream": stream,
                    "enable_proxy": enable_proxy,
                    "fallback_order": fallback_order,
                    "thinking": thinking,
                    "custom_params": custom_params,
                    "__template_key": "provider",
                }
                if not native and not entry["base_url"]:
                    warn("node_incomplete", "base_url", f"节点「{provider_id}」未填写接口地址，保存后仍不参与调用。")
                if not native and not entry["api_keys"]:
                    warn("node_incomplete", "api_keys", f"节点「{provider_id}」没有可用的 API Key，保存后仍不参与调用。")
                capability = str((_api_type_meta(api_type).get("capabilities") or {}).get("thinking", "none"))
                if thinking and capability == "none":
                    warn("thinking_ignored", "thinking", f"节点「{provider_id}」的思考档位会被保存，但 {api_type} 协议不会发送思考字段。")
                _params, param_errors = _parse_params(custom_params, api_type)
                for error in param_errors:
                    if str(error.get("reason", "")) == "unknown_for_type":
                        warnings.append(
                            {
                                "code": "param_unsupported_for_type",
                                "key": str(error.get("key", "")),
                                "api_type": api_type,
                                "node": provider_id,
                                "message": f"节点「{provider_id}」的参数「{error.get('key', '')}」不属于 {api_type}，会保留但不会发送。",
                            }
                        )
                saved.append(entry)
                seen.add(provider_id)

        primary_present = "primary_provider_id" in body
        stored_primary = str(self.plugin.store.get_primary_provider_id() or "").strip()
        primary = str(body.get("primary_provider_id", "")).strip() if primary_present else stored_primary
        if saved is not None and primary:
            saved_ids = {entry["id"] for entry in saved}
            if primary not in saved_ids and primary.partition("/")[0] not in saved_ids:
                return _err("主节点不在当前列表中", 400, code="missing_field", field="primary_provider_id")

        # 组配置：缺席=保留；出现=按描述表逐键写入（非法键只告警）。
        # 遮挡节点必须是「本次保存后」已启用且配置完整的节点。
        if saved is not None:
            group_provider_ids = {
                entry["id"] for entry in saved if entry.get("enabled") and _provider_complete(entry)
            }
        else:
            group_provider_ids = {
                str(item.get("id", "")).strip()
                for item in old_items
                if _provider_enabled(item) and _provider_complete(item)
            }
        merged_groups: dict[str, dict[str, Any]] = {}
        for name, definition in GROUP_DEFS.items():
            payload_key = definition["payload"]
            if payload_key not in body:
                continue
            payload = body.get(payload_key)
            if not isinstance(payload, Mapping):
                return _err(f"{definition['label']}配置格式无效", 400, code="invalid_request", field=payload_key)
            warnings.extend(_group_warnings(name, definition, payload, provider_ids=group_provider_ids))
            old_group = dict(self.plugin.raw_config.get(name) or {})
            for field in definition["fields"]:
                key = field["key"]
                if key not in payload:
                    continue
                value, ok = _coerce_group_value(field, payload[key], strict=True, provider_ids=group_provider_ids)
                if ok:
                    old_group[key] = value
            merged_groups[name] = old_group

        fallback_group = merged_groups.get("reference_fallback_config") or dict(
            self.plugin.raw_config.get("reference_fallback_config") or {}
        )
        experimental_group = merged_groups.get("experimental_reference_config") or dict(
            self.plugin.raw_config.get("experimental_reference_config") or {}
        )
        fallback_on, _ = _as_bool(fallback_group.get("enable", False), False)
        experimental_on, _ = _as_bool(experimental_group.get("enable", False), False)
        occluder_id = str(experimental_group.get("occluder_provider_id", "")).strip()
        if experimental_on and not fallback_on:
            warnings.append(
                {
                    "code": "experimental_without_fallback",
                    "key": "experimental_reference_config.enable",
                    "message": "实验遮挡已保存为开启，但「参考图文字降级」未开启，本轮不会生效。",
                }
            )
        if experimental_on and fallback_on and occluder_id and occluder_id not in group_provider_ids:
            warnings.append(
                {
                    "code": "occluder_unavailable",
                    "key": "experimental_reference_config.occluder_provider_id",
                    "message": f"遮挡编辑节点「{occluder_id}」已停用或配置不完整，实验遮挡不会生效。",
                }
            )

        optimizer_present = "optimizer_provider_id" in body or "vision_provider_id" in body
        old_optimizer = dict(self.plugin.raw_config.get("optimizer_config", {}) or {})
        new_optimizer = dict(old_optimizer)
        if "optimizer_provider_id" in body:
            new_optimizer["optimizer_provider_id"] = str(body.get("optimizer_provider_id", "")).strip()
        if "vision_provider_id" in body:
            new_optimizer["vision_provider_id"] = str(body.get("vision_provider_id", "")).strip()

        snapshot_before = {
            "providers": self.plugin.raw_config.get("providers"),
            "optimizer_config": self.plugin.raw_config.get("optimizer_config"),
            **{name: self.plugin.raw_config.get(name) for name in GROUP_DEFS},
        }
        try:
            if saved is not None:
                self.plugin.raw_config["providers"] = saved
            if optimizer_present:
                self.plugin.raw_config["optimizer_config"] = new_optimizer
            for name, group in merged_groups.items():
                self.plugin.raw_config[name] = group
            save_fn = getattr(self.plugin.raw_config, "save_config", None)
            if not callable(save_fn):
                raise RuntimeError("当前 AstrBot 配置对象不支持持久化")
            save_fn()
            if primary_present:
                self.plugin.store.set_primary_provider_id(primary)
        except Exception:
            # 原子性：写盘失败时恢复内存里的旧值，避免“页面显示已保存、盘上没写”。
            for key, value in snapshot_before.items():
                if value is None:
                    self.plugin.raw_config.pop(key, None)
                else:
                    self.plugin.raw_config[key] = value
            return _err("无法保存节点配置", 500)

        snapshot = self._snapshot()
        return _ok("节点配置已保存", data=snapshot, warnings=warnings, **snapshot)

    # ------------------------------------------------------------------ 额度
    async def quotas(self):
        try:
            policy = load_config(self.plugin.raw_config).quota
            return _ok(
                quotas=self.plugin.quota_store.list_rows(policy),
                quota_enabled=policy.enabled,
                daily_refresh_enabled=policy.daily_refresh_enabled,
                daily_quota_target=policy.daily_quota_target,
                checkin_enabled=policy.checkin_enabled,
                checkin_quota_min=policy.checkin_quota_min,
                checkin_quota_max=policy.checkin_quota_max,
            )
        except Exception:
            return _err("无法读取绘图额度", 500)

    async def save_quotas(self):
        try:
            body = await request.get_json(force=True)
            items = (body or {}).get("quotas", [])
            if not isinstance(items, list) or len(items) > 5000:
                return _err("额度列表格式无效")
            rows = self.plugin.quota_store.set_many(
                items,
                load_config(self.plugin.raw_config).quota,
            )
            return _ok("绘图额度已保存", quotas=rows)
        except (TypeError, ValueError) as exc:
            return _err(str(exc))
        except Exception:
            return _err("无法保存绘图额度", 500)

    async def set_primary_provider(self):
        # 目前 WebUI 走 providers/save 一并提交 primary_provider_id，本路由未被
        # 前端调用；保留作为独立 API 入口（命令 /imago_fusion provider-primary 同功能）。
        try:
            body = await request.get_json(force=True)
            provider_id = str((body or {}).get("provider_id", "")).strip()
            configured = [str(item.get("id", "")).strip() for item in (self.plugin.raw_config.get("providers", []) or []) if isinstance(item, Mapping)]
            if provider_id not in configured:
                return _err("图片节点不存在", 404)
            self.plugin.store.set_primary_provider_id(provider_id)
            return _ok("主图片节点已更新", primary_provider_id=provider_id)
        except Exception:
            return _err("无法保存主图片节点", 500)
