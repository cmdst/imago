"""IMAGO·FUSION v1.3.0 Web API / WebUI 专属回归（t3）。

覆盖双界面契约的读写形状：catalog 单一来源、三态保存、密钥不回显、类型切换、
主节点停用语义、两个新配置组（含 t8 rev3 的拒绝后遮挡实验组）与原子性。
另以子进程运行 ``test/test_webui_v130.js``，验证真实 ``app.js`` 的渲染与载荷。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
from pathlib import Path

import pytest
from quart import Quart

from imago_fusion.integrations.web_api import PROVIDER_FIELDS, PageAPI

REPO_ROOT = Path(__file__).resolve().parent.parent
JS_TEST = REPO_ROOT / "test" / "test_webui_v130.js"


# ---------------------------------------------------------------- 夹具
class RawConfig(dict):
    """模拟 AstrBot 的配置对象：dict + save_config()，可注入写盘失败。"""

    def __init__(self, data):
        super().__init__(copy.deepcopy(data))
        self.saved = 0
        self.fail = False

    def save_config(self):
        if self.fail:
            raise RuntimeError("disk full")
        self.saved += 1


class Store:
    def __init__(self, primary="main"):
        self.primary = primary
        self.calls = []

    def get_primary_provider_id(self):
        return self.primary

    def set_primary_provider_id(self, value):
        self.primary = value
        self.calls.append(value)


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
    def __init__(self, raw, primary="main", providers=None):
        self.raw_config = raw
        self.store = Store(primary)
        self.context = Context(providers)

    async def list_persona_ids(self):
        return []


def base_config():
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
                "fallback_order": 5,
                "model": "gpt-image-1",
                "reference_image_limit": 3,
                "available_models": ["gpt-image-1-mini"],
                "default_size": "1024x1024",
                "timeout": 180,
                "thinking": "high",
                "custom_params": "quality=high\nfoo=bar",
                "extra_legacy": {"keep": True},
            },
            {
                "id": "native",
                "api_type": "astrbot_native",
                "enabled": False,
                "base_url": "",
                "api_keys": [],
                "astrbot_provider_id": "cpa/gemini",
                "stream": True,
                "enable_proxy": False,
                "fallback_order": 1,
                "model": "",
                "reference_image_limit": 0,
                "available_models": [],
                "default_size": "1024x1024",
                "timeout": 180,
                "thinking": "",
                "custom_params": "",
            },
        ],
        "optimizer_config": {"optimizer_provider_id": "", "vision_provider_id": "vision-1"},
        "reference_fallback_config": {
            "enable": False,
            "max_extra_attempts": 1,
            "caption_max_chars": 200,
            "vision_timeout_seconds": 20,
            "notify_user": True,
        },
        "experimental_reference_config": {
            "enable": False,
            "occluder_provider_id": "",
            "occlusion_prompt": "默认遮挡提示词。",
            "max_images": 1,
            "timeout_seconds": 20,
            "caption_max_chars": 200,
        },
    }


def build_app(raw, primary="main"):
    plugin = Plugin(raw, primary)
    api = PageAPI(plugin)
    app = Quart(__name__)
    app.add_url_rule("/imago_fusion/providers", "providers", api.providers, methods=["GET"])
    app.add_url_rule("/imago_fusion/providers/save", "save_providers", api.save_providers, methods=["POST"])
    return app, plugin


def request(app, method="GET", path="/imago_fusion/providers", body=None):
    async def run():
        async with app.test_client() as client:
            if method == "GET":
                response = await client.get(path)
            else:
                response = await client.post(path, json=body)
        return response.status_code, await response.get_json()

    return asyncio.run(run())


@pytest.fixture()
def env():
    raw = RawConfig(base_config())
    app, plugin = build_app(raw)
    return app, plugin, raw


def get_state(env):
    app, _plugin, _raw = env
    status, payload = request(app)
    assert status == 200
    return payload


def save(env, body):
    app, _plugin, _raw = env
    return request(app, "POST", "/imago_fusion/providers/save", body)


# ---------------------------------------------------------------- catalog / GET
class TestProvidersRead:
    def test_provider_fields_are_the_frozen_sixteen(self):
        assert len(PROVIDER_FIELDS) == 16
        assert set(PROVIDER_FIELDS) == {
            "enabled", "id", "api_type", "base_url", "api_keys", "astrbot_provider_id",
            "stream", "enable_proxy", "fallback_order", "model", "reference_image_limit",
            "available_models", "default_size", "timeout", "thinking", "custom_params",
        }

    def test_catalog_has_seven_types_and_openai_image_thinking_is_complexity(self, env):
        catalog = get_state(env)["catalog"]
        assert catalog["version"] == 1
        values = [entry["value"] for entry in catalog["api_types"]]
        assert values == [
            "openai_image", "openai_chat", "gemini_official", "dashscope_multimodal",
            "custom_endpoint", "astrbot_native", "xai_image",
        ]
        by_value = {entry["value"]: entry for entry in catalog["api_types"]}
        assert by_value["openai_image"]["capabilities"]["thinking"] == "complexity"
        assert by_value["openai_chat"]["capabilities"]["thinking"] == "reasoning_effort"
        assert by_value["gemini_official"]["capabilities"]["thinking"] == "thinking_level"
        for value in ("dashscope_multimodal", "custom_endpoint", "astrbot_native", "xai_image"):
            assert by_value[value]["capabilities"]["thinking"] == "none"
        expected = {
            "openai_image": ("native", "complexity", "edits"),
            "openai_chat": ("none", "reasoning_effort", "inline"),
            "gemini_official": ("undocumented", "thinking_level", "inline"),
            "dashscope_multimodal": ("none", "none", "array"),
            "custom_endpoint": ("none", "none", "array"),
            "astrbot_native": ("none", "none", "inline"),
            "xai_image": ("undocumented", "none", "edits"),
        }
        for value, (quality, thinking, references) in expected.items():
            caps = by_value[value]["capabilities"]
            assert (caps["quality"], caps["thinking"], caps["references"]) == (quality, thinking, references), value

    def test_catalog_quality_allows_unknown_for_type_passthrough(self, env):
        catalog = get_state(env)["catalog"]
        by_value = {entry["value"]: entry for entry in catalog["api_types"]}
        assert by_value["openai_image"]["capabilities"]["quality"] == "native"
        assert by_value["xai_image"]["capabilities"]["quality"] == "undocumented"
        assert "quality" not in [field["key"] for field in by_value["xai_image"]["fields"]]

    def test_catalog_groups_follow_rev3_and_do_not_freeze_old_experiment(self, env):
        groups = {group["key"]: group for group in get_state(env)["catalog"]["groups"]}
        assert set(groups) == {"reference_fallback_config", "experimental_reference_config"}
        experimental = groups["experimental_reference_config"]
        assert [field["key"] for field in experimental["fields"]] == [
            "enable", "occluder_provider_id", "occlusion_prompt",
            "max_images", "timeout_seconds", "caption_max_chars",
        ]
        serialized = json.dumps(experimental, ensure_ascii=False)
        for obsolete in ("occlude_mode", "occlude_strength", "max_edge"):
            assert obsolete not in serialized
        assert experimental["depends_on"]["payload"] == "reference_fallback"
        assert experimental["depends_on"]["field"] == "enable"

    def test_occluder_description_says_original_image_is_sent(self, env):
        groups = {group["key"]: group for group in get_state(env)["catalog"]["groups"]}
        description = groups["experimental_reference_config"]["description"]
        assert "会把参考原图发给" in description
        assert "费用另计" in description
        assert "不外传原图" not in description

    def test_get_never_echoes_api_keys(self, env):
        _status, text = request(env[0])
        body = json.dumps(text, ensure_ascii=False)
        assert "sk-main" not in body
        assert "sk-backup" not in body
        main = next(item for item in text["providers"] if item["id"] == "main")
        assert main["api_keys_configured"] is True
        assert "api_keys" not in main

    def test_node_carries_new_fields_and_parsed_params(self, env):
        main = next(item for item in get_state(env)["providers"] if item["id"] == "main")
        assert main["enabled"] is True
        assert main["thinking"] == "high"
        assert main["custom_params"] == "quality=high\nfoo=bar"
        assert main["params"]["quality"] == "high"
        assert main["params"]["foo"] == "bar"
        assert main["param_mode"] == "typed"
        assert main["capabilities"]["thinking"] == "complexity"
        assert any(error["reason"] == "unknown_for_type" for error in main["param_errors"])

    def test_stored_primary_wins_even_with_worse_fallback_order(self, env):
        state = get_state(env)
        assert state["stored_primary_provider_id"] == "main"
        assert state["primary_provider_id"] == "main"
        main = next(item for item in state["providers"] if item["id"] == "main")
        native = next(item for item in state["providers"] if item["id"] == "native")
        assert main["primary"] is True
        assert native["enabled"] is False

    def test_disabled_stored_primary_falls_back_and_flags(self, env):
        env[2]["providers"][0]["enabled"] = False
        state = get_state(env)
        assert state["stored_primary_provider_id"] == "main"
        main = next(item for item in state["providers"] if item["id"] == "main")
        assert main["primary"] is False
        assert main["primary_disabled"] is True
        # native 也停用 → 没有可用节点
        assert state["primary_provider_id"] == ""

    def test_all_disabled_returns_empty_primary_and_all_nodes(self, env):
        env[2]["providers"][0]["enabled"] = False
        state = get_state(env)
        assert state["primary_provider_id"] == ""
        assert len(state["providers"]) == 2

    def test_fallback_picks_first_enabled_complete_node(self, env):
        env[2]["providers"][0]["enabled"] = False
        env[2]["providers"][1]["enabled"] = True
        env[2]["providers"][1]["enabled"] = True
        state = get_state(env)
        assert state["primary_provider_id"] == "native"
        main = next(item for item in state["providers"] if item["id"] == "main")
        assert main["primary_disabled"] is True

    def test_groups_get_shape_and_dependency_flags(self, env):
        state = get_state(env)
        fallback = state["reference_fallback"]
        assert fallback["enable"] is False
        assert fallback["max_extra_attempts"] == 1
        assert fallback["vision_provider_id"] == "vision-1"
        assert fallback["vision_provider_ready"] is False  # 假 context 里没有该 chat provider
        experimental = state["experimental_reference"]
        for key in (
            "enable", "occluder_provider_id", "occlusion_prompt",
            "max_images", "timeout_seconds", "caption_max_chars",
        ):
            assert key in experimental
        assert experimental["dependency_ready"] is False
        assert experimental["occluder_ready"] is False

    def test_dependency_ready_requires_fallback_and_valid_occluder(self, env):
        env[2]["reference_fallback_config"]["enable"] = True
        env[2]["experimental_reference_config"]["enable"] = True
        env[2]["experimental_reference_config"]["occluder_provider_id"] = "main"
        state = get_state(env)
        assert state["experimental_reference"]["occluder_ready"] is True
        assert state["experimental_reference"]["dependency_ready"] is True

    def test_success_shape_duplicates_data_for_bridge_compat(self, env):
        state = get_state(env)
        assert state["ok"] is True and state["success"] is True
        assert state["data"]["providers"] == state["providers"]


# ---------------------------------------------------------------- POST 三态
class TestProvidersSave:
    def test_absent_fields_keep_stored_values(self, env):
        status, payload = save(env, {"providers": [{"id": "main", "api_type": "openai_image"}]})
        assert status == 200, payload
        stored = env[2]["providers"][0]
        assert stored["model"] == "gpt-image-1"
        assert stored["thinking"] == "high"
        assert stored["custom_params"] == "quality=high\nfoo=bar"
        assert stored["api_keys"] == ["sk-main", "sk-backup"]
        assert stored["timeout"] == 180
        assert stored["fallback_order"] == 5
        assert stored["enabled"] is True

    def test_explicit_empty_values_are_written(self, env):
        status, payload = save(env, {"providers": [{
            "id": "main", "api_type": "openai_image",
            "enabled": False, "thinking": "", "custom_params": "",
            "model": "", "fallback_order": 0, "default_size": "",
        }]})
        assert status == 200, payload
        stored = env[2]["providers"][0]
        assert stored["enabled"] is False
        assert stored["thinking"] == ""
        assert stored["custom_params"] == ""
        assert stored["model"] == ""
        assert stored["fallback_order"] == 0
        assert stored["default_size"] == ""

    def test_invalid_field_only_warns_and_keeps_old_value(self, env):
        status, payload = save(env, {"providers": [{
            "id": "main", "api_type": "openai_image",
            "timeout": "abc", "reference_image_limit": -9,
            "thinking": "ultra", "enabled": "maybe",
        }]})
        assert status == 200, payload
        stored = env[2]["providers"][0]
        assert stored["timeout"] == 180
        assert stored["reference_image_limit"] == 0
        assert stored["thinking"] == "high"
        assert stored["enabled"] is True
        codes = [warning["code"] for warning in payload["warnings"]]
        assert codes.count("param_invalid_value") >= 3

    def test_clamped_integer_fields_follow_contract(self, env):
        status, _payload = save(env, {"providers": [{
            "id": "main", "api_type": "openai_image",
            "timeout": 3, "reference_image_limit": 12,
        }]})
        assert status == 200
        stored = env[2]["providers"][0]
        assert stored["timeout"] == 10
        assert stored["reference_image_limit"] == 12

    def test_api_keys_absent_or_empty_keeps_and_non_empty_replaces(self, env):
        status, _payload = save(env, {"providers": [{
            "id": "main", "api_type": "openai_image", "api_keys": [],
        }]})
        assert status == 200
        assert env[2]["providers"][0]["api_keys"] == ["sk-main", "sk-backup"]

        status, _payload = save(env, {"providers": [{
            "id": "main", "api_type": "openai_image", "api_keys": ["sk-new"],
        }]})
        assert status == 200
        assert env[2]["providers"][0]["api_keys"] == ["sk-new"]

        status, _payload = save(env, {"providers": [{
            "id": "main", "api_type": "openai_image", "clear_api_keys": True,
        }]})
        assert status == 200
        assert env[2]["providers"][0]["api_keys"] == []

    def test_type_switch_keeps_shared_fields_and_zeroes_native_only(self, env):
        status, payload = save(env, {"providers": [{
            "id": "main", "api_type": "xai_image",
            "enabled": True, "thinking": "max", "custom_params": "resolution=2k",
        }]})
        assert status == 200, payload
        stored = env[2]["providers"][0]
        assert stored["api_type"] == "xai_image"
        assert stored["thinking"] == "max"
        assert stored["custom_params"] == "resolution=2k"
        assert stored["base_url"] == "https://api.invalid/v1"
        assert stored["model"] == "gpt-image-1"
        assert stored["api_keys"] == ["sk-main", "sk-backup"]
        assert stored["astrbot_provider_id"] == ""
        assert stored["stream"] is False
        main = next(item for item in payload["providers"] if item["id"] == "main")
        assert main["param_mode"] == "typed"

    def test_native_requires_provider_id(self, env):
        status, payload = save(env, {"providers": [{
            "id": "native", "api_type": "astrbot_native", "astrbot_provider_id": "",
        }]})
        assert status == 400
        assert payload["error"]["code"] == "native_missing_provider"
        assert payload["error"]["field"] == "astrbot_provider_id"

    def test_duplicate_or_missing_id_rejects_whole_request_atomically(self, env):
        before = copy.deepcopy(dict(env[2]))
        status, payload = save(env, {"providers": [
            {"id": "main", "api_type": "openai_image"},
            {"id": "main", "api_type": "openai_chat"},
        ]})
        assert status == 400
        assert payload["error"]["code"] == "duplicate_id"
        assert dict(env[2]) == before
        assert env[2].saved == 0

        status, payload = save(env, {"providers": [{"id": "", "api_type": "openai_image"}]})
        assert status == 400
        assert payload["error"]["code"] == "missing_field"

    def test_unknown_api_type_rejects(self, env):
        status, payload = save(env, {"providers": [{"id": "main", "api_type": "midjourney"}]})
        assert status == 400
        assert payload["error"]["code"] == "invalid_api_type"

    def test_unknown_legacy_keys_survive_round_trip(self, env):
        status, _payload = save(env, {"providers": [{"id": "main", "api_type": "openai_image"}]})
        assert status == 200
        assert env[2]["providers"][0]["extra_legacy"] == {"keep": True}
        assert env[2]["providers"][0]["__template_key"] == "provider"

    def test_primary_may_point_to_disabled_node_and_template_prefix(self, env):
        status, payload = save(env, {
            "providers": [
                {"id": "main", "api_type": "openai_image", "enabled": False},
                {"id": "native", "api_type": "astrbot_native", "enabled": True,
                 "astrbot_provider_id": "cpa/gemini"},
            ],
            "primary_provider_id": "main",
        })
        assert status == 200, payload
        assert env[1].store.primary == "main"
        assert payload["primary_provider_id"] == "native"
        main = next(item for item in payload["providers"] if item["id"] == "main")
        assert main["primary_disabled"] is True

        status, payload = save(env, {"providers": [
            {"id": "native", "api_type": "astrbot_native", "enabled": True,
             "astrbot_provider_id": "cpa/gemini"},
        ], "primary_provider_id": "native/cpa"})
        assert status == 200, payload
        assert env[1].store.primary == "native/cpa"

    def test_write_failure_rolls_back_memory(self, env):
        env[2].fail = True
        before = copy.deepcopy(dict(env[2]))
        status, payload = save(env, {"providers": [{
            "id": "main", "api_type": "openai_image", "model": "changed",
        }]})
        assert status == 500
        assert payload["ok"] is False
        assert dict(env[2]) == before

    def test_param_unsupported_warning_for_typed_type(self, env):
        status, payload = save(env, {"providers": [{
            "id": "main", "api_type": "xai_image",
            "custom_params": "quality=high",
        }]})
        assert status == 200
        assert any(w["code"] == "param_unsupported_for_type" for w in payload["warnings"])
        assert env[2]["providers"][0]["custom_params"] == "quality=high"

    def test_thinking_ignored_warning_for_unsupported_protocol(self, env):
        status, payload = save(env, {"providers": [{
            "id": "main", "api_type": "xai_image", "thinking": "high",
        }]})
        assert status == 200
        assert any(w["code"] == "thinking_ignored" for w in payload["warnings"])


# ---------------------------------------------------------------- 配置组
class TestConfigGroups:
    def test_fallback_group_absent_is_kept(self, env):
        status, payload = save(env, {"providers": [{"id": "main", "api_type": "openai_image"}]})
        assert status == 200
        assert env[2]["reference_fallback_config"]["enable"] is False
        assert payload["reference_fallback"]["enable"] is False

    def test_fallback_group_writes_and_clamps(self, env):
        status, payload = save(env, {"reference_fallback": {
            "enable": True, "max_extra_attempts": 9, "caption_max_chars": 999,
            "vision_timeout_seconds": 1, "notify_user": False,
        }})
        assert status == 200, payload
        stored = env[2]["reference_fallback_config"]
        assert stored["enable"] is True
        assert stored["max_extra_attempts"] == 1
        assert stored["caption_max_chars"] == 400
        assert stored["vision_timeout_seconds"] == 5
        assert stored["notify_user"] is False
        assert payload["reference_fallback"]["enable"] is True

    def test_fallback_group_invalid_value_warns_and_keeps_old(self, env):
        status, payload = save(env, {"reference_fallback": {"max_extra_attempts": "many"}})
        assert status == 200
        assert env[2]["reference_fallback_config"]["max_extra_attempts"] == 1
        assert any(w["code"] == "param_invalid_value" for w in payload["warnings"])

    def test_experimental_group_round_trip_with_all_six_keys(self, env):
        env[2]["reference_fallback_config"]["enable"] = True
        status, payload = save(env, {"experimental_reference": {
            "enable": True,
            "occluder_provider_id": "main",
            "occlusion_prompt": "挡住脸",
            "max_images": 2,
            "timeout_seconds": 30,
            "caption_max_chars": 300,
        }})
        assert status == 200, payload
        stored = env[2]["experimental_reference_config"]
        assert stored["enable"] is True
        assert stored["occluder_provider_id"] == "main"
        assert stored["occlusion_prompt"] == "挡住脸"
        assert stored["max_images"] == 2
        assert stored["timeout_seconds"] == 30
        assert stored["caption_max_chars"] == 300
        assert payload["experimental_reference"]["dependency_ready"] is True

    def test_experimental_clamps_and_truncates_prompt(self, env):
        status, payload = save(env, {"experimental_reference": {
            "max_images": 99, "timeout_seconds": 2, "caption_max_chars": 1000,
            "occlusion_prompt": "长" * 600,
        }})
        assert status == 200, payload
        stored = env[2]["experimental_reference_config"]
        assert stored["max_images"] == 3
        assert stored["timeout_seconds"] == 5
        assert stored["caption_max_chars"] == 400
        assert len(stored["occlusion_prompt"]) == 500

    def test_occluder_must_be_enabled_and_complete(self, env):
        env[2]["reference_fallback_config"]["enable"] = True
        env[2]["experimental_reference_config"]["enable"] = True
        status, payload = save(env, {"experimental_reference": {
            "enable": True, "occluder_provider_id": "native",
        }})
        assert status == 200, payload
        # native 已停用 → 字段级拒绝，保留旧值（空）
        assert env[2]["experimental_reference_config"]["occluder_provider_id"] == ""
        assert any(w["code"] == "param_invalid_value" for w in payload["warnings"])

    def test_experimental_without_fallback_warns(self, env):
        status, payload = save(env, {"experimental_reference": {"enable": True}})
        assert status == 200
        assert any(w["code"] == "experimental_without_fallback" for w in payload["warnings"])
        assert payload["experimental_reference"]["dependency_ready"] is False

    def test_disabling_occluder_node_warns_on_next_save(self, env):
        env[2]["reference_fallback_config"]["enable"] = True
        env[2]["experimental_reference_config"]["enable"] = True
        env[2]["experimental_reference_config"]["occluder_provider_id"] = "main"
        status, payload = save(env, {"providers": [{
            "id": "main", "api_type": "openai_image", "enabled": False,
        }]})
        assert status == 200
        assert any(w["code"] == "occluder_unavailable" for w in payload["warnings"])

    def test_group_payload_shape_must_be_object(self, env):
        status, payload = save(env, {"reference_fallback": "on"})
        assert status == 400
        assert payload["error"]["code"] == "invalid_request"

    def test_providers_and_groups_save_in_one_atomic_request(self, env):
        status, payload = save(env, {
            "providers": [{"id": "main", "api_type": "openai_image", "enabled": True}],
            "reference_fallback": {"enable": True},
            "experimental_reference": {"enable": True, "occluder_provider_id": "main"},
        })
        assert status == 200, payload
        assert env[2]["reference_fallback_config"]["enable"] is True
        assert env[2]["experimental_reference_config"]["enable"] is True
        assert payload["experimental_reference"]["dependency_ready"] is True
        assert env[2].saved == 1


# ---------------------------------------------------------------- 前端静态契约
class TestWebuiAssets:
    def test_page_manifest_and_containers(self):
        webui = REPO_ROOT / "pages" / "webui"
        assert (webui / "_page.json").exists()
        html = (webui / "index.html").read_text(encoding="utf-8")
        for element_id in ("provider-list", "provider-summary", "provider-warnings", "config-groups"):
            assert f'id="{element_id}"' in html

    def test_app_js_syntax(self):
        result = subprocess.run(
            ["node", "--check", str(REPO_ROOT / "pages" / "webui" / "app.js")],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_app_js_has_no_field_table_copy(self):
        source = (REPO_ROOT / "pages" / "webui" / "app.js").read_text(encoding="utf-8")
        for api_type in ("openai_image", "openai_chat", "gemini_official", "custom_endpoint"):
            assert f"{api_type}:" not in source
        assert "PARAM_FIELDS" not in source

    def test_single_mobile_breakpoint(self):
        css = (REPO_ROOT / "pages" / "webui" / "style.css").read_text(encoding="utf-8")
        # 唯一移动断点仍是 760px；1400px 只是宽屏放宽，prefers-color-scheme 非布局断点。
        assert css.count("(max-width") == 1
        assert "(max-width:760px)" in css.replace(" ", "") or "(max-width: 760px)" in css
        assert css.count("(min-width") == 1

    def test_mobile_block_collapses_new_grids(self):
        css = (REPO_ROOT / "pages" / "webui" / "style.css").read_text(encoding="utf-8")
        start = css.index("@media(max-width:760px)")
        block = css[start:]
        assert ".group-fields{grid-template-columns:1fr}" in block.replace(" ", "")
        assert ".node-fields,.brain-selectors" in block.replace(" ", "") or ".node-fields" in block
        assert "overflow-x:auto" in css  # 宽表自身横滚，不撑破页面

    def test_node_regression_suite(self, tmp_path):
        if not JS_TEST.exists():
            pytest.fail("缺少 test/test_webui_v130.js")
        # 把真实 core/params.py 的 catalog 桥接给 Node，夹具偏差会直接红。
        from imago_fusion.core.params import build_catalog

        catalog_file = tmp_path / "catalog.json"
        catalog_file.write_text(json.dumps(build_catalog(), ensure_ascii=False), encoding="utf-8")
        env = dict(os.environ, IMAGO_WEBUI_CATALOG=str(catalog_file))
        result = subprocess.run(
            ["node", str(JS_TEST)], capture_output=True, text=True,
            cwd=str(REPO_ROOT), check=False, env=env,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "桥接" in result.stdout
        assert "全部" in result.stdout
