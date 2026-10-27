"""百度「图像无损放大」的网络层：token 单飞缓存、放大请求、失败保留上一级。

接入点只有一处（``main.py`` 的 ``_finish_task``，**在增强之后**）：图片已由
``core.network.materialize_result`` 落盘、并可能已被 ``BaiduEnhancer`` 换成增强产物；
这里把它放大后**另存**一份新文件并返回新 ``Path``。任何失败、超时、超限或本地预检
跳过都**原样返回入参** ``value`` —— 「保留上一级结果」，**不回退原图**（v3.5.3）。

三条硬约束（照 ``core/enhance_client.py``，对放大同样适用）：① 放大失败**绝不**写
``task.errors``（否则发「部分图片绘制失败。」、状态降级 PARTIAL_SUCCESS），统计只写
``task.runtime["upscale"]``；② **绝不吞** ``asyncio.CancelledError``（只 except Exception）；
③ 不改 ``task.state`` / ``task.stage``。

为什么不抽公共 token 模块（spec N11 / Q4）：两级各自一套凭据与缓存（按 key 对隔离、
告警 marker 独立），抽公共模块必然要动 ``core/enhance_client.py``（本版硬约束：一字
不动）。因此这里按同构骨架写一份紧凑实现（token 缓存 + 单飞 + 一次重试 + 熔断），只
import ``core.enhance`` 的产品级常量与错误码集合。代理只透传，地址**绝不进日志**。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import enhance as _enhance
from . import upscale as upscale_logic
from .errors import UpscaleError, UpscaleSkipped
from .security import redact

# token 提前 5 分钟过期（时钟偏斜余量）；``expires_in`` 缺失/非法时退回 30 天默认值。
TOKEN_SKEW_SECONDS = 300
DEFAULT_TOKEN_TTL = 2592000
TRANSIENT_BACKOFF_SECONDS = 0.5
# 这些 kind 一旦出现就本任务熔断放大（服务不可用时 N 张图 × 超时会把任务拖超时）。
# 与增强的 CIRCUIT_KINDS 同口径、不同对象：放大熔断独立复位，增强继续照跑。
UPSCALE_CIRCUIT_KINDS = frozenset({"token", "permission", "quota", "transient"})

# DEBUG 三条格式（spec v3.6.3 逐字，每图一条）；读不出尺寸时单独一条 ``dims=-``。
_DEBUG_DONE = "放大 完成 index=%d in_bytes=%d out_bytes=%d dims=%dx%d->%dx%d cost=%.1fs log_id=%s"
_DEBUG_DONE_NO_DIMS = "放大 完成 index=%d in_bytes=%d out_bytes=%d dims=- cost=%.1fs log_id=%s"
_DEBUG_SKIP = "放大 跳过 index=%d reason=%s %s"
_DEBUG_FAIL = "放大 失败 index=%d kind=%s code=%s detail=%s"


@dataclass
class UpscaleState:
    """单任务内的放大统计（每次 ``_finish_task`` new 一个，只写 ``task.runtime``）。

    与 ``EnhanceState`` 同构但**独立**：``circuit_open`` 互不影响（spec ``v3.5.4``）。
    """

    attempted: int = 0
    succeeded: int = 0
    skipped: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    circuit_open: bool = False
    log_id: int = 0


def _int_or(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class BaiduUpscaler:
    """百度「图像无损放大」客户端（token 单飞缓存 + 一次放大 + 保留上一级语义）。

    ``session`` 是 ``main.initialize()`` 创建的共享 ClientSession（本类不新建、不关闭）；
    ``config_getter`` 返回 ``RuntimeConfig``（取 generation_timeout / max_upload_bytes /
    proxy_url，每次现算）；``debug`` 形状 ``callable(task, message, *args)``，``warn`` 形状
    ``callable(message, *args)``（每失败类只调一次）。
    """

    def __init__(self, session, config_getter, *, debug=None, warn=None) -> None:
        self.session = session
        self._config_getter = config_getter
        self._debug = debug
        self._warn = warn
        # 缓存键 -> (access_token, expires_at[monotonic])。**与增强的缓存互不共享**
        # （spec Q4）：同应用时多一次 oauth 调用 / 30 天，换两级彻底解耦。
        self._tokens: dict[str, tuple[str, float]] = {}
        # 每个缓存键一把锁：并发任务只有一个协程真正打 token 接口（单飞）。
        self._locks: dict[str, asyncio.Lock] = {}
        # 告警 marker 去重（marker 与增强的**不共用**，否则会抢走对方的告警名额）。
        self._warned: set[str] = set()
        # 最近一次放大响应的 log_id：只进 runtime["upscale"]["log_id"]（排障用），不进
        # INFO 汇总行；apply() 在 await 之后同步取走，不存在跨任务串值。
        self._last_log_id: int = 0

    # ------------------------------------------------------------------ token
    # 本节的 token 缓存 + 单飞**骨架**按 spec N11 复制自 core/enhance_client.py（同构、
    # 非共享）：两级必须各持一份缓存与告警 marker（Q4：同应用时多一次 oauth / 30 天），
    # 而抽公共模块会动到「一字不改」的增强模块。复制面仅限这一节（约 30 行骨架），
    # 端点、三条 DEBUG 格式、10 条 WARNING 文案、2× 观测与统计键均为本级独有。

    @staticmethod
    def _cache_key(cfg) -> str:
        """缓存键 = sha256(api_key + NUL + secret_key) 前 16 位 hex（不拿 raw secret 当键）。"""
        material = f"{cfg.api_key}\x00{cfg.secret_key}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:16]

    def invalidate(self, cfg) -> None:
        """丢掉某个账号缓存的 token（110/111/100/401 之后调用）。"""
        self._tokens.pop(self._cache_key(cfg), None)

    async def token(self, cfg, *, force: bool = False, timeout: float | None = None, runtime_cfg=None) -> str:
        """取 ``access_token``：内存缓存 + 按缓存键单飞（并发只打一次接口）。

        Args: 上传用 cfg（api_key / secret_key / timeout_seconds）；``force`` 时无视缓存
        强制重取（110/111/100/401 之后恰好一次）；``runtime_cfg`` 只为取 ``proxy_url``。

        Raises:
            UpscaleError: ``kind="token"``（网络失败、响应非法、``invalid_client``）。
        """
        key = self._cache_key(cfg)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # 锁内二次检查：等锁的协程直接复用刚取到的新 token。
            cached = self._tokens.get(key)
            if cached is not None and not force and cached[1] > time.monotonic():
                return cached[0]
            token, expires_in = await self._fetch_token(cfg, timeout=timeout, runtime_cfg=runtime_cfg)
            self._tokens[key] = (token, time.monotonic() + expires_in - TOKEN_SKEW_SECONDS)
            return token

    async def _fetch_token(self, cfg, *, timeout: float | None = None, runtime_cfg=None) -> tuple[str, int]:
        budget = float(timeout or cfg.timeout_seconds or 20)
        params = {
            "grant_type": "client_credentials",
            "client_id": cfg.api_key,
            "client_secret": cfg.secret_key,
        }
        try:
            async with asyncio.timeout(budget):
                async with self.session.post(
                    _enhance.TOKEN_URL, params=params, **self._proxy_kwargs(cfg, runtime_cfg)
                ) as response:
                    payload = await response.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise UpscaleError("token", f"token_request:{type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise UpscaleError("token", "token_payload_invalid")
        token = str(payload.get("access_token") or "").strip()
        if not token:
            detail = str(payload.get("error_description") or payload.get("error") or "unknown")
            # 文案点明最常见的原因：两把 key 不在同一个应用下。只放常量与该说明。
            self._warn_once(
                "upscale_credentials",
                "放大 失败：用 API Key / Secret Key 换取 access_token 被拒（%s），"
                "两把 key 必须属于同一个「图像增强与特效」应用；本次任务不再放大。",
                redact(detail),
            )
            raise UpscaleError("token", f"token:{redact(detail)}")
        expires_in = _int_or(payload.get("expires_in"), DEFAULT_TOKEN_TTL)
        return token, expires_in if expires_in > 0 else DEFAULT_TOKEN_TTL

    # ---------------------------------------------------------------- upscale

    async def upscale(self, data: bytes, cfg, *, timeout: float | None = None, runtime_cfg=None) -> tuple[bytes, str]:
        """调一次「图像无损放大」，返回 ``(放大后的字节, MIME)``。

        重试策略与增强同口径：token 族（110/111/100/HTTP 401）失效重取 + **恰好重试
        一次**；transient 族（1/2/4/216401/216630/282000、HTTP ≥400）退避 0.5 s 重试一次；
        传输层失败**不重试**（时间已花掉，直接保留上一级）。
        """
        runtime_cfg = runtime_cfg if runtime_cfg is not None else self._runtime_config()
        token = await self.token(cfg, timeout=timeout, runtime_cfg=runtime_cfg)
        try:
            payload = await self._exchange(data, token, cfg, timeout=timeout, runtime_cfg=runtime_cfg)
        except UpscaleError as exc:
            if exc.kind == "token":  # HTTP 401
                token = await self._refresh_token(cfg, timeout=timeout, runtime_cfg=runtime_cfg)
                payload = await self._exchange(data, token, cfg, timeout=timeout, runtime_cfg=runtime_cfg)
            elif exc.kind == "transient" and str(exc.detail).startswith("http_"):
                await asyncio.sleep(TRANSIENT_BACKOFF_SECONDS)
                payload = await self._exchange(data, token, cfg, timeout=timeout, runtime_cfg=runtime_cfg)
            else:
                raise
        code = upscale_logic.error_code_of(payload)
        if code in _enhance.TOKEN_ERROR_CODES:
            token = await self._refresh_token(cfg, timeout=timeout, runtime_cfg=runtime_cfg)
            payload = await self._exchange(data, token, cfg, timeout=timeout, runtime_cfg=runtime_cfg)
            code = upscale_logic.error_code_of(payload)
            if code in _enhance.TOKEN_ERROR_CODES:
                raise UpscaleError("token", "token_rejected_after_refresh", code=code)
        elif code in _enhance.TRANSIENT_ERROR_CODES:
            await asyncio.sleep(TRANSIENT_BACKOFF_SECONDS)
            payload = await self._exchange(data, token, cfg, timeout=timeout, runtime_cfg=runtime_cfg)
        # 输出体积上限在 apply() 里按 storage_config 判（spec v3.4 第 7 条），这里不传。
        decoded = upscale_logic.decode_payload(payload)
        self._last_log_id = _int_or(payload.get("log_id"), 0) if isinstance(payload, dict) else 0
        return decoded

    async def _refresh_token(self, cfg, *, timeout: float | None, runtime_cfg=None) -> str:
        self.invalidate(cfg)
        return await self.token(cfg, force=True, timeout=timeout, runtime_cfg=runtime_cfg)

    async def _exchange(self, data: bytes, token: str, cfg, *, timeout: float | None = None, runtime_cfg=None):
        """POST 一次放大接口，返回响应 JSON（``error_code`` 未判，交上层分类）。"""
        # aiohttp 的 data=dict 自动做 application/x-www-form-urlencoded 编码；
        # 不用 json=（会被拒），不手写 Content-Type（不覆盖它）。
        form = {"image": upscale_logic.encode_image(data)}
        try:
            async with self.session.post(
                upscale_logic.UPSCALE_URL,
                params={"access_token": token},
                data=form,
                **self._proxy_kwargs(cfg, runtime_cfg),
            ) as response:
                status = int(getattr(response, "status", 200) or 200)
                try:
                    payload = await response.json(content_type=None)
                except Exception:
                    payload = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 传输层失败：**不重试**（时间已经花掉，直接保留上一级）。
            raise UpscaleError("transient", f"transport:{type(exc).__name__}") from exc
        if status == 401:
            raise UpscaleError("token", f"http_{status}", code=status)
        if status >= 400 or payload is None:
            raise UpscaleError("transient", f"http_{status}", code=status)
        if not isinstance(payload, dict):
            raise UpscaleError("unknown", "payload_not_object")
        return payload

    # ------------------------------------------------------------------ apply

    async def apply(self, task, value, out_dir: Path, cfg, state: UpscaleState):
        """把一张成品图放大；**永远返回一个可用的值**（失败时原样返回上一级产物）。

        ``value`` 是上一级（增强产物或原图）的返回值，只有 ``Path`` 才会放大；``cfg`` /
        ``state`` 由 ``_finish_task`` 在循环外各取一次 / new 一个。返回：放大成功时是
        新落盘的 ``Path``，关闭/未配置/跳过/失败一律原样返回入参。
        """
        # Z0：开关关闭时连 runtime 键都不建、零网络、零日志、返回值 === 入参。
        if not cfg.enabled:
            return value
        if not cfg.configured:
            # 已开启但 key 不全 ⇒ 视为未配置：零网络，整插件生命周期只告警一次
            # （marker 与增强的 ``unconfigured`` **不共用**）。
            self._warn_once(
                "upscale_unconfigured",
                "放大 已在配置里开启，但 API Key / Secret Key 未填全，本轮不放大（保留上一级结果）。",
            )
            return value
        if state.circuit_open or not isinstance(value, Path):
            return value
        # 每张图只解析一次运行时配置，避免逐图重复 load_config。
        runtime_cfg = self._runtime_config()
        try:
            data = value.read_bytes()
        except OSError:
            state.skipped.append("read_failed")
            self._record(task, state)
            self._debug_log(task, _DEBUG_SKIP, len(state.skipped), "read_failed", "")
            return value
        ok, reason, detail = upscale_logic.plan(
            data, cfg, remaining=self._remaining(task, runtime_cfg, cfg)
        )
        if not ok:
            state.skipped.append(reason)
            self._record(task, state)
            self._debug_log(task, _DEBUG_SKIP, len(state.skipped), reason, detail)
            return value
        state.attempted += 1
        budget = self._budget(task, runtime_cfg, cfg)
        started = time.monotonic()
        try:
            async with asyncio.timeout(budget):
                enlarged, mime = await self.upscale(data, cfg, timeout=budget, runtime_cfg=runtime_cfg)
        except UpscaleSkipped as exc:
            state.skipped.append(exc.reason)
            self._record(task, state)
            self._debug_log(task, _DEBUG_SKIP, len(state.skipped), exc.reason, redact(exc.detail))
            return value
        except UpscaleError as exc:
            state.failures.append(f"{exc.kind}:{exc.code or '-'}")
            if exc.kind in UPSCALE_CIRCUIT_KINDS:
                state.circuit_open = True
            self._record(task, state)
            self._debug_log(
                task, _DEBUG_FAIL, state.attempted, exc.kind, exc.code or "-", redact(exc.detail)
            )
            self._warn_for(exc)
            return value
        except TimeoutError:
            # 单图预算耗尽：不再重试，本任务熔断（N 张图 × 超时会把任务拖超时）。
            state.failures.append("timeout")
            state.circuit_open = True
            self._record(task, state)
            self._debug_log(task, _DEBUG_FAIL, state.attempted, "timeout", "-", f"budget={budget:.1f}s")
            self._warn_once("upscale_timeout", "放大 超时（单张预算 %.0f s），本次任务保留上一级结果。", budget)
            return value
        except Exception as exc:
            # 兜底：任何未预期异常都降级，绝不拖累出图。CancelledError 会绕过本分支。
            state.failures.append(type(exc).__name__)
            self._record(task, state)
            self._debug_log(task, _DEBUG_FAIL, state.attempted, type(exc).__name__, "-", redact(str(exc)))
            return value
        # 走到这里说明放大成功返回了解码后的字节与 MIME（失败分支都已 return）。
        state.log_id = self._last_log_id
        max_output_bytes = self._max_output_bytes(runtime_cfg)
        if max_output_bytes > 0 and len(enlarged) > max_output_bytes:
            # 2× 输出的字节数可能显著变大（内容量 ×4 后再编码）：超过存储上限即跳过、
            # 不落盘（保留上一级），免得写一个平台发不出去的巨图。
            state.skipped.append("output_oversize")
            self._record(task, state)
            self._debug_log(
                task, _DEBUG_SKIP, len(state.skipped), "output_oversize",
                f"bytes={len(enlarged)} limit={max_output_bytes}",
            )
            return value
        suffix = upscale_logic.mime_suffix(mime).lstrip(".")
        out = out_dir / f"{uuid.uuid4().hex}.{suffix}"
        try:
            out.write_bytes(enlarged)
        except OSError as exc:
            state.failures.append("write")
            self._record(task, state)
            self._debug_log(task, _DEBUG_FAIL, state.attempted, "write", "-", type(exc).__name__)
            return value
        state.succeeded += 1
        self._record(task, state)
        # 2× 语义只做观测：dims= 段原样记录输入/输出尺寸；不是 2 倍也**不失败**（响应
        # 字节原样落盘），且不额外加日志行，以免破坏 v3.6.3「每图一条」的口径。
        cost = time.monotonic() - started
        dims_before = upscale_logic.image_dimensions(data)
        dims_after = upscale_logic.image_dimensions(enlarged)
        if dims_before is None or dims_after is None:
            self._debug_log(
                task, _DEBUG_DONE_NO_DIMS, state.succeeded, len(data), len(enlarged), cost,
                state.log_id or "-",
            )
        else:
            self._debug_log(
                task, _DEBUG_DONE, state.succeeded, len(data), len(enlarged),
                dims_before[0], dims_before[1], dims_after[0], dims_after[1], cost,
                state.log_id or "-",
            )
        return out

    # ----------------------------------------------------------------- 内部

    def _runtime_config(self):
        """解析一次 ``RuntimeConfig``；取不到时返回 ``None``（各取值点都有兜底）。"""
        try:
            return self._config_getter()
        except Exception:
            return None

    def _proxy_kwargs(self, cfg, runtime_cfg=None) -> dict:
        """``proxy=`` 关键字参数（与 ``_ProxySession`` 同语义）；默认直连、地址不落日志。"""
        if not cfg.enable_proxy:
            return {}
        runtime_cfg = runtime_cfg if runtime_cfg is not None else self._runtime_config()
        proxy_url = str(getattr(runtime_cfg, "proxy_url", "") or "").strip()
        return {"proxy": proxy_url} if proxy_url else {}

    def _remaining(self, task, runtime_cfg, cfg) -> float:
        """本任务剩余总预算（秒），算法与 ``main.py`` 的配文预算一致。"""
        try:
            generation_timeout = float(getattr(runtime_cfg, "generation_timeout", 300))
        except (TypeError, ValueError):
            generation_timeout = 300.0
        created_at = float(getattr(task, "created_at", 0.0) or 0.0)
        return generation_timeout - (time.monotonic() - created_at)

    def _budget(self, task, runtime_cfg, cfg) -> float:
        """单图预算：``min(timeout_seconds, remaining - 余量)``，至少 1 s。

        余量**复用** ``core/enhance.BUDGET_MARGIN_SECONDS``（不复制常量、不改其值）：
        两级共用同一个余量，「两级都跑完还能发图」这个不变式才算得清。
        """
        remaining = self._remaining(task, runtime_cfg, cfg)
        return max(
            1.0,
            min(float(cfg.timeout_seconds), remaining - upscale_logic.BUDGET_MARGIN_SECONDS),
        )

    def _max_output_bytes(self, runtime_cfg=None) -> int:
        runtime_cfg = runtime_cfg if runtime_cfg is not None else self._runtime_config()
        try:
            return max(0, int(getattr(runtime_cfg, "max_upload_bytes", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _debug_log(self, task, message, *args) -> None:
        """写插件 DEBUG 日志（注入的回调自带脱敏与前缀；未注入时静默）。"""
        if self._debug is not None:
            self._debug(task, message, *args)

    def _warn_once(self, marker: str, message: str, *args) -> None:
        """每失败类只告警一次（避免 N 张图刷屏）；文案只放常量与错误码。"""
        if marker in self._warned:
            return
        self._warned.add(marker)
        if self._warn is not None:
            self._warn(message, *args)

    def _warn_for(self, exc: UpscaleError) -> None:
        """按 ``kind`` 选一条**逐字**文案（spec ``v3.6.2``）；marker 与增强不共用。"""
        code = exc.code or "-"
        if exc.kind == "permission":
            if exc.code == 6:
                self._warn_once(
                    "upscale_permission:6",
                    "放大 失败：控制台应用未勾选「图像无损放大」（error_code=6），"
                    "本次任务改为保留上一级结果。",
                )
            else:
                self._warn_once(
                    f"upscale_permission:{code}",
                    "放大 失败：应用无权访问该能力（error_code=%s），本次任务改为保留上一级结果。",
                    code,
                )
        elif exc.kind == "quota":
            self._warn_once(
                f"upscale_quota:{code}",
                "放大 失败：百度接口额度已用尽（error_code=%s：日限额/QPS/总量），"
                "本次任务改为保留上一级结果。",
                code,
            )
        elif exc.kind == "token":
            self._warn_once(
                "upscale_token",
                "放大 失败：access_token 不可用（%s），本次任务改为保留上一级结果。",
                redact(exc.detail),
            )
        elif exc.kind == "transient":
            self._warn_once(
                f"upscale_transient:{code}",
                "放大 失败：百度接口暂时不可用（%s），本次任务改为保留上一级结果。",
                code if code != "-" else redact(exc.detail),
            )
        elif exc.kind == "config":
            self._warn_once(
                f"upscale_config:{code}",
                "放大 失败：请求参数被拒（error_code=%s），本次任务改为保留上一级结果。",
                code,
            )
        else:
            self._warn_once(
                f"upscale_unknown:{code}",
                "放大 失败：未知错误（error_code=%s），本次任务改为保留上一级结果。",
                code,
            )

    def _record(self, task, state: UpscaleState) -> None:
        """把统计写进 ``task.runtime["upscale"]``（**不碰** ``task.errors``，恰 6 键）。"""
        try:
            task.runtime["upscale"] = {
                "attempted": state.attempted,
                "succeeded": state.succeeded,
                "skipped": list(state.skipped),
                "failures": list(state.failures),
                "circuit_open": state.circuit_open,
                "log_id": state.log_id,
            }
        except Exception:  # pragma: no cover - 防御：统计失败不能影响出图
            pass
