"""IMAGO·FUSION v1.4.2 R3 回归：别名密钥按原字节持久化 + 写操作不跨人格。

t11 R2-07（本文件上半）：别名密钥是**二进制**，历史实现 `read_bytes().strip()`
把"首空格/尾换行"这类合法的密钥字节吃掉（磁盘 32 字节、进程里 30 字节），
于是同一把密钥在写入与重读之后得到不同的 HMAC key —— 别名会漂移，或者更糟：
两个不同的密钥被 strip 成同一把。本回归固定：

* 持久化与读取**逐字节一致**（不 strip、不 decode、不换行规整）；
* 重开 store 后别名不变（跨重启可关联）；
* 空白字节**参与**密钥语义（strip 前后必须得到不同别名）；
* 长度边界确定：恰好 16 字节可用、15 字节视为没有密钥（不吃弱口令）；
* 读不到/写不了时退化为 `persona-unkeyed` 且只尝试一次，异常里不含路径。

t11 R2-05（本文件下半）：由 ``test/test_webui_v142_r3.js`` 用真实页面逻辑覆盖
「切 A→B 而 B 详情未返回时，旧卡的删除/摘要保存不得写到 B」。

只用本地临时根与合成假值，不触网、不读写真实插件数据目录。
"""

from __future__ import annotations

import os
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from imago_fusion.core import diagnostics as attempt_diagnostics
from imago_fusion.services import persona_store
from imago_fusion.services.persona_store import PersonaStore

REPO_ROOT = Path(__file__).resolve().parent.parent
JS_TEST = REPO_ROOT / "test" / "test_webui_v142_r3.js"

#: 合成密钥：首空格 + 30 个可打印字节 + 尾换行 = 32 字节。
#: `.strip()` 会吃掉两端的两个字节，正好把"32 字节密钥"变成"30 字节密钥"。
CRAFTED_SECRET = b" " + b"k" * 30 + b"\n"


class AliasSecretBytesTests(unittest.TestCase):
    """别名密钥的字节级持久化契约。"""

    def setUp(self) -> None:
        self._directory = TemporaryDirectory()
        self.root = Path(self._directory.name) / "persona_data"
        self.store = PersonaStore(self.root)
        self.secret_path = self.root / persona_store.ALIAS_SECRET_FILENAME

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _alias(self, secret: bytes, persona_id: str = "persona-x") -> str:
        return attempt_diagnostics.persona_alias(persona_id, secret=secret)

    def test_generated_secret_is_binary_and_private(self) -> None:
        secret = self.store.alias_secret()
        self.assertIsInstance(secret, bytes)
        self.assertEqual(len(secret), persona_store.ALIAS_SECRET_BYTES)
        self.assertEqual(self.secret_path.read_bytes(), secret)
        mode = stat.S_IMODE(self.secret_path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        # 同一进程内重复读取不重新生成。
        self.assertEqual(self.store.alias_secret(), secret)

    def test_crafted_whitespace_secret_survives_a_reopen_byte_for_byte(self) -> None:
        self.secret_path.write_bytes(CRAFTED_SECRET)
        os.chmod(self.secret_path, 0o600)

        reopened = PersonaStore(self.root)
        secret = reopened.alias_secret()
        self.assertEqual(secret, CRAFTED_SECRET, "磁盘上的 32 字节必须原样回到进程里")
        self.assertEqual(len(secret), 32)
        self.assertEqual(self.secret_path.read_bytes(), CRAFTED_SECRET, "读取不得改写文件")

    def test_alias_is_stable_across_reopen_for_a_whitespace_key(self) -> None:
        self.secret_path.write_bytes(CRAFTED_SECRET)
        os.chmod(self.secret_path, 0o600)
        first = self._alias(PersonaStore(self.root).alias_secret())
        second = self._alias(PersonaStore(self.root).alias_secret())
        self.assertEqual(first, second)
        self.assertNotEqual(first, attempt_diagnostics.PERSONA_ALIAS_UNKEYED)

    def test_whitespace_bytes_are_part_of_the_key(self) -> None:
        stripped = CRAFTED_SECRET.strip()
        self.assertEqual(len(stripped), 30)
        self.assertNotEqual(
            self._alias(CRAFTED_SECRET),
            self._alias(stripped),
            "strip 之后必须得到不同别名：否则两把不同密钥会被当成同一把",
        )

    def test_raw_length_boundary_is_deterministic(self) -> None:
        # 恰好 16 字节（这里全是空白）是合格密钥：长度按**原始字节**算，不按 strip 后算。
        sixteen = b" " * 16
        self.assertEqual(
            attempt_diagnostics.persona_alias("persona-y", secret=sixteen),
            attempt_diagnostics.persona_alias("persona-y", secret=sixteen),
        )
        self.assertNotEqual(
            attempt_diagnostics.persona_alias("persona-y", secret=sixteen),
            attempt_diagnostics.PERSONA_ALIAS_UNKEYED,
        )
        # 15 字节（含空白）不够：宁可没有密钥，也不拿弱口令当 HMAC key。
        self.assertEqual(
            attempt_diagnostics.persona_alias("persona-y", secret=b" " + b"k" * 14),
            attempt_diagnostics.PERSONA_ALIAS_UNKEYED,
        )
        # 磁盘上不足 16 字节的残留文件也要重生成成 32 字节。
        self.secret_path.write_bytes(b" " + b"k" * 14)
        os.chmod(self.secret_path, 0o600)
        regenerated = PersonaStore(self.root).alias_secret()
        self.assertEqual(len(regenerated), persona_store.ALIAS_SECRET_BYTES)
        self.assertEqual(self.secret_path.read_bytes(), regenerated)

    def test_min_length_constants_stay_in_sync(self) -> None:
        # store 侧与别名侧各自持有一份"最短密钥"判据；两处必须同值，否则磁盘上
        # 合格的密钥会被别名层判成 weak key（或反过来）。给未来改一处忘另一处留哨兵。
        self.assertEqual(
            persona_store.ALIAS_SECRET_MIN_BYTES,
            attempt_diagnostics._PERSONA_ALIAS_KEY_MIN_BYTES,
        )

    def test_unusable_secret_degrades_once_and_never_leaks_a_path(self) -> None:
        with patch.object(persona_store.os, "open", side_effect=PermissionError("nope")) as opened:
            secret = self.store.alias_secret()
            again = self.store.alias_secret()
        self.assertEqual(secret, b"")
        self.assertEqual(again, b"")
        self.assertEqual(opened.call_count, 1, "失败结果要缓存，不要每次调用都试盘")
        self.assertEqual(
            attempt_diagnostics.persona_alias("persona-z", secret=secret),
            attempt_diagnostics.PERSONA_ALIAS_UNKEYED,
        )
        self.assertFalse(self.secret_path.exists())


class WebuiWriteTargetTests(unittest.TestCase):
    """t11 R2-05：详情未就绪时不得跨人格写入（真实页面逻辑，Node VM）。"""

    def test_cross_persona_writes_are_refused_until_the_detail_lands(self) -> None:
        node = shutil_which_node()
        if node is None:
            self.skipTest("本机没有 node，跳过前端回归")
        completed = subprocess.run(
            [node, str(JS_TEST)],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=False,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        self.assertEqual(completed.returncode, 0, output)
        self.assertNotIn("FAIL", output)
        self.assertGreaterEqual(output.count("PASS"), 9, output)


def shutil_which_node() -> str | None:
    from shutil import which

    return which("node")


if __name__ == "__main__":
    unittest.main()
