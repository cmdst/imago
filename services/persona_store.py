from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat as stat_module
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import DuplicateImage
from ..core.security import ensure_child, safe_component

ALLOWED_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}

#: 本机别名密钥：文件名、长度（字节）、权限。密钥只用于把 Persona 原始 ID
#: 投影成不可枚举的观测别名（见 ``core.diagnostics.persona_alias``）。
#: **不入库、不进配置/schema、不进日志、不进 task.json**；部署/重载只换代码，
#: 数据目录不动 ⇒ 同一部署内别名跨重启保持一致，换机器则自然重生成（旧别名
#: 不再可关联，这是有意的：没有密钥的别名本来就不该跨部署解码）。
ALIAS_SECRET_FILENAME = "persona_alias.key"
ALIAS_SECRET_BYTES = 32
ALIAS_SECRET_MODE = 0o600
#: 可用密钥的最短**原始字节**数（与 ``core.diagnostics._PERSONA_ALIAS_KEY_MIN_BYTES``
#: 同值；短于它就当没有密钥，绝不拿弱口令当 HMAC key）。长度按原始字节算，
#: 不按 ``strip()`` 之后的长度算 —— 空白字节也是密钥的一部分。
ALIAS_SECRET_MIN_BYTES = 16

#: 图库三态 + 故障态。值域与 ``core.diagnostics.PERSONA_LIBRARY_*`` 逐字相同；
#: 这里只留字符串常量，避免 ``services`` 反向依赖 ``core.diagnostics``。
REFERENCE_STATUS_OK = "ok"
REFERENCE_STATUS_EMPTY = "empty"
REFERENCE_STATUS_MISSING = "missing"
REFERENCE_STATUS_ERROR = "error"


class PersonaReferenceError(RuntimeError):
    """Persona 图库无法**安全**枚举或读取。

    与"空库"必须分开：空库是正常状态（继续只发显式图或只用 prompt），读取失败
    必须让任务安全失败（0 provider calls），不许静默伪装成"这个人格没有参考图"。
    异常文本只放稳定原因码，不含 Persona ID、文件名、目录或本地路径。
    """

    def __init__(self, reason: str, status: str = REFERENCE_STATUS_ERROR):
        self.reason = str(reason or "unknown")
        self.status = str(status or REFERENCE_STATUS_ERROR)
        super().__init__(f"Persona 参考图读取失败（reason={self.reason}）")


@dataclass(frozen=True, slots=True)
class ReferenceScan:
    """一次图库扫描的结果：状态位 + 可用条目 + 被跳过的条目数。"""

    status: str
    entries: tuple[dict, ...] = ()
    skipped: int = 0
    reason: str = ""

    @property
    def pool(self) -> int:
        #: 可读参考图张数（**不是**任何节点实际发出去的张数）。
        return len(self.entries)


class PersonaStore:
    def __init__(
        self,
        root: Path,
        max_upload_bytes: int | Callable[[], int] = 20 * 1024 * 1024,
    ):
        self.root = root
        self.summaries = root / "summaries.json"
        self.settings = root / "settings.json"
        self.references = root / "persona_references"
        self.task_cache = root / "task_cache"
        self._max_upload_bytes = max_upload_bytes
        self._alias_secret: bytes | None = None
        self.references.mkdir(parents=True, exist_ok=True)
        self.task_cache.mkdir(parents=True, exist_ok=True)

    @property
    def max_upload_bytes(self) -> int:
        value = self._max_upload_bytes() if callable(self._max_upload_bytes) else self._max_upload_bytes
        return max(1, int(value))

    @staticmethod
    def _task_id(task_id: str) -> str:
        value = str(task_id).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{16,64}", value):
            raise ValueError("任务 ID 不安全")
        return value

    def task_dir(self, task_id: str) -> Path:
        return ensure_child(self.task_cache, self.task_cache / self._task_id(task_id))

    def task_output_dir(self, task_id: str) -> Path:
        path = self.task_dir(task_id) / "outputs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def update_task_manifest(self, task_id: str, **fields) -> dict:
        directory = self.task_dir(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "task.json"
        try:
            value = json.loads(target.read_text("utf-8"))
            value = value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            value = {}
        value.setdefault("task_id", self._task_id(task_id))
        value.setdefault("created_at", int(time.time()))
        value.update(fields)
        value["updated_at"] = int(time.time())
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
        os.replace(temporary, target)
        return value

    def persist_task_inputs(self, task_id: str, images) -> list[dict]:
        directory = self.task_dir(task_id) / "inputs"
        directory.mkdir(parents=True, exist_ok=True)
        records = []
        suffixes = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
        for index, image in enumerate(images, start=1):
            digest = hashlib.sha256(image.data).hexdigest()
            suffix = suffixes.get(image.mime_type, ".bin")
            filename = f"{index:02d}_{digest[:16]}{suffix}"
            target = ensure_child(directory, directory / filename)
            target.write_bytes(image.data)
            records.append({"file": filename, "mime_type": image.mime_type, "size": len(image.data), "sha256": digest})
        self.update_task_manifest(task_id, inputs=records, input_count=len(records), input_bytes=sum(item["size"] for item in records))
        return records

    def record_task_outputs(self, task_id: str, paths: list[Path]) -> list[dict]:
        records = []
        for path in paths:
            if not path.is_file() or path.is_symlink():
                continue
            records.append({"file": path.name, "size": path.stat().st_size})
        self.update_task_manifest(task_id, outputs=records, output_count=len(records), output_bytes=sum(item["size"] for item in records))
        return records

    def prune_task_cache(self, max_bytes: int, protected: set[str] | None = None) -> None:
        limit = max(16 * 1024 * 1024, int(max_bytes))
        protected = {self._task_id(value) for value in (protected or set())}
        directories = [path for path in self.task_cache.iterdir() if path.is_dir() and path.name not in protected]
        total = sum(path.stat().st_size for path in self.task_cache.rglob("*") if path.is_file())
        if total <= limit:
            return
        for directory in sorted(directories, key=lambda path: path.stat().st_mtime):
            size = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())
            shutil.rmtree(directory, ignore_errors=True)
            total -= size
            if total <= limit:
                break

    @staticmethod
    def prompt_hash(prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    def _load_summaries(self) -> dict[str, dict]:
        try:
            value = json.loads(self.summaries.read_text("utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_summaries(self, value: dict[str, dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.summaries.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
            os.replace(temporary, self.summaries)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def get_primary_provider_id(self) -> str:
        try:
            value = json.loads(self.settings.read_text("utf-8"))
            return str(value.get("primary_provider_id", "")).strip() if isinstance(value, dict) else ""
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return ""

    def set_primary_provider_id(self, provider_id: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.settings.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps({"primary_provider_id": provider_id}, ensure_ascii=False, indent=2), "utf-8")
            os.replace(temporary, self.settings)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def get_summary(self, persona_id: str, prompt: str) -> dict | None:
        """取已保存的外观摘要。

        摘要只认用户显式操作（WebUI 重建/保存、`/imago_fusion summary-rebuild`）：
        Persona Prompt 变化或参考图增删都不会让它失效，避免「没动它就没了」。
        prompt 参数仅为兼容调用方签名（只在 set_summary 时记录 source_hash）。
        """
        return self._load_summaries().get(persona_id)

    def set_summary(self, persona_id: str, prompt: str, summary: str, *, manual: bool, reference_names=None) -> dict:
        summary = " ".join(summary.split())
        if not manual:
            summary = summary[:220]
        if not summary:
            raise ValueError("外观摘要不能为空")
        reference_names = [str(value) for value in (reference_names or [])]
        values = self._load_summaries()
        values[persona_id] = item = {
            "summary": summary,
            "source_hash": self.prompt_hash(prompt),
            "reference_hash": self.reference_fingerprint(persona_id, reference_names),
            "reference_names": reference_names,
            "uses_references": bool(reference_names),
            "updated_at": int(time.time()),
            "manual": bool(manual),
        }
        self._save_summaries(values)
        return item

    def reference_fingerprint(self, persona_id: str, names=None) -> str:
        names = sorted(str(value) for value in (names if names is not None else [item["name"] for item in self.list_references(persona_id)]))
        if names:
            for name in names:
                try:
                    self.reference_path(persona_id, name)
                except (ValueError, FileNotFoundError):
                    return "missing"
        return hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest() if names else ""

    def persona_dir(self, persona_id: str) -> Path:
        return self.references / safe_component(persona_id)

    def add_reference(self, persona_id: str, data: bytes, mime_type: str) -> dict[str, str | int]:
        if mime_type not in ALLOWED_MIME or not data or len(data) > self.max_upload_bytes:
            raise ValueError("图片格式或大小不符合要求")
        digest = hashlib.md5(data, usedforsecurity=False).hexdigest()
        directory = self.persona_dir(persona_id)
        directory.mkdir(parents=True, exist_ok=True)
        if any(path.name.startswith(digest + ".") for path in directory.iterdir()):
            raise DuplicateImage("该 Persona 已有相同内容的图片")
        target = directory / f"{digest}{ALLOWED_MIME[mime_type]}"
        target.write_bytes(data)
        return {"name": target.name, "md5": digest, "size": len(data), "mime_type": mime_type}

    def list_references(self, persona_id: str) -> list[dict[str, int | str]]:
        directory = self.persona_dir(persona_id)
        if not directory.exists():
            return []
        return [{"name": p.name, "size": p.stat().st_size} for p in sorted(directory.iterdir()) if p.is_file() and not p.is_symlink()]

    def scan_references(self, persona_id: str) -> ReferenceScan:
        """区分「目录不存在 / 库为空 / 枚举失败」，并给出可读条目。

        ``list_references`` 对"目录不存在"和"空目录"都返回 ``[]``（历史行为，保留
        给调用方），本方法补上状态位，供 ``_prepare`` 留痕与安全失败使用：缺目录与
        空库都是正常的"没有图"，枚举失败则是故障，不能伪装成前两者。
        符号链接与非普通文件不计入可读条目（与 ``reference_path`` 的拒绝口径一致），
        但计入 ``skipped``，避免"文件在那儿却发不出去"无声无息。
        """
        directory = self.persona_dir(persona_id)
        try:
            if not directory.exists():
                return ReferenceScan(REFERENCE_STATUS_MISSING)
            if not directory.is_dir():
                return ReferenceScan(REFERENCE_STATUS_ERROR, reason="not_a_directory")
            children = sorted(directory.iterdir())
        except OSError as error:
            return ReferenceScan(
                REFERENCE_STATUS_ERROR, reason=f"enum_failed:{type(error).__name__}"
            )
        entries: list[dict] = []
        skipped = 0
        for path in children:
            # ``Path.is_file()``/``is_symlink()`` 会把 OSError 吞成 ``False``：用它
            # 判"是不是普通文件"，一次 stat 被拒（EACCES/EIO/ESTALE）就会被读成
            # "这一条不算数"，整库于是**伪空**，任务照常请求 adapter（t11 R1-09）。
            # 这里改成显式探测：链接/非普通文件是**故意排除**（计入 skipped），
            # 真正的 lstat/stat 故障一律上抛成 ``error``，不再伪装成空库。
            try:
                info = path.lstat()
                if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
                    skipped += 1
                    continue
                size = int(path.stat().st_size)
            except OSError as error:
                return ReferenceScan(
                    REFERENCE_STATUS_ERROR,
                    tuple(entries),
                    skipped,
                    reason=f"stat_failed:{type(error).__name__}",
                )
            entries.append({"name": path.name, "size": size})
        status = REFERENCE_STATUS_OK if entries else REFERENCE_STATUS_EMPTY
        return ReferenceScan(status, tuple(entries), skipped)

    def alias_secret(self) -> bytes:
        """读取（首次则生成）本机别名密钥；任何 IO 问题都退化为"没有密钥"。

        持久化按**原始字节**：读回来的是什么字节，参与 HMAC 的就是什么字节
        （不 strip、不 decode、不做换行规整），因此同一把密钥跨重启得到同一别名。

        权限最小化：新文件建出来就是 ``0600``，再补一次 ``chmod`` 兜住 umask
        差异；文件内容只在内存里用，永不写日志、manifest 或异常消息。失败
        （只读盘、权限不对）时返回 ``b""``，调用方据此退化成
        ``persona-unkeyed`` —— 宁可不给可关联别名，也不退回可枚举的公共盐。
        """
        if self._alias_secret is not None:
            return self._alias_secret
        path = self.root / ALIAS_SECRET_FILENAME
        try:
            # 首次调用时文件还不存在：读失败（FileNotFoundError 也是 OSError）
            # 只能说明"该生成一把"，不能直接退化成没有密钥。
            #
            # t11 R2-07：密钥是**二进制**，必须逐字节原样读写。历史实现在这里
            # ``.strip()``，把"首空格/尾换行"这类合法密钥字节吃掉（磁盘 32 字节、
            # 进程里 30 字节）—— 于是持久化的字节与真正参与 HMAC 的 key 不是同一份，
            # 两把不同的密钥也可能被规整成同一把。这里不再做任何文本化处理。
            existing = path.read_bytes() if path.is_file() else b""
            if len(existing) >= ALIAS_SECRET_MIN_BYTES:
                self._alias_secret = existing
                return existing
            secret = secrets.token_bytes(ALIAS_SECRET_BYTES)
            handle = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, ALIAS_SECRET_MODE)
            try:
                written = 0
                while written < len(secret):
                    written += os.write(handle, secret[written:])
            finally:
                os.close(handle)
            try:
                os.chmod(path, ALIAS_SECRET_MODE)
            except OSError:
                pass
            self._alias_secret = secret
            return secret
        except OSError:
            self._alias_secret = b""
            return b""

    def read_reference(self, persona_id: str, filename: str) -> bytes:
        """读取一张参考图字节；任何 IO/路径问题统一转 :class:`PersonaReferenceError`。"""
        try:
            return self.reference_path(persona_id, filename).read_bytes()
        except PersonaReferenceError:
            raise
        except FileNotFoundError as error:
            raise PersonaReferenceError("read_missing") from error
        except OSError as error:
            raise PersonaReferenceError(f"read_failed:{type(error).__name__}") from error
        except ValueError as error:
            raise PersonaReferenceError("read_unsafe_name") from error

    def reference_path(self, persona_id: str, filename: str) -> Path:
        if Path(filename).name != filename:
            raise ValueError("文件名不安全")
        directory = self.persona_dir(persona_id)
        path = ensure_child(directory, directory / filename)
        if path.is_symlink() or not path.is_file():
            raise FileNotFoundError(filename)
        return path

    def delete_reference(self, persona_id: str, filename: str) -> None:
        self.reference_path(persona_id, filename).unlink()
