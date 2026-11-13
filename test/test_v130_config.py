"""v1.3.0：新配置组的默认值/钳制、节点新键与旧配置兼容。"""

import unittest

from imago_fusion.core.config import load_config, matches_template


def _node(**overrides):
    node = {
        "id": "node", "api_type": "openai_image",
        "base_url": "https://example.invalid/v1", "api_keys": ["key_demo"],
    }
    node.update(overrides)
    return node


class NewGroupDefaultsTests(unittest.TestCase):
    def test_missing_groups_are_off_with_dictionary_defaults(self):
        cfg = load_config({"providers": [_node()]})
        fb = cfg.reference_fallback
        self.assertFalse(fb.enable)
        self.assertEqual(
            (fb.max_extra_attempts, fb.caption_max_chars, fb.vision_timeout_seconds, fb.notify_user),
            (1, 200, 20, True),
        )
        exp = cfg.experimental_reference
        self.assertFalse(exp.enable)
        self.assertEqual(exp.occluder_provider_id, "")
        self.assertIn("滑稽表情", exp.occlusion_prompt)
        self.assertEqual(
            (exp.max_images, exp.timeout_seconds, exp.caption_max_chars), (1, 20, 200),
        )

    def test_groups_are_clamped(self):
        cfg = load_config({
            "providers": [_node()],
            "reference_fallback_config": {
                "enable": True, "max_extra_attempts": 9, "caption_max_chars": 10,
                "vision_timeout_seconds": 999, "notify_user": False,
            },
            "experimental_reference_config": {
                "enable": True, "max_images": 99, "timeout_seconds": 1,
                "caption_max_chars": 9999,
            },
        })
        self.assertEqual(cfg.reference_fallback.max_extra_attempts, 1)
        self.assertEqual(cfg.reference_fallback.caption_max_chars, 80)
        self.assertEqual(cfg.reference_fallback.vision_timeout_seconds, 60)
        self.assertFalse(cfg.reference_fallback.notify_user)
        self.assertEqual(cfg.experimental_reference.max_images, 3)
        self.assertEqual(cfg.experimental_reference.timeout_seconds, 5)
        self.assertEqual(cfg.experimental_reference.caption_max_chars, 400)

    def test_occlusion_prompt_is_truncated_to_500(self):
        cfg = load_config({
            "providers": [_node()],
            "experimental_reference_config": {"occlusion_prompt": "x" * 900},
        })
        self.assertEqual(len(cfg.experimental_reference.occlusion_prompt), 500)


class ProviderNewKeyTests(unittest.TestCase):
    def test_enabled_thinking_and_custom_params_are_loaded(self):
        cfg = load_config({
            "providers": [_node(
                thinking="medium", custom_params="quality=low\nsize=3:4", enabled=False,
            )],
        })
        provider = cfg.providers[0]
        self.assertFalse(provider.enabled)
        self.assertEqual(provider.thinking, "medium")
        self.assertEqual(provider.custom_params, "quality=low\nsize=3:4")

    def test_invalid_thinking_falls_back_to_empty(self):
        cfg = load_config({"providers": [_node(thinking="turbo")]})
        self.assertEqual(cfg.providers[0].thinking, "")

    def test_disabled_native_still_needs_provider_id(self):
        cfg = load_config({"providers": [_node(
            api_type="astrbot_native", base_url="", api_keys=[], enabled=False,
        )]})
        self.assertEqual(cfg.providers, ())
        kept = load_config({"providers": [_node(
            api_type="astrbot_native", base_url="", api_keys=[],
            astrbot_provider_id="p", enabled=False,
        )]})
        self.assertFalse(kept.providers[0].enabled)

    def test_xai_type_is_accepted_and_old_types_are_kept(self):
        types = ["openai_image", "openai_chat", "gemini_official",
                 "dashscope_multimodal", "custom_endpoint", "xai_image"]
        nodes = [_node(id=f"n{index}", api_type=api_type) for index, api_type in enumerate(types)]
        cfg = load_config({"providers": nodes})
        self.assertEqual([item.api_type for item in cfg.providers], types)

    def test_incomplete_non_native_node_is_dropped(self):
        cfg = load_config({"providers": [
            _node(id="no_base", base_url=""), _node(id="no_key", api_keys=[]),
        ]})
        self.assertEqual(cfg.providers, ())

    def test_old_ten_group_config_loads_without_new_groups(self):
        cfg = load_config({
            "providers": [_node()],
            "enhance_config": {"enable": True},
            "task_config": {"inject_reference_note": False},
        })
        self.assertTrue(cfg.enhance.enabled)
        self.assertFalse(cfg.inject_reference_note)
        self.assertFalse(cfg.reference_fallback.enable)


class TemplateMatchTests(unittest.TestCase):
    def test_disabled_template_node_is_ignored(self):
        cfg = load_config({"providers": [
            _node(id="tpl_node", api_type="openai_image", enabled=False),
            _node(id="other", api_type="openai_image"),
        ]})
        self.assertFalse(matches_template(cfg.providers, "tpl_node/model", enabled=True))
        self.assertTrue(matches_template(cfg.providers, "other/model", enabled=True))


if __name__ == "__main__":
    unittest.main()
