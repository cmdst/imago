"""P0 守卫：provider 错误钩子必须接受运行时容器的 **2 参** 调用契约。

事故背景（工作区手册 `AGENTS.md` §4，CHANGELOG 1.0.0「已知问题」→ 1.1.0「修复」）：
`main.py` 把 **3 参** 的 `_provider_error(provider, exc, task)` 绑给
`core.runtime.configure(error_reporter=...)`，而 `core.runtime.report_provider_error()`
按 **2 参**调用注入的 reporter。于是每一次原生节点失败，都在「上报失败」这一行抛

    TypeError: ImagoFusion._provider_error() missing 1 required positional argument: 'task'

真实失败原因（超时 / 4xx-5xx / 无输出）被这层 `TypeError` 覆盖，`attempt_errors`
从未写入（终态日志只剩 `errors=TypeError attempts=-`），多节点 fallback 被整段跳过。

两条守卫互相独立（任一处回退都会红）：

1. `test_provider_error_task_argument_is_optional`：第三个参数 `task` 必须有默认值
   （签名层）。前两个参数的名字也锁住，避免日后「换个参数名再给它默认值」绕过契约。
2. `test_two_argument_report_does_not_raise`：按 `initialize()` 的方式把**真实的**
   `ImagoFusion._provider_error` 绑定成 reporter，用**真实的**
   `runtime.report_provider_error()` 以 2 参驱动一次，必须落到日志而不是抛异常；
   同一测试顺带覆盖调度器那种带 `task` 的 3 参调用（两条调用形状都要能用）。

两个测试都直接 import 真实的 `imago_fusion.main`：只有这样才能证明「绑上去的方法」
本身可被 2 参调用，而不是证明一个本地替身。
"""

import inspect
import sys
import types
import unittest

from imago_fusion.core import runtime
from imago_fusion.core.errors import ProviderError
from imago_fusion.core.models import ProviderConfig

PROVIDER = ProviderConfig("node_a", "astrbot_native", "", (), model="model-x")


def _ensure_real_astrbot() -> None:
    """在 import ``imago_fusion.main`` 之前保证 ``astrbot.api.logger`` 是真的。

    ``test/test_native_adapter.py`` 在自己的用例里把 ``sys.modules["astrbot.api"]``
    换成一个只有 ``message_components`` 的桩；pytest 按文件名顺序跑，轮到本文件时
    那个桩还挂着，直接 import ``main`` 会 `ImportError: cannot import name 'logger'`。
    这里把桩过的 `astrbot*` 摘掉，让 PYTHONPATH 上的真包重新导入。
    """
    import astrbot.api  # noqa: F401  —— 可能拿到桩，下面判定

    if getattr(sys.modules["astrbot.api"], "logger", None) is not None:
        return
    for name in [key for key in sys.modules if key == "astrbot" or key.startswith("astrbot.")]:
        del sys.modules[name]
    import astrbot.api as real_api

    if getattr(real_api, "logger", None) is None:  # pragma: no cover - 环境损坏
        raise AssertionError("astrbot.api.logger 不可用：无法验证 P0 钩子契约")


def _plugin_main():
    """导入插件的真实模块对象（`main.py`）。"""
    _ensure_real_astrbot()
    import imago_fusion.main as plugin_main

    return plugin_main


class _PluginStandIn:
    """`_provider_error` 只碰 `self._log_prefix(...)`，其余状态与本守卫无关。"""

    raw_config: dict = {}

    def _log_prefix(self, event=None) -> str:
        return "[ImagoFusion]"


class _RecordingLogger:
    """替换 `imago_fusion.main.logger`，记录 `warning` 行（不写真实日志文件）。"""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, tuple]] = []

    def warning(self, message, *args) -> None:
        self.warnings.append((message, args))

    def debug(self, message, *args) -> None:  # pragma: no cover - 未使用
        pass

    def info(self, message, *args) -> None:  # pragma: no cover - 未使用
        pass

    def error(self, message, *args) -> None:  # pragma: no cover - 未使用
        pass


class ProviderErrorHookTests(unittest.TestCase):
    """P0：`_provider_error` 的 `task` 可缺省，2 参上报不抛。"""

    def tearDown(self) -> None:
        runtime.reset()

    def test_provider_error_task_argument_is_optional(self):
        main = _plugin_main()
        parameters = list(inspect.signature(main.ImagoFusion._provider_error).parameters.values())
        self.assertEqual(
            [parameter.name for parameter in parameters],
            ["self", "provider", "exc", "task"],
            "参数名/顺序变了：运行时容器按位置传 (provider, exc)",
        )
        task_parameter = parameters[3]
        self.assertIsNot(
            task_parameter.default,
            inspect.Parameter.empty,
            "task 必须是可缺省参数，否则 runtime.report_provider_error() 的 2 参调用会抛 TypeError",
        )
        self.assertIsNone(task_parameter.default, "task 的缺省值必须是 None")

    def test_two_argument_report_does_not_raise(self):
        main = _plugin_main()
        recorder = _RecordingLogger()
        original_logger = main.logger
        main.logger = recorder
        try:
            # 与 main.ImagoFusion.initialize() 完全同形：把（未绑定的）真实方法绑到
            # 插件实例上，再交给运行时容器；容器只按 2 参回调。
            runtime.configure(
                error_reporter=types.MethodType(main.ImagoFusion._provider_error, _PluginStandIn())
            )
            try:
                runtime.report_provider_error(PROVIDER, ProviderError("timeout"))
            except TypeError as exc:  # pragma: no cover - 未打补丁时走这里（必红）
                self.fail(f"2 参上报仍然抛 TypeError（P0 回归）：{exc}")
            self.assertEqual(len(recorder.warnings), 1, "2 参上报必须打出恰好一条『模型失败』行")
            message, args = recorder.warnings[0]
            self.assertIn("模型失败", message)
            # 行参形状：(前缀, 异常类型名, 节点 id, 模型, status, detail)
            self.assertEqual(args[0], "[ImagoFusion]", "缺省 task 时前缀不能带 platform 段")
            self.assertEqual(args[1], "ProviderError")
            self.assertEqual(args[2], PROVIDER.id)

            # 调度器路径：带 task 的 3 参调用仍然可用（缺省值不改变既有行为）。
            task = types.SimpleNamespace(runtime={"source_event": None})
            main.ImagoFusion._provider_error(_PluginStandIn(), PROVIDER, ProviderError("boom"), task)
            self.assertEqual(len(recorder.warnings), 2)
        finally:
            main.logger = original_logger


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
