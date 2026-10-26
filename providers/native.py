"""Native (AstrBot provider) image adapter.

Routes image generation through a provider already configured inside AstrBot
(the ``astrbot_provider_id`` node field) instead of talking HTTP directly. This
is the migration target of the big-banana bridge: the reverse-proxy / web-model
node is registered in AstrBot, and this adapter only moves image references
around.

Design rules (see ``DESIGN.md`` §2):

- ``session`` and ``api_key`` are ignored: authentication and transport belong
  to the AstrBot provider, whose ``provider_source`` holds the real key.
- The adapter never decodes, downloads or sniffs images. It emits *references*
  (``http(s)://``, ``data:image/...`` or ``base64://``) and lets
  ``imago_fusion.core.network.fetch_reference``/``materialize_result`` do the
  decoding, size check, magic-number check and private-network guard. Downloading
  here would duplicate those guards and silently drift from them.
- Only the four documented reference prefixes are accepted. Bare local paths and
  ``file://`` are rejected: see DESIGN §4 A7, where the original plugin allowed
  a provider-supplied message chain to read arbitrary host files.
"""

from __future__ import annotations

import base64
import re
from collections.abc import AsyncIterator
from typing import Any

from ..core import runtime
from ..core.errors import NoOutputError, ProviderError
from ..core.models import GenerationRequest, ImageResult, ProviderConfig
from .base import ProviderAdapter

# Same Markdown image semantics as the original bridge (big-banana
# ``core/providers/utils.py``): non-greedy alt text, ``<...>`` wrapper stripped.
MARKDOWN_IMAGE = re.compile(r"!\[.*?\]\((.*?)\)")

# Reference prefixes accepted from a provider response. Anything else (bare
# relative/absolute local paths, ``file://``, unknown schemes) is skipped.
_ACCEPTED_PREFIXES = ("http://", "https://", "data:image/", "base64://")

# Cap for the diagnostic text kept from a response without images.
_DETAIL_LIMIT = 200


def _accepted_reference(raw: Any) -> str:
    """Return the reference when it is one of the four allowed forms, else ``""``.

    Args:
        raw: Candidate reference taken from an ``Image`` component or a Markdown
            image link.

    Returns:
        The stripped reference, or an empty string when it must be rejected.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        # Protocol-relative URL: providers do emit these. http is the pragmatic
        # choice because a reverse proxy in front of the upstream is normally
        # plain http; an https-only redirect would fail loudly instead of
        # silently fetching plaintext.
        return f"http:{text}"
    return text if text.startswith(_ACCEPTED_PREFIXES) else ""


class NativeAdapter(ProviderAdapter):
    """Generate images through an AstrBot-native provider (reverse proxy / web model).

    The node only needs ``astrbot_provider_id``; ``base_url`` and ``api_keys`` are
    irrelevant because AstrBot performs the request with the key stored in its own
    ``provider_sources`` entry.
    """

    def __init__(self, config: ProviderConfig, *, context: Any = None) -> None:
        super().__init__(config, context=context)
        # References already emitted for this task; a streaming provider repeats
        # the complete result in its final frame, and a retried non-stream call
        # can return the same picture again.
        self._referenced: set[str] = set()

    async def generate(
        self, session: Any, request: GenerationRequest, api_key: str
    ) -> list[ImageResult]:
        """Call the configured AstrBot provider and return image references.

        Count semantics: a single chat call usually returns one image, so the
        provider is called in a loop until ``count`` distinct references are
        collected, capped at four calls. A missing provider id aborts the loop
        immediately (a wrong id cannot become correct on a retry) so the
        scheduler can fall back to the next node. Individual call failures are
        logged and reported; a partially successful node returns the images it
        did collect instead of failing the whole task.

        Args:
            session: Ignored. Kept for the :class:`ProviderAdapter` interface.
            request: Generation request; ``references`` become ``data:`` URLs and
                ``count`` bounds the number of provider calls.
            api_key: Ignored. The AstrBot provider carries its own credentials.

        Returns:
            One or more ``ImageResult`` objects carrying references (never bytes).

        Raises:
            ProviderError: The node is misconfigured, the provider is missing, or
                every provider call failed.
            NoOutputError: The provider answered but exposed no usable image
                reference (the scheduler settles such tasks as NO_OUTPUT).
        """
        provider_id = self.config.astrbot_provider_id.strip()
        if not provider_id:
            raise ProviderError(
                "原生节点未选择 AstrBot Provider（astrbot_provider_id 为空）"
            )
        context = self.context if self.context is not None else runtime.context()
        if context is None:
            raise ProviderError("原生节点缺少 AstrBot context，无法调用 Provider")
        image_urls = [
            f"data:{item.mime_type};base64,{base64.b64encode(item.data or b'').decode()}"
            for item in request.references
        ]
        # A single chat call usually returns a single image, so the request is
        # retried up to ``count`` times to honour the requested image count.
        attempts = max(1, min(int(request.count or 1), 4))
        texts: list[str] = []
        last_error: Exception | None = None
        results: list[ImageResult] = []
        for index in range(attempts):
            try:
                if self.config.stream:
                    found, text = await self._call_stream(
                        context, provider_id, request.prompt, image_urls
                    )
                else:
                    found, text = await self._call_once(
                        context, provider_id, request.prompt, image_urls
                    )
            except ProviderError as exc:
                last_error = exc
                self._log_attempt_error(provider_id, index + 1, attempts, exc)
                if "不存在" in str(exc):
                    # A wrong provider id will not become correct on a retry: stop
                    # immediately so the scheduler can fall back to the next node.
                    break
                continue
            if text:
                texts.append(text)
            for item in found:
                if item.url not in self._referenced:
                    self._referenced.add(item.url)
                    results.append(item)
            if len(results) >= attempts:
                break
        if results:
            return results
        if last_error is not None:
            raise last_error
        raise NoOutputError(
            f"原生 Provider {provider_id} 未返回图片数据{self._text_detail(texts)}"
        )

    async def _call_once(
        self, context: Any, provider_id: str, prompt: str, image_urls: list[str]
    ) -> tuple[list[ImageResult], str]:
        """Single non-stream call through ``context.llm_generate``.

        ``request_max_retries=1`` keeps AstrBot from retrying a request the node
        timeout is about to abort (DESIGN §2.2). ``max_retries`` is disabled on
        the provider client so the SDK loop cannot spend the node budget on a
        second attempt.

        Args:
            context: AstrBot plugin context used to dispatch the call.
            provider_id: AstrBot chat provider id, e.g. ``cpa_Gemini/web-gemini-image``.
            prompt: Prompt text sent to the provider.
            image_urls: Reference images already encoded as ``data:`` URLs.

        Returns:
            A tuple of ``(image references, response text)``.

        Raises:
            ProviderError: The provider is missing or the call failed.
        """
        try:
            provider = await context.provider_manager.get_provider_by_id(provider_id)
        except Exception:
            # Provider lookup is best-effort: the dispatch below reports the
            # real failure when the id is wrong or the manager is unavailable.
            provider = None
        _disable_provider_retries(provider)
        try:
            resp = await context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                image_urls=image_urls or None,
                request_max_retries=1,
            )
        except Exception as exc:
            if _is_provider_missing(exc):
                raise ProviderError(f"原生 Provider {provider_id} 不存在") from exc
            raise ProviderError(
                f"原生 Provider 请求失败 ({type(exc).__name__})"
            ) from exc
        return self._extract(resp)

    async def _call_stream(
        self, context: Any, provider_id: str, prompt: str, image_urls: list[str]
    ) -> tuple[list[ImageResult], str]:
        """Streaming call through ``provider.text_chat_stream``.

        AstrBot streams partial frames and repeats the complete result as the last
        frame, so frames are extracted one by one and de-duplicated instead of
        trusting the last frame only (DESIGN §2.3).

        Args:
            provider_id: AstrBot chat provider id.
            prompt: Prompt text sent to the provider.
            image_urls: Reference images already encoded as ``data:`` URLs.

        Returns:
            A tuple of ``(image references, concatenated response text)``.

        Raises:
            ProviderError: The provider is missing or the stream failed.
        """
        try:
            provider = await context.provider_manager.get_provider_by_id(provider_id)
        except Exception as exc:
            raise ProviderError(
                f"原生 Provider 请求失败 ({type(exc).__name__})"
            ) from exc
        if provider is None:
            raise ProviderError(f"原生 Provider {provider_id} 不存在")
        _disable_provider_retries(provider)
        try:
            frames: AsyncIterator[Any] = provider.text_chat_stream(
                prompt=prompt,
                image_urls=image_urls or None,
                request_max_retries=1,
            )
            images: list[ImageResult] = []
            texts: list[str] = []
            async for frame in frames:
                found, text = self._extract(frame)
                images.extend(found)
                if text:
                    texts.append(text)
        except Exception as exc:
            raise ProviderError(
                f"原生 Provider 流式请求失败 ({type(exc).__name__})"
            ) from exc
        return images, "".join(texts)

    def _extract(self, response: Any) -> tuple[list[ImageResult], str]:
        """Extract image references from one ``LLMResponse``.

        Two extraction levels, in the order documented by DESIGN §2.4:

        1. ``result_chain`` ``Image`` components (``url`` first, then ``file``);
        2. Markdown image links inside ``completion_text``.

        Everything becomes a reference string handed to the plugin's network
        layer, which performs decoding, size, magic-number and private-network
        checks.

        Args:
            response: ``astrbot.core.provider.entities.LLMResponse`` (or a test double).

        Returns:
            A tuple of ``(image references, response text)``. The text is only
            returned when the response carried no image reference at all, so a
            fully failed node can show the model's own reason.
        """
        images: list[ImageResult] = []
        chain = getattr(response, "result_chain", None)
        components = getattr(chain, "chain", None) if chain is not None else None
        for component in list(components or []):
            # Imported lazily: the module is only importable inside AstrBot, and
            # pure-logic tests import this adapter without a running AstrBot.
            from astrbot.api.message_components import Image as MessageImage

            if not isinstance(component, MessageImage):
                continue
            reference = _accepted_reference(
                getattr(component, "url", "") or getattr(component, "file", "")
            )
            if reference:
                images.append(ImageResult(url=reference))
            else:
                # Never echo the rejected reference: it may embed credentials.
                runtime.debug_log(
                    "原生 Provider 结果消息链图片引用已跳过（仅允许 http/https/data/base64）"
                )
        text = str(getattr(response, "completion_text", "") or "")
        if images or not text:
            return images, ""
        for match in MARKDOWN_IMAGE.finditer(text):
            source = match.group(1).strip()
            if source.startswith("<") and source.endswith(">"):
                # Markdown allows wrapping the target in angle brackets.
                source = source[1:-1].strip()
            if source.startswith("data:image/"):
                images.append(ImageResult(url=source))
                continue
            reference = _accepted_reference(source)
            if reference:
                images.append(ImageResult(url=reference))
            else:
                runtime.debug_log(
                    "原生 Provider 结果 Markdown 图片引用已跳过（仅允许 http/https/data/base64）"
                )
        if images:
            return images, ""
        return images, text

    def _log_attempt_error(
        self, provider_id: str, index: int, attempts: int, exc: Exception
    ) -> None:
        """Report one failed provider attempt for diagnostics.

        Args:
            provider_id: AstrBot provider id being attempted.
            index: 1-based attempt number.
            attempts: Total planned attempts.
            exc: Failure raised by the attempt.
        """
        runtime.debug_log(
            "原生 Provider 尝试失败 provider=%s attempt=%d/%d type=%s detail=%s",
            provider_id,
            index,
            attempts,
            type(exc).__name__,
            str(exc),
        )
        runtime.report_provider_error(self.config, exc)

    @staticmethod
    def _text_detail(texts: list[str]) -> str:
        """Build a short, redacted diagnostic suffix from response texts.

        Args:
            texts: Response texts collected from failed attempts.

        Returns:
            A ``: detail`` suffix, or an empty string when there is nothing to show.
        """
        if not texts:
            return ""
        from ..core.security import redact_debug

        detail = redact_debug(" ".join(texts)).strip()[:_DETAIL_LIMIT]
        return f": {detail}" if detail else ""


def _disable_provider_retries(provider: Any) -> None:
    """Disable the OpenAI SDK retry loop for one AstrBot-native call.

    ``request_max_retries=1`` only bounds the ``tenacity`` wrapper around the
    call; the ``AsyncOpenAI`` client built by AstrBot keeps its own default
    ``max_retries=2`` and retries timeouts and connection errors on its own
    (``openai/_base_client.py``). Image generation through a reverse proxy
    already runs minutes long, so a hidden second attempt can outlive the node
    timeout: the scheduler kills the second request while it is running and the
    task settles with no output and no fallback.

    Args:
        provider: Provider instance returned by AstrBot's provider manager. It
            may be a test double, so every attribute lookup is defensive.
    """
    client = getattr(provider, "client", None)
    max_retries = getattr(client, "max_retries", None)
    if not isinstance(max_retries, int) or max_retries <= 0:
        return
    client.max_retries = 0
    logger = getattr(provider, "logger", None)
    if logger is not None and hasattr(logger, "debug"):
        # The SDK reads ``max_retries`` on every request, so one assignment
        # covers the whole adapter call.
        logger.debug(
            "原生 Provider SDK 重试已关闭（原 max_retries=%d），单次请求由节点预算兜底",
            max_retries,
        )


def _is_provider_missing(exc: Exception) -> bool:
    """Detect an AstrBot "provider not found" failure.

    ``astrbot.core.exceptions.ProviderNotFoundError`` is not importable outside
    AstrBot, so the class name is matched instead; the exception chain is walked
    because AstrBot may wrap it.

    Args:
        exc: Exception raised by the provider call.

    Returns:
        True when the failure means "no such provider id".
    """
    current: BaseException | None = exc
    while current is not None:
        if type(current).__name__ == "ProviderNotFoundError":
            return True
        current = current.__cause__ or current.__context__
    return False
