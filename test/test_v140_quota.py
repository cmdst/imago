"""v1.4.0 契约 §10.4：结算规则不因文字降级而改变（T-Q1..T-Q3）。

降级链路**不额外扣配额**：一次任务仍然只 ``consume(count)`` 一次；纯文字重试
既不加扣也不退款。终态退款沿用既有规则（``terminal_refund_amount``）：
FAILED / CANCELLED 全额退，NO_OUTPUT / SUCCEEDED / TIMED_OUT / PARTIAL_SUCCESS 退 0。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from imago_fusion.core.models import QuotaConfig, TaskState
from imago_fusion.services.quota_store import QuotaStore, terminal_refund_amount


def _policy(**overrides) -> QuotaConfig:
    base = {
        "enabled": True,
        "daily_refresh_enabled": False,
    }
    base.update(overrides)
    return QuotaConfig(**base)


class TerminalRefundMatrixTests(unittest.TestCase):
    """T-Q2 / T-Q3：终态退款矩阵，与 v1.3.0 逐项相同。"""

    def test_q2_failed_refunds_everything(self):
        self.assertEqual(terminal_refund_amount(TaskState.FAILED, 3), 3)

    def test_q2_no_output_refunds_nothing(self):
        self.assertEqual(terminal_refund_amount(TaskState.NO_OUTPUT, 3), 0)

    def test_q1_success_and_partial_refund_nothing(self):
        self.assertEqual(terminal_refund_amount(TaskState.SUCCEEDED, 3), 0)
        self.assertEqual(terminal_refund_amount(TaskState.PARTIAL_SUCCESS, 3), 0)

    def test_timeout_is_not_refunded(self):
        # TIMED_OUT 在 v1.4.0 仍是"已发生调用"，不退；与 FAILED 区分。
        self.assertEqual(terminal_refund_amount(TaskState.TIMED_OUT, 3), 0)

    def test_q3_cancelled_refunds_everything(self):
        self.assertEqual(terminal_refund_amount(TaskState.CANCELLED, 2), 2)

    def test_negative_charge_is_clamped(self):
        self.assertEqual(terminal_refund_amount(TaskState.FAILED, -1), 0)


class ConsumeOnceTests(unittest.TestCase):
    """T-Q1：一次任务只按 count 扣一次，纯文字重试不产生第二次扣费。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = QuotaStore(Path(self._tmp.name), today_provider=lambda: "2026-11-15")
        self.policy = _policy()
        # QuotaConfig 没有 default_quota 字段：记录初始为 0，显式预置余额。
        self.store.adjust("user:1", "set", 100, self.policy)

    def test_q1_consume_is_charged_once_per_task(self):
        first = self.store.consume("user:1", 2, self.policy)
        self.assertTrue(first.allowed)
        self.assertEqual(first.charged, 2)
        # 模拟降级期间两次纯文字生成：不应再调 consume。
        snapshot = self.store.inspect("user:1", self.policy)
        self.assertEqual(snapshot.quota, 98)

    def test_q1_amount_is_count_not_count_times_nodes(self):
        decision = self.store.consume("user:1", 1, self.policy)
        self.assertEqual(decision.charged, 1)
        self.assertEqual(self.store.inspect("user:1", self.policy).quota, 99)

    def test_q2_refund_restores_charge(self):
        decision = self.store.consume("user:1", 4, self.policy)
        refund = terminal_refund_amount(TaskState.FAILED, decision.charged)
        self.store.adjust("user:1", "add", refund, self.policy)
        self.assertEqual(self.store.inspect("user:1", self.policy).quota, 100)

    def test_q3_cancelled_charge_round_trips(self):
        decision = self.store.consume("user:1", 2, self.policy)
        self.store.adjust(
            "user:1", "add", terminal_refund_amount(TaskState.CANCELLED, decision.charged), self.policy,
        )
        self.assertEqual(self.store.inspect("user:1", self.policy).quota, 100)

    def test_quota_file_shape_unchanged(self):
        self.store.consume("user:1", 1, self.policy)
        payload = json.loads((Path(self._tmp.name) / "quotas.json").read_text(encoding="utf-8"))
        self.assertIn("users", payload)
        self.assertIn("user:1", payload["users"])
