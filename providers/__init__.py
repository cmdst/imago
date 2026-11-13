from .custom import CustomEndpointAdapter
from .dashscope import DashScopeMultimodalAdapter
from .gemini import GeminiAdapter
from .native import NativeAdapter
from .openai_chat import OpenAIChatAdapter
from .openai_image import OpenAIImageAdapter
from .xai_image import XaiImageAdapter

# api_type -> adapter. ``core.config.API_TYPES`` must contain exactly these keys;
# the copy is hand-maintained because ``core`` must not import ``providers``
# (providers depend on core, never the other way round). test/test_native_adapter.py
# asserts the equality so the two lists cannot drift apart silently.
# v1.3.0: six legacy types keep their order, ``xai_image`` is appended last (契约 C1).
ADAPTERS = {
    "openai_image": OpenAIImageAdapter,
    "openai_chat": OpenAIChatAdapter,
    "gemini_official": GeminiAdapter,
    "dashscope_multimodal": DashScopeMultimodalAdapter,
    "custom_endpoint": CustomEndpointAdapter,
    "astrbot_native": NativeAdapter,
    "xai_image": XaiImageAdapter,
}
