from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .models import ProviderConfig, QuotaConfig, RuntimeConfig, EnhanceConfig

API_TYPES = {
    "openai_image",
    "openai_chat",
    "gemini_official",
    "dashscope_multimodal",
    "custom_endpoint",
    "astrbot_native",
}
STYLES = {"none", "default", "realistic", "real3d", "cg3d", "illustration", "pixel", "logo", "auto"}
STYLE_OPTIONS = {
    "None(无)": "none",
    "default(通用)": "default",
    "realistic(真人实拍)": "realistic",
    "real3d(照片级三维渲染)": "real3d",
    "cg3d(风格化三维渲染)": "cg3d",
    "illustration(手绘插画)": "illustration",
    "pixel(像素阵列)": "pixel",
    "logo(LOGO 设计)": "logo",
    "auto(自动)": "auto",
}


def _line_items(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, list) else str(value).splitlines()
    return tuple(str(item).strip() for item in values if str(item).strip())


def _keys(value: Any) -> tuple[str, ...]:
    return _line_items(value)


def _ids(value: Any) -> frozenset[str]:
    return frozenset(_line_items(value))


def _style(value: Any) -> str:
    """解析 optimizer_style：现用内部键或 WebUI 标签，其余一律回退 default(通用)。

    不做旧值兼容：1.1.6 之前的标签（`anime(动漫)`、`3d(3D渲染)` 等）不再识别，
    升级后需要重新选一次；旧配置不会被改写，只是解析结果落到 default(通用)。
    """
    text = str(value).strip()
    if text in STYLES:
        return text
    return STYLE_OPTIONS.get(text, "default")


def _int_or_default(value: Any, default: int) -> int:
    """配置整数兜底：None/非数字回退默认；0 等合法值原样保留（调用方再 clamp）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def matches_template(providers: list[ProviderConfig], reference: str, *, enabled: bool) -> bool:
    """判断节点引用是否命中 ``template/model`` 覆写语法。

    大香蕉原实现无条件按 ``name/model`` 覆写（先查模板名，查不到再 ``partition("/")``），
    这在 AstrBot 原生 id 上会出问题：真实原生 id 本身带 ``/``
    （``cpa_Gemini/web-gemini-image``），只要用户建了一个与 id 前缀同名的模板，
    原生路由就会被静默换成「模板 base_url + 模板 key + 覆写模型名」。

    本插件的判据是**前缀必须真的是一个已配置模板名**：``/`` 前段命中模板才覆写，
    否则引用整体按 AstrBot provider id 交给原生节点。这样开启覆写也不会劫持
    原生路由，两种形态可以共存。

    碰撞守卫：若 ``/`` 前的模板名与某个 ``astrbot_native`` 类型节点的 id **完全相同**，
    说明同名标识同时被模板与原生节点占用，覆写会让主节点落到模板上、遮蔽原生节点
    （典型场景：模板名写成 ``cpa_Gemini``，而原生节点 id 就是 ``cpa_Gemini``）。
    此时记 WARNING 并**跳过覆写**，把引用留给原生节点解析。

    Args:
        providers: 已解析的节点列表（模板 + 原生节点）。
        reference: 用户指定的主节点引用，形如 ``模板名`` 或 ``模板名/模型名``。
        enabled: ``template_override`` 开关；关闭时一律不覆写。

    Returns:
        True 表示该引用应作为模板覆写处理。
    """
    if not enabled or "/" not in reference:
        return False
    template_name = reference.partition("/")[0].strip()
    for item in providers:
        if item.id != template_name:
            continue
        if item.api_type == "astrbot_native":
            # 同名原生节点存在：跳过覆写，避免遮蔽原生路由。
            _warn_template_collision(template_name, reference)
            return False
        return True
    return False


def _warn_template_collision(template_name: str, reference: str) -> None:
    """记录「模板名与原生节点 id 同名」导致的覆写跳过。

    懒导入 logger：``core.config`` 必须能在不含 AstrBot 的纯逻辑测试环境里导入。

    Args:
        template_name: 与原生节点 id 相同的模板名。
        reference: 用户指定的完整引用（``模板名/模型名``）。
    """
    try:
        from astrbot.api import logger
    except ImportError:  # pragma: no cover - 仅在缺少 AstrBot 的源码环境触发
        return
    logger.warning(
        "[ImagoFusion] template_override 已跳过：模板名 %s 与 astrbot_native 节点 id 相同"
        "（引用 %s），为避免遮蔽原生路由，本次按原生节点解析。",
        template_name,
        reference,
    )


def load_config(raw: Mapping[str, Any]) -> RuntimeConfig:
    providers: list[ProviderConfig] = []
    seen: set[str] = set()
    for item in raw.get("providers", []) or []:
        if not isinstance(item, Mapping):
            continue
        provider_id = str(item.get("id", "")).strip()
        api_type = str(item.get("api_type", "")).strip()
        base_url = str(item.get("base_url", "")).strip()
        api_keys = _keys(item.get("api_keys", ""))
        astrbot_provider_id = str(item.get("astrbot_provider_id", "")).strip()
        native = api_type == "astrbot_native"
        if not provider_id or provider_id in seen or api_type not in API_TYPES:
            continue
        # ``astrbot_native`` 由 AstrBot provider 自理鉴权与地址，base_url/api_keys
        # 留空是合法配置；其余类型两者都必填（否则请求无法构造）。
        if native:
            if not astrbot_provider_id:
                continue
        elif not base_url or not api_keys:
            continue
        seen.add(provider_id)
        providers.append(ProviderConfig(
            id=provider_id,
            api_type=api_type,
            base_url=base_url,
            api_keys=api_keys,
            model=str(item.get("model", "")).strip(),
            available_models=tuple(str(v) for v in (item.get("available_models", []) or [])),
            reference_image_limit=max(0, int(item.get("reference_image_limit", 0) or 0)),
            # 留空是合法配置（等同"不指定"：请求里不发送 size 字段），不做兜底替换。
            default_size=str(item.get("default_size", "1024x1024")).strip(),
            timeout=max(10, int(item.get("timeout", 180) or 180)),
            astrbot_provider_id=astrbot_provider_id if native else "",
            stream=bool(item.get("stream", False)) if native else False,
            enable_proxy=bool(item.get("enable_proxy", False)),
            fallback_order=_int_or_default(item.get("fallback_order"), 0),
        ))
    # 回退顺序：数字小的先试，同值保持列表顺序（Python 排序稳定）。
    providers.sort(key=lambda item: item.fallback_order)
    optimizer = raw.get("optimizer_config", {}) or {}
    quota_raw = raw.get("quota_config", {}) or {}
    tasks = raw.get("task_config", {}) or {}
    storage = raw.get("storage_config", {}) or {}
    enhance_raw = raw.get("enhance_config", {}) or {}
    style = _style(optimizer.get("optimizer_style", "default(通用)"))
    checkin_min = max(0, int(quota_raw.get("daily_checkin_quota_min", 1) or 0))
    checkin_max = max(checkin_min, int(quota_raw.get("daily_checkin_quota_max", 3) or 0))
    return RuntimeConfig(
        providers=tuple(providers),
        quota=QuotaConfig(
            enabled=bool(quota_raw.get("enable_quota", False)),
            blacklist_ids=_ids(quota_raw.get("blacklist_ids", [])),
            unlimited_whitelist_ids=_ids(quota_raw.get("unlimited_whitelist_ids", [])),
            daily_refresh_enabled=bool(quota_raw.get("enable_daily_refresh", True)),
            daily_quota_target=max(0, int(quota_raw.get("daily_quota_target", 0) or 0)),
            checkin_enabled=bool(quota_raw.get("enable_checkin", False)),
            checkin_quota_min=checkin_min,
            checkin_quota_max=checkin_max,
        ),
        optimizer_enabled=bool(optimizer.get("enable_optimizer", True)),
        optimize_plain_draw=bool(optimizer.get("optimize_plain_draw", False)),
        optimizer_provider_id=str(optimizer.get("optimizer_provider_id", "")).strip(),
        vision_provider_id=str(optimizer.get("vision_provider_id", "")).strip(),
        reference_caption=bool(optimizer.get("reference_caption", False)),
        optimizer_prompt=str(optimizer.get("optimizer_prompt", "")).strip(),
        optimizer_style=style,
        fallback_style_injection=bool(optimizer.get("fallback_style_injection", False)),
        generation_timeout=max(30, _int_or_default(tasks.get("generation_timeout"), 300)),
        max_concurrent_tasks=max(1, _int_or_default(tasks.get("max_concurrent_tasks"), 2)),
        llm_retry=max(1, min(5, int(tasks.get("llm_retry", 1) or 1))),
        llm_caption=bool(tasks.get("llm_caption", False)),
        llm_caption_cm_context=bool(tasks.get("llm_caption_cm_context", False)),
        llm_caption_pregen=bool(tasks.get("llm_caption_pregen", False)),
        at_trigger_user=bool(tasks.get("at_trigger_user", True)),
        max_upload_bytes=max(1, int(storage.get("max_upload_mb", 20) or 20)) * 1024 * 1024,
        temp_cache_bytes=max(16, int(storage.get("temp_cache_mb", 512) or 512)) * 1024 * 1024,
        block_private_networks=bool(storage.get("block_private_networks", True)),
        log_with_bot_id=bool(raw.get("log_with_bot_id", False)),
        proxy_url=str(raw.get("proxy_url", "")).strip(),
        template_override=bool(raw.get("template_override", False)),
        # 百度「图像清晰度增强」：键名与 _conf_schema.json 的 enhance_config 组一一对应
        # （7 键全部在这里读取，不允许「schema 有、解析不转发」的静默丢键）。
        # 数值按百度硬限 clamp：base64 ≤4M、长宽乘积 ≤2000×2000、超时 5-60 s。
        enhance=EnhanceConfig(
            enabled=bool(enhance_raw.get("enable", False)),
            api_key=str(enhance_raw.get("api_key", "") or "").strip(),
            secret_key=str(enhance_raw.get("secret_key", "") or "").strip(),
            max_image_mb=max(1, min(4, _int_or_default(enhance_raw.get("max_image_mb"), 4))),
            max_pixels=max(250_000, min(4_000_000, _int_or_default(enhance_raw.get("max_pixels"), 4_000_000))),
            timeout_seconds=max(5, min(60, _int_or_default(enhance_raw.get("timeout_seconds"), 20))),
            enable_proxy=bool(enhance_raw.get("enable_proxy", False)),
        ),
    )


def optimizer_in_use(cfg: RuntimeConfig, *, persona: bool, plain_draw_tool: bool) -> bool:
    """本轮任务是否调用副脑（纯逻辑，与风格联动完全独立）。

    - `enable_optimizer` 是副脑总开关，关闭时任何路径都不调用；
    - Persona 出镜任务在总开关开启时始终调用；
    - 普通绘图只有来自 LLM 工具（`generate_fusion_image`）且 `optimize_plain_draw` 开启
      时才调用——该开关只决定这件事，不参与风格裁决（风格联动见
      `core.prompting.resolve_style`，唯一联动点是配置为 `auto`）。
    """
    if not cfg.optimizer_enabled:
        return False
    if persona:
        return True
    return bool(plain_draw_tool and cfg.optimize_plain_draw)


def style_arg_consumed(cfg: RuntimeConfig, *, use_optimizer: bool) -> bool:
    """主 LLM 传的基准参数是否会被本轮消费（纯逻辑）。

    会被消费的只有两种情形：

    - 本轮副脑参与：参数交给 `core.prompting.resolve_style` 裁决，配置为 auto 时生效；
    - 「副脑降级时注入风格后缀」开启：副脑没跑（或调用失败降级）时也要按它注入后缀。

    其余情形参数被丢弃——副脑没跑又没有降级注入时，解析出来的基准没有任何消费方，
    留着只会让 `/画` 之类的直连路径多一个看不见的状态。
    """
    return bool(use_optimizer or cfg.fallback_style_injection)


def persona_provider_settings(umo_config: Any, default_config: Any) -> dict:
    """人设解析用的 provider_settings，与主链 LLM 请求的实际数据源对齐。

    AstrBot 4.27.x 主链 `_decorate_llm_request`：

        cfg = config.provider_settings or context.get_config(umo=...).get("provider_settings", {})

    `config.provider_settings` 是 pipeline 按 UMO 路由（umop_config_routing）命中的
    配置文件快照，回退分支 `get_config(umo)` 同样是会话命中配置（acm.get_conf 未命中
    时才内部回退默认配置）——两个分支都不读全局默认配置。多配置文件场景下默认配置与
    会话命中配置的 default_personality 不同，插件侧必须用同一数据源，否则 Persona 任务
    会解析成默认配置的人设（如本插件历史上「全局优先」导致的错位）。

    等价实现：优先 umo_config（会话命中配置），仅当其为 None（调用方未取到）时才
    回退 default_config；会话命中配置存在但 provider_settings 为空时返回空 dict，
    与主链行为一致（不吞回全局默认）。
    """
    source = umo_config if umo_config is not None else default_config
    return (source or {}).get("provider_settings", {}) or {}
