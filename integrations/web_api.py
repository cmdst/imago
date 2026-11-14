from __future__ import annotations

import base64
import binascii
import copy
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
    **extra: Any,
):
    error: dict[str, Any] = {"code": code or "invalid_request", "message": message}
    if field:
        error["field"] = field
    if index is not None:
        error["index"] = index
    # 回滚结果等结构化诊断（FF-06）：不隐藏“磁盘/存储是否已回滚”，方便前端如实提示。
    error.update({key: value for key, value in extra.items() if value is not None})
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
        "description": (
            "当节点明确拒绝参考图时，把完整参考图集合改写成纯文字请求重绘；技术性拒绝最多额外 1 次，"
            "安全类拒绝直接停止；未知原因不猜测。默认关闭，关闭时不产生任何额外调用。"
        ),
        "fields": [
            {
                "key": "enable",
                "label": "参考图被拒时改走文字重绘",
                "control": "switch",
                "type": "bool",
                "default": False,
                "help": (
                    "优先使用结构化业务证据（响应体 code / finish_reason 等，HTTP 200 响应体内的业务拒绝也算）；"
                    "仅有状态码或文本特征时只当辅助证据；未知原因不猜测。超时、429、401 不触发。"
                ),
            },
            {
                "key": "max_extra_attempts",
                "label": "每任务最多额外生成次数",
                "control": "number",
                "type": "int",
                "default": 1,
                "min": 0,
                "max": 1,
                "help": "硬上限 1；0 = 只记录拒绝事件，不再重试；安全类拒绝即使还有剩余次数也直接停止。",
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
            "仅在拒绝后的降级阶段生效：把本次失败尝试的完整参考图集合发给另配的遮挡编辑节点，"
            "逐图编辑（最多 max_images 张，每笔按供应商单独计费），再对产物识图生成与遮挡图一致的描述；"
            "随后至多一次最终生成把遮挡产物与描述提交给原目标（原目标只收到遮挡产物和描述）。"
            "参考图数量超过上限、或遇到容量/安全限制时，按契约改纯文字或停止，不会悄悄丢弃部分参考图。"
            "参考原图会发送给所选遮挡节点；插件不做任何本地图像处理。依赖「参考图文字降级」开启。"
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
    # FF-R2-02：strict 模式下文本字段只接收真正的字符串；对象/数组/数字等
    # 一律字段级拒绝（保留旧值 + 由调用方给出该 key 的 warning），不做 repr 落盘。
    if strict and not isinstance(value, str):
        return None, False
    text = value if isinstance(value, str) else _text(value)
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
                    # FF-05：旧配置里可能是字符串 "false"/"0"；不能用 bool(raw)（非空字符串恒为 True）。
                    "stream": _as_bool(item.get("stream", False), False)[0],
                    "enable_proxy": _as_bool(item.get("enable_proxy", False), False)[0],
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

        deleted_raw = body.get("deleted_provider_ids", [])
        if deleted_raw is None:
            deleted_raw = []
        if not isinstance(deleted_raw, list) or any(not isinstance(value, str) for value in deleted_raw):
            return _err(
                "deleted_provider_ids 必须是节点 ID 列表",
                400,
                code="invalid_request",
                field="deleted_provider_ids",
            )
        declared_deleted = {value.strip() for value in deleted_raw if value.strip()}

        old_items = [item for item in (self.plugin.raw_config.get("providers", []) or []) if isinstance(item, Mapping)]
        old_by_id: dict[str, Mapping[str, Any]] = {}
        old_order: list[str] = []
        for item in old_items:
            provider_id = str(item.get("id", "")).strip()
            if provider_id and provider_id not in old_by_id:
                old_by_id[provider_id] = item
                old_order.append(provider_id)
        if declared_deleted - set(old_by_id):
            return _err(
                "要删除的节点不在当前配置里，无法确认身份",
                400,
                code="unknown_deleted_id",
                field="deleted_provider_ids",
            )

        warnings: list[dict[str, Any]] = []
        saved: list[dict[str, Any]] | None = None
        known_types = _api_type_set()
        thinking_values = (
            {str(value) for value in getattr(core_params, "THINKING_VALUES", ())}
            if core_params
            else {"", "off", "low", "medium", "high", "max"}
        )

        if incoming is _MISSING and not declared_deleted:
            saved = None
        else:
            updates: dict[str, dict[str, Any]] = {}
            new_ids: list[str] = []
            seen: set[str] = set()
            for index, item in enumerate(incoming if incoming is not _MISSING else []):
                if not isinstance(item, Mapping):
                    return _err("节点配置格式无效", 400, code="invalid_request", index=index)

                # FF-03/FF-04 / FF-R2-02：ID 与接口类型必须“存在且为合法文本”；
                # 缺失与非法都属于结构性错误，整单 400、两份持久化零写入。
                if "id" not in item:
                    return _err("节点缺少 ID", 400, code="missing_field", field="id", index=index)
                raw_id = item.get("id")
                if not isinstance(raw_id, str):
                    return _err("节点 ID 必须是文本", 400, code="invalid_id", field="id", index=index)
                provider_id = raw_id.strip()
                if not provider_id:
                    return _err("节点 ID 不能为空", 400, code="missing_field", field="id", index=index)
                if len(provider_id) > 128 or any(ord(char) < 32 for char in provider_id):
                    return _err(
                        "节点 ID 形状无效（最长 128 字符，且不能包含控制字符）",
                        400,
                        code="invalid_id",
                        field="id",
                        index=index,
                    )
                if provider_id in seen:
                    return _err(f"节点 ID「{provider_id}」重复", 400, code="duplicate_id", field="id", index=index)
                if provider_id in declared_deleted:
                    return _err(
                        f"节点「{provider_id}」同时出现在保存列表和删除列表里",
                        400,
                        code="conflicting_delete",
                        field="id",
                        index=index,
                    )

                old = old_by_id.get(provider_id, {})
                is_new = provider_id not in old_by_id
                old_api_type = str(old.get("api_type", "")).strip()
                if "api_type" not in item:
                    return _err(
                        f"节点「{provider_id}」缺少接口类型",
                        400,
                        code="missing_field",
                        field="api_type",
                        index=index,
                    )
                raw_type = item.get("api_type")
                if not isinstance(raw_type, str):
                    return _err(
                        f"节点「{provider_id}」的接口类型必须是文本",
                        400,
                        code="invalid_api_type",
                        field="api_type",
                        index=index,
                    )
                api_type = raw_type.strip()
                if api_type not in known_types:
                    return _err(
                        f"节点「{provider_id}」的接口类型无效",
                        400,
                        code="invalid_api_type",
                        field="api_type",
                        index=index,
                    )
                native = api_type == "astrbot_native"

                def warn(code: str, key: str, message: str):
                    warnings.append({"code": code, "key": key, "node": provider_id, "message": message})

                def text_field(key: str, fallback: str, *, label: str, strip: bool = True) -> str:
                    if key not in item:
                        return fallback
                    value = item.get(key)
                    if not isinstance(value, str):
                        warn("param_invalid_value", key, f"节点「{provider_id}」的{label}必须是文本，已保留原值。")
                        return fallback
                    return value.strip() if strip else value

                # 布尔字段：出现即写入；非法值字段级拒绝（保留旧值）。
                stored_enabled = _provider_enabled(old)
                enabled, ok = _as_bool(item.get("enabled", stored_enabled), stored_enabled)
                if "enabled" in item and not ok:
                    warn("param_invalid_value", "enabled", f"节点「{provider_id}」的启用开关取值无效，已保留原值。")
                    enabled = stored_enabled

                stored_proxy = _as_bool(old.get("enable_proxy", False), False)[0]
                enable_proxy, ok = _as_bool(item.get("enable_proxy", stored_proxy), stored_proxy)
                if "enable_proxy" in item and not ok:
                    warn("param_invalid_value", "enable_proxy", f"节点「{provider_id}」的代理开关取值无效，已保留原值。")
                    enable_proxy = stored_proxy

                if native:
                    stored_stream = _as_bool(old.get("stream", False), False)[0]
                    stream, ok = _as_bool(item.get("stream", stored_stream), stored_stream)
                    if "stream" in item and not ok:
                        warn("param_invalid_value", "stream", f"节点「{provider_id}」的流式开关取值无效，已保留原值。")
                        stream = stored_stream
                else:
                    # 切离原生：流式字段归零（不残留）。
                    stream = False

                # 整数字段：坏值字段级拒绝；越界按钳制表夹紧。
                stored_ref = _safe_int(old.get("reference_image_limit"), 0, 0)
                ref_limit, ok = _int_checked(item.get("reference_image_limit", stored_ref), 0, None)
                if not ok:
                    if "reference_image_limit" in item:
                        warn("param_invalid_value", "reference_image_limit", f"节点「{provider_id}」的参考图上限不是有效整数，已保留原值。")
                    ref_limit = stored_ref
                stored_timeout = _safe_int(old.get("timeout"), 180, 10)
                timeout, ok = _int_checked(item.get("timeout", stored_timeout), 10, None)
                if not ok:
                    if "timeout" in item:
                        warn("param_invalid_value", "timeout", f"节点「{provider_id}」的超时不是有效整数，已保留原值。")
                    timeout = stored_timeout
                if "fallback_order" in item:
                    order, ok = _int_checked(item.get("fallback_order"))
                    fallback_order = order if ok else 0
                else:
                    fallback_order = _safe_int(old.get("fallback_order"), 0)

                stored_thinking = str(old.get("thinking", "") or "")
                thinking = stored_thinking
                if "thinking" in item:
                    candidate = item.get("thinking")
                    if not isinstance(candidate, str):
                        warn("param_invalid_value", "thinking", f"节点「{provider_id}」的思考档位必须是文本，已保留原值。")
                    else:
                        candidate = candidate.strip()
                        if candidate in thinking_values:
                            thinking = candidate
                        else:
                            warn("param_invalid_value", "thinking", f"节点「{provider_id}」的思考档位无效，已保留原值。")

                custom_params = text_field(
                    "custom_params",
                    str(old.get("custom_params", "") or ""),
                    label="高级参数",
                    strip=False,
                )

                if "available_models" in item:
                    raw_models = item.get("available_models")
                    if isinstance(raw_models, list) and all(isinstance(value, str) for value in raw_models):
                        available_models = [value.strip() for value in raw_models if value.strip()]
                    elif isinstance(raw_models, str):
                        available_models = _line_items(raw_models)
                    else:
                        warn("param_invalid_value", "available_models", f"节点「{provider_id}」的可用模型必须是文本列表，已保留原值。")
                        available_models = _line_items(old.get("available_models", []))
                else:
                    available_models = _line_items(old.get("available_models", []))

                if native:
                    raw_provider_id = item.get("astrbot_provider_id", old.get("astrbot_provider_id", ""))
                    if not isinstance(raw_provider_id, str):
                        return _err(
                            f"原生节点「{provider_id}」的 Provider ID 形状无效",
                            400,
                            code="invalid_astrbot_provider_id",
                            field="astrbot_provider_id",
                            index=index,
                        )
                    astrbot_provider_id = raw_provider_id.strip()
                    if not astrbot_provider_id:
                        return _err(
                            f"原生节点「{provider_id}」必须选择 AstrBot Provider",
                            400,
                            code="native_missing_provider",
                            field="astrbot_provider_id",
                            index=index,
                        )
                else:
                    astrbot_provider_id = ""

                # 密钥三态（FF-02）：缺席/空=保留；非空=整体替换；clear_api_keys=true=清空。
                # 不再因为切到原生就丢弃休眠的 HTTP 密钥（切回来仍可用）。
                stored_keys = _line_items(old.get("api_keys", []))
                clear_keys, clear_ok = _as_bool(item.get("clear_api_keys", False), False)
                if "clear_api_keys" in item and not clear_ok:
                    warn("param_invalid_value", "clear_api_keys", f"节点「{provider_id}」的清空密钥标记取值无效，已忽略。")
                    clear_keys = False
                submitted_keys: list[str] = []
                keys_shape_ok = True
                if "api_keys" in item:
                    raw_keys = item.get("api_keys")
                    if isinstance(raw_keys, str):
                        submitted_keys = _line_items(raw_keys)
                    elif isinstance(raw_keys, list) and all(isinstance(value, str) for value in raw_keys):
                        submitted_keys = [value.strip() for value in raw_keys if value.strip()]
                    else:
                        keys_shape_ok = False
                        warn("param_invalid_value", "api_keys", f"节点「{provider_id}」的 API Key 必须是文本或文本列表，已保留原值。")
                if clear_keys:
                    api_keys = []
                elif not keys_shape_ok:
                    api_keys = stored_keys
                elif submitted_keys:
                    api_keys = submitted_keys
                else:
                    api_keys = stored_keys

                if clear_keys and not native:
                    # 服务端兜底：显式清空 HTTP key 必须落成“已停用”，
                    # 不接受客户端“清空但仍启用”的组合。
                    enabled = False

                # FF-R2-01 / FF-R3-01：显式清空按“原类型 + 最终类型”分治。
                # HTTP 节点由服务端强制停用；只有原本就是原生节点的显式清空才保留
                # 原有 enabled / provider / stream（即使请求里带了相反的值）。
                # HTTP→native 或新建 native 时，上一步校验过的 provider/stream/启停
                # 必须原样落盘，禁止用 old（通常为空）覆盖后 200 却让 load_config 丢节点。
                if clear_keys and native and old_api_type == "astrbot_native":
                    enabled = stored_enabled
                    astrbot_provider_id = str(old.get("astrbot_provider_id", "")).strip()
                    stream = _as_bool(old.get("stream", False), False)[0]

                base_url = text_field("base_url", str(old.get("base_url", "")).strip(), label="接口地址")
                model = text_field("model", str(old.get("model", "")).strip(), label="模型")
                default_size = text_field(
                    "default_size",
                    str(old.get("default_size", "1024x1024")).strip(),
                    label="默认尺寸",
                )

                extra = {key: value for key, value in old.items() if key not in PROVIDER_FIELDS and key != "__template_key"}
                entry = {
                    **extra,
                    "id": provider_id,
                    "api_type": api_type,
                    "enabled": enabled,
                    "base_url": base_url,
                    "api_keys": api_keys,
                    "model": model,
                    "available_models": available_models,
                    "reference_image_limit": ref_limit,
                    "default_size": default_size,
                    "timeout": timeout,
                    "astrbot_provider_id": astrbot_provider_id,
                    "stream": stream,
                    "enable_proxy": enable_proxy,
                    "fallback_order": fallback_order,
                    "thinking": thinking,
                    "custom_params": custom_params,
                    "__template_key": "provider",
                }

                # FF-04：HTTP 节点必须带地址与密钥才能落盘；显式清空必须同时停用，
                # 普通新建 / 遗漏 key / 启用但不完整一律整单拒绝（原子，不写盘）。
                if not native and not api_keys:
                    already_keyless_disabled = (not is_new) and (not enabled) and bool(base_url)
                    if (is_new or enabled) or not already_keyless_disabled:
                        return _err(
                            f"节点「{provider_id}」配置不完整：缺 API Key。"
                            "如确实要清空密钥，请先勾选“清空 API Key”并确认，插件会自动停用该节点后再保存。",
                            400,
                            code="node_incomplete",
                            field="api_keys",
                            index=index,
                        )
                if not native and not base_url:
                    return _err(
                        f"节点「{provider_id}」配置不完整：缺接口地址。",
                        400,
                        code="node_incomplete",
                        field="base_url",
                        index=index,
                    )

                if is_new:
                    warnings.append(
                        {
                            "code": "new_node_no_key_inheritance",
                            "key": "id",
                            "node": provider_id,
                            "message": (
                                f"「{provider_id}」按新增节点保存，不会继承任何旧节点的密钥或配置；"
                                "如要替换旧节点，请显式删除旧节点。"
                            ),
                        }
                    )

                capabilities = dict(_api_type_meta(api_type).get("capabilities") or {})
                param_mode = str(_api_type_meta(api_type).get("param_mode", "ignored"))
                if thinking and str(capabilities.get("thinking", "none")) == "none":
                    warn(
                        "thinking_ignored",
                        "thinking",
                        f"节点「{provider_id}」的思考档位会保存但不会发送（{api_type} 没有思考字段）；切换接口后仍保留。",
                    )

                # FF-09：区分「未声明但会透传」与「原生节点根本不发」，不写空头承诺。
                _params, param_errors = _parse_params(custom_params, api_type)
                for error in param_errors:
                    if str(error.get("reason", "")) != "unknown_for_type":
                        continue
                    key = str(error.get("key", ""))
                    if key == "quality" and api_type == "openai_image":
                        continue
                    if param_mode == "ignored":
                        message = (
                            f"节点「{provider_id}」的参数「{key}」不会被发送：原生节点由 AstrBot Provider 处理，"
                            "插件不发送自定义参数；该键只保留在配置里。"
                        )
                    else:
                        message = (
                            f"节点「{provider_id}」的参数「{key}」未获 {api_type} 公开声明支持，会原样透传给服务端；"
                            "服务端可能返回 400，插件不会自动重试，也不会因此触发参考图降级。"
                        )
                    warnings.append(
                        {
                            "code": "param_unsupported_for_type",
                            "key": key,
                            "api_type": api_type,
                            "node": provider_id,
                            "param_mode": param_mode,
                            "passthrough": param_mode != "ignored",
                            "message": message,
                        }
                    )
                # 原生节点把鉴权交给 AstrBot Provider，本插件不发送任何自定义参数：
                # 已解析出的键也要明确告知“只保存、不会发送”，不留空头承诺。
                if param_mode == "ignored" and custom_params.strip():
                    for key in _params:
                        warnings.append(
                            {
                                "code": "param_unsupported_for_type",
                                "key": key,
                                "api_type": api_type,
                                "node": provider_id,
                                "param_mode": param_mode,
                                "passthrough": False,
                                "message": (
                                    f"节点「{provider_id}」的参数「{key}」不会被发送：原生节点由 AstrBot Provider 处理，"
                                    "插件不发送自定义参数；该键只保留在配置里。"
                                ),
                            }
                        )

                updates[provider_id] = entry
                seen.add(provider_id)
                if is_new and provider_id not in new_ids:
                    new_ids.append(provider_id)

            # FF-03：以旧顺序合并——未提交的旧节点原样保留（局部保存不会“改名”或删除它），
            # 新节点按提交顺序追加；删除只认显式 deleted_provider_ids，绝不按位置猜身份。
            assembled: list[dict[str, Any]] = []
            for old_id in old_order:
                if old_id in declared_deleted:
                    continue
                replacement = updates.pop(old_id, None)
                assembled.append(replacement if replacement is not None else copy.deepcopy(dict(old_by_id[old_id])))
            for new_id in new_ids:
                if new_id in declared_deleted:
                    continue
                replacement = updates.pop(new_id, None)
                if replacement is not None:
                    assembled.append(replacement)
            assembled.extend(updates.values())
            saved = assembled

        primary_present = "primary_provider_id" in body
        stored_primary = str(self.plugin.store.get_primary_provider_id() or "").strip()
        if primary_present:
            raw_primary = body.get("primary_provider_id")
            if raw_primary is not None and not isinstance(raw_primary, str):
                return _err("主节点 ID 形状无效", 400, code="invalid_primary", field="primary_provider_id")
            primary = (raw_primary or "").strip()
        else:
            primary = stored_primary
        # 仅当被删除的正是存储主节点、且本次没有明确指定新主节点时，才清空存储值。
        clear_stored_primary = bool(stored_primary and stored_primary in declared_deleted and not primary_present)
        if clear_stored_primary:
            primary = ""
        current_ids = {entry["id"] for entry in saved} if saved is not None else (set(old_by_id) - declared_deleted)
        if saved is not None and primary and primary not in current_ids and primary.partition("/")[0] not in current_ids:
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
                if str(item.get("id", "")).strip() not in declared_deleted
                and _provider_enabled(item)
                and _provider_complete(item)
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

        # FF-06：配置与主节点存储是两份持久化；任一步失败都要恢复“内存 + 磁盘 + 存储”，
        # 并在响应里如实说明回滚是否成功（不再出现 500 但磁盘已更新的假象）。
        state_before: dict[str, Any] = {
            "providers": copy.deepcopy(self.plugin.raw_config.get("providers")),
            "optimizer_config": copy.deepcopy(self.plugin.raw_config.get("optimizer_config")),
            **{name: copy.deepcopy(self.plugin.raw_config.get(name)) for name in GROUP_DEFS},
        }
        stored_before = str(self.plugin.store.get_primary_provider_id() or "").strip()
        save_fn = getattr(self.plugin.raw_config, "save_config", None)
        if not callable(save_fn):
            return _err("当前 AstrBot 配置对象不支持持久化", 500, code="config_unsupported")

        def restore_memory() -> None:
            for key, value in state_before.items():
                if value is None:
                    self.plugin.raw_config.pop(key, None)
                else:
                    self.plugin.raw_config[key] = value

        try:
            if saved is not None:
                self.plugin.raw_config["providers"] = saved
            if optimizer_present:
                self.plugin.raw_config["optimizer_config"] = new_optimizer
            for name, group in merged_groups.items():
                self.plugin.raw_config[name] = group
            save_fn()
        except Exception as exc:
            restore_memory()
            rollback_ok = True
            rollback_error = ""
            try:
                save_fn()
            except Exception as rollback_exc:
                rollback_ok = False
                rollback_error = str(rollback_exc) or rollback_exc.__class__.__name__
            return _err(
                "无法保存节点配置" + ("；已恢复保存前状态" if rollback_ok else "；恢复保存前状态失败，请人工核对配置"),
                500,
                code="config_write_failed",
                rollback="ok" if rollback_ok else "failed",
                detail=str(exc) or exc.__class__.__name__,
                rollback_error=rollback_error or None,
            )

        store_changed = (primary_present or clear_stored_primary) and primary != stored_before
        if store_changed:
            try:
                self.plugin.store.set_primary_provider_id(primary)
            except Exception as exc:
                restore_memory()
                config_restored = True
                config_error = ""
                try:
                    save_fn()
                except Exception as rollback_exc:
                    config_restored = False
                    config_error = str(rollback_exc) or rollback_exc.__class__.__name__
                store_restored = True
                store_error = ""
                try:
                    self.plugin.store.set_primary_provider_id(stored_before)
                except Exception as rollback_exc:
                    store_restored = False
                    store_error = str(rollback_exc) or rollback_exc.__class__.__name__
                fully_rolled_back = config_restored and store_restored
                return _err(
                    "主节点保存失败" + ("；已回滚到保存前状态" if fully_rolled_back else "；回滚未完全成功，请人工核对"),
                    500,
                    code="primary_store_failed",
                    rollback="ok" if fully_rolled_back else "failed",
                    config_restored=config_restored,
                    store_restored=store_restored,
                    detail=str(exc) or exc.__class__.__name__,
                    rollback_error=(config_error or store_error) or None,
                )

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
