"""v1.3.0：节点参数目录（catalog）、custom_params 解析与思考控制映射。"""

import unittest

from imago_fusion.core import params
from imago_fusion.core.config import API_TYPES
from imago_fusion.providers import ADAPTERS


class CatalogTests(unittest.TestCase):
    def test_seven_api_types_share_one_order(self):
        self.assertEqual(len(API_TYPES), 7)
        self.assertEqual(set(params.API_TYPE_ORDER), set(API_TYPES))
        self.assertEqual(set(ADAPTERS), set(params.API_TYPE_ORDER))
        self.assertEqual(API_TYPES, set(params.API_TYPE_ORDER))
        # 六个旧类型顺序不变，xai_image 追加在末尾（契约 C1）。
        self.assertEqual(
            params.API_TYPE_ORDER,
            (
                "openai_image", "openai_chat", "gemini_official",
                "dashscope_multimodal", "custom_endpoint", "astrbot_native",
                "xai_image",
            ),
        )

    def test_catalog_shape_matches_dictionary(self):
        catalog = params.build_catalog()
        self.assertEqual(catalog["version"], 1)
        values = [item["value"] for item in catalog["api_types"]]
        self.assertEqual(values, list(params.API_TYPE_ORDER))
        for item in catalog["api_types"]:
            self.assertEqual(item["fields"], params.PARAM_FIELDS[item["value"]])
            self.assertIn(item["param_mode"], {"typed", "free", "ignored"})

    def test_capabilities_use_enum_references(self):
        expected = {
            "openai_image": {"quality": "native", "thinking": "complexity", "references": "edits"},
            "openai_chat": {"quality": "none", "thinking": "reasoning_effort", "references": "inline"},
            "gemini_official": {"quality": "undocumented", "thinking": "thinking_level", "references": "inline"},
            "dashscope_multimodal": {"quality": "none", "thinking": "none", "references": "array"},
            "custom_endpoint": {"quality": "none", "thinking": "none", "references": "array"},
            "astrbot_native": {"quality": "none", "thinking": "none", "references": "inline"},
            "xai_image": {"quality": "undocumented", "thinking": "none", "references": "edits"},
        }
        for api_type, capabilities in expected.items():
            self.assertEqual(params.capabilities(api_type), capabilities)
        self.assertEqual(params.capabilities("unknown_type")["references"], "none")

    def test_field_limits_and_options(self):
        fields = {field["key"]: field for field in params.PARAM_FIELDS["openai_image"]}
        self.assertIsNone(fields["size"]["max_length"])
        self.assertEqual(
            fields["quality"]["options"], ["", "auto", "low", "medium", "high", "xhigh", "max"],
        )
        gemini = {field["key"]: field for field in params.PARAM_FIELDS["gemini_official"]}
        self.assertEqual(gemini["image_size"]["options"], ["", "512", "1K", "2K", "4K"])
        dashscope = {field["key"]: field for field in params.PARAM_FIELDS["dashscope_multimodal"]}
        self.assertEqual(dashscope["negative_prompt"]["max_length"], 500)
        xai = {field["key"]: field for field in params.PARAM_FIELDS["xai_image"]}
        self.assertEqual(xai["resolution"]["options"], ["", "1k", "1.5k", "2k"])
        self.assertEqual(xai["edit_mode"]["default"], "json")


class CustomParamsTests(unittest.TestCase):
    def test_separators_trim_and_values(self):
        values, errors = params.parse_custom_params(
            "size = 1024x1024\nquality: medium", "openai_image",
        )
        self.assertEqual(values, {"size": "1024x1024", "quality": "medium"})
        self.assertEqual(errors, [])

    def test_comments_blank_lines_and_duplicates(self):
        values, errors = params.parse_custom_params(
            "\n# 注释\nsize=1:1\nsize=3:4\n", "openai_image",
        )
        self.assertEqual(values, {"size": "1:1"})
        self.assertEqual([item["reason"] for item in errors], ["duplicate_key"])

    def test_reserved_and_illegal_keys_are_skipped(self):
        values, errors = params.parse_custom_params(
            "thinking=high\n9bad=1\nno_separator\nsize=2:3\nimage_urls=x", "openai_image",
        )
        self.assertEqual(values, {"size": "2:3"})
        reasons = {item["key"]: item["reason"] for item in errors}
        self.assertEqual(reasons["thinking"], "reserved_key")
        self.assertIn("9bad", reasons)
        self.assertEqual(reasons["image_urls"], "reserved_key")

    def test_bool_and_int_coercion(self):
        values, errors = params.parse_custom_params(
            "prompt_extend=false\nwatermark=yes\nseed=7", "dashscope_multimodal",
        )
        self.assertEqual(values, {"prompt_extend": False, "watermark": True, "seed": 7})
        self.assertEqual(errors, [])

    def test_int_range_and_undeclared_quality(self):
        values, errors = params.parse_custom_params(
            "seed=2147483648\nquality=ultra", "dashscope_multimodal",
        )
        # 百炼未声明 quality：按字典约定原样透传（unknown_for_type），不硬拒。
        self.assertEqual(values, {"quality": "ultra"})
        reasons = {item["key"]: item["reason"] for item in errors}
        self.assertEqual(reasons, {"seed": "invalid_value", "quality": "unknown_for_type"})

    def test_unknown_keys_pass_through(self):
        values, errors = params.parse_custom_params("providerOptions={\"a\":1}", "custom_endpoint")
        self.assertEqual(values, {"providerOptions": "{\"a\":1}"})
        self.assertEqual(errors, [])

    def test_effective_params_precedence(self):
        merged = params.effective_params(
            {"size": "1:1", "quality": "low"}, {"size": "16:9"}, reserved={"n"},
        )
        self.assertEqual(merged, {"size": "16:9", "quality": "low"})
        dropped = params.effective_params({"quality": ""}, None)
        self.assertEqual(dropped, {})


class ThinkingTests(unittest.TestCase):
    def test_empty_sends_nothing(self):
        self.assertEqual(params.resolve_thinking("openai_image", "m", "").carrier, "")

    def test_off_maps_per_openai_protocol(self):
        images = params.resolve_thinking("openai_image", "m", "off")
        self.assertEqual((images.carrier, images.value), ("complexity", "low"))
        chat = params.resolve_thinking("openai_chat", "m", "off")
        self.assertEqual((chat.carrier, chat.value), ("reasoning_effort", "none"))

    def test_gemini_off_warns_and_sends_nothing(self):
        decision = params.resolve_thinking("gemini_official", "gemini-3.1-flash-lite-image", "off")
        self.assertEqual(decision.carrier, "")
        self.assertIn("gemini_thinking_cannot_disable", decision.warnings)

    def test_gemini_only_declared_models_send(self):
        lite = params.resolve_thinking("gemini_official", "gemini-3.1-flash-lite-image", "medium")
        self.assertEqual((lite.carrier, lite.value), ("thinking_level", "HIGH"))
        pro = params.resolve_thinking("gemini_official", "gemini-3-pro-image", "high")
        self.assertEqual(pro.carrier, "")
        self.assertTrue(any("model_capability_undeclared" in line for line in pro.debug))

    def test_gemini_low_level_maps_minimal(self):
        decision = params.resolve_thinking("gemini_official", "gemini-3.1-flash-lite-image", "low")
        self.assertEqual(decision.value, "MINIMAL")

    def test_max_maps_to_strongest_supported(self):
        images = params.resolve_thinking("openai_image", "m", "max")
        chat = params.resolve_thinking("openai_chat", "m", "max")
        self.assertEqual(images.value, "high")
        self.assertEqual(chat.value, "xhigh")

    def test_unsupported_protocols_never_send(self):
        for api_type in ("xai_image", "dashscope_multimodal", "astrbot_native", "custom_endpoint"):
            decision = params.resolve_thinking(api_type, "m", "high")
            self.assertEqual(decision.carrier, "", api_type)
            self.assertTrue(decision.debug, api_type)

    def test_invalid_value_is_ignored(self):
        decision = params.resolve_thinking("openai_image", "m", "turbo")
        self.assertEqual(decision.carrier, "")
        self.assertTrue(any("invalid_value" in line for line in decision.debug))

    def test_ratio_helpers(self):
        self.assertEqual(params.gemini_aspect_ratio("1024x1536"), "2:3")
        self.assertEqual(params.gemini_aspect_ratio("16:9"), "16:9")
        self.assertEqual(params.gemini_aspect_ratio("auto"), "")
        self.assertEqual(params.xai_aspect_ratio("1280x720"), "16:9")
        self.assertEqual(params.xai_aspect_ratio("2048x2048"), "1:1")
        self.assertEqual(params.xai_aspect_ratio("auto"), "auto")
        self.assertEqual(params.format_param(True), "true")


if __name__ == "__main__":
    unittest.main()
