"""IMAGO·FUSION v1.4.1 参考图压缩组（Web API + WebUI）专属回归。

契约 §6.4 / §6.5 的可执行版本：``reference_compress_config`` 这一组必须
「一次描述、三处贯通」——``GROUP_DEFS`` 是唯一形状源，catalog、快照与保存
回路都从它派生，前端不再写第二份字段表。

覆盖点：
* catalog：7 键冻结顺序、默认值与 ``_conf_schema.json`` 一致、单位/范围、
  与「文字降级」两组并存且互不冒充；
* 快照：缺组时按默认补齐、``false`` 与 ``max_edge=0`` 原样返回、不夹带派生或凭据；
* 保存：缺席=保留、出现=逐键写入、越界钳制、类型非法只告警、未知键忽略、
  载荷形状错误整单拒绝、写盘失败回滚，且不影响其它组、其它节点与 API Key；
* 前端：以子进程运行 ``test/test_webui_v141.js``，验证真实 ``pages/webui/app.js``。
"""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
from pathlib import Path

import pytest
from quart import Quart

from imago_fusion.integrations.web_api import GROUP_DEFS, PageAPI

REPO_ROOT = Path(__file__).resolve().parent.parent
JS_TEST = REPO_ROOT / "test" / "test_webui_v141.js"
SCHEMA = REPO_ROOT / "_conf_schema.json"

COMPRESS_GROUP = "reference_compress_config"
COMPRESS_PAYLOAD = "reference_compress"
FALLBACK_GROUP = "reference_fallback_config"

# 契约 §3.3 冻结的 7 键（顺序即 catalog 顺序）
COMPRESS_KEYS = [
    "enable",
    "max_edge",
    "jpeg_quality",
    "target_kb",
    "min_bytes",
    "keep_alpha",
    "max_pixels",
]

# 契约 §3.3 冻结的范围（1 KB = 1024 B）
COMPRESS_RANGES = {
    "max_edge": (0, 4096),
    "jpeg_quality": (40, 95),
    "target_kb": (32, 2048),
    "min_bytes": (0, 1048576),
    "max_pixels": (1000000, 100000000),
}

COMPRESS_DEFAULTS = {
    "enable": True,
    "max_edge": 1536,
    "jpeg_quality": 85,
    "target_kb": 128,
    "min_bytes": 32768,
    "keep_alpha": False,
    "max_pixels": 40000000,
}


# ---------------------------------------------------------------- 夹具
class RawConfig(dict):
    """模拟 AstrBot 配置对象：dict + save_config()，可注入写盘失败与真实落盘。"""

    def __init__(self, data, path=None):
        super().__init__(copy.deepcopy(data))
        self.saved = 0
        self.fail = False
        self.path = Path(path) if path else None

    def save_config(self):
        if self.fail:
            raise RuntimeError("disk full")
        if self.path is not None:
            self.path.write_text(json.dumps(dict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        self.saved += 1


class Store:
    def __init__(self, primary="main"):
        self.primary = primary

    def get_primary_provider_id(self):
        return self.primary

    def set_primary_provider_id(self, value):
        self.primary = value


class Provider:
    def __init__(self, provider_id, model, kind):
        self._id = provider_id
        self._model = model
        self._kind = kind

    def meta(self):
        return type("Meta", (), {"id": self._id, "model": self._model, "type": self._kind})()


class Context:
    def __init__(self, providers=None):
        self._providers = providers or []

    def get_all_providers(self):
        return self._providers


class Plugin:
    def __init__(self, raw, primary="main"):
        self.raw_config = raw
        self.store = Store(primary)
        self.context = Context()

    async def list_persona_ids(self):
        return []


def base_config():
    """与升级前的真实配置同形：**没有** reference_compress_config。"""
    return {
        "providers": [
            {
                "id": "main",
                "api_type": "openai_image",
                "enabled": True,
                "base_url": "https://api.invalid/v1",
                "api_keys": ["sk-main", "sk-backup"],
                "astrbot_provider_id": "",
                "stream": False,
                "enable_proxy": False,
                "fallback_order": 0,
                "model": "gpt-image-1",
                "reference_image_limit": 3,
                "available_models": [],
                "default_size": "1024x1024",
                "timeout": 180,
                "thinking": "high",
                "custom_params": "",
                "extra_legacy": {"keep": True},
            }
        ],
        "optimizer_config": {"optimizer_provider_id": "", "vision_provider_id": ""},
        "reference_fallback_config": {
            "enable": False,
            "max_retries_per_node": 2,
            "caption_max_chars": 200,
            "vision_timeout_seconds": 20,
            "notify_user": True,
        },
    }


def build_app(raw):
    plugin = Plugin(raw)
    api = PageAPI(plugin)
    app = Quart(__name__)
    app.add_url_rule("/imago_fusion/providers", "providers", api.providers, methods=["GET"])
    app.add_url_rule("/imago_fusion/providers/save", "save_providers", api.save_providers, methods=["POST"])
    return app, plugin


def request(app, method="GET", path="/imago_fusion/providers", body=None):
    async def run():
        async with app.test_client() as client:
            response = await client.get(path) if method == "GET" else await client.post(path, json=body)
        return response.status_code, await response.get_json()

    return asyncio.run(run())


@pytest.fixture()
def env():
    raw = RawConfig(base_config())
    app, _plugin = build_app(raw)
    return app, raw


@pytest.fixture()
def env_disk(tmp_path):
    path = tmp_path / "imago_fusion_config.json"
    raw = RawConfig(base_config(), path=path)
    app, _plugin = build_app(raw)
    return app, raw, path


def get_state(env):
    status, payload = request(env[0])
    assert status == 200, payload
    return payload


def save(env, body):
    return request(env[0], "POST", "/imago_fusion/providers/save", body)


def read_disk(env_disk):
    return json.loads(env_disk[2].read_text(encoding="utf-8"))


# ---------------------------------------------------------------- catalog
class TestCompressCatalog:
    def test_catalog_lists_compressed_group_with_frozen_seven_keys(self, env):
        catalog = get_state(env)["catalog"]
        keys = [group["key"] for group in catalog["groups"]]
        # 两组并存，顺序稳定（文字降级在前、压缩在后），且不夹带 v1.4.0 之前的实验组。
        assert keys == [FALLBACK_GROUP, COMPRESS_GROUP]

        group = next(item for item in catalog["groups"] if item["key"] == COMPRESS_GROUP)
        assert group["payload"] == COMPRESS_PAYLOAD
        assert group["label"] == "参考图压缩"
        assert [field["key"] for field in group["fields"]] == COMPRESS_KEYS
        for field in group["fields"]:
            assert field["label"], field["key"]
            assert field["help"], field["key"]
            assert field["control"] in {"switch", "number", "textarea", "select", "provider_select"}
            assert field["type"] in {"bool", "int", "float", "str"}

    def test_catalog_defaults_match_conf_schema(self, env):
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        items = schema[COMPRESS_GROUP]["items"]
        assert list(items) == COMPRESS_KEYS, "schema 12 组内的键顺序是契约冻结的"
        group = next(item for item in get_state(env)["catalog"]["groups"] if item["key"] == COMPRESS_GROUP)
        by_key = {field["key"]: field for field in group["fields"]}
        for key in COMPRESS_KEYS:
            assert by_key[key]["default"] == items[key]["default"], key
            assert by_key[key]["type"] == items[key]["type"], key
        assert {key: by_key[key]["default"] for key in COMPRESS_KEYS} == COMPRESS_DEFAULTS

    def test_catalog_ranges_match_contract_and_zero_is_legal(self, env):
        group = next(item for item in get_state(env)["catalog"]["groups"] if item["key"] == COMPRESS_GROUP)
        by_key = {field["key"]: field for field in group["fields"]}
        for key, (low, high) in COMPRESS_RANGES.items():
            assert (by_key[key]["min"], by_key[key]["max"]) == (low, high), key
        # max_edge=0 是「完全不缩放」的合法值，不能被写成 min=1 之类的禁用态。
        assert by_key["max_edge"]["min"] == 0
        assert "0" in by_key["max_edge"]["help"]
        assert "不缩放" in by_key["max_edge"]["help"]
        assert "0" in by_key["min_bytes"]["help"]

    def test_catalog_help_states_soft_target_and_close_semantics(self, env):
        group = next(item for item in get_state(env)["catalog"]["groups"] if item["key"] == COMPRESS_GROUP)
        by_key = {field["key"]: field for field in group["fields"]}
        target_help = by_key["target_kb"]["help"]
        assert "软目标" in target_help and "硬限制" in target_help
        assert "target_unmet" in target_help
        assert "1024" in target_help, "1 KB = 1024 B 必须写在面板上"
        assert "零解码" in by_key["min_bytes"]["help"]
        assert "启用参考图压缩" == by_key["enable"]["label"]
        # 关闭语义必须在开关旁可见：按原字节发送，不产生压缩文件/诊断/日志。
        assert "原字节" in by_key["enable"]["help"]
        assert "不产生任何压缩文件" in by_key["enable"]["help"]

    def test_catalog_description_keeps_compress_separate_from_enhance(self, env):
        group = next(item for item in get_state(env)["catalog"]["groups"] if item["key"] == COMPRESS_GROUP)
        description = group["description"]
        assert "副本" in description
        assert "inputs/" in description
        assert "增强" in description and "放大" in description
        assert "关闭时零解码" in description

    def test_fallback_group_keeps_its_own_extra_control(self, env):
        groups = {group["key"]: group for group in get_state(env)["catalog"]["groups"]}
        assert groups[FALLBACK_GROUP].get("extra_control") == "vision_provider_select"
        # 压缩组没有非字段控件，也不该继承派生依赖。
        assert "extra_control" not in groups[COMPRESS_GROUP]
        assert "depends_on" not in groups[COMPRESS_GROUP]

    def test_catalog_group_defs_shape_is_single_source(self):
        definition = GROUP_DEFS[COMPRESS_GROUP]
        assert definition["payload"] == COMPRESS_PAYLOAD
        assert [field["key"] for field in definition["fields"]] == COMPRESS_KEYS
        # 字段表里不得混入密钥类字段（压缩是纯本地运算，不需要任何凭据）。
        serialized = json.dumps(definition, ensure_ascii=False)
        for forbidden in ("api_key", "secret", "token", "provider_id"):
            assert forbidden not in serialized


# ---------------------------------------------------------------- 快照
class TestCompressSnapshot:
    def test_snapshot_fills_defaults_when_group_missing(self, env):
        state = get_state(env)
        values = state[COMPRESS_PAYLOAD]
        # Quart 的 JSON provider 会按字母序输出键，顺序契约由 catalog 的字段表保证。
        assert set(values) == set(COMPRESS_KEYS)
        assert values == COMPRESS_DEFAULTS

    def test_snapshot_keeps_false_and_zero_verbatim(self, env):
        env[1][COMPRESS_GROUP] = {
            "enable": False,
            "max_edge": 0,
            "jpeg_quality": 60,
            "target_kb": 96,
            "min_bytes": 0,
            "keep_alpha": False,
            "max_pixels": 4000000,
        }
        values = get_state(env)[COMPRESS_PAYLOAD]
        assert values["enable"] is False
        assert values["max_edge"] == 0
        assert values["min_bytes"] == 0
        assert values["keep_alpha"] is False
        assert values["target_kb"] == 96

    def test_snapshot_accepts_legacy_string_booleans(self, env):
        env[1][COMPRESS_GROUP] = {"enable": "false", "keep_alpha": "true"}
        values = get_state(env)[COMPRESS_PAYLOAD]
        assert values["enable"] is False
        assert values["keep_alpha"] is True

    def test_snapshot_clamps_corrupted_numbers_for_display(self, env):
        env[1][COMPRESS_GROUP] = {"max_edge": 999999, "jpeg_quality": 5, "target_kb": "abc"}
        values = get_state(env)[COMPRESS_PAYLOAD]
        assert values["max_edge"] == 4096
        assert values["jpeg_quality"] == 40
        assert values["target_kb"] == COMPRESS_DEFAULTS["target_kb"]

    def test_snapshot_never_returns_derived_or_secret_fields(self, env):
        values = get_state(env)[COMPRESS_PAYLOAD]
        assert set(values) == set(COMPRESS_KEYS)
        for derived in ("vision_provider_id", "vision_provider_ready", "target_bytes", "api_key", "secret_key"):
            assert derived not in values
        body = json.dumps(get_state(env), ensure_ascii=False)
        assert "sk-main" not in body and "sk-backup" not in body

    def test_defaults_match_runtime_load_config(self):
        """面板默认值必须和真正跑推理的 load_config() 一致，否则「看着开着其实关着」。"""
        from imago_fusion.core.config import load_config

        config = load_config({})
        compress = config.reference_compress
        assert compress.enable is True
        assert compress.max_edge == COMPRESS_DEFAULTS["max_edge"]
        assert compress.jpeg_quality == COMPRESS_DEFAULTS["jpeg_quality"]
        assert compress.target_kb == COMPRESS_DEFAULTS["target_kb"]
        assert compress.target_bytes == COMPRESS_DEFAULTS["target_kb"] * 1024
        assert compress.min_bytes == COMPRESS_DEFAULTS["min_bytes"]
        assert compress.keep_alpha is False
        assert compress.max_pixels == COMPRESS_DEFAULTS["max_pixels"]

        off = load_config({COMPRESS_GROUP: {"enable": False, "max_edge": 0}}).reference_compress
        assert off.enable is False
        assert off.max_edge == 0


# ---------------------------------------------------------------- 保存
class TestCompressSave:
    def test_absent_group_keeps_stored_values(self, env):
        env[1][COMPRESS_GROUP] = dict(COMPRESS_DEFAULTS, enable=False, max_edge=0)
        status, payload = save(env, {"providers": []})
        assert status == 200, payload
        assert env[1][COMPRESS_GROUP]["enable"] is False
        assert env[1][COMPRESS_GROUP]["max_edge"] == 0

    def test_all_seven_keys_round_trip(self, env):
        body = {
            COMPRESS_PAYLOAD: {
                "enable": False,
                "max_edge": 0,
                "jpeg_quality": 70,
                "target_kb": 256,
                "min_bytes": 0,
                "keep_alpha": True,
                "max_pixels": 8000000,
            }
        }
        status, payload = save(env, body)
        assert status == 200, payload
        stored = env[1][COMPRESS_GROUP]
        assert stored["enable"] is False
        assert stored["max_edge"] == 0
        assert stored["keep_alpha"] is True
        # 保存响应里直接带回新快照，前端不用再 GET 一次。
        assert payload[COMPRESS_PAYLOAD]["max_edge"] == 0
        assert payload[COMPRESS_PAYLOAD]["keep_alpha"] is True
        # 再 GET 一次确认可回读。
        assert get_state(env)[COMPRESS_PAYLOAD]["jpeg_quality"] == 70

    @pytest.mark.parametrize(
        "key,raw,expected",
        [
            ("max_edge", 99999, 4096),
            ("max_edge", -3, 0),
            ("jpeg_quality", 10, 40),
            ("jpeg_quality", 200, 95),
            ("target_kb", 8, 32),
            ("target_kb", 4096, 2048),
            ("min_bytes", -1, 0),
            ("min_bytes", 99999999, 1048576),
            ("max_pixels", 1, 1000000),
            ("max_pixels", 10**9, 100000000),
        ],
    )
    def test_out_of_range_values_are_clamped(self, env, key, raw, expected):
        status, payload = save(env, {COMPRESS_PAYLOAD: {key: raw}})
        assert status == 200, payload
        assert env[1][COMPRESS_GROUP][key] == expected

    @pytest.mark.parametrize("bad", [{"x": 1}, "abc", None])
    def test_invalid_type_warns_and_keeps_old_value(self, env, bad):
        env[1][COMPRESS_GROUP] = dict(COMPRESS_DEFAULTS)
        status, payload = save(env, {COMPRESS_PAYLOAD: {"target_kb": bad, "jpeg_quality": 88}})
        assert status == 200, payload
        # 同一请求里合法字段照写，非法字段保留旧值 —— 不因一个键整单 500。
        assert env[1][COMPRESS_GROUP]["target_kb"] == COMPRESS_DEFAULTS["target_kb"]
        assert env[1][COMPRESS_GROUP]["jpeg_quality"] == 88
        warnings = [item for item in payload["warnings"] if item["code"] == "param_invalid_value"]
        assert [item["key"] for item in warnings] == [f"{COMPRESS_GROUP}.target_kb"]
        assert "目标体积" in warnings[0]["message"]

    def test_boolean_field_accepts_string_false(self, env):
        status, payload = save(env, {COMPRESS_PAYLOAD: {"enable": "false", "keep_alpha": "1"}})
        assert status == 200, payload
        assert env[1][COMPRESS_GROUP]["enable"] is False
        assert env[1][COMPRESS_GROUP]["keep_alpha"] is True

    def test_unknown_key_inside_group_is_ignored(self, env):
        status, payload = save(env, {COMPRESS_PAYLOAD: {"enable": False, "unknown_knob": 7}})
        assert status == 200, payload
        assert "unknown_knob" not in env[1][COMPRESS_GROUP]
        assert "unknown_knob" not in payload[COMPRESS_PAYLOAD]

    def test_group_payload_must_be_object(self, env):
        before = copy.deepcopy(dict(env[1]))
        status, payload = save(env, {COMPRESS_PAYLOAD: ["enable"]})
        assert status == 400
        assert payload["error"]["field"] == COMPRESS_PAYLOAD
        assert "参考图压缩" in payload["message"]
        assert dict(env[1]) == before

    def test_save_compress_keeps_fallback_provider_and_other_keys(self, env):
        before_provider = copy.deepcopy(env[1]["providers"][0])
        status, payload = save(env, {COMPRESS_PAYLOAD: {"max_edge": 1024}})
        assert status == 200, payload
        assert env[1]["providers"][0] == before_provider
        assert env[1]["providers"][0]["api_keys"] == ["sk-main", "sk-backup"]
        assert env[1]["providers"][0]["extra_legacy"] == {"keep": True}
        assert env[1]["reference_fallback_config"]["max_retries_per_node"] == 2
        assert env[1]["optimizer_config"] == {"optimizer_provider_id": "", "vision_provider_id": ""}

    def test_save_fallback_keeps_compress_values(self, env):
        env[1][COMPRESS_GROUP] = dict(COMPRESS_DEFAULTS, enable=False, max_edge=0, target_kb=512)
        status, payload = save(env, {"reference_fallback": {"enable": True}})
        assert status == 200, payload
        assert env[1][COMPRESS_GROUP]["enable"] is False
        assert env[1][COMPRESS_GROUP]["max_edge"] == 0
        assert env[1][COMPRESS_GROUP]["target_kb"] == 512
        assert env[1]["reference_fallback_config"]["enable"] is True
        assert payload[COMPRESS_PAYLOAD]["target_kb"] == 512

    def test_both_groups_save_in_one_atomic_request(self, env):
        status, payload = save(
            env,
            {
                COMPRESS_PAYLOAD: {"enable": False, "max_edge": 0},
                "reference_fallback": {"enable": True, "max_retries_per_node": 3},
            },
        )
        assert status == 200, payload
        assert env[1][COMPRESS_GROUP]["max_edge"] == 0
        assert env[1]["reference_fallback_config"]["max_retries_per_node"] == 3
        assert env[1].saved == 1, "两组必须落在同一次 save_config()"

    def test_one_bad_group_rolls_back_the_other(self, env):
        before = copy.deepcopy(dict(env[1]))
        status, payload = save(
            env,
            {
                COMPRESS_PAYLOAD: {"max_edge": 1024},
                "reference_fallback": ["not-an-object"],
            },
        )
        assert status == 400
        assert payload["error"]["field"] == "reference_fallback"
        assert dict(env[1]) == before, "整单拒绝时压缩组不得被写入"

    def test_disk_round_trip_keeps_both_groups_and_secrets(self, env_disk):
        status, payload = save(
            env_disk,
            {COMPRESS_PAYLOAD: {"enable": False, "max_edge": 0, "target_kb": 64}},
        )
        assert status == 200, payload
        stored = read_disk(env_disk)
        assert stored[COMPRESS_GROUP]["enable"] is False
        assert stored[COMPRESS_GROUP]["max_edge"] == 0
        assert stored[COMPRESS_GROUP]["target_kb"] == 64
        assert stored["providers"][0]["api_keys"] == ["sk-main", "sk-backup"]
        assert stored["reference_fallback_config"]["max_retries_per_node"] == 2

    def test_write_failure_rolls_back_memory(self, env_disk):
        before = copy.deepcopy(dict(env_disk[1]))
        env_disk[1].fail = True
        status, payload = save(env_disk, {COMPRESS_PAYLOAD: {"enable": False}})
        assert status == 500
        assert payload["error"]["code"] == "config_write_failed"
        assert dict(env_disk[1]) == before
        # 写盘在抛错前中止：磁盘上要么没有文件，要么没有新组。
        if env_disk[2].exists():
            assert COMPRESS_GROUP not in read_disk(env_disk)


# ---------------------------------------------------------------- 前端（真实 app.js）
class TestCompressWebui:
    def test_v141_js_suite_runs_against_real_app_js(self):
        if not JS_TEST.exists():
            pytest.fail("缺少 test/test_webui_v141.js")
        result = subprocess.run(
            ["node", str(JS_TEST)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "FAIL" not in result.stdout
        assert result.stdout.count("PASS") >= 10

    def test_app_js_has_no_second_field_table(self):
        source = (REPO_ROOT / "pages" / "webui" / "app.js").read_text(encoding="utf-8")
        # enable 是通用控制词（默认关闭 pill / 开关联动都会用到），其余 6 个
        # 压缩专属键名与组名一个都不许出现在前端。
        for key in [item for item in COMPRESS_KEYS if item != "enable"]:
            assert f"'{key}'" not in source and f'"{key}"' not in source, key
        assert COMPRESS_GROUP not in source
        assert FALLBACK_GROUP not in source

    def test_group_container_exists_and_no_inline_script(self):
        html = (REPO_ROOT / "pages" / "webui" / "index.html").read_text(encoding="utf-8")
        assert 'id="config-groups"' in html
        assert 'id="provider-warnings"' in html
        assert "<script>" not in html
