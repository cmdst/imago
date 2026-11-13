"""v1.3.0：参考图拒绝判定器（先排除层、后判定层；结构化优先于文本）。"""

import asyncio
import unittest

from imago_fusion.core.errors import NoOutputError, ProviderError, ProviderTimeout
from imago_fusion.core.references import CAPTION_MAX_IMAGES, RejectionVerdict, classify_rejection


class TextRejectionTests(unittest.TestCase):
    def test_invalid_image_400_is_convertible(self):
        verdict = classify_rejection(ProviderError("Invalid image format", status=400))
        self.assertEqual(verdict.kind, "image_invalid")
        self.assertTrue(verdict.convertible)

    def test_oversized_image_413_is_convertible(self):
        verdict = classify_rejection(ProviderError("image exceeds 10MB limit", status=413))
        self.assertTrue(verdict.convertible)
        self.assertIn(verdict.kind, {"image_invalid", "image_unsupported"})

    def test_chinese_image_rejection_422_is_convertible(self):
        verdict = classify_rejection(ProviderError("图片过大，无法处理", status=422))
        self.assertTrue(verdict.convertible)

    def test_text_only_model_is_convertible(self):
        verdict = classify_rejection(
            ProviderError("This model does not support image input", status=400),
        )
        self.assertEqual(verdict.kind, "image_unsupported")
        self.assertTrue(verdict.convertible)

    def test_prompt_too_long_is_not_convertible(self):
        verdict = classify_rejection(ProviderError("prompt too long", status=400))
        self.assertEqual(verdict.kind, "text_rejected")
        self.assertFalse(verdict.convertible)

    def test_auth_and_rate_limit_are_excluded(self):
        for status, message in ((401, "Invalid API key"), (429, "rate limit"),
                                (500, "image unsupported")):  # 5xx 一律排除，不看文本
            with self.subTest(status=status):
                verdict = classify_rejection(ProviderError(message, status=status))
                self.assertEqual(verdict.kind, "unknown")
                self.assertFalse(verdict.convertible)

    def test_bad_key_400_is_excluded_not_text_rejected(self):
        verdict = classify_rejection(ProviderError("Invalid API key provided", status=400))
        self.assertEqual(verdict.kind, "unknown")

    def test_message_without_evidence_is_unknown(self):
        verdict = classify_rejection(ProviderError("something went wrong"))
        self.assertEqual(verdict.kind, "unknown")


class StructuredRejectionTests(unittest.TestCase):
    def test_http200_safety_block_is_classified_without_status(self):
        exc = NoOutputError("provider refused", block_reason="SAFETY")
        verdict = classify_rejection(exc)
        self.assertEqual(verdict.kind, "safety")
        self.assertFalse(verdict.convertible)

    def test_http200_structured_status_is_preserved(self):
        exc = NoOutputError("blocked", block_reason="SAFETY")
        exc.status = 200
        verdict = classify_rejection(exc)
        self.assertEqual(verdict.kind, "safety")

    def test_block_reason_other_alone_is_unknown(self):
        verdict = classify_rejection(NoOutputError("other", block_reason="OTHER"))
        self.assertEqual(verdict.kind, "unknown")

    def test_structured_code_with_none_status_is_classified(self):
        verdict = classify_rejection(ProviderError("rejected", code="invalid_image"))
        self.assertEqual(verdict.kind, "image_invalid")
        self.assertTrue(verdict.convertible)

    def test_text_rejected_wins_over_safety(self):
        exc = ProviderError("blocked", status=200, code="prompt_rejected", finish_reason="SAFETY")
        verdict = classify_rejection(exc)
        self.assertEqual(verdict.kind, "text_rejected")
        self.assertFalse(verdict.convertible)

    def test_timeout_and_transport_are_excluded(self):
        self.assertEqual(classify_rejection(asyncio.TimeoutError()).kind, "unknown")
        self.assertEqual(classify_rejection(ProviderTimeout("slow")).kind, "unknown")


class VerdictShapeTests(unittest.TestCase):
    def test_caption_capacity_constant(self):
        self.assertEqual(CAPTION_MAX_IMAGES, 4)

    def test_verdict_is_a_frozen_dataclass(self):
        verdict = RejectionVerdict()
        self.assertEqual(verdict.kind, "unknown")
        with self.assertRaises(Exception):
            verdict.kind = "safety"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
