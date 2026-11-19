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

import hashlib
import io
import json
import stat
import random
import re
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
    TaskStage,
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


def _no_identity(case, text, persona_id):
    """断言 ``persona_id`` 没有作为**独立标识**出现在 text 里。

    别名是 ``persona-`` + 12 位十六进制，字面上可能以 ID 的开头字符续写
    （``persona-a`` + ``a2caa…``）；这不等于泄漏，所以按"整词"匹配而不是子串。
    """

    pattern = r"(?<![\w-])" + re.escape(str(persona_id)) + r"(?![\w-])"
    case.assertNotRegex(text, pattern)


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

    async def send(
        self, umo, event, chain, before_send=None, identity=None, mention_target="",
        image_only=False, image_allowlist=None, mention_needs_image=False,
        products=None,
    ):
        if before_send is not None:
            before_send()
        self.chain = list(chain)
        self.identity = identity
        self.mention_target = mention_target
        self.mention_needs_image = mention_needs_image
        self.products = products
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
    SECRET = b"k" * 32

    def test_alias_is_stable_and_reveals_nothing(self):
        persona_id = "8642135790"
        first = attempt_diagnostics.persona_alias(persona_id, secret=self.SECRET)
        second = attempt_diagnostics.persona_alias(persona_id, secret=self.SECRET)
        self.assertEqual(first, second, "同一部署内同一 Persona 的别名必须稳定（可跨日志追溯）")
        self.assertTrue(first.startswith("persona-"))
        self.assertEqual(len(first), len("persona-") + 12)
        self.assertNotIn(persona_id, first)
        self.assertNotEqual(
            first, attempt_diagnostics.persona_alias("8642135791", secret=self.SECRET)
        )
        # 不可枚举：别名是 48 bit 的 HMAC 前缀，不是可逆编码或序号。
        self.assertNotRegex(first, r"^\d+$")

    def test_no_key_never_falls_back_to_a_derivable_alias(self):
        """t11 R1-11：拿不到本机密钥时宁可"不可关联"，也不退回可枚举的公共盐。"""

        self.assertEqual(
            attempt_diagnostics.persona_alias("8642135790"),
            attempt_diagnostics.PERSONA_ALIAS_UNKEYED,
        )
        self.assertEqual(
            attempt_diagnostics.persona_alias("8642135790", secret=b"too-short"),
            attempt_diagnostics.PERSONA_ALIAS_UNKEYED,
            "短于 16 字节的口令不得当 HMAC key",
        )
        self.assertEqual(
            attempt_diagnostics.persona_alias("8642135790", secret=None),
            attempt_diagnostics.PERSONA_ALIAS_UNKEYED,
        )

    def test_public_constant_dictionary_cannot_recover_the_alias(self):
        """反证"给两个假 ID 算一次字典就能反查 12-hex 别名"这条攻击。"""

        target = attempt_diagnostics.persona_alias("哈基米", secret=self.SECRET)
        self.assertNotEqual(target, attempt_diagnostics.PERSONA_ALIAS_UNKEYED)
        # 攻击者可用的全部公开常量（含旧版字面盐）都命不中。
        for salt in ("imago-fusion/persona-alias/v1", "", "imago", "imago-fusion"):
            guess = "persona-" + hashlib.sha256(f"{salt}:哈基米".encode("utf-8")).hexdigest()[:12]
            self.assertNotEqual(guess, target, f"公开盐 {salt!r} 不得复现别名")
        # 换一把部署密钥 ⇒ 同一 ID 的别名完全不同（跨部署不可关联）。
        self.assertNotEqual(
            attempt_diagnostics.persona_alias("哈基米", secret=b"j" * 32), target
        )

    def test_empty_and_non_string_ids_are_handled(self):
        self.assertEqual(attempt_diagnostics.persona_alias("", secret=self.SECRET), "persona-none")
        self.assertEqual(attempt_diagnostics.persona_alias(None, secret=self.SECRET), "persona-none")
        self.assertEqual(
            attempt_diagnostics.persona_alias(12345, secret=self.SECRET),
            attempt_diagnostics.persona_alias("12345", secret=self.SECRET),
        )

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
                "seq", "route", "node_index", "node_count", "model_index", "model_attempts",
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

    def __init__(self, personas, configs, *, system_default="persona-system", webchat=False, sp=None):
        self.personas = dict(personas)
        self.configs = dict(configs)
        # 会话偏好（SP）是宿主判定"强制人格"的**唯一**权威来源；``sp`` 一旦给出，
        # 就与配置文件里的 ``session_service_config`` 解耦 —— 后者可能只是旧版本
        # 残留的值（t5 现场：SP 无 force、旧配置仍写着 persona_id、会话人格胜出）。
        self.sp = None if sp is None else dict(sp)
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
        if self.sp is None:
            forced = (config.get("session_service_config") or {}).get("persona_id")
        else:
            forced = (self.sp.get(str(umo)) or {}).get("persona_id")
        persona_id = forced or conversation_persona_id
        if persona_id is None:
            runner = config.get("agent_runner") or {}
            # 宿主读的是 ``agent_runner["config"]``（不是把 persona 直接挂在
            # agent_runner 下）；更老的形状仍被插件侧兼容读取。
            runner_config = runner.get("config")
            if not isinstance(runner_config, dict):
                runner_config = runner
            if runner.get("runner_type") == "local":
                persona_id = (runner_config.get("persona") or {}).get("persona_id") or self.system_default
            else:
                persona_id = runner_config.get("persona_id") or self.system_default
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

    def get_messages(self):
        return []


def _frozen_identity(umo="aiocqhttp:FriendMessage:1"):
    # 惰性导入：与其它测试文件里的 astrbot 桩保持原来的 import 顺序。
    from imago_fusion.core.route_identity import freeze_route_identity

    return freeze_route_identity(_MessageEvent(umo))


class _SubmitScheduler(_SchedulerStub):
    """``_submit`` 只需要 ``submit``/``query``；顺带把提交到的任务留下来供断言。"""

    def __init__(self):
        super().__init__()
        self.submitted = []

    def submit(self, task):
        # ``_submit`` 返回的是 scheduler 的提交结果（任务 id），不是任务对象。
        self.submitted.append(task)
        self.tasks[task.id] = task
        return task.id

    def query(self, *, umo, owner_user_id, bot_instance_id):
        return [
            task
            for task in self.submitted
            if task.umo == umo
            and task.owner_user_id == owner_user_id
            and task.bot_instance_id == bot_instance_id
        ]


class _QuotaDecision:
    def __init__(self, allowed=True, charged=0, reason=""):
        self.allowed = allowed
        self.charged = charged
        self.reason = reason


class _QuotaStore:
    """额度替身：不拦不扣，只为让 ``_submit`` 的账务分支跑通。"""

    def __init__(self):
        self.consumed = []

    def can_consume(self, user_id, amount, policy):
        return _QuotaDecision()

    def consume(self, user_id, amount, policy):
        self.consumed.append((user_id, amount))
        return _QuotaDecision(charged=amount)


PERSONAS = {
    "persona-umo": SimpleNamespace(prompt="UMO 强制的人设"),
    "persona-conv": SimpleNamespace(prompt="会话覆盖的人设"),
    "persona-runner": SimpleNamespace(prompt="runner 配置的人设"),
    "persona-webchat": SimpleNamespace(prompt="webchat 默认人设"),
    "persona-stale": SimpleNamespace(prompt="旧配置残留的人设"),
    "persona-mystery": SimpleNamespace(prompt="来源不明的人设"),
    "default": SimpleNamespace(prompt="系统默认人设"),
}
UMO = "aiocqhttp:FriendMessage:1"


class ResolvePersonaSourceTests(unittest.IsolatedAsyncioTestCase):
    def _plugin(self, **context):
        plugin = object.__new__(_plugin_class())
        plugin.context = _Context(**context)
        # ``_submit`` 需要的最小落地面：一个可用节点、一个真 store、一个提交桩。
        plugin.raw_config = {
            "providers": [
                {
                    "id": "node-a",
                    "api_type": "openai_chat",
                    "base_url": "https://example.invalid/v1",
                    "api_keys": ["TEST_KEY"],
                    "model": "test-model",
                }
            ],
            "storage_config": {"max_upload_mb": 20, "block_private_networks": False},
            "reference_compress_config": {"enable": False},
            "quota_config": {"enable": False},
            "task_config": {"llm_caption": False},
        }
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        plugin.store = PersonaStore(Path(self._tmp.name), lambda: 20 * 1024 * 1024)
        plugin.scheduler = _SubmitScheduler()
        plugin.quota_store = _QuotaStore()
        plugin.session = None
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

    async def test_true_force_wins_even_when_the_conversation_names_the_same_persona(self):
        """宿主真的强制了：即使会话覆盖同名，来源也只能是 ``umo_forced``。

        「同名」是最容易被反推成 ``conversation`` 的形状 —— 胜出者相同不等于
        来源相同，判定必须看是哪一层给出的信号。
        """

        for conversation_id, expected_persona in (("persona-umo", "persona-umo"), ("persona-conv", "persona-umo")):
            resolver = _HostResolver(
                PERSONAS,
                {UMO: {"session_service_config": {"persona_id": "persona-umo"}}},
            )
            plugin = self._plugin(
                resolver=resolver, conversation_personas={UMO: conversation_id}
            )
            persona_id, _prompt, source = await plugin._resolve_persona_detail(_MessageEvent(UMO))
            self.assertEqual(persona_id, expected_persona, conversation_id)
            self.assertEqual(
                source, attempt_diagnostics.PERSONA_SOURCE_UMO, conversation_id
            )

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
        """宿主读的是 ``agent_runner["config"]``；更老的平铺形状仍要能读出来。"""

        for runner in (
            {"runner_type": "local", "config": {"persona": {"persona_id": "persona-runner"}}},
            {"runner_type": "provider", "config": {"persona_id": "persona-runner"}},
            # 老形状（persona 直接挂在 agent_runner 下）——兼容读，不误判成默认
            {"runner_type": "local", "persona": {"persona_id": "persona-runner"}},
            {"runner_type": "provider", "persona_id": "persona-runner"},
        ):
            configs = {UMO: {"agent_runner": runner}}
            resolver = _HostResolver(PERSONAS, configs)
            plugin = self._plugin(resolver=resolver, configs=configs)
            persona_id, _prompt, source = await plugin._resolve_persona_detail(_MessageEvent(UMO))
            self.assertEqual(persona_id, "persona-runner", runner)
            self.assertEqual(source, attempt_diagnostics.PERSONA_SOURCE_RUNNER, runner)

    async def test_system_default_runner_is_labelled_implicit_not_runner(self):
        """没有任何来源信号时宿主落到系统默认人设：这是 implicit，不是 runner。"""

        configs = {UMO: {"agent_runner": {"runner_type": "local", "config": {}}}}
        resolver = _HostResolver(PERSONAS, configs, system_default="default")
        plugin = self._plugin(resolver=resolver, configs=configs)
        persona_id, _prompt, source = await plugin._resolve_persona_detail(_MessageEvent(UMO))
        self.assertEqual(persona_id, "default")
        self.assertEqual(source, attempt_diagnostics.PERSONA_SOURCE_IMPLICIT)

    async def test_stale_config_force_never_beats_the_host_session_preference(self):
        """t5 现场（t11 R1-08 补例）：SP 无 force、旧配置里仍写着 persona_id。

        宿主权威的"强制"只来自会话偏好（SP）；配置文件里残留的 ``persona_id``
        不是强制来源。此时会话人格胜出、图也选对，来源必须是 ``conversation``，
        绝不能因为旧配置有个值就贴 ``umo_forced``。
        """

        configs = {
            UMO: {
                # 旧版本残留值 —— **不是** 权威强制来源
                "session_service_config": {"persona_id": "persona-stale"},
                "agent_runner": {
                    "runner_type": "local",
                    "config": {"persona": {"persona_id": "persona-runner"}},
                },
            }
        }
        resolver = _HostResolver(PERSONAS, configs, sp={UMO: {}})
        plugin = self._plugin(
            resolver=resolver,
            configs=configs,
            conversation_personas={UMO: "persona-conv"},
        )
        persona_id, prompt, source = await plugin._resolve_persona(_MessageEvent(UMO))
        self.assertEqual(persona_id, "persona-conv")
        self.assertEqual(prompt, "会话覆盖的人设")
        self.assertEqual(source, attempt_diagnostics.PERSONA_SOURCE_CONVERSATION)
        # 落进任务与 manifest 的也是同一份来源，不会事后改判。
        task_id = await plugin._submit(
            _MessageEvent(UMO), "画一只猫", persona=True, identity=_frozen_identity(UMO)
        )
        task = plugin.scheduler.submitted[-1]
        self.assertEqual(task.id, task_id)
        self.assertEqual(task.persona_id, "persona-conv")
        self.assertEqual(
            task.runtime["persona_source"],
            attempt_diagnostics.PERSONA_SOURCE_CONVERSATION,
        )

    async def test_unattributable_winner_is_labelled_unknown(self):
        """来源信号和实际胜出者对不上时只能标 unknown——不凭 id 相同反推来源。"""

        class _OddResolver(_HostResolver):
            async def resolve_selected_persona(self, **kwargs):
                await super().resolve_selected_persona(**kwargs)
                return "persona-mystery", PERSONAS["persona-mystery"], None, False

        configs = {UMO: {}}
        plugin = self._plugin(resolver=_OddResolver(PERSONAS, configs), configs=configs)
        persona_id, _prompt, source = await plugin._resolve_persona_detail(_MessageEvent(UMO))
        self.assertEqual(persona_id, "persona-mystery")
        self.assertEqual(source, attempt_diagnostics.PERSONA_SOURCE_UNKNOWN)

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

    async def test_detail_alias_matches_the_real_entry_point(self):
        """``_resolve_persona`` 是权威三元组入口；``_resolve_persona_detail`` 只是别名。

        t11 R1-08：出镜命令必须整条拿走三元组，不能经二元组退化 unknown。
        """

        resolver = _HostResolver(
            PERSONAS,
            {UMO: {"session_service_config": {"persona_id": "persona-umo"}}},
        )
        plugin = self._plugin(resolver=resolver)
        resolved = await plugin._resolve_persona(_MessageEvent(UMO))
        self.assertEqual(resolved, ("persona-umo", "UMO 强制的人设", attempt_diagnostics.PERSONA_SOURCE_UMO))
        self.assertEqual(
            await plugin._resolve_persona_detail(_MessageEvent(UMO)),
            resolved,
            "两个名字必须给出完全相同的三元组（同一次解析、同一份来源）",
        )

    def test_source_helper_domain_is_closed_and_unknown_is_never_guessed(self):
        source = _plugin_class()._persona_source
        self.assertEqual(
            source(forced_persona_id="p", persona_id="p"),
            attempt_diagnostics.PERSONA_SOURCE_UMO,
        )
        self.assertEqual(
            source(conversation_persona_id="p", persona_id="p"),
            attempt_diagnostics.PERSONA_SOURCE_CONVERSATION,
        )
        self.assertEqual(
            source(runner_persona_id="p", persona_id="p"),
            attempt_diagnostics.PERSONA_SOURCE_RUNNER,
        )
        self.assertEqual(source(webchat_default=True), attempt_diagnostics.PERSONA_SOURCE_IMPLICIT)
        self.assertEqual(source(persona_id="default"), attempt_diagnostics.PERSONA_SOURCE_IMPLICIT)
        # 没有任何来源信号、或信号与胜出者对不上 ⇒ unknown（不猜、不靠 id 相等反推）
        self.assertEqual(source(persona_id="p"), attempt_diagnostics.PERSONA_SOURCE_UNKNOWN)
        self.assertEqual(
            source(forced_persona_id="p", persona_id="q"),
            attempt_diagnostics.PERSONA_SOURCE_UNKNOWN,
        )
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
        secret = plugin.store.alias_secret()
        self.assertIn(attempt_diagnostics.persona_alias("persona-a", secret=secret), line)
        _no_identity(self, line, "persona-a")

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


    async def test_single_file_stat_failure_fails_safely_before_any_provider_call(self):
        """t11 R1-09：单个文件 stat 被拒**不得**把整库读成"空库"（基线会把 adapter 照调）。"""

        plugin = self._plugin()
        plugin.store.add_reference("persona-a", png_bytes(seed=21), PNG_MIME)
        name = plugin.store.scan_references("persona-a").entries[0]["name"]
        real_stat = Path.stat

        def failing_stat(self, *args, **kwargs):
            if self.name == name:
                raise PermissionError("denied")
            return real_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", failing_stat):
            scan = plugin.store.scan_references("persona-a")
            self.assertEqual(scan.status, persona_store.REFERENCE_STATUS_ERROR)
            self.assertEqual(scan.reason, "stat_failed:PermissionError")
            self.assertEqual(scan.pool, 0, "没读出来的条目不能算可读池")
            task = self._task()
            with patch("imago_fusion.main.logger", self.logger):
                with self.assertRaises(PersonaReferenceError) as raised:
                    await plugin._prepare(task)
        self.assertEqual(raised.exception.status, persona_store.REFERENCE_STATUS_ERROR)
        # 失败快照与日志同源：同一份 status/pool/reason，且 0 次 adapter。
        self.assertEqual(task.runtime["persona_library_status"], persona_store.REFERENCE_STATUS_ERROR)
        self.assertEqual(task.runtime["persona_reference_pool"], 0)
        self.assertEqual(task.runtime["persona_library_reason"], "stat_failed:PermissionError")
        self.assertNotIn(TaskStage.REQUESTING_PROVIDER, plugin.scheduler.stages)
        warnings = [item for item in self.logger.lines if "人格图库" in item]
        self.assertEqual(len(warnings), 1, self.logger.lines)
        self.assertIn("status=error", warnings[0])
        self.assertIn("reason=stat_failed:PermissionError", warnings[0])
        # 日志不落私密路径 / 原始 ID / 文件名。
        self.assertNotIn(str(self.root), warnings[0])
        _no_identity(self, warnings[0], "persona-a")
        case_no_name = warnings[0]
        self.assertNotIn(name, case_no_name)

    async def test_read_failure_reports_only_the_images_it_actually_read(self):
        """读取在途中失败：池只报**已真正读到的**张数，不拿枚举数冒充实发。"""

        plugin = self._plugin()
        for seed in (31, 32, 33):
            plugin.store.add_reference("persona-a", png_bytes(seed=seed), PNG_MIME)
        names = [entry["name"] for entry in plugin.store.scan_references("persona-a").entries]
        broken = names[1]
        real_read = plugin.store.read_reference
        calls = {"count": 0}

        def flaky_read(persona_id, filename):
            calls["count"] += 1
            if filename == broken:
                raise PersonaReferenceError("read_failed:PermissionError")
            return real_read(persona_id, filename)

        task = self._task()
        with patch.object(plugin.store, "read_reference", flaky_read):
            with patch("imago_fusion.main.logger", self.logger):
                with self.assertRaises(PersonaReferenceError) as raised:
                    await plugin._prepare(task)
        self.assertEqual(raised.exception.reason, "read_failed:PermissionError")
        self.assertEqual(task.runtime["persona_library_status"], persona_store.REFERENCE_STATUS_ERROR)
        self.assertEqual(task.runtime["persona_reference_pool"], 1, "只有 1 张真的读进内存")
        self.assertEqual(task.runtime["persona_library_reason"], "read_failed:PermissionError")
        self.assertEqual(calls["count"], 2, "读到坏文件就停，不再往下读")
        self.assertNotIn(TaskStage.REQUESTING_PROVIDER, plugin.scheduler.stages)
        warnings = [item for item in self.logger.lines if "人格图库" in item]
        self.assertIn("pool=1", warnings[0])
        self.assertIn("reason=read_failed:PermissionError", warnings[0])

    async def test_file_vanishing_after_enumeration_is_error_not_ok_or_empty(self):
        """t5 现场：枚举成功、随后文件消失。

        首候选在这种输入上给出 ``library=ok, pool=1`` 并照常请求 adapter；正确行为是
        ``library=error`` + 只报**真正读进内存**的张数（这里是 0），并且在任何
        adapter 调用之前安全失败。
        """

        plugin = self._plugin()
        for seed in (41, 42, 43):
            plugin.store.add_reference("persona-a", png_bytes(seed=seed), PNG_MIME)
        self.assertEqual(len(plugin.store.scan_references("persona-a").entries), 3)
        real_read = plugin.store.read_reference

        def vanishing_read(persona_id, filename):
            # 枚举已经数到这张，真正要读时它已经不在了（外部清理/并发删除）。
            (plugin.store.persona_dir(persona_id) / filename).unlink(missing_ok=True)
            return real_read(persona_id, filename)

        task = self._task()
        with patch.object(plugin.store, "read_reference", vanishing_read):
            with patch("imago_fusion.main.logger", self.logger):
                with self.assertRaises(PersonaReferenceError) as raised:
                    await plugin._prepare(task)
        self.assertEqual(raised.exception.reason, "read_missing")
        self.assertEqual(
            task.runtime["persona_library_status"], persona_store.REFERENCE_STATUS_ERROR
        )
        self.assertEqual(task.runtime["persona_reference_pool"], 0, "一张都没读进内存")
        self.assertEqual(task.runtime["persona_library_reason"], "read_missing")
        self.assertNotIn(TaskStage.REQUESTING_PROVIDER, plugin.scheduler.stages)
        warnings = [item for item in self.logger.lines if "人格图库" in item]
        self.assertEqual(len(warnings), 1)
        self.assertIn("status=error", warnings[0])
        self.assertIn("pool=0", warnings[0])
        self.assertIn("reason=read_missing", warnings[0])
        _no_identity(self, warnings[0], "persona-a")


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
        _no_identity(self, json.dumps(record, ensure_ascii=False), "persona-a")


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

    def _plugin(self):
        plugin = object.__new__(_plugin_class())
        plugin.raw_config = {}
        plugin.session = None
        plugin.store = PersonaStore(self.root, lambda: 20 * 1024 * 1024)
        plugin.scheduler = _SchedulerStub()
        plugin.enhancer = _Counter()
        plugin.upscaler = _Counter()
        plugin.sender = _Sender()
        plugin.quota_store = None
        return plugin

    async def _finalize(self, task):
        plugin = self._plugin()
        with patch("imago_fusion.main.logger", self.logger):
            await plugin._finalize_task(task)
        return json.loads((plugin.store.task_dir(task.id) / "task.json").read_text("utf-8"))

    def _task(self, task_id, *, persona_id="persona-a", persona_source=attempt_diagnostics.PERSONA_SOURCE_UMO):
        task = DrawTask(task_id, UMO, GenerationRequest("draw", count=1))
        task.created_at = __import__("time").monotonic()
        task.runtime["source_event"] = _Event()
        task.runtime["original_user_prompt"] = "draw"
        task.state = TaskState.SUCCEEDED
        if persona_id:
            task.persona_id = persona_id
            task.persona_prompt = "人设"
            task.runtime["persona_source"] = persona_source
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
        secret = PersonaStore(self.root, lambda: 20 * 1024 * 1024).alias_secret()
        self.assertEqual(persona["alias"], attempt_diagnostics.persona_alias("persona-a", secret=secret))
        self.assertNotEqual(persona["alias"], attempt_diagnostics.PERSONA_ALIAS_UNKEYED)
        _no_identity(self, json.dumps(manifest["persona"], ensure_ascii=False), "persona-a")
        self.assertEqual(len(manifest["reference_plans"]), 1)
        plan = manifest["reference_plans"][0]
        self.assertEqual((plan["explicit_sent"], plan["persona_sent"]), (1, 0))
        self.assertEqual(plan["reason"], "limit_exhausted_by_explicit")
        # 被限额挤占必须留一条可检索的 WARNING，且不含 Persona 身份。
        squeeze = [item for item in self.logger.lines if "限额挤占" in item]
        self.assertTrue(squeeze)
        _no_identity(self, squeeze[0], "persona-a")

    async def test_manifest_and_library_log_project_the_same_source_value(self):
        """manifest 的 persona.source 与「人格图库」日志同取 runtime 判定值，不二次猜测。"""

        cases = (
            (attempt_diagnostics.PERSONA_SOURCE_CONVERSATION, "6" * 32),
            (attempt_diagnostics.PERSONA_SOURCE_RUNNER, "7" * 32),
            ("", "9" * 32),
        )
        for value, task_id in cases:
            task = self._task(task_id, persona_source=value)
            manifest = await self._finalize(task)
            expected = value or attempt_diagnostics.PERSONA_SOURCE_UNKNOWN
            self.assertEqual(manifest["persona"]["source"], expected, value)
            _no_identity(self, json.dumps(manifest["persona"], ensure_ascii=False), "persona-a")

            with patch("imago_fusion.main.logger", self.logger):
                self._plugin()._log_persona_library(task)
            line = [item for item in self.logger.lines if "人格图库" in item][-1]
            self.assertIn(f"source={expected}", line, value)
            _no_identity(self, line, "persona-a")

    async def test_manifest_reports_library_error_and_bounded_plan_range(self):
        """失败快照与 manifest 同源；逐腿范围可还原全局腿序（t11 R1-09/R1-12）。"""

        task = self._task("8" * 32)
        task.runtime["persona_library_status"] = persona_store.REFERENCE_STATUS_ERROR
        task.runtime["persona_library_reason"] = "stat_failed:PermissionError"
        task.runtime["persona_reference_pool"] = 0
        task.runtime["reference_plans_dropped"] = 3
        task.runtime["reference_plans"] = [
            {
                "node": "node-a", "model": "model-1", "kind": "normal",
                "reference_limit": 1, "explicit_sent": 1, "persona_sent": 0,
                "persona_pool": 0, "persona_budget": 0,
                "library": persona_store.REFERENCE_STATUS_ERROR,
                "reason": "limit_exhausted_by_explicit",
                "seq": 4, "route": "primary", "node_index": 1, "node_count": 2,
                "model_index": 1, "model_attempts": 1,
            },
            {
                "node": "node-b", "model": "model-2", "kind": "normal",
                "reference_limit": 2, "explicit_sent": 1, "persona_sent": 1,
                "persona_pool": 1, "persona_budget": 1,
                "library": persona_store.REFERENCE_STATUS_ERROR,
                "reason": "limit_covers_pool",
                "seq": 5, "route": "fallback", "node_index": 2, "node_count": 2,
                "model_index": 1, "model_attempts": 1,
            },
        ]
        manifest = await self._finalize(task)
        persona = manifest["persona"]
        self.assertEqual(persona["library"], persona_store.REFERENCE_STATUS_ERROR)
        self.assertEqual(persona["pool"], 0)
        self.assertEqual(persona["reason"], "stat_failed:PermissionError")
        self.assertEqual(
            manifest["reference_plan_range"],
            {"limit": 16, "kept": 2, "dropped": 3, "first_seq": 4, "last_seq": 5},
        )
        self.assertEqual([item["seq"] for item in manifest["reference_plans"]], [4, 5])
        _no_identity(self, json.dumps(manifest["reference_plans"], ensure_ascii=False), "persona-a")

    async def test_manifest_without_persona_has_no_persona_block(self):
        manifest = await self._finalize(self._task("7" * 32, persona_id=None))
        self.assertNotIn("persona", manifest)


# ======================================================================
# 7.5 部署密钥：别名可追溯但不可被公开常量反查（t11 R1-11）
# ======================================================================
class StoreAliasSecretTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_secret_is_created_owner_only_and_survives_restart(self):
        store = PersonaStore(self.root, lambda: 20 * 1024 * 1024)
        secret = store.alias_secret()
        self.assertEqual(len(secret), persona_store.ALIAS_SECRET_BYTES)
        path = self.root / persona_store.ALIAS_SECRET_FILENAME
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, "密钥文件必须仅属主可读")
        # 重启（新实例）复用同一把：否则日志里的别名跨重启就失去可追溯性。
        again = PersonaStore(self.root, lambda: 20 * 1024 * 1024).alias_secret()
        self.assertEqual(again, secret)
        # 别名稳定且不是"公开常量 + ID"的可枚举映射。
        self.assertEqual(
            attempt_diagnostics.persona_alias("persona-a", secret=secret),
            attempt_diagnostics.persona_alias("persona-a", secret=again),
        )

    def test_unwritable_root_degrades_to_unkeyed_instead_of_raising(self):
        store = PersonaStore(self.root, lambda: 20 * 1024 * 1024)
        with patch.object(persona_store.os, "open", side_effect=PermissionError("denied")):
            self.assertEqual(store.alias_secret(), b"")
        # 已经失败过就别每次请求都重试磁盘。
        with patch.object(persona_store.os, "open", side_effect=AssertionError("不该再试")):
            self.assertEqual(store.alias_secret(), b"")
        self.assertEqual(
            attempt_diagnostics.persona_alias("persona-a", secret=store.alias_secret()),
            attempt_diagnostics.PERSONA_ALIAS_UNKEYED,
        )


# ======================================================================
# 7.6 逐腿记账：真实调用边界 + 有界单调序号（t11 R1-12）
# ======================================================================
class ReferencePlanOrderTests(unittest.IsolatedAsyncioTestCase):
    def _scheduler(self, runtime):
        return TaskScheduler(
            lambda: runtime,
            None,
            lambda *_: True,
            lambda *_: None,
            lambda *_a: None,
        )

    def _task(self, *, explicit=1, persona=1):
        task = DrawTask(
            "9" * 32,
            UMO,
            GenerationRequest("画一只猫", references=tuple(_image(seed=seed) for seed in range(1, explicit + 1))),
        )
        task.runtime["persona_references"] = [_persona_image(seed=100 + seed) for seed in range(persona)]
        task.runtime["persona_library_status"] = "ok"
        task.runtime["persona_reference_pool"] = persona
        return task

    async def test_adapter_construction_failure_records_no_leg(self):
        """构造就抛错的腿**没有**真正调用 adapter，不得冒充实发。"""

        task = self._task()

        def explode(config):
            raise RuntimeError("boom")

        original = scheduler_module.ADAPTERS.get("custom_endpoint")
        scheduler_module.ADAPTERS["custom_endpoint"] = explode
        try:
            scheduler = self._scheduler(RuntimeConfig(providers=(_provider(limit=2),)))
            try:
                with self.assertRaises(RuntimeError):
                    await scheduler._generate(task)
            finally:
                await scheduler.close()
        finally:
            if original is None:
                scheduler_module.ADAPTERS.pop("custom_endpoint", None)
            else:
                scheduler_module.ADAPTERS["custom_endpoint"] = original
        self.assertFalse(task.runtime.get("reference_plans"), task.runtime.get("reference_plans"))
        self.assertFalse(task.runtime.get("reference_plan_seq"))

    async def test_each_real_call_gets_a_monotonic_seq_and_route(self):
        calls = []

        class _Adapter:
            def __init__(self, config):
                self.config = config

            async def generate(self, session, request, key):
                calls.append({"node": self.config.id, "model": self.config.model})
                return [ImageResult(data=b"image")]

        original = scheduler_module.ADAPTERS.get("custom_endpoint")
        scheduler_module.ADAPTERS["custom_endpoint"] = _Adapter
        try:
            providers = (
                _provider("node-a", limit=1, models=("m1", "m2")),
                _provider("node-b", limit=2, models=("m3",)),
            )
            task = self._task()
            scheduler = self._scheduler(RuntimeConfig(providers=providers))
            try:
                await scheduler._generate(task)
            finally:
                await scheduler.close()
        finally:
            if original is None:
                scheduler_module.ADAPTERS.pop("custom_endpoint", None)
            else:
                scheduler_module.ADAPTERS["custom_endpoint"] = original
        plans = task.runtime["reference_plans"]
        # 第一节点第一模型就成功 ⇒ 只有 1 条真实腿。
        self.assertEqual(len(plans), len(calls))
        self.assertEqual([item["seq"] for item in plans], [1])
        self.assertEqual(plans[0]["route"], "primary")
        self.assertEqual(plans[0]["node_index"], 1)
        self.assertEqual(plans[0]["node_count"], 2)
        self.assertEqual(plans[0]["model_index"], 1)
        self.assertEqual(plans[0]["model_attempts"], 2)

    def test_truncation_keeps_the_tail_and_a_countable_dropped_prefix(self):
        task = self._task()
        plan = TaskScheduler._plan_attempt_request(task, _provider(limit=2), RuntimeConfig(providers=()))
        for index in range(20):
            TaskScheduler._record_reference_plan(task, plan, node="node-%d" % index, model="m1")
        plans = task.runtime["reference_plans"]
        self.assertEqual(len(plans), 16, "有界：最多留 16 条")
        self.assertEqual(task.runtime["reference_plans_dropped"], 4)
        self.assertEqual([item["seq"] for item in plans], list(range(5, 21)), "保留原序、序号不重编")
        self.assertEqual(plans[0]["node"], "node-4", "截断掉的是最早那几条")


# ======================================================================
# 7.7 挤占提示：无身份、按胜出腿、尊重纯图策略（t11 R1-13）
# ======================================================================
class PersonaTrimNoticeTests(unittest.IsolatedAsyncioTestCase):
    def _task(self, *, mode="normal", node="node-a", model="m1"):
        task = DrawTask("a" * 32, UMO, GenerationRequest("draw", count=1))
        task.created_at = __import__("time").monotonic()
        task.runtime["succeeded"] = {"provider_id": node, "model": model, "mode": mode}
        return task

    def _plan(self, reason, *, node="node-a", model="m1", kind="normal"):
        return {
            "node": node, "model": model, "kind": kind, "reference_limit": 1,
            "explicit_sent": 1, "persona_sent": 0, "persona_pool": 1, "persona_budget": 0,
            "library": "ok", "reason": reason, "seq": 1, "route": "primary",
            "node_index": 1, "node_count": 1, "model_index": 1, "model_attempts": 1,
        }

    def test_notice_is_chosen_from_the_winning_leg_only(self):
        plugin = _plugin_class()
        exhausted = self._task()
        exhausted.runtime["reference_plans"] = [self._plan("limit_exhausted_by_explicit")]
        self.assertEqual(plugin._persona_trim_notice(exhausted), plugin.PERSONA_TRIM_NOTICE_EXHAUSTED)
        trimmed = self._task()
        trimmed.runtime["reference_plans"] = [self._plan("limit_trimmed")]
        self.assertEqual(plugin._persona_trim_notice(trimmed), plugin.PERSONA_TRIM_NOTICE_TRIMMED)

    def test_failed_leg_trim_never_mislabels_the_winning_leg(self):
        plugin = _plugin_class()
        task = self._task(node="node-b", model="m2")
        task.runtime["reference_plans"] = [
            self._plan("limit_exhausted_by_explicit", node="node-a", model="m1"),
            self._plan("limit_covers_pool", node="node-b", model="m2"),
        ]
        self.assertEqual(plugin._persona_trim_notice(task), "", "胜出腿没被挤占就不该提示")

    def test_text_fallback_winner_is_matched_by_its_own_kind(self):
        plugin = _plugin_class()
        task = self._task(mode="text_fallback")
        task.runtime["reference_plans"] = [
            self._plan("limit_exhausted_by_explicit", kind="normal"),
            self._plan("text_fallback", kind="text_fallback"),
        ]
        self.assertEqual(plugin._persona_trim_notice(task), "", "文字腿 0/0 不是「挤占」")

    def test_pure_image_only_strategy_is_respected(self):
        from imago_fusion.core.route_identity import RouteIdentity

        plugin = _plugin_class()
        group = RouteIdentity(message_type="GroupMessage", platform_name="aiocqhttp")
        silent = RouteIdentity(
            message_type="GroupMessage", platform_name="aiocqhttp", sender_user_id="10001"
        )
        friend = RouteIdentity(
            message_type="FriendMessage", platform_name="aiocqhttp", sender_user_id="10001"
        )
        cfg = SimpleNamespace(at_trigger_user=True)
        self.assertTrue(
            plugin._pure_image_only_reply(cfg=cfg, identity=group, generation_success=True),
            "群聊开启 @ 但没有可信目标 ⇒ 只能纯图，提示必须让位",
        )
        self.assertFalse(
            plugin._pure_image_only_reply(cfg=cfg, identity=silent, generation_success=True)
        )
        self.assertFalse(
            plugin._pure_image_only_reply(cfg=cfg, identity=friend, generation_success=True)
        )
        self.assertFalse(
            plugin._pure_image_only_reply(
                cfg=SimpleNamespace(at_trigger_user=False), identity=group, generation_success=True
            )
        )

    async def test_trim_notice_reaches_the_chain_but_never_the_bare_image_branch(self):
        from imago_fusion.core.route_identity import RouteIdentity

        for sender, expected in (("10001", True), ("", False)):
            with TemporaryDirectory() as directory:
                logger = _Logger()
                plugin = object.__new__(_plugin_class())
                plugin.raw_config = {}
                plugin.session = None
                plugin.store = PersonaStore(Path(directory), lambda: 20 * 1024 * 1024)
                plugin.scheduler = _SchedulerStub()
                plugin.enhancer = _Counter()
                plugin.upscaler = _Counter()
                plugin.sender = _Sender()
                plugin.quota_store = None
                task = DrawTask("b" * 32, UMO, GenerationRequest("draw", count=1))
                task.created_at = __import__("time").monotonic()
                task.runtime["source_event"] = _Event()
                task.runtime["original_user_prompt"] = "draw"
                task.runtime["route_identity"] = RouteIdentity(
                    message_type="GroupMessage", platform_name="aiocqhttp", sender_user_id=sender
                )
                task.runtime["succeeded"] = {"provider_id": "node-a", "model": "m1", "mode": "normal"}
                task.runtime["reference_plans"] = [self._plan("limit_exhausted_by_explicit")]
                with patch("imago_fusion.main.logger", logger):
                    await plugin._finish_task(task, [ImageResult(data=b"image", mime_type=PNG_MIME)])
                chain = plugin.sender.chain or []
                texts = [getattr(item, "text", "") for item in chain]
                self.assertEqual(any("人格参考图" in text for text in texts), expected, texts)
                if expected:
                    self.assertTrue(task.runtime.get("persona_trim_notified"))


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
