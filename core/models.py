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
    # ``enabled=False`` 的节点不参与路由：调度器完全不尝试、不算有效主节点，
    # 但配置原样保留（v1.3.0 契约 F1；缺键时默认 True = 与 v1.2.0 等价）。
    enabled: bool = True
    # 思考控制的一级键（``""``/``off``/``low``/``medium``/``high``/``max``）。
    # 它**不**写进 ``custom_params``，也不与之合并（契约 F4/F5、字典 §3.4）。
    thinking: str = ""
    # 节点级参数原文（``key=value`` 行）。由 ``core.params.parse_custom_params``
    # 按协议解析后再落到请求容器；这里保留原文以便两套界面三态往返。
    custom_params: str = ""


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
    """百度「图像清晰度增强」的开关、凭据与本地预检阈值（``enhance_config`` 组）。

    ``enabled`` 默认 **False**：关闭时 ``core.enhance_client.BaiduEnhancer`` 在所有路径
    上立即返回原图，零网络、零新文件、零 ``task.runtime`` 键、零新增日志。

    Attributes:
        enabled: 总开关（按次计费，默认关闭）。
        api_key: 百度「图像增强与特效」应用的 API Key；明文存在插件配置里。
        secret_key: 同一应用的 Secret Key。
        max_image_mb: 本地预检的 **base64** 上限（MB），官方硬限 10。
        max_edge: 本地预检的**最长边**上限（px），官方 5000。
        max_aspect: 本地预检的**长宽比**上限（长边 ÷ 短边），官方 4:1；边界 4.0 允许。
        timeout_seconds: 单张增强的超时（秒）。
        enable_proxy: 增强请求是否走插件顶层的出站代理（``proxy_url``）。
    """

    enabled: bool = False
    api_key: str = ""
    secret_key: str = ""
    max_image_mb: int = 10
    max_edge: int = 5000
    max_aspect: float = 4.0
    timeout_seconds: int = 20
    enable_proxy: bool = False

    @property
    def configured(self) -> bool:
        """开关打开**且**两把 key 都在：只有这时才允许发起任何百度请求。"""
        return self.enabled and bool(self.api_key) and bool(self.secret_key)

    @property
    def max_raw_bytes(self) -> int:
        """原图字节上限：base64 膨胀 4/3，故上限 = ``max_image_mb`` × 3/4。

        默认 10 MiB ⇒ 7 864 320 B（约 7.5 MiB，见
        ``core/enhance.py`` 的 ``MAX_RAW_BYTES_FOR_10MB_BASE64``）。用原图字节数判等价
        于判 base64 长度（单调），省一次编码；见 ``core/enhance.py`` 的 ``plan()``。
        """
        return self.max_image_mb * 1024 * 1024 * 3 // 4


@dataclass(frozen=True, slots=True)
class UpscaleConfig:
    """百度「图像无损放大」的开关、凭据与本地预检阈值（``upscale_config`` 组）。

    与 :class:`EnhanceConfig` **分别建模**（spec 变更令 v3 的「分别建模」7 条硬约束）：
    两个接口的限值完全不同（放大：base64 ≤ 4M、长宽**乘积** ≤ 2000×2000；增强：
    base64 ≤ 10M、最短边 ≥ 10px、最长边 ≤ 5000px、长宽比 ≤ 4:1），共用一份配置会让
    一组阈值被另一级误用。

    ``enabled`` 默认 **False**：关闭时 ``core.upscale_client.BaiduUpscaler`` 在所有路径
    上立即返回上一级产物，零网络、零新文件、零 ``task.runtime`` 键、零新增日志。

    Attributes:
        enabled: 总开关（按次计费，默认关闭）。
        api_key: 百度「图像增强与特效」应用的 API Key；明文存在插件配置里。
        secret_key: 同一应用的 Secret Key（两把 key 同应用，否则 ``invalid_client``）。
        max_image_mb: 本地预检的 **base64** 上限（MB），官方硬限 4 ⇒ clamp [1, 4]。
            调高只会换来服务端失败+计费，故上限就是 4。
        max_pixels: 本地预检的**长宽乘积**上限（px），官方 2000×2000 = 4 000 000。
        timeout_seconds: 单张放大的超时（秒），同时受任务总预算约束。
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

        默认 4 MiB ⇒ 3 145 728 B（见 ``core/upscale.py`` 的
        ``MAX_RAW_BYTES_FOR_4MB_BASE64``）。用原图字节数判等价于判 base64 长度
        （单调），省一次编码；见 ``core/upscale.py`` 的 ``plan()``。
        """
        return self.max_image_mb * 1024 * 1024 * 3 // 4


@dataclass(frozen=True, slots=True)
class ReferenceFallbackConfig:
    """参考图文字降级（``reference_fallback_config`` 组，默认关闭）。

    关闭时参考图链路与 v1.2.0 逐字节等价：零额外请求、零额外文件、零费用。
    打开后只在"上游明确拒绝图片输入"时触发一次，且降级请求不携带任何图片
    （契约 F6；判定器见 ``core.references.classify_rejection``）。

    Attributes:
        enabled: 总开关，默认 **False**。
        max_extra_attempts: 每任务允许的**额外**生成次数，0..1（默认 1）。
        caption_max_chars: 降级文字说明的总字数上限 80..400（默认 200）。
        vision_timeout_seconds: 为降级描述图集而调用识图 Provider 的单次超时 5..60。
        notify_user: 是否在结果尾部告知本次参考图未被使用（默认 True）。
    """

    enable: bool = False
    max_extra_attempts: int = 1
    caption_max_chars: int = 200
    vision_timeout_seconds: int = 20
    notify_user: bool = True


DEFAULT_OCCLUSION_PROMPT = (
    "在你认为的敏感部位用滑稽表情挡住；只做不透明覆盖，不要描述或还原被遮挡内容。"
)


@dataclass(frozen=True, slots=True)
class ExperimentalReferenceConfig:
    """实验遮挡（``experimental_reference_config`` 组，默认关闭，t8 rev3）。

    与 t1 的"本地整图 blur"完全不同：**不**在首轮做任何图像处理，只在参考图被上游
    拒绝后的降级阶段，调用一个**明确配置的生成节点**对失败尝试的图片做遮挡编辑，
    再把产物与一致的文字摘要交给原目标节点重试。首轮零改动、零额外请求、零费用。

    依赖 ``reference_fallback_config.enable``：降级关闭时本组即使开启也不生效。

    Attributes:
        enable: 总开关，默认 **False**。
        occluder_provider_id: 执行遮挡编辑的节点 id，必须是"存在 + 启用 + 配置完整"
            的 ``providers[].id``；不合法时跳过实验路径（DEBUG 记原因）。
        occlusion_prompt: 遮挡编辑指令，≤500 字符。
        max_images: 最多处理的图数，clamp 1..3。
        timeout_seconds: 单次遮挡编辑的超时，clamp 5..60。
        caption_max_chars: 与产物一致的摘要总字数上限，clamp 80..400。
    """

    enable: bool = False
    occluder_provider_id: str = ""
    occlusion_prompt: str = DEFAULT_OCCLUSION_PROMPT
    max_images: int = 1
    timeout_seconds: int = 20
    caption_max_chars: int = 200


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
    # 参考图关系声明注入开关（`task_config.inject_reference_note`，第 8 键，默认 True）。
    # True = 与 v1.1.0 逐字节相同（显式参考图存在时在最终 prompt 末尾追加低优先级
    # 关系声明 + 人设位置补充段）；False = **完全不追加**，`request.prompt` 逐字节等于
    # `task.request.prompt`（不删既有文本、不留空行）。见 `services/scheduler.py`。
    inject_reference_note: bool = True
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
    # 百度「图像无损放大」：默认关闭，且在增强**之后**串一级（spec 变更令 v3.5）。
    # 与 ``enhance`` 各自一套阈值与熔断，互不读取对方的键。
    upscale: UpscaleConfig = field(default_factory=UpscaleConfig)
    # 参考图文字降级：默认关闭；关闭时链路与 v1.2.0 等价（契约 F6）。
    reference_fallback: ReferenceFallbackConfig = field(default_factory=ReferenceFallbackConfig)
    # 实验遮挡：默认关闭，且只有降级阶段才可能用到（t8 rev3）；首轮零改动。
    experimental_reference: ExperimentalReferenceConfig = field(
        default_factory=ExperimentalReferenceConfig,
    )


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
