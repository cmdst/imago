"""v1.4.0 契约 §10.1：`reference_fallback_config` 新键、schema 与迁移。

覆盖 T-C1..T-C9：
- schema 只剩 11 组 / 降级组 5 键 / 默认值 1；
- ``load_config`` 的钳制（缺省、4、-1、"2"、None）；
- 类型面彻底删掉实验遮挡配置与旧次数键；
- 迁移的新键优先、幂等、无键不捏造、BOM 与其余组不动、落盘失败只 warning。
"""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock

from imago_fusion.core.config import load_config
from imago_fusion.core.models import ReferenceFallbackConfig, RuntimeConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "_conf_schema.json"
GROUP = "reference_fallback_config"
NEW_KEY = "max_retries_per_node"
OLD_KEY = "max_extra_attempts"


class _RawConfig(dict):
    """模拟 AstrBot 的配置对象：dict + save_config()（真实落盘，utf-8-sig 保 BOM）。"""

    def __init__(self, data, path=None):
        super().__init__(copy.deepcopy(data))
        self.path = Path(path) if path else None
        self.saved = 0
        self.fail = False

    def save_config(self):
        if self.fail:
            raise RuntimeError("disk full")
        if self.path is not None:
            self.path.write_text(
                json.dumps(dict(self), ensure_ascii=False, indent=2), encoding="utf-8-sig",
            )
        self.saved += 1


def _plugin(raw):
    from imago_fusion.main import ImagoFusion

    plugin = object.__new__(ImagoFusion)
    plugin.raw_config = raw
    return plugin


class SchemaShapeTests(unittest.TestCase):
    def test_c1_schema_has_eleven_groups_and_five_fallback_keys(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(schema), {
            "log_with_bot_id", "providers", "optimizer_config", "quota_config",
            "task_config", "storage_config", GROUP,
            "enhance_config", "upscale_config", "proxy_url", "template_override",
        })
        items = schema[GROUP]["items"]
        self.assertEqual(set(items), {
            "enable", NEW_KEY, "caption_max_chars", "vision_timeout_seconds", "notify_user",
        })
        self.assertEqual(items[NEW_KEY]["default"], 1)
        # schema 只登记类型与默认值；边界在 core/config.py 与 web_api 各钳一次。
        for field in items.values():
            self.assertEqual(set(field) & {"min", "max"}, set())
        # 组级 hint 说明"只有明确安全/内容合规拒绝才触发"。
        hint = schema[GROUP]["hint"]
        self.assertIn("安全", hint)
        self.assertIn("内容合规", hint)
        self.assertIn("默认关闭", hint)
        self.assertNotIn("遮挡", json.dumps(schema, ensure_ascii=False))

    def test_c3_type_surface_has_no_experimental_knobs(self):
        self.assertFalse(hasattr(RuntimeConfig, "experimental_reference"))
        names = set(ReferenceFallbackConfig.__dataclass_fields__)
        self.assertEqual(names, {
            "enable", NEW_KEY, "caption_max_chars", "vision_timeout_seconds", "notify_user",
        })
        self.assertNotIn(OLD_KEY, names)


class ClampTests(unittest.TestCase):
    @staticmethod
    def _value(raw_value, *, present=True):
        group = {"enable": True}
        if present:
            group[NEW_KEY] = raw_value
        cfg = load_config({"reference_fallback_config": group})
        return cfg.reference_fallback.max_retries_per_node

    def test_c2_default_when_absent(self):
        self.assertEqual(self._value(None, present=False), 1)

    def test_c2_bounds(self):
        self.assertEqual(self._value(4), 3)
        self.assertEqual(self._value(-1), 0)
        self.assertEqual(self._value(3), 3)
        self.assertEqual(self._value(0), 0)

    def test_c2_string_digits_are_read(self):
        self.assertEqual(self._value("2"), 2)

    def test_c2_none_value_falls_back_to_default(self):
        self.assertEqual(self._value(None), 1)

    def test_c2_garbage_falls_back_to_default(self):
        self.assertEqual(self._value("many"), 1)

    def test_c9_disabled_by_default(self):
        cfg = load_config({"reference_fallback_config": {}})
        self.assertFalse(cfg.reference_fallback.enable)


class MigrationTests(unittest.TestCase):
    def _base(self):
        return {
            "log_with_bot_id": True,
            "providers": [{"id": "main", "api_type": "openai_image",
                           "base_url": "https://api.invalid/v1", "api_keys": ["k"]}],
            "task_config": {"generation_timeout": 300},
            "enhance_config": {"enable": True, "api_key": "a", "secret_key": "b"},
            GROUP: {OLD_KEY: 2, "enable": True, "caption_max_chars": 180,
                    "vision_timeout_seconds": 15, "notify_user": False},
            "experimental_reference_config": {
                "enable": False, "occluder_provider_id": "main",
                "occlusion_prompt": "x" * 12, "max_images": 1,
                "timeout_seconds": 20, "caption_max_chars": 200,
            },
        }

    def test_c4_old_key_converted_and_experimental_group_dropped(self, tmp_path=None):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "imago_fusion_config.json"
            path.write_text(json.dumps(self._base(), ensure_ascii=False, indent=2),
                            encoding="utf-8-sig")
            raw = _RawConfig(json.loads(path.read_text(encoding="utf-8-sig")), path)
            before = copy.deepcopy(dict(raw))
            _plugin(raw)._migrate_reference_fallback_keys()
            self.assertEqual(raw[GROUP][NEW_KEY], 2)
            self.assertNotIn(OLD_KEY, raw[GROUP])
            self.assertNotIn("experimental_reference_config", raw)
            # 除删除项与被转换键外，所有叶值逐一相等。
            for key, value in before.items():
                if key in ("experimental_reference_config", GROUP):
                    continue
                self.assertEqual(raw[key], value, key)
            self.assertEqual(raw[GROUP]["enable"], True)
            self.assertEqual(raw[GROUP]["caption_max_chars"], 180)
            # 落盘仍带 BOM（迁移只经由 save_config，不自己写文件）。
            self.assertEqual(path.read_bytes()[:3], b"\xef\xbb\xbf")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8-sig"))[GROUP][NEW_KEY], 2)

    def test_c5_new_key_wins_over_old(self):
        raw = _RawConfig(self._base())
        raw[GROUP].update({NEW_KEY: 3, OLD_KEY: 0})
        _plugin(raw)._migrate_reference_fallback_keys()
        self.assertEqual(raw[GROUP][NEW_KEY], 3)
        self.assertNotIn(OLD_KEY, raw[GROUP])

    def test_c6_migration_is_idempotent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.json"
            path.write_text(json.dumps(self._base(), ensure_ascii=False, indent=2),
                            encoding="utf-8-sig")
            raw = _RawConfig(json.loads(path.read_text(encoding="utf-8-sig")), path)
            plugin = _plugin(raw)
            plugin._migrate_reference_fallback_keys()
            first = hashlib.sha256(path.read_bytes()).hexdigest()
            plugin._migrate_reference_fallback_keys()
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), first)
            self.assertEqual(raw.saved, 1)

    def test_c7_no_keys_means_no_write(self):
        raw = _RawConfig({"log_with_bot_id": True, GROUP: {"enable": False}})
        _plugin(raw)._migrate_reference_fallback_keys()
        self.assertNotIn(NEW_KEY, raw[GROUP])
        self.assertEqual(raw.saved, 0)

    def test_c8_save_failure_only_warns(self):
        raw = _RawConfig(self._base())
        raw.fail = True
        plugin = _plugin(raw)
        with mock.patch("imago_fusion.main.logger") as fake_logger:
            plugin._migrate_reference_fallback_keys()
        # 内存里仍已迁移（仅本次运行生效），但只打一条 warning、不抛。
        self.assertEqual(raw[GROUP][NEW_KEY], 2)
        warnings = [call.args[0] for call in fake_logger.warning.call_args_list]
        self.assertEqual(len(warnings), 1)
        self.assertIn("落盘失败", warnings[0])

    def test_c4_migration_does_not_open_the_config_file_itself(self):
        raw = _RawConfig(self._base())
        plugin = _plugin(raw)
        # 迁移只改字典并调 save_config()，不自己做文件 IO。
        with mock.patch("builtins.open", side_effect=AssertionError("no file IO")):
            plugin._migrate_reference_fallback_keys()
        self.assertEqual(raw[GROUP][NEW_KEY], 2)
