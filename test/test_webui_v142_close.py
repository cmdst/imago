"""IMAGO·FUSION v1.4.2 定向收尾回归（图库归属窗口）。

本文件只做一件事：在门禁里以子进程运行 ``test/test_webui_v142_close.js``。
真正的用例与夹具都在那份 Node 脚本里——它用 ``vm`` 载入真实的
``pages/webui/app.js``，用可控预览夹具摆出"详情已换、旧图卡仍在 DOM"的窗口，
并断言窗口内的删除/选用/重建都不会写到新人格名下。

修前红证：把 ``IMAGO_WEBUI_APP`` 指向冻结基线（``git show f63787c:pages/webui/app.js``）
再跑同一个脚本，应看到 4 条失败且 POST 体是``persona_id=B`` 配 ``A 的文件名``。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
JS_TEST = REPO_ROOT / "test" / "test_webui_v142_close.js"
APP_JS = REPO_ROOT / "pages" / "webui" / "app.js"


def _node_env(**extra: str) -> dict:
    env = dict(os.environ)
    env.pop("IMAGO_WEBUI_APP", None)
    env.update(extra)
    return env


def test_gallery_owner_window_regression():
    assert JS_TEST.exists(), "缺少 test/test_webui_v142_close.js"
    result = subprocess.run(
        ["node", str(JS_TEST)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
        env=_node_env(),
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "FAIL" not in output, output
    # 10 条：2 条窗口反例 × 3（删除/选用/重建）+ 3 条就绪反控 + detail-pending +
    # A→B→A + 上传绑定 + 静态守卫。
    assert output.count("PASS") >= 10, output


def test_app_js_still_parses():
    result = subprocess.run(
        ["node", "--check", str(APP_JS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not JS_TEST.exists(), reason="缺少 Node 回归脚本")
def test_red_control_against_pre_fix_baseline(tmp_path):
    """反控自检：把基线 app.js 塞回去，同一套用例必须红（否则测不到东西）。"""

    baseline = subprocess.run(
        ["git", "show", "f63787c:pages/webui/app.js"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    if baseline.returncode != 0 or not baseline.stdout.strip():
        pytest.skip("取不到冻结基线的 pages/webui/app.js（浅克隆/无 git）")
    candidate = tmp_path / "app-baseline.js"
    candidate.write_text(baseline.stdout, encoding="utf-8")
    result = subprocess.run(
        ["node", str(JS_TEST)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
        env=_node_env(IMAGO_WEBUI_APP=str(candidate)),
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert output.count("FAIL") >= 4, output
    # 修前的真实载荷：目标已经是 B，内容却还是 A 的。
    assert "'persona-b'" in output, output
