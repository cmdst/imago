"""IMAGO·FUSION v1.4.2 C-PERSONA：人格绑定一致性、只用本人格图库、逐节点实发观测。

契约要点（t3 冻结，本文件是它的可执行版本）：

* **绑定**：上传/命令/工具都必须绑定「已核对的那个 Persona ID」。WebUI 的目标在
  **点击那一瞬**锁定，FileReader 期间与之后切换 Persona 都不改变归属；迟到的完成
  回调不得清空/刷新"已经不是它"的文件与界面（基线 a1056441 的 3/3 错绑复现见
  ``test/test_webui_v142.js``，本文件只负责在门禁里跑它）。
* **图库**：只读本 Persona 的目录，不搬旧图、不跨人格借库、不改原图。缺目录、
  空库、枚举/读取失败三者**必须分开**：前两者是正常"没有图"，后者必须安全失败
  （0 provider calls），不许静默伪装成空库。
* **实发**：日志与 manifest 里的人格张数是**插件实际传给适配器的入参**，不是
  "可读池"也不是"上一次 attempt 的残留值"。文字腿严格 0/0。
* **观测**：只用稳定、不可逆、不可枚举的 Persona 别名 + 封闭来源值域；判不出就写
  unknown，绝不臆测；不落原始 ID、名称、提示词、路径或凭据。

全程离线：无网络、无真实 Provider 调用、不写生产目录。
"""

from __future__ import annotations

import io
import json
import random
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from imago_fusion.core import diagnostics as attempt_diagnostics
from imago_fusion.core.errors import ProviderError
from imago_fusion.core.models import (
    DrawTask,
    GenerationRequest,
    ImageInput,
    ImageResult,
    ProviderConfig,
    ReferenceFallbackConfig,
    RuntimeConfig,
    TaskState,
)
from imago_fusion.services import persona_store
from imago_fusion.services import scheduler as scheduler_module
from imago_fusion.services.persona_store import PersonaReferenceError, PersonaStore
from imago_fusion.services.scheduler import ReferencePlan, TaskScheduler

REPO_ROOT = Path(__file__).resolve().parent.parent
JS_TEST = REPO_ROOT / "test" / "test_webui_v142.js"
BASELINE_COMMIT = "a1056441da3f1c921f93559689987bd4dc387faa"
PNG_MIME = "image/png"


def _plugin_class():
    """**惰性**导入插件主类：收集阶段不碰 ``astrbot`` 真模块（同 v141 的理由）。"""

    from imago_fusion.main import ImagoFusion

    return ImagoFusion


def png_bytes(width=64, height=48, seed=0, mode="RGB") -> bytes:
    rng = random.Random(seed)
    channels = 4 if mode == "RGBA" else 3
    payload = bytes(rng.randrange(256) for _ in range(width * height * channels))
    buffer = io.BytesIO()
    Image.frombytes(mode, (width, height), payload).save(buffer, format="PNG")
    return buffer.getvalue()


def _image(seed=0, name="ref.png") -> ImageInput:
    return ImageInput(png_bytes(48, 32, seed=seed), PNG_MIME, name)


def _persona_image(seed=100, name=None) -> ImageInput:
    payload = png_bytes(40, 30, seed=seed)
    return ImageInput(payload, PNG_MIME, name or f"persona-{seed}.png")


class _Logger:
    """记录 INFO/WARNING 行，供"日志只写脱敏别名与实发计数"的断言使用。"""

    def __init__(self):
        self.lines = []

    def info(self, message, *args):
        self.lines.append(message % args if args else message)

    def warning(self, message, *args):
        self.lines.append(message % args if args else message)

    def error(self, message, *args):  # pragma: no cover - 本组用例不依赖错误行
        self.lines.append(message % args if args else message)

    def debug(self, message, *args):  # pragma: no cover
        pass


class _SchedulerStub:
    """``_prepare``/_finalize_task 只需要 ``set_stage``/``tasks``/``accepting`` 三个触点。"""

    accepting = True

    def __init__(self):
        self.stages = []
        self.tasks = {}

    def set_stage(self, task, stage):
        task.stage = stage
        self.stages.append(stage)


class _Counter:
    """增强/放大替身：不修改产物、只计数。"""

    def __init__(self):
        self.calls = 0

    async def apply(self, task, value, output_dir, config, state):
        self.calls += 1
        return value


class _Outcome:
    def __init__(self, success=True, error=""):
        self.success = success
        self.error = error
        self.side_effects_started = True
        self.side_send_started = True
        self.side_send_error = ""


class _Sender:
    def __init__(self):
        self.chain = None

    async def send(self, umo, event, chain, before_send=None, identity=None, mention_target=""):
        if before_send is not None:
            before_send()
        self.chain = list(chain)
        self.identity = identity
        self.mention_target = mention_target
        return _Outcome(True)


class _Event:
    def get_sender_id(self):
        return "1"


# ======================================================================
# 1. PersonaStore：缺目录 / 空库 / 枚举失败 / 读取失败 四态分开
# ======================================================================
class StoreScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = PersonaStore(self.root, lambda: 20 * 1024 * 1024)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_directory_is_missing_not_empty(self):
        scan = self.store.scan_references("persona-absent")
        self.assertEqual(scan.status, persona_store.REFERENCE_STATUS_MISSING)
        self.assertEqual(scan.pool, 0)
        self.assertEqual(scan.entries, ())
        self.assertEqual(scan.reason, "")

    def test_existing_but_empty_directory_is_empty(self):
        directory = self.store.persona_dir("persona-empty")
        directory.mkdir(parents=True, exist_ok=True)
        scan = self.store.scan_references("persona-empty")
        self.assertEqual(scan.status, persona_store.REFERENCE_STATUS_EMPTY)
        self.assertEqual(scan.pool, 0)
        self.assertTrue(directory.exists(), "空库不能因为「没有图」就被当成缺目录")

    def test_entries_are_listed_with_size_and_sorted_by_name(self):
        self.store.add_reference("persona-a", png_bytes(seed=1), PNG_MIME)
        self.store.add_reference("persona-a", png_bytes(seed=2), PNG_MIME)
        scan = self.store.scan_references("persona-a")
        self.assertEqual(scan.status, persona_store.REFERENCE_STATUS_OK)
        self.assertEqual(scan.pool, 2)
        names = [entry["name"] for entry in scan.entries]
        self.assertEqual(names, sorted(names))
        self.assertTrue(all(entry["size"] > 0 for entry in scan.entries))
        # 条目形状与既有 list_references 兼容（只有 name/size），不夹带路径。
        self.assertEqual(set(scan.entries[0]), {"name", "size"})

    def test_symlink_and_subdirectory_entries_are_skipped_not_counted(self):
        directory = self.store.persona_dir("persona-skip")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "real.png").write_bytes(png_bytes(seed=3))
        (directory / "nested").mkdir()
        outside = self.root / "outside.png"
        outside.write_bytes(png_bytes(seed=4))
        (directory / "linked.png").symlink_to(outside)
        scan = self.store.scan_references("persona-skip")
        self.assertEqual(scan.status, persona_store.REFERENCE_STATUS_OK)
        self.assertEqual([entry["name"] for entry in scan.entries], ["real.png"])
        self.assertEqual(scan.pool, 1)
        self.assertEqual(scan.skipped, 2)

    def test_enumeration_failure_is_error_and_never_looks_empty(self):
        directory = self.store.persona_dir("persona-bad")
        directory.mkdir(parents=True, exist_ok=True)
        original = Path.iterdir

        def boom(self):
            raise OSError(13, "permission denied")

        with patch.object(Path, "iterdir", boom):
            scan = self.store.scan_references("persona-bad")
        self.assertEqual(scan.status, persona_store.REFERENCE_STATUS_ERROR)
        self.assertTrue(scan.reason.startswith("enum_failed:"), scan.reason)
        self.assertEqual(scan.pool, 0)
        self.assertIs(Path.iterdir, original)

    def test_non_directory_path_is_error(self):
        directory = self.store.persona_dir("persona-file")
        directory.parent.mkdir(parents=True, exist_ok=True)
        directory.write_bytes(b"not a directory")
        scan = self.store.scan_references("persona-file")
        self.assertEqual(scan.status, persona_store.REFERENCE_STATUS_ERROR)
        self.assertEqual(scan.reason, "not_a_directory")

    def test_read_reference_reports_missing_and_unsafe_names(self):
        self.store.add_reference("persona-r", png_bytes(seed=5), PNG_MIME)
        name = self.store.scan_references("persona-r").entries[0]["name"]
        self.assertTrue(self.store.read_reference("persona-r", name))
        with self.assertRaises(PersonaReferenceError) as missing:
            self.store.read_reference("persona-r", "gone.png")
        self.assertEqual(missing.exception.reason, "read_missing")
        with self.assertRaises(PersonaReferenceError) as unsafe:
            self.store.read_reference("persona-r", "../escape.png")
        self.assertEqual(unsafe.exception.reason, "read_unsafe_name")
        # 异常消息不得夹带 Persona ID / 文件名 / 本地路径（只留 reason 与状态）。
        for error in (missing.exception, unsafe.exception):
            text = str(error)
            self.assertNotIn("persona-r", text)
            self.assertNotIn(str(self.root), text)
            self.assertNotIn("escape", text)
            self.assertIn("reason=", text)

    def test_list_references_backward_compatible(self):
        """旧 API 语义不变（缺目录与空库都返回 []）——只为兼容，别用它判状态。"""
        self.assertEqual(self.store.list_references("persona-absent"), [])
        self.store.persona_dir("persona-empty2").mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.store.list_references("persona-empty2"), [])


# ======================================================================
# 2. 稳定脱敏观测投影
# ======================================================================
class PersonaAliasTests(unittest.TestCase):
    def test_alias_is_stable_and_reveals_nothing(self):
        persona_id = "8642135790"
        first = attempt_diagnostics.persona_alias(persona_id)
        second = attempt_diagnostics.persona_alias(persona_id)
        self.assertEqual(first, second, "同一 Persona 的别名必须稳定（可跨日志追溯）")
        self.assertTrue(first.startswith("persona-"))
        self.assertEqual(len(first), len("persona-") + 12)
        self.assertNotIn(persona_id, first)
        self.assertNotEqual(first, attempt_diagnostics.persona_alias("8642135791"))
        # 不可枚举：别名是 48 bit 的哈希前缀，不是可逆编码或序号。
        self.assertNotRegex(first, r"^\d+$")

    def test_empty_and_non_string_ids_are_handled(self):
        self.assertEqual(attempt_diagnostics.persona_alias(""), "persona-none")
        self.assertEqual(attempt_diagnostics.persona_alias(None), "persona-none")
        self.assertEqual(attempt_diagnostics.persona_alias(12345), attempt_diagnostics.persona_alias("12345"))

    def test_safe_label_bounds_and_redacts(self):
        self.assertEqual(attempt_diagnostics.safe_label("node-a"), "node-a")
        self.assertEqual(len(attempt_diagnostics.safe_label("x" * 200)), attempt_diagnostics.MAX_LABEL_CHARS)
        self.assertNotIn("\n", attempt_diagnostics.safe_label("a\nb"))
        # 与既有 DEBUG 同一套脱敏口径：URL / 本地路径 / 长数字标识都不外泄。
        self.assertIn("<url>", attempt_diagnostics.safe_label("https://example.invalid/v1?key=SECRET"))
        self.assertNotIn("SECRET", attempt_diagnostics.safe_label("https://example.invalid/v1?key=SECRET"))
        self.assertIn("<path>", attempt_diagnostics.safe_label("/root/secret/dir/file.png"))
        self.assertIn("<id>", attempt_diagnostics.safe_label("node-1234567890"))

    def test_as_reference_plans_projects_only_fixed_keys(self):
        plans = attempt_diagnostics.as_reference_plans(
            [
                {
                    "node": "node-a",
                    "model": "model-1",
                    "kind": "normal",
                    "reference_limit": 2,
                    "explicit_sent": 1,
                    "persona_sent": 1,
                    "persona_pool": 3,
                    "persona_budget": 1,
                    "library": "ok",
                    "reason": "limit_covers_pool",
                    "persona_id": "leak-me",
                    "prompt": "leak-me-too",
                }
            ]
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(
            set(plans[0]),
            {
                "node", "model", "kind", "reference_limit", "explicit_sent",
                "persona_sent", "persona_pool", "persona_budget", "library", "reason",
            },
        )
        self.assertNotIn("leak-me", json.dumps(plans, ensure_ascii=False))
        self.assertNotIn("prompt", plans[0])


# ======================================================================
# 3. 解析来源：按宿主 resolver 的真实规则标注，判不出就 unknown
# ======================================================================
class _HostResolver:
    """宿主 ``persona_mgr.resolve_selected_persona`` 的只读副本（规则同源）。

    优先级：UMO 会话配置 > 会话覆盖 > runner 配置 > 系统默认。
    ``[%None]`` 是"禁用"标记：宿主原样返回，不会落到 runner 配置。
    """

    def __init__(self, personas, configs, *, system_default="persona-system", webchat=False):
        self.personas = dict(personas)
        self.configs = dict(configs)
        self.system_default = system_default
        self.webchat = webchat
        self.calls = []

    async def resolve_selected_persona(self, *, umo, conversation_persona_id, platform_name, provider_settings):
        self.calls.append(
            {
                "umo": umo,
                "conversation_persona_id": conversation_persona_id,
                "platform_name": platform_name,
                "provider_settings": provider_settings,
            }
        )
        config = self.configs.get(str(umo)) or {}
        forced = (config.get("session_service_config") or {}).get("persona_id")
        persona_id = forced or conversation_persona_id
        if persona_id is None:
            runner = config.get("agent_runner") or {}
            if runner.get("runner_type") == "local":
                persona_id = (runner.get("persona") or {}).get("persona_id") or self.system_default
            else:
                persona_id = runner.get("persona_id") or self.system_default
        persona = self.personas.get(persona_id)
        return persona_id, persona, forced, self.webchat


class _Conversations:
    def __init__(self, persona_by_umo=None):
        self.persona_by_umo = dict(persona_by_umo or {})

    async def get_curr_conversation_id(self, umo):
        return "cid-1" if str(umo) in self.persona_by_umo else None

    async def get_conversation(self, umo, cid):
        if str(umo) not in self.persona_by_umo:
            return None
        return SimpleNamespace(persona_id=self.persona_by_umo[str(umo)])


class _Context:
    def __init__(self, *, resolver=None, configs=None, conversation_personas=None):
        self.persona_manager = (
            SimpleNamespace(resolve_selected_persona=resolver.resolve_selected_persona)
            if resolver is not None
            else None
        )
        self.conversation_manager = _Conversations(conversation_personas)
        self._configs = dict(configs or {})

    def get_config(self, umo=None):
        if umo is None:
            return self._configs.get("*", {})
        return self._configs.get(str(umo)) or self._configs.get("*", {})


class _MessageEvent:
    def __init__(self, umo="aiocqhttp:FriendMessage:1"):
        self.unified_msg_origin = umo

    def get_platform_name(self):
        return "aiocqhttp"

    def get_sender_id(self):
        return "1"


PERSONAS = {
    "persona-umo": SimpleNamespace(prompt="UMO 强制的人设"),
    "persona-conv": SimpleNamespace(prompt="会话覆盖的人设"),
    "persona-runner": SimpleNamespace(prompt="runner 配置的人设"),
    "persona-webchat": SimpleNamespace(prompt="webchat 默认人设"),
}
UMO = "aiocqhttp:FriendMessage:1"


class ResolvePersonaSourceTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, **context):
        plugin = object.__new__(_plugin_class())
        plugin.context = _Context(**context)
        return plugin

    async def test_umo_level_selection_wins_and_is_labelled_umo_forced(self):
        resolver = _HostResolver(
            PERSONAS,
            {UMO: {"session_service_config": {"persona_id": "persona-umo"}}},
        )
        plugin = self._plugin(resolver=resolver)
        persona_id, prompt, source = await plugin._resolve_persona_detail(_MessageEvent(UMO))
        self.assertEqual(persona_id, "persona-umo")
        self.assertEqual(prompt, "UMO 强制的人设")
        self.assertEqual(source, attempt_diagnostics.PERSONA_SOURCE_UMO)
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(resolver.calls[0]["umo"], UMO)

    async def test_disabled_persona_never_falls_back_to_runner_config(self):
        """``[%None]``（禁用）必须安全失败，不能悄悄落到 runner 配置的人设。"""

        resolver = _HostResolver(
            PERSONAS,
            {
                UMO: {
                    "session_service_config": {"persona_id": "[%None]"},
                    "agent_runner": {"runner_type": "local", "persona": {"persona_id": "persona-runner"}},
                }
            },
        )
        plugin = self._plugin(resolver=resolver)
        with self.assertRaises(ValueError) as raised:
            await plugin._resolve_persona_detail(_MessageEvent(UMO))
        self.assertIn("Persona", str(raised.exception))
        self.assertNotIn("persona-runner", str(raised.exception))

    async def test_conversation_override_is_labelled_conversation(self):
        resolver = _HostResolver(PERSONAS, {UMO: {}})
        plugin = self._plugin(resolver=resolver, conversation_personas={UMO: "persona-conv"})
        persona_id, _prompt, source = await plugin._resolve_persona_detail(_MessageEvent(UMO))
        self.assertEqual(persona_id, "persona-conv")
        self.assertEqual(source, attempt_diagnostics.PERSONA_SOURCE_CONVERSATION)

    async def test_runner_config_shapes_local_and_other(self):
        for runner in (
            {"runner_type": "local", "persona": {"persona_id": "persona-runner"}},
            {"runner_type": "provider", "persona_id": "persona-runner"},
        ):
            resolver = _HostResolver(PERSONAS, {UMO: {"agent_runner": runner}})
            plugin = self._plugin(resolver=resolver)
            persona_id, _prompt, source = await plugin._resolve_persona_detail(_MessageEvent(UMO))
            self.assertEqual(persona_id, "persona-runner", runner)
            self.assertEqual(source, attempt_diagnostics.PERSONA_SOURCE_RUNNER, runner)

    async def test_webchat_default_is_labelled_implicit(self):
        resolver = _HostResolver(
            {"persona-webchat": PERSONAS["persona-webchat"]},
            {UMO: {"agent_runner": {"runner_type": "local", "persona": {"persona_id": "persona-webchat"}}}},
            webchat=True,
        )
        plugin = self._plugin(resolver=resolver)
        _persona_id, _prompt, source = await plugin._resolve_persona_detail(_MessageEvent(UMO))
        self.assertEqual(source, attempt_diagnostics.PERSONA_SOURCE_IMPLICIT)

    async def test_missing_resolver_is_a_hard_error(self):
        plugin = self._plugin(resolver=None)
        with self.assertRaises(ValueError) as raised:
            await plugin._resolve_persona_detail(_MessageEvent(UMO))
        self.assertIn("不支持", str(raised.exception))

    async def test_legacy_two_tuple_entry_point_still_works(self):
        """``_resolve_persona`` 保持 2 元组：6 个既有调用点不受影响。"""

        resolver = _HostResolver(
            PERSONAS,
            {UMO: {"session_service_config": {"persona_id": "persona-umo"}}},
        )
        plugin = self._plugin(resolver=resolver)
        persona_id, prompt = await plugin._resolve_persona(_MessageEvent(UMO))
        self.assertEqual((persona_id, prompt), ("persona-umo", "UMO 强制的人设"))

    def test_source_helper_domain_is_closed_and_unknown_is_never_guessed(self):
        source = _plugin_class()._persona_source
        self.assertEqual(source(forced_persona_id="p"), attempt_diagnostics.PERSONA_SOURCE_UMO)
        self.assertEqual(source(conversation_persona_id="p"), attempt_diagnostics.PERSONA_SOURCE_CONVERSATION)
        self.assertEqual(source(webchat_default=True), attempt_diagnostics.PERSONA_SOURCE_IMPLICIT)
        self.assertEqual(source(), attempt_diagnostics.PERSONA_SOURCE_RUNNER)
        self.assertIn(
            attempt_diagnostics.PERSONA_SOURCE_UNKNOWN, attempt_diagnostics.PERSONA_SOURCES
        )


# ======================================================================
# 4. _prepare：只用本人格图库；显式图优先；读取失败安全失败
# ======================================================================
class PreparePersonaLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.logger = _Logger()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _plugin(self):
        plugin = object.__new__(_plugin_class())
        plugin.raw_config = {
            "providers": [],
            "storage_config": {"max_upload_mb": 20, "block_private_networks": False},
            "reference_compress_config": {"enable": False},
            "task_config": {"llm_caption": False},
        }
        plugin.store = PersonaStore(self.root, lambda: 20 * 1024 * 1024)
        plugin.scheduler = _SchedulerStub()
        plugin.session = None
        plugin.quota_store = None

        async def _summary(persona_id, prompt, umo="", reference_names=None):
            return "一张固定的外观摘要"

        plugin._generate_summary = _summary
        return plugin

    def _task(self, *, persona_id="persona-a", references=()):
        task = DrawTask("1" * 32, UMO, GenerationRequest("画一只猫", references=tuple(references)))
        task.persona_id = persona_id
        task.persona_prompt = "人设提示词"
        task.created_at = __import__("time").monotonic()
        task.runtime["source_event"] = None
        task.runtime["deferred_references"] = []
        task.runtime["persona_source"] = attempt_diagnostics.PERSONA_SOURCE_UMO
        return task

    async def test_missing_library_is_reported_as_missing_and_sends_nothing(self):
        plugin = self._plugin()
        task = self._task()
        with patch("imago_fusion.main.logger", self.logger):
            await plugin._prepare(task)
        self.assertEqual(task.runtime["persona_library_status"], attempt_diagnostics.PERSONA_LIBRARY_MISSING)
        self.assertEqual(task.runtime["persona_reference_pool"], 0)
        self.assertEqual(task.runtime["persona_references"], [])
        line = [item for item in self.logger.lines if "人格图库" in item][0]
        self.assertIn("status=missing", line)
        self.assertIn("pool=0", line)
        self.assertIn("source=umo_forced", line)
        # 日志里只有稳定别名，绝不出现原始 Persona ID。
        self.assertIn(attempt_diagnostics.persona_alias("persona-a"), line)
        self.assertNotIn("persona-a", line)

    async def test_empty_library_is_distinguishable_from_missing(self):
        plugin = self._plugin()
        plugin.store.persona_dir("persona-a").mkdir(parents=True, exist_ok=True)
        task = self._task()
        with patch("imago_fusion.main.logger", self.logger):
            await plugin._prepare(task)
        self.assertEqual(task.runtime["persona_library_status"], attempt_diagnostics.PERSONA_LIBRARY_EMPTY)
        self.assertEqual(task.runtime["persona_reference_pool"], 0)
        self.assertIn("status=empty", [item for item in self.logger.lines if "人格图库" in item][0])

    async def test_only_this_personas_images_are_loaded(self):
        plugin = self._plugin()
        plugin.store.add_reference("persona-a", png_bytes(seed=1), PNG_MIME)
        plugin.store.add_reference("persona-a", png_bytes(seed=2), PNG_MIME)
        plugin.store.add_reference("persona-b", png_bytes(seed=3), PNG_MIME)
        before = {path.name: path.read_bytes() for path in plugin.store.persona_dir("persona-a").iterdir()}
        task = self._task()
        with patch("imago_fusion.main.logger", self.logger):
            await plugin._prepare(task)
        loaded = task.runtime["persona_references"]
        self.assertEqual(len(loaded), 2, "只能读本 Persona 的 2 张，不能把 B 的图借过来")
        self.assertEqual(task.runtime["persona_reference_pool"], 2)
        self.assertEqual(
            sorted(item.data for item in loaded),
            sorted(before.values()),
        )
        after = {path.name: path.read_bytes() for path in plugin.store.persona_dir("persona-a").iterdir()}
        self.assertEqual(before, after, "读取不得改动原图（含文件名）")

    async def test_enumeration_failure_fails_safely_with_zero_provider_calls(self):
        plugin = self._plugin()
        plugin.store.persona_dir("persona-a").mkdir(parents=True, exist_ok=True)
        task = self._task()

        def boom(self):
            raise OSError(5, "input/output error")

        adapter_calls = []

        class _Adapter:
            def __init__(self, config):
                self.config = config

            async def generate(self, session, request, key):
                adapter_calls.append(request)
                return [ImageResult(data=b"image")]

        original = scheduler_module.ADAPTERS.get("custom_endpoint")
        scheduler_module.ADAPTERS["custom_endpoint"] = _Adapter
        try:
            provider = ProviderConfig(
                "node", "custom_endpoint", "https://example.invalid/v1", ("key",), model="m"
            )
            scheduler = TaskScheduler(
                lambda: RuntimeConfig(providers=(provider,)),
                None,
                lambda *_: True,
                lambda *_: None,
            )
            with patch.object(Path, "iterdir", boom), patch("imago_fusion.main.logger", self.logger):
                with self.assertRaises(PersonaReferenceError) as raised:
                    await plugin._prepare(task)
            self.assertTrue(raised.exception.reason.startswith("enum_failed:"), raised.exception.reason)
            self.assertEqual(adapter_calls, [], "读取失败必须在任何 provider 调用之前终止")
            self.assertEqual(task.runtime["persona_library_status"], attempt_diagnostics.PERSONA_LIBRARY_ERROR)
            await scheduler.close()
        finally:
            if original is None:
                scheduler_module.ADAPTERS.pop("custom_endpoint", None)
            else:
                scheduler_module.ADAPTERS["custom_endpoint"] = original

    async def test_explicit_references_stay_first_and_untruncated(self):
        plugin = self._plugin()
        for seed in (11, 12, 13):
            plugin.store.add_reference("persona-a", png_bytes(seed=seed), PNG_MIME)
        explicit = [_image(seed=21, name="user-1.png"), _image(seed=22, name="user-2.png")]
        task = self._task(references=explicit)
        with patch("imago_fusion.main.logger", self.logger):
            await plugin._prepare(task)
        self.assertEqual(list(task.request.references), explicit, "显式参考图必须原样保留")
        plan = TaskScheduler._plan_attempt_request(
            task,
            ProviderConfig("node", "custom_endpoint", "https://example.invalid/v1", ("k",), reference_image_limit=2),
            RuntimeConfig(providers=()),
        )
        self.assertEqual(plan.explicit_count, 2)
        self.assertEqual(plan.persona_count, 0, "limit=2 已被显式图占满，人格图一张都不该发")
        self.assertEqual(plan.reference_limit, 2)
        self.assertEqual(plan.reason, "limit_exhausted_by_explicit")
        self.assertEqual(list(plan.request.references), explicit, "显式图不能被限额截断")


# ======================================================================
# 5. 逐节点实发：ReferencePlan 的计数、原因与 t2 分母
# ======================================================================
def _provider(provider_id="node", *, limit=0, models=("model",)):
    chain = tuple(models)
    return ProviderConfig(
        provider_id,
        "custom_endpoint",
        "https://example.invalid/v1",
        ("key",),
        model=chain[0],
        available_models=chain[1:],
        reference_image_limit=limit,
    )


class ReferencePlanTests(unittest.TestCase):
    def _task(self, explicit=(), persona=(), *, library="ok", pool=None):
        task = DrawTask("2" * 32, UMO, GenerationRequest("画一只猫", references=tuple(explicit)))
        task.runtime["persona_references"] = list(persona)
        task.runtime["persona_library_status"] = library
        task.runtime["persona_reference_pool"] = len(persona) if pool is None else pool
        return task

    def test_one_explicit_plus_one_persona_with_limit_one_sends_one_plus_zero(self):
        """t2 分母（判据来自 t2 隔离试验，本轮据此对齐）：``limit=1`` ⇒ 1+0。"""

        task = self._task(explicit=[_image(seed=1)], persona=[_persona_image(seed=2)])
        plan = TaskScheduler._plan_attempt_request(task, _provider(limit=1), RuntimeConfig(providers=()))
        self.assertEqual((plan.explicit_count, plan.persona_count), (1, 0))
        self.assertEqual(plan.total, 1)
        self.assertEqual(plan.persona_pool, 1)
        self.assertEqual(plan.persona_budget, 0)
        self.assertEqual(plan.reason, "limit_exhausted_by_explicit")
        self.assertEqual(len(plan.request.references), 1)

    def test_fallback_limit_two_sends_one_plus_one(self):
        """后备节点 ``limit=2`` ⇒ 1+1（t2 分母的另一半）。"""

        task = self._task(explicit=[_image(seed=1)], persona=[_persona_image(seed=2)])
        plan = TaskScheduler._plan_attempt_request(task, _provider(limit=2), RuntimeConfig(providers=()))
        self.assertEqual((plan.explicit_count, plan.persona_count), (1, 1))
        self.assertEqual(plan.total, 2)
        self.assertEqual(plan.persona_budget, 1)
        self.assertEqual(plan.reason, "limit_covers_pool")
        self.assertEqual(
            [item.filename for item in plan.request.references],
            [task.request.references[0].filename, plan.request.references[1].filename],
        )
        self.assertIs(plan.request.references[0], task.request.references[0], "显式图必须排在最前")

    def test_zero_limit_means_unlimited_and_keeps_the_whole_persona_pool(self):
        persona = [_persona_image(seed=seed) for seed in (2, 3, 4)]
        task = self._task(persona=persona)
        plan = TaskScheduler._plan_attempt_request(task, _provider(limit=0), RuntimeConfig(providers=()))
        self.assertEqual((plan.explicit_count, plan.persona_count), (0, 3))
        self.assertEqual(plan.persona_budget, 3)
        self.assertEqual(plan.reason, "limit_unlimited")

    def test_limited_pool_is_trimmed_and_reason_says_so(self):
        persona = [_persona_image(seed=seed) for seed in (2, 3, 4, 5, 6)]
        task = self._task(explicit=[_image(seed=1)], persona=persona)
        plan = TaskScheduler._plan_attempt_request(task, _provider(limit=3), RuntimeConfig(providers=()))
        self.assertEqual((plan.explicit_count, plan.persona_count), (1, 2))
        self.assertEqual(plan.persona_pool, 5)
        self.assertEqual(plan.persona_budget, 2)
        self.assertEqual(plan.reason, "limit_trimmed")
        sent_persona = plan.request.references[1:]
        self.assertEqual(len(sent_persona), 2)
        for item in sent_persona:
            self.assertIn(item, persona, "只能从本人格图库里采样")

    def test_library_state_distinguishes_missing_empty_and_unreadable(self):
        cases = (
            ("missing", "library_missing"),
            ("empty", "library_empty"),
            ("error", "library_error"),
        )
        for library, expected in cases:
            task = self._task(library=library, persona=[])
            plan = TaskScheduler._plan_attempt_request(task, _provider(limit=2), RuntimeConfig(providers=()))
            self.assertEqual(plan.reason, expected, library)
            self.assertEqual(plan.persona_count, 0, library)

    def test_library_ok_but_no_usable_pool_is_its_own_reason(self):
        task = self._task(library="ok", persona=[], pool=0)
        plan = TaskScheduler._plan_attempt_request(task, _provider(limit=2), RuntimeConfig(providers=()))
        self.assertEqual(plan.reason, "no_persona_pool")

    def test_send_side_copies_take_precedence_over_the_raw_pool(self):
        """压缩副本（``send_persona_references``）才是实发源，池只是候选。"""

        compressed = [_persona_image(seed=42, name="compressed.png")]
        task = self._task(persona=[_persona_image(seed=2), _persona_image(seed=3)])
        task.runtime["send_persona_references"] = compressed
        plan = TaskScheduler._plan_attempt_request(task, _provider(limit=0), RuntimeConfig(providers=()))
        self.assertEqual(plan.persona_count, 1)
        self.assertIs(plan.request.references[0], compressed[0])
        self.assertEqual(plan.persona_pool, 2, "池仍报原始可读张数，不冒充实发")

    def test_legacy_entry_point_still_returns_a_request_and_keeps_current_keys(self):
        """``_request_for_attempt`` 的静态签名 + 三个 ``current_*`` 键语义保持不变。"""

        task = self._task(explicit=[_image(seed=1)], persona=[_persona_image(seed=2)])
        request = TaskScheduler._request_for_attempt(task, _provider(limit=2), RuntimeConfig(providers=()))
        self.assertEqual(len(request.references), 2)
        self.assertEqual(task.runtime["current_explicit_reference_count"], 1)
        self.assertEqual(task.runtime["current_persona_reference_count"], 1)
        self.assertEqual(task.runtime["current_reference_count"], 2)
        self.assertEqual(task.runtime["current_reference_reason"], "limit_covers_pool")

    def test_reference_relation_note_counts_what_is_actually_sent(self):
        """关系声明的数量必须按**本节点实发**写，不能按全量池虚高。"""

        task = self._task(explicit=[_image(seed=1)], persona=[_persona_image(seed=2)])
        limited = TaskScheduler._plan_attempt_request(
            task, _provider(limit=1), RuntimeConfig(providers=())
        )
        self.assertEqual(limited.persona_count, 0)
        self.assertNotIn("remaining", limited.request.prompt, "1+0 时不该声称还有人设图")
        wide = TaskScheduler._plan_attempt_request(
            task, _provider(limit=2), RuntimeConfig(providers=())
        )
        self.assertEqual(wide.persona_count, 1)
        self.assertIn("The remaining 1 image(s)", wide.request.prompt)
        self.assertIn("first 1 attached", wide.request.prompt)

    def test_plan_record_is_bounded_and_carries_no_identity(self):
        task = self._task(explicit=[_image(seed=1)], persona=[_persona_image(seed=2)])
        plan = TaskScheduler._plan_attempt_request(task, _provider(limit=2), RuntimeConfig(providers=()))
        record = plan.as_record(node="node-a", model="model-1")
        self.assertEqual(record["persona_sent"], plan.persona_count)
        self.assertEqual(record["explicit_sent"], plan.explicit_count)
        self.assertEqual(record["library"], "ok")
        self.assertNotIn("persona-a", json.dumps(record, ensure_ascii=False))


class AttemptMetaTests(unittest.IsolatedAsyncioTestCase):
    """``attempt_meta`` 与 DEBUG 行必须报**本次 attempt 实际发出去**的张数。"""

    def setUp(self):
        self._old = scheduler_module.ADAPTERS.get("custom_endpoint")
        self.calls = []
        calls = self.calls

        class _Adapter:
            def __init__(self, config):
                self.config = config

            async def generate(self, session, request, key):
                calls.append({"node": self.config.id, "references": list(request.references)})
                return [ImageResult(data=b"image")]

        scheduler_module.ADAPTERS["custom_endpoint"] = _Adapter
        self.debug_lines = []

    def tearDown(self):
        if self._old is None:
            scheduler_module.ADAPTERS.pop("custom_endpoint", None)
        else:
            scheduler_module.ADAPTERS["custom_endpoint"] = self._old

    def _scheduler(self, runtime):
        return TaskScheduler(
            lambda: runtime,
            None,
            lambda *_: True,
            lambda *_: None,
            lambda _task, message, *args: self.debug_lines.append(message % args if args else message),
        )

    async def test_attempt_meta_reports_actual_counts_and_roles(self):
        provider = _provider(limit=2)
        runtime = RuntimeConfig(providers=(provider,))
        scheduler = self._scheduler(runtime)
        task = DrawTask(
            "3" * 32,
            UMO,
            GenerationRequest("画一只猫", references=(_image(seed=1),)),
        )
        # t2 的 1+1 分母：库里只有 1 张，limit=2 整除 ⇒ 显式 1 + 人格 1、无裁剪。
        task.runtime["persona_references"] = [_persona_image(seed=2)]
        task.runtime["persona_library_status"] = "ok"
        task.runtime["persona_reference_pool"] = 1
        try:
            await scheduler._generate(task)
        finally:
            await scheduler.close()
        meta = task.runtime["attempt_meta"]
        self.assertEqual(meta["explicit_count"], 1, "explicit_count 必须是实发的显式张数")
        self.assertEqual(meta["persona_count"], 1, "limit=2 ⇒ 实发 1 张人格图")
        self.assertEqual(meta["persona_pool"], 1, "池报可读张数，不冒充实发")
        self.assertEqual(meta["reference_limit"], 2)
        self.assertEqual(meta["reference_reason"], "limit_covers_pool")
        roles = [item["role"] for item in meta["references"]]
        self.assertEqual(roles, ["explicit", "persona"])
        self.assertEqual(len(self.calls[0]["references"]), 2, "适配器实收 2 张")
        plans = task.runtime["reference_plans"]
        self.assertEqual(plans[-1]["persona_sent"], 1)
        self.assertEqual(plans[-1]["kind"], "normal")
        line = [item for item in self.debug_lines if "绘图节点尝试" in item][-1]
        self.assertIn("references=2", line)
        self.assertIn("persona_refs=1", line)
        self.assertIn("explicit=1", line)
        self.assertIn("reason=limit_covers_pool", line)

    async def test_text_fallback_legs_report_zero_persona_references(self):
        """t2 的 ``references=0 persona_refs=1`` 错报：文字腿必须 0/0。"""

        provider = _provider(limit=2)
        runtime = RuntimeConfig(
            providers=(provider,),
            generation_timeout=120,
            vision_provider_id="vision",
            reference_fallback=ReferenceFallbackConfig(enable=True, max_retries_per_node=1),
        )
        state = {"count": 0}

        class _Adapter:
            def __init__(self, config):
                self.config = config

            async def generate(self, session, request, key):
                state["count"] += 1
                if not request.references:
                    return [ImageResult(data=b"text-only")]
                raise ProviderError("blocked", status=400, block_reason="SAFETY")

        class _Captioner:
            async def __call__(self, task, config, prompt_text, images, *, timeout, max_chars):
                return "一名短发女性站在窗边。"

        original = scheduler_module.ADAPTERS["custom_endpoint"]
        scheduler_module.ADAPTERS["custom_endpoint"] = _Adapter
        try:
            scheduler = TaskScheduler(
                lambda: runtime,
                None,
                lambda *_: True,
                lambda *_: None,
                captioner=_Captioner(),
                debug=lambda _task, message, *args: self.debug_lines.append(message % args if args else message),
            )
            task = DrawTask(
                "4" * 32,
                UMO,
                GenerationRequest("画一只猫", references=(_image(seed=1),)),
            )
            task.runtime["original_user_prompt"] = "画一只猫"
            task.runtime["persona_references"] = [_persona_image(seed=2)]
            task.runtime["persona_library_status"] = "ok"
            task.runtime["persona_reference_pool"] = 1
            try:
                await scheduler._generate(task)
            finally:
                await scheduler.close()
        finally:
            scheduler_module.ADAPTERS["custom_endpoint"] = original

        self.assertGreaterEqual(state["count"], 2, "应当先被拒一次再走文字腿")
        text_lines = [item for item in self.debug_lines if "绘图节点尝试" in item and "references=0" in item]
        self.assertTrue(text_lines, self.debug_lines)
        for line in text_lines:
            self.assertIn("persona_refs=0", line, line)
            self.assertIn("reason=text_fallback", line, line)
            self.assertIn("explicit=0", line, line)
        text_plans = [item for item in task.runtime["reference_plans"] if item["kind"] == "text_fallback"]
        self.assertTrue(text_plans)
        for plan in text_plans:
            self.assertEqual((plan["explicit_sent"], plan["persona_sent"]), (0, 0))
            self.assertEqual(plan["reason"], "text_fallback")
            self.assertEqual(plan["persona_pool"], 1, "池仍如实报 1，但不冒充实发")


# ======================================================================
# 6. Web API：upload 的 persona_id 必须是显式校验过的那个
# ======================================================================
class UploadPersonaIdTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from quart import Quart

        from imago_fusion.integrations.web_api import PageAPI

        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

        class _Plugin:
            def __init__(self, store):
                self.store = store

            async def get_persona_prompt(self, persona_id):
                return "人设" if persona_id == "persona-a" else ""

        self.plugin = _Plugin(PersonaStore(self.root, lambda: 20 * 1024 * 1024))
        api = PageAPI(self.plugin)
        self.app = Quart(__name__)
        self.app.add_url_rule("/imago_fusion/persona/upload", "upload", api.upload, methods=["POST"])

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _post(self, body):
        async with self.app.test_client() as client:
            response = await client.post("/imago_fusion/persona/upload", json=body)
        return response.status_code, await response.get_json()

    async def test_blank_persona_id_is_rejected_before_any_write(self):
        status, payload = await self._post(
            {"persona_id": "   ", "images": [{"mime_type": PNG_MIME, "data": "AA=="}]}
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["message"], "persona_id 无效")
        self.assertFalse(payload["success"])
        self.assertEqual(list(self.plugin.store.references.iterdir()), [], "空 ID 不得落盘")

    async def test_overlong_persona_id_is_rejected(self):
        status, payload = await self._post(
            {"persona_id": "p" * 300, "images": [{"mime_type": PNG_MIME, "data": "AA=="}]}
        )
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload["message"], "persona_id 无效")

    async def test_unknown_persona_still_reports_not_found(self):
        status, payload = await self._post(
            {"persona_id": "persona-zzz", "images": [{"mime_type": PNG_MIME, "data": "AA=="}]}
        )
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload["message"], "Persona 不存在")

    async def test_valid_upload_writes_to_the_given_persona_only(self):
        import base64

        payload = png_bytes(seed=7)
        status, result = await self._post(
            {
                "persona_id": "persona-a",
                "images": [{"mime_type": PNG_MIME, "data": base64.b64encode(payload).decode("ascii")}],
            }
        )
        self.assertEqual(status, 200, result)
        self.assertTrue(result["success"], result)
        self.assertEqual(self.plugin.store.scan_references("persona-a").pool, 1)
        self.assertEqual(self.plugin.store.scan_references("persona-b").status, "missing")


# ======================================================================
# 7. 观测落盘：echo 不伪称"检查通过"；manifest 写人格与逐腿实发
# ======================================================================
class EchoNotComparedTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_checks_are_logged_as_not_compared(self):
        logger = _Logger()
        with TemporaryDirectory() as directory:
            plugin = object.__new__(_plugin_class())
            plugin.raw_config = {}
            plugin.session = None
            plugin.store = PersonaStore(Path(directory), lambda: 20 * 1024 * 1024)
            plugin.scheduler = _SchedulerStub()
            plugin.enhancer = _Counter()
            plugin.upscaler = _Counter()
            plugin.sender = _Sender()
            plugin.quota_store = None
            task = DrawTask("5" * 32, UMO, GenerationRequest("draw", count=1))
            task.created_at = __import__("time").monotonic()
            task.runtime["source_event"] = _Event()
            task.runtime["original_user_prompt"] = "draw"
            # 纯文字腿：发了 0 张参考图，闸门没有可比对对象。
            task.runtime["delivered_references"] = []
            task.runtime["delivered_references_text"] = True
            with patch("imago_fusion.main.logger", logger):
                await plugin._finish_task(task, [ImageResult(data=b"text", mime_type=PNG_MIME)])
                task.state = TaskState.SUCCEEDED
                await plugin._finalize_task(task)
            manifest = json.loads((plugin.store.task_dir(task.id) / "task.json").read_text("utf-8"))
        lines = [item for item in logger.lines if "回吐闸" in item]
        self.assertTrue(lines)
        self.assertIn("references_sent=0", lines[0])
        self.assertIn("未比较", lines[0], "检查=0 不能读成「检查通过」")
        self.assertFalse(manifest["echo_guard"]["compared"])
        self.assertEqual(manifest["echo_guard"]["checked"], 0)


class ManifestPersonaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.logger = _Logger()

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _finalize(self, task):
        plugin = object.__new__(_plugin_class())
        plugin.raw_config = {}
        plugin.session = None
        plugin.store = PersonaStore(self.root, lambda: 20 * 1024 * 1024)
        plugin.scheduler = _SchedulerStub()
        plugin.enhancer = _Counter()
        plugin.upscaler = _Counter()
        plugin.sender = _Sender()
        plugin.quota_store = None
        with patch("imago_fusion.main.logger", self.logger):
            await plugin._finalize_task(task)
        return json.loads((plugin.store.task_dir(task.id) / "task.json").read_text("utf-8"))

    def _task(self, task_id, *, persona_id="persona-a"):
        task = DrawTask(task_id, UMO, GenerationRequest("draw", count=1))
        task.created_at = __import__("time").monotonic()
        task.runtime["source_event"] = _Event()
        task.runtime["original_user_prompt"] = "draw"
        task.state = TaskState.SUCCEEDED
        if persona_id:
            task.persona_id = persona_id
            task.persona_prompt = "人设"
            task.runtime["persona_source"] = attempt_diagnostics.PERSONA_SOURCE_UMO
            task.runtime["persona_library_status"] = "ok"
            task.runtime["persona_reference_pool"] = 3
            task.runtime["persona_reference_skipped"] = 1
        return task

    async def test_manifest_records_persona_and_reference_plans(self):
        task = self._task("6" * 32)
        task.runtime["reference_plans"] = [
            ReferencePlan(
                request=task.request,
                explicit_count=1,
                persona_count=0,
                persona_pool=3,
                persona_budget=0,
                reference_limit=1,
                reason="limit_exhausted_by_explicit",
                library_status="ok",
            ).as_record(node="node-a", model="model-1")
        ]
        manifest = await self._finalize(task)
        persona = manifest["persona"]
        self.assertEqual(persona["source"], attempt_diagnostics.PERSONA_SOURCE_UMO)
        self.assertEqual(persona["library"], "ok")
        self.assertEqual(persona["pool"], 3)
        self.assertEqual(persona["skipped"], 1)
        self.assertEqual(persona["alias"], attempt_diagnostics.persona_alias("persona-a"))
        self.assertNotIn("persona-a", json.dumps(manifest["persona"], ensure_ascii=False))
        self.assertEqual(len(manifest["reference_plans"]), 1)
        plan = manifest["reference_plans"][0]
        self.assertEqual((plan["explicit_sent"], plan["persona_sent"]), (1, 0))
        self.assertEqual(plan["reason"], "limit_exhausted_by_explicit")
        # 被限额挤占必须留一条可检索的 WARNING，且不含 Persona 身份。
        squeeze = [item for item in self.logger.lines if "限额挤占" in item]
        self.assertTrue(squeeze)
        self.assertNotIn("persona-a", squeeze[0])

    async def test_manifest_without_persona_has_no_persona_block(self):
        manifest = await self._finalize(self._task("7" * 32, persona_id=None))
        self.assertNotIn("persona", manifest)


# ======================================================================
# 8. 前端：真实 app.js 的绑定回归（含基线红证）
# ======================================================================
class WebuiBindingTests(unittest.TestCase):
    def _run_node(self, app_js=None):
        env = None
        if app_js is not None:
            import os

            env = dict(os.environ, IMAGO_WEBUI_APP=str(app_js))
        return subprocess.run(
            ["node", str(JS_TEST)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            check=False,
        )

    def test_v142_js_suite_runs_against_real_app_js(self):
        if not JS_TEST.exists():
            self.fail("缺少 test/test_webui_v142.js")
        result = self._run_node()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)
        self.assertGreaterEqual(result.stdout.count("PASS"), 12)

    def test_same_suite_turns_red_on_the_frozen_baseline(self):
        """红证：同一份用例在基线 a1056441 的 app.js 上必须失败（3/3 错绑）。"""

        repo = REPO_ROOT
        probe = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{BASELINE_COMMIT}^{{commit}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode != 0:
            self.skipTest("当前检出没有基线提交，跳过红证（不影响绿证）")
        baseline = subprocess.run(
            ["git", "-C", str(repo), "show", f"{BASELINE_COMMIT}:pages/webui/app.js"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(baseline.returncode, 0, baseline.stderr)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "app-baseline.js"
            path.write_text(baseline.stdout, encoding="utf-8")
            result = self._run_node(path)
        combined = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, "基线必须红：同一用例不能放过错绑")
        self.assertIn("FAIL", combined)
        self.assertIn("persona-b", combined, "基线实际把 A 的图发给了 B")
