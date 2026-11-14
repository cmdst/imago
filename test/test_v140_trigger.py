"""v1.4.0 契约 §10.2：触发判据（T-R1..T-R6）与判定器冻结。

关键点（§3）：只有 ``kind == "safety"`` **且** ``evidence`` 非空的拒绝才触发逐节点
纯文字降级。``convertible`` 不再参与判定（v1.3.0 曾用它把 ``image_unsupported``
也当可降级）；HTTP 状态码本身不是判据，只决定是否允许看错误文本；401/403/429/5xx、
超时、传输错误、纯提示词拒绝、未知错误一律降级到 ``unknown`` → 零识图、零文字调用。
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import tokenize
import unittest
from pathlib import Path

from imago_fusion.core.errors import NoOutputError, ProviderError
from imago_fusion.core.references import (
    CAPTION_MAX_IMAGES,
    EXCLUDED_STATUSES,
    REJECTION_STATUSES,
    classify_rejection,
)
from imago_fusion.services.scheduler import TaskScheduler

REPO_ROOT = Path(__file__).resolve().parent.parent
REFERENCES_PATH = REPO_ROOT / "core" / "references.py"

#: 基线 ``4387189:core/references.py`` 的 `classify_rejection` 源码段 sha256
#: （用 ``ast.get_source_segment`` 取整段，含缩进；N4 要求行为语义冻结）。
BASELINE_CLASSIFY_SHA256 = "d1ba1fe708ee0fca8562425085a41c94329d25e12618b7cb5fdef5b4490dc516"

#: 基线判定器的**语义黄金集**：``(样本名, kind, convertible, evidence)``。
#: 数值取自 ``4387189:core/references.py``（与当前逐字节同源）。任何判定行为漂移
#: （换 kind、丢证据、把 401/5xx/传输错误当安全拒绝）都会让这张表变红；§11.4 允许
#: 的注释/文档措辞改写不影响它。
BASELINE_VERDICTS = (
    ("block_reason SAFETY 400", "safety", False, "block:SAFETY"),
    ("code content_policy 200", "safety", False, "code:content_policy"),
    ("finish_reason safety 400", "safety", False, "finish:safety"),
    ("text safety 400", "safety", False, "text"),
    ("unsupported 400", "image_unsupported", True, "text"),
    ("invalid 400", "image_invalid", True, "text"),
    ("textonly 400", "unknown", False, "text"),
    ("generic 400", "unknown", False, "text"),
    ("401", "unknown", False, "status:401"),
    ("429", "unknown", False, "status:429"),
    ("503", "unknown", False, "status:503"),
    ("transport", "unknown", False, "source:transport"),
    ("body", "unknown", False, "source:body"),
    ("no_output safety 200", "safety", False, "block:SAFETY"),
    ("no_output plain", "unknown", False, "status:None"),
    ("size too large", "image_invalid", True, "text"),
)

_SAMPLES = {
    "block_reason SAFETY 400": (lambda: ProviderError("blocked", status=400, block_reason="SAFETY"), "openai_image"),
    "code content_policy 200": (lambda: ProviderError("blocked", status=200, code="content_policy"), "openai_image"),
    "finish_reason safety 400": (lambda: ProviderError("x", status=400, finish_reason="safety"), "gemini_image"),
    "text safety 400": (lambda: ProviderError("blocked by safety policy", status=400), "openai_image"),
    "unsupported 400": (lambda: ProviderError("This model does not support image input", status=400), "openai_image"),
    "invalid 400": (lambda: ProviderError("Invalid image format", status=400), "openai_image"),
    "textonly 400": (lambda: ProviderError("only text prompt is supported", status=400), "openai_image"),
    "generic 400": (lambda: ProviderError("bad request", status=400), "openai_image"),
    "401": (lambda: ProviderError("unauthorized", status=401), "openai_image"),
    "429": (lambda: ProviderError("rate limited", status=429), "openai_image"),
    "503": (lambda: ProviderError("down", status=503), "openai_image"),
    "transport": (lambda: ProviderError("reset", status=400, source="transport"), "openai_image"),
    "body": (lambda: ProviderError("blocked", status=400, source="body"), "openai_image"),
    "no_output safety 200": (lambda: NoOutputError("no image", status=200, block_reason="SAFETY"), "astrbot_native"),
    "no_output plain": (lambda: NoOutputError("no image"), "astrbot_native"),
    "size too large": (lambda: ProviderError("image size exceeds 20MB", status=400), "dashscope_multimodal"),
}


def _code_hash(text: str) -> str:
    """注释/空行不敏感的 token 流哈希（行号也去掉，避免注释增删影响）。"""
    parts = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING):
            continue
        parts.append("%d:%s" % (tok.type, tok.string))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _function_hash(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(text, node)
            return hashlib.sha256(segment.encode("utf-8")).hexdigest()
    raise AssertionError("缺少函数 " + name)


class TriggerPredicateTests(unittest.TestCase):
    """T-R1 / T-R4：只认 safety+evidence，``convertible`` 不参与。"""

    def test_r1_structured_safety_triggers(self):
        verdict = classify_rejection(ProviderError("blocked", status=400, block_reason="SAFETY"))
        self.assertEqual(verdict.kind, "safety")
        self.assertTrue(verdict.evidence)
        self.assertTrue(TaskScheduler._is_safety_rejection(verdict))

    def test_r1_http200_structured_safety_triggers(self):
        verdict = classify_rejection(ProviderError("blocked", status=200, code="content_policy"))
        self.assertEqual(verdict.kind, "safety")
        self.assertTrue(TaskScheduler._is_safety_rejection(verdict))

    def test_r1_text_safety_with_rejectable_status_triggers(self):
        verdict = classify_rejection(ProviderError("safety violation", status=400))
        self.assertEqual(verdict.kind, "safety")
        self.assertTrue(TaskScheduler._is_safety_rejection(verdict))

    def test_r4_image_unsupported_does_not_trigger_despite_convertible(self):
        verdict = classify_rejection(ProviderError("This model does not support image input", status=400))
        self.assertEqual(verdict.kind, "image_unsupported")
        self.assertTrue(verdict.convertible)
        # v1.3.0 的判据会是 convertible=True → 降级；v1.4.0 必须是 False。
        self.assertFalse(TaskScheduler._is_safety_rejection(verdict))

    def test_r4_image_invalid_does_not_trigger(self):
        verdict = classify_rejection(ProviderError("Invalid image format", status=400))
        self.assertEqual(verdict.kind, "image_invalid")
        self.assertFalse(TaskScheduler._is_safety_rejection(verdict))

    def test_r4_text_rejected_does_not_trigger(self):
        verdict = classify_rejection(
            ProviderError("only text prompt is supported", status=400),
        )
        self.assertFalse(TaskScheduler._is_safety_rejection(verdict))

    def test_r5_safety_without_evidence_does_not_trigger(self):
        class _Verdict:
            kind = "safety"
            evidence = ""

        self.assertFalse(TaskScheduler._is_safety_rejection(_Verdict()))


class StatusAndTransportTests(unittest.TestCase):
    """T-R2 / T-R3：状态码单独不构成依据；网络/超时/鉴权/限流/5xx 都是 unknown。"""

    def test_r2_generic_400_text_is_not_safety(self):
        verdict = classify_rejection(ProviderError("bad request", status=400))
        self.assertNotEqual(verdict.kind, "safety")
        self.assertFalse(TaskScheduler._is_safety_rejection(verdict))

    def test_r2_status_alone_never_triggers(self):
        for status in (400, 413, 415, 422):
            verdict = classify_rejection(ProviderError("error", status=status))
            self.assertEqual(verdict.kind, "unknown", status)
            self.assertFalse(TaskScheduler._is_safety_rejection(verdict))

    def test_r3_excluded_statuses_are_unknown(self):
        for status in sorted(EXCLUDED_STATUSES) + [500, 502, 503, 504]:
            verdict = classify_rejection(ProviderError("boom", status=status))
            self.assertEqual(verdict.kind, "unknown", status)

    def test_r3_timeout_is_unknown(self):
        verdict = classify_rejection(asyncio.TimeoutError(), api_type="openai_image")
        self.assertEqual(verdict.kind, "unknown")

    def test_r3_transport_source_is_unknown(self):
        verdict = classify_rejection(ProviderError("reset", status=400, source="transport"))
        self.assertEqual(verdict.kind, "unknown")

    def test_r3_body_source_is_unknown(self):
        verdict = classify_rejection(ProviderError("blocked", status=400, source="body"))
        self.assertEqual(verdict.kind, "unknown")


class NoOutputRoutingTests(unittest.TestCase):
    """T-R1 补充：HTTP 200 结构化拒绝（原生适配器的 NoOutputError）能触发。"""

    def test_structured_no_output_safety_triggers(self):
        exc = NoOutputError("原生 Provider 未返回图片数据", status=200, block_reason="SAFETY")
        verdict = classify_rejection(exc, api_type="astrbot_native")
        self.assertEqual(verdict.kind, "safety")
        self.assertTrue(TaskScheduler._is_safety_rejection(verdict))

    def test_plain_no_output_without_evidence_does_not_trigger(self):
        exc = NoOutputError("原生 Provider 未返回图片数据")
        verdict = classify_rejection(exc, api_type="astrbot_native")
        self.assertNotEqual(verdict.kind, "safety")
        self.assertFalse(TaskScheduler._is_safety_rejection(verdict))


class JudgeFreezeTests(unittest.TestCase):
    """T-R6：判定器与常量冻结（N4）。"""

    def test_r6_function_source_is_frozen(self):
        self.assertEqual(_function_hash(REFERENCES_PATH, "classify_rejection"),
                         BASELINE_CLASSIFY_SHA256)

    def test_r6_verdict_semantics_match_the_baseline_golden_set(self):
        for label, kind, convertible, evidence in BASELINE_VERDICTS:
            factory, api_type = _SAMPLES[label]
            verdict = classify_rejection(factory(), api_type=api_type)
            self.assertEqual(
                (verdict.kind, verdict.convertible, verdict.evidence),
                (kind, convertible, evidence),
                label,
            )

    def test_r6_no_behavioral_test_can_silently_drop_a_sample(self):
        self.assertEqual({row[0] for row in BASELINE_VERDICTS}, set(_SAMPLES))

    def test_r6_constants_are_frozen(self):
        self.assertEqual(CAPTION_MAX_IMAGES, 4)
        self.assertEqual(set(REJECTION_STATUSES), {400, 413, 415, 422})

    def test_r6_allowed_comment_rewrite_does_not_break_the_code_hash(self):
        text = REFERENCES_PATH.read_text(encoding="utf-8")
        mutated = text.replace('"""', '"""', 1)
        # 注释/空行变化不影响代码哈希（证明 §11.4 允许的注释改写不会被误判）。
        self.assertEqual(_code_hash(mutated + "\n# 注释\n\n"), _code_hash(text))
