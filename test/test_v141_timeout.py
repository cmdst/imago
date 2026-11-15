"""v1.4.1 C-TIMEOUT：三层超时的行为用例（T-X01…T-X08）。

判据（contract rev2 §5）：

* **L1** 节点/单次调用超时 ⇒ 包成 :class:`ProviderTimeout`，记
  ``<node>:<model>:ProviderTimeout`` 后继续 fallback，**绝不**冒充任务级超时；
* **L2** 只有真实 deadline（``created_at + generation_timeout``）到期才是
  ``TIMED_OUT``，且 ``task.errors`` 必须有 ``timeout`` 与 ``last_provider_error``；
* 准备/发送阶段的 ``TimeoutError`` 属内部异常 ⇒ 普通失败（不给用户"超时"文案）；
* 取消立即上抛；NoOutput 优先；先失败后成功不得污染 ``errors``；退款只看终态。
"""

import asyncio
import time
import unittest
from unittest.mock import patch

import aiohttp

from imago_fusion.core.errors import NoOutputError, ProviderError
from imago_fusion.core.models import (
    DrawTask,
    GenerationRequest,
    ImageInput,
    ImageResult,
    ProviderConfig,
    RuntimeConfig,
    TaskState,
)
from imago_fusion.providers import ADAPTERS
from imago_fusion.services.quota_store import terminal_refund_amount
from imago_fusion.services.scheduler import TaskScheduler

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _node(provider_id="node_a", *, timeout=90, model="m1", api_type="openai_image"):
    return ProviderConfig(
        provider_id, api_type, "https://example.invalid/v1", ("key",),
        model=model, timeout=timeout,
    )


def _config(**overrides):
    options = {"providers": (_node(),), "generation_timeout": 300}
    options.update(overrides)
    return RuntimeConfig(**options)


def _task(*, created_at=None, count=1):
    task = DrawTask(
        "d" * 32, "aiocqhttp:GroupMessage:1",
        GenerationRequest("draw", count=count, references=(ImageInput(PNG, "image/png"),)),
    )
    task.created_at = time.monotonic() if created_at is None else created_at
    task.runtime["original_user_prompt"] = "draw"
    return task


class _Outcome:
    def __init__(self, success=True):
        self.success = success
        self.error = ""
        self.side_effects_started = True
        self.side_send_started = True
        self.side_send_error = ""


def _scheduler(config, *, runner=None):
    async def default_runner(task, results):
        return _Outcome(True)

    # 第四参是"错误上报函数" ``logger(provider, exc, task)``（main 里绑的是
    # ``ImagoFusion._provider_error``），不是 logging.Logger。
    scheduler = TaskScheduler(
        lambda: config, None, runner or default_runner,
        lambda provider, exc, task: None,
    )
    return scheduler


def _adapter(behaviour):
    class _Adapter:
        def __init__(self, config):
            self.config = config

        async def generate(self, session, request, key):
            return await behaviour(self.config, request)

    return _Adapter


class NodeTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_node_timeout_is_wrapped_as_provider_timeout(self):
        async def slow(config, request):
            await asyncio.sleep(5)
            return [ImageResult(data=PNG, mime_type="image/png")]

        config = _config(providers=(_node(timeout=0.05),))
        task = _task()
        scheduler = _scheduler(config)
        with patch.dict(ADAPTERS, {"openai_image": _adapter(slow)}, clear=False):
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        errors = task.runtime["attempt_errors"]
        self.assertEqual(errors[-1], "node_a:m1:ProviderTimeout")
        # 没有冒充任务级超时：state 仍由 _execute 决定，这里根本不该是 TIMED_OUT。
        self.assertNotEqual(task.state, TaskState.TIMED_OUT)

    async def test_text_stage_skips_when_budget_below_guard(self):
        """保护余量不足 ⇒ 连识图都不发起（``budget`` 跳过），绝不挤占发送预算。"""
        from imago_fusion.core.models import ReferenceFallbackConfig
        from imago_fusion.core.references import RejectionVerdict

        calls = {"caption": 0}

        async def captioner(task, config, prompt, references, *, timeout, max_chars):
            calls["caption"] += 1
            return "description"

        config = _config(
            reference_fallback=ReferenceFallbackConfig(enable=True, vision_timeout_seconds=20),
            vision_provider_id="vision",
        )
        task = _task()
        scheduler = _scheduler(config)
        scheduler.captioner = captioner
        verdict = RejectionVerdict(kind="safety", convertible=True, evidence="policy", detail="")
        outcome = await scheduler._text_stage(
            task, config, _node(),
            GenerationRequest("draw", references=(ImageInput(PNG, "image/png"),)),
            verdict, status=400, deadline=time.monotonic() + 5.0, text_quota=1,
        )
        self.assertIsNone(outcome.results)
        self.assertEqual(calls["caption"], 0)  # 预算不够就不识图（省钱也省时）
        info = task.runtime["reference_fallback"]
        self.assertEqual(info["nodes"][0]["reason"], "budget")


class ExecuteTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_nodes_timeout_within_budget_is_failed(self):
        async def slow(config, request):
            await asyncio.sleep(5)
            return [ImageResult(data=PNG, mime_type="image/png")]

        config = _config(providers=(_node(timeout=0.05), _node("node_b", timeout=0.05)))
        task = _task()
        scheduler = _scheduler(config)
        with patch.dict(ADAPTERS, {"openai_image": _adapter(slow)}, clear=False):
            await scheduler._execute(task)
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertTrue(task.errors)
        self.assertIn("ProviderTimeout", " ".join(task.runtime["attempt_errors"]))
        self.assertNotEqual(terminal_refund_amount(task.state, 1), 0)

    async def test_real_budget_exhaustion_is_timed_out_with_reason(self):
        async def slow_runner(task, results):
            await asyncio.sleep(2)
            return _Outcome(True)

        async def instant(config, request):
            return [ImageResult(data=PNG, mime_type="image/png")]

        config = _config(generation_timeout=1)
        task = _task()
        scheduler = _scheduler(config, runner=slow_runner)
        with patch.dict(ADAPTERS, {"openai_image": _adapter(instant)}, clear=False):
            await scheduler._execute(task)
        self.assertEqual(task.state, TaskState.TIMED_OUT)
        self.assertEqual(task.errors, ["timeout"])
        self.assertEqual(task.runtime["last_provider_error"], "任务超时")
        self.assertEqual(task.runtime["timeout_kind"], "task_budget")
        self.assertEqual(terminal_refund_amount(task.state, 1), 0)

    async def test_deadline_uses_created_at_not_execution_start(self):
        seen = {"elapsed": None}

        async def slow_runner(task, results):
            # 终态通知路径会用空结果再调一次 runner，这里只给真实投递计时。
            if not results:
                return _Outcome(True)
            started = time.monotonic()
            try:
                await asyncio.sleep(2)
            finally:
                seen["elapsed"] = time.monotonic() - started
            return _Outcome(True)

        async def instant(config, request):
            return [ImageResult(data=PNG, mime_type="image/png")]

        config = _config(generation_timeout=1.0)
        # 排队已经吃掉 0.7 s：真正可用的只剩 0.3 s，而不是整 1.0 s。
        task = _task(created_at=time.monotonic() - 0.7)
        scheduler = _scheduler(config, runner=slow_runner)
        with patch.dict(ADAPTERS, {"openai_image": _adapter(instant)}, clear=False):
            await scheduler._execute(task)
        self.assertEqual(task.state, TaskState.TIMED_OUT)
        self.assertIsNotNone(seen["elapsed"])
        self.assertLess(seen["elapsed"], 0.8)  # 0.3 s 处被取消，而不是重新计时 1 s
        self.assertGreaterEqual(float(task.runtime.get("timeout_elapsed", 0)), 0.9)

    async def test_cancel_propagates_and_refunds(self):
        async def cancelling_runner(task, results):
            raise asyncio.CancelledError()

        async def instant(config, request):
            return [ImageResult(data=PNG, mime_type="image/png")]

        config = _config()
        task = _task()
        scheduler = _scheduler(config, runner=cancelling_runner)
        with patch.dict(ADAPTERS, {"openai_image": _adapter(instant)}, clear=False):
            with self.assertRaises(asyncio.CancelledError):
                await scheduler._execute(task)
        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertEqual(terminal_refund_amount(task.state, 1), 1)

    async def test_prepare_timeout_is_internal_failure_not_task_timeout(self):
        async def prepare(task):
            raise asyncio.TimeoutError()

        async def instant(config, request):  # pragma: no cover - 不会走到
            return [ImageResult(data=PNG, mime_type="image/png")]

        config = _config()
        task = _task()
        task.runtime["prepare"] = prepare
        scheduler = _scheduler(config)
        with patch.dict(ADAPTERS, {"openai_image": _adapter(instant)}, clear=False):
            await scheduler._execute(task)
        self.assertEqual(task.state, TaskState.FAILED)
        self.assertEqual(task.errors, ["TimeoutError"])
        self.assertNotEqual(task.runtime.get("last_provider_error"), "任务超时")

    async def test_no_output_wins_over_last_error_and_does_not_refund(self):
        async def no_output(config, request):
            raise NoOutputError("no image", status=200)

        config = _config(providers=(_node(), _node("node_b")))
        task = _task()
        scheduler = _scheduler(config)
        with patch.dict(ADAPTERS, {"openai_image": _adapter(no_output)}, clear=False):
            await scheduler._execute(task)
        self.assertEqual(task.state, TaskState.NO_OUTPUT)
        self.assertEqual(terminal_refund_amount(task.state, 1), 0)

    async def test_success_after_failure_is_clean(self):
        async def flaky_factory():
            calls = {"n": 0}

            async def flaky(config, request):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise ProviderError("upstream 500", status=500)
                return [ImageResult(data=PNG, mime_type="image/png")]

            return flaky

        flaky = await flaky_factory()
        config = _config(providers=(_node(), _node("node_b")))
        task = _task()
        scheduler = _scheduler(config)
        with patch.dict(ADAPTERS, {"openai_image": _adapter(flaky)}, clear=False):
            await scheduler._execute(task)
        self.assertEqual(task.state, TaskState.SUCCEEDED)
        self.assertEqual(task.errors, [])
        # 失败的那次只留在 runtime 诊断里，不污染终态展示。
        self.assertTrue(task.runtime["attempt_errors"])
        self.assertNotIn("部分图片绘制失败。", " ".join(str(item) for item in task.errors))


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_records_phase_bytes_and_timeout(self):
        async def disconnect(config, request):
            raise aiohttp.ServerDisconnectedError()

        config = _config(providers=(_node(timeout=42),))
        task = _task()
        scheduler = _scheduler(config)
        with patch.dict(ADAPTERS, {"openai_image": _adapter(disconnect)}, clear=False):
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        entries = task.runtime.get("attempt_diagnostics")
        self.assertTrue(entries)
        entry = entries[-1]
        # 没有分片回调、也没有收到响应头 ⇒ 唯一诚实的答案是 unknown（CORE-05）。
        self.assertEqual(entry["phase"], "unknown")
        self.assertEqual(entry["up_bytes"], "unknown")
        self.assertIn("elapsed", entry)
        self.assertEqual(entry["timeout"], 42)
        self.assertEqual(entry["provider_id"], "node_a")
        self.assertNotIn("http", str(entry.get("detail", "")))  # 不落完整 URL

    async def test_connector_error_reports_connect_phase(self):
        from aiohttp.client_reqrep import ConnectionKey

        async def refuse(config, request):
            raise aiohttp.ClientConnectorError(
                ConnectionKey("example.invalid", 443, True, True, None, None, None),
                OSError("dns"),
            )

        config = _config(providers=(_node(timeout=7),))
        task = _task()
        scheduler = _scheduler(config)
        with patch.dict(ADAPTERS, {"openai_image": _adapter(refuse)}, clear=False):
            with self.assertRaises(ProviderError):
                await scheduler._generate(task)
        entry = task.runtime["attempt_diagnostics"][-1]
        # 连接失败是**可观察事实**：异常类型自己就说明了阶段。
        self.assertEqual(entry["phase"], "connect")
        self.assertEqual(entry["timeout"], 7)

    async def test_up_bytes_is_a_plain_int_when_chunks_were_observed(self):
        from imago_fusion.core import diagnostics

        task = _task()
        trace = diagnostics.AttemptTrace().begin()
        trace.record_chunk(512)
        token = diagnostics.push(trace)
        try:
            diagnostics.record(
                task, provider_id="node_a", model="m1",
                phase=diagnostics.PHASE_UNKNOWN, elapsed=0.4,
                up_bytes=trace.up_bytes, timeout=90, error_type="ClientError",
                detail="https://user:pass@example.invalid/secret?token=abc",
            )
        finally:
            diagnostics.pop(token)
        entry = task.runtime["attempt_diagnostics"][-1]
        self.assertEqual(entry["up_bytes"], 512)
        # detail 过 redact_debug + 截断：不带凭据、不带完整 URL。
        self.assertNotIn("pass@", str(entry["detail"]))
        self.assertLessEqual(len(str(entry["detail"])), 160)

    async def test_text_stage_records_attempt_diagnostic(self):
        from imago_fusion.core.models import ReferenceFallbackConfig
        from imago_fusion.core.references import RejectionVerdict

        async def captioner(task, config, prompt, references, *, timeout, max_chars):
            # 识图成功 ⇒ 真的发起一次纯文字请求；请求失败也要留一条诊断（验收 8）。
            return "一名短发女性站在窗边"

        async def boom(config, request):
            raise ProviderError("参考图被拒", status=400)

        config = _config(
            providers=(_node(timeout=30),),
            vision_provider_id="vision",
            reference_fallback=ReferenceFallbackConfig(enable=True, max_retries_per_node=1),
        )
        task = _task()
        scheduler = _scheduler(config)
        scheduler.captioner = captioner
        scheduler.info = lambda *a, **k: None
        scheduler.warn = lambda *a, **k: None
        verdict = RejectionVerdict(
            kind="safety", convertible=True, evidence="block:SAFETY", detail=""
        )
        request = GenerationRequest("draw", references=(ImageInput(PNG, "image/png"),))
        with patch.dict(ADAPTERS, {"openai_image": _adapter(boom)}, clear=False):
            outcome = await scheduler._text_stage(
                task, config, _node(timeout=30), request, verdict,
                status=400, deadline=time.monotonic() + 250, text_quota=1,
            )
        self.assertIsNone(outcome.results)
        entry = task.runtime["attempt_diagnostics"][-1]
        # 配置里没有 id=vision 的节点时，文字请求复用触发它的那个节点。
        self.assertEqual(entry["provider_id"], "node_a")
        self.assertEqual(entry["error_type"], "ProviderError")
        self.assertEqual(entry["phase"], "unknown")
        self.assertEqual(entry["up_bytes"], "unknown")

    async def test_node_timeouts_are_visible_in_errors_and_runtime(self):
        async def slow(config, request):
            await asyncio.sleep(5)
            return [ImageResult(data=PNG, mime_type="image/png")]

        infos: list[str] = []
        config = _config(
            providers=(_node("node_a", timeout=0.05), _node("node_b", timeout=0.05)),
        )
        task = _task()
        scheduler = _scheduler(config)
        scheduler.info = lambda task_, message, *args: infos.append(
            message % args if args else message
        )
        with patch.dict(ADAPTERS, {"openai_image": _adapter(slow)}, clear=False):
            await scheduler._execute(task)
        self.assertEqual(task.state, TaskState.FAILED)
        # ① errors：两个节点各一条脱敏标记（节点 id + 预算，无 URL/图片）。
        markers = [item for item in task.errors if item.startswith("ProviderTimeout node=")]
        self.assertEqual(len(markers), 2)
        self.assertTrue(any("node=node_a" in item for item in markers))
        self.assertTrue(any("node=node_b" in item for item in markers))
        # ② runtime：同一份事实，给 manifest / INFO 复用。
        timeouts = task.runtime["timeouts"]
        self.assertEqual([entry["node"] for entry in timeouts], ["node_a", "node_b"])
        self.assertTrue(all(entry["model"] == "m1" for entry in timeouts))
        self.assertTrue(all("elapsed" in entry for entry in timeouts))
        # ③ INFO：节点超时本身也要有一条可读行（与 errors/manifest 同源）。
        self.assertEqual(len([line for line in infos if "节点超时 node=" in line]), 2)


if __name__ == "__main__":
    unittest.main()
