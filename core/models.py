from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class TaskState(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL_SUCCESS = "partial_success"
    DELIVERY_FAILED = "delivery_failed"
    NO_OUTPUT = "no_output"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """是否已结束（不再占用并发额度，只在完成/发送与记账阶段被引用）。"""
        return self in _TERMINAL_STATES


_TERMINAL_STATES = frozenset({
    TaskState.SUCCEEDED,
    TaskState.PARTIAL_SUCCESS,
    TaskState.DELIVERY_FAILED,
    TaskState.NO_OUTPUT,
    TaskState.FAILED,
    TaskState.TIMED_OUT,
    TaskState.CANCELLED,
})


class TaskStage(str, Enum):
    QUEUED = "queued"
    PREPARING_REFERENCES = "preparing_references"
    BUILDING_PERSONA = "building_persona"
    OPTIMIZING_PROMPT = "optimizing_prompt"
    REQUESTING_PROVIDER = "requesting_provider"
    PROCESSING_RESULT = "processing_result"
    DECORATING = "decorating"
    SENDING = "sending"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    id: str
    api_type: str
    base_url: str
    api_keys: tuple[str, ...]
    model: str = ""
    available_models: tuple[str, ...] = ()
    reference_image_limit: int = 0
    default_size: str = "1024x1024"
    timeout: int = 180
    # AstrBot chat provider id used by ``astrbot_native`` nodes, e.g.
    # ``cpa_Gemini/web-gemini-image``. That provider owns the real key, so such a
    # node needs neither ``base_url`` nor ``api_keys``.
    astrbot_provider_id: str = ""
    # ``astrbot_native`` only: use ``provider.text_chat_stream`` instead of
    # ``context.llm_generate``.
    stream: bool = False
    # HTTP direct nodes only: route the request through the plugin-wide outbound
    # proxy from ``proxy_url``. Meaningless for ``astrbot_native`` (the AstrBot
    # provider decides its own proxy) and harmful for a local reverse proxy.
    enable_proxy: bool = False
    # Smallest value is tried first during fallback; the primary node selected in
    # the WebUI always stays first.
    fallback_order: int = 0


@dataclass(frozen=True, slots=True)
class QuotaConfig:
    enabled: bool = False
    blacklist_ids: frozenset[str] = frozenset()
    unlimited_whitelist_ids: frozenset[str] = frozenset()
    daily_refresh_enabled: bool = True
    daily_quota_target: int = 0
    checkin_enabled: bool = False
    checkin_quota_min: int = 1
    checkin_quota_max: int = 3


@dataclass(frozen=True, slots=True)
class EnhanceConfig:
    """百度「图像无损放大」的开关、凭据与本地预检阈值（``enhance_config`` 组）。

    ``enabled`` 默认 **False**：关闭时 ``core.enhance_client.BaiduEnhancer`` 在所有路径
    上立即返回原图，零网络、零新文件、零 ``task.runtime`` 键、零新增日志。

    Attributes:
        enabled: 总开关（按次计费，默认关闭）。
        api_key: 百度「图像增强与特效」应用的 API Key；明文存在插件配置里。
        secret_key: 同一应用的 Secret Key。
        max_image_mb: 本地预检的 base64 上限（MB），百度硬限 4。
        max_pixels: 本地预检的长宽乘积上限（px²），默认 2000×2000。
        timeout_seconds: 单张放大的超时（秒）。
        enable_proxy: 放大请求是否走插件顶层的出站代理（``proxy_url``）。
    """

    enabled: bool = False
    api_key: str = ""
    secret_key: str = ""
    max_image_mb: int = 4
    max_pixels: int = 4_000_000
    timeout_seconds: int = 20
    enable_proxy: bool = False

    @property
    def configured(self) -> bool:
        """开关打开**且**两把 key 都在：只有这时才允许发起任何百度请求。"""
        return self.enabled and bool(self.api_key) and bool(self.secret_key)

    @property
    def max_raw_bytes(self) -> int:
        """原图字节上限：base64 膨胀 4/3，故上限 = ``max_image_mb`` × 3/4。

        默认 4 MiB ⇒ 3 145 728 B。用原图字节数判等价于判 base64 长度（单调），
        省一次编码；见 ``core/enhance.py`` 的 ``plan()``。
        """
        return self.max_image_mb * 1024 * 1024 * 3 // 4


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    providers: tuple[ProviderConfig, ...]
    quota: QuotaConfig = field(default_factory=QuotaConfig)
    optimizer_enabled: bool = True
    optimize_plain_draw: bool = False
    optimizer_provider_id: str = ""
    vision_provider_id: str = ""
    reference_caption: bool = False
    optimizer_prompt: str = ""
    optimizer_style: str = "default"
    fallback_style_injection: bool = False
    generation_timeout: int = 300
    max_concurrent_tasks: int = 2
    llm_retry: int = 1
    llm_caption: bool = False
    llm_caption_cm_context: bool = False
    llm_caption_pregen: bool = False
    at_trigger_user: bool = True
    max_upload_bytes: int = 20 * 1024 * 1024
    temp_cache_bytes: int = 512 * 1024 * 1024
    block_private_networks: bool = True
    log_with_bot_id: bool = False
    # Outbound proxy applied to ``enable_proxy`` HTTP nodes; empty = direct.
    proxy_url: str = ""
    # Allow ``template/model`` model overrides on the primary node. Kept behind a
    # switch because such a string is also a valid AstrBot provider id
    # (``cpa_Gemini/web-gemini-image``); see ``core/config.py``.
    template_override: bool = False
    # 百度「图像清晰度增强」：默认关闭。关闭时出图链路与 v1.0.0 逐字节等价
    # （零网络、零新文件、零 runtime 键），见 ``core/enhance_client.py``。
    enhance: EnhanceConfig = field(default_factory=EnhanceConfig)


@dataclass(slots=True)
class ImageInput:
    data: bytes
    mime_type: str
    filename: str = "reference"


@dataclass(slots=True)
class ImageResult:
    url: str = ""
    data: bytes | None = None
    mime_type: str = "image/png"
    local_path: Path | None = None


@dataclass(slots=True)
class GenerationRequest:
    prompt: str
    count: int = 1
    size: str = ""
    aspect_ratio: str = ""
    references: list[ImageInput] = field(default_factory=list)
    extra_params: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class DrawTask:
    id: str
    umo: str
    request: GenerationRequest
    persona_id: str = ""
    persona_prompt: str = ""
    owner_user_id: str = ""
    bot_instance_id: str = ""
    kind: str = "draw"
    state: TaskState = TaskState.CREATED
    stage: TaskStage = TaskStage.QUEUED
    created_at: float = 0.0
    updated_at: float = 0.0
    errors: list[str] = field(default_factory=list)
    runtime: dict[str, Any] = field(default_factory=dict)
