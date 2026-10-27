"""百度「图像无损放大」的网络层：token 单飞缓存、放大请求、失败降级。

接入点只有一处（``main.py`` 的 ``_finish_task``）：图片已经由
``core.network.materialize_result`` 落盘成 ``Path``，这里把它放大后**另存**一份新
文件并返回新 ``Path``（原图保留在任务缓存目录里做兜底/对账）；任何失败、超时、超限
或本地预检跳过都**原样返回入参** ``value``。

三条硬约束（每条都有代码级后果）：

1. 放大失败**绝不**写 ``task.errors``。两处消费者会因此说谎：``main.py`` 会给用户
   发「部分图片绘制失败。」（实际每张图都发出去了），``services/scheduler.py`` 会把
   状态降级成 ``PARTIAL_SUCCESS``（影响额度退回判定与部署回执）。放大统计只写
   ``task.runtime["enhance"]``。
2. **绝不吞** ``asyncio.CancelledError``。``_finish_task`` 跑在调度器的
   ``asyncio.timeout(remaining)`` 里，外层超时靠 CancelledError 传播；这里只
   ``except Exception``（``TimeoutError`` 是 Exception 子类会被接住，
   ``CancelledError`` 在 3.8+ 是 BaseException，会正常上抛）。**没有** ``except
   BaseException``。
3. 不改 ``task.state`` / ``task.stage``：放大属于 ``PROCESSING_RESULT`` 阶段。

代理：``core/network.py`` 与 ``services/scheduler._ProxySession`` 都**不动**；需要
代理时本模块自己按「``enhance_config.enable_proxy`` 且顶层 ``proxy_url`` 非空」在
请求里传 ``proxy=``（与 ``_ProxySession`` 同语义）。代理地址**绝不进日志**。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import enhance as enhance_logic
from .errors import EnhanceError, EnhanceSkipped
from .security import redact

# token 提前 5 分钟视为过期（时钟偏斜余量）。
TOKEN_SKEW_SECONDS = 300
# 文档：有效期 30 天。``expires_in`` 缺失/非法时退回这个默认值。
DEFAULT_TOKEN_TTL = 2592000
# 瞬时错误（集群/内部错误、HTTP ≥400）退避一次。
TRANSIENT_BACKOFF_SECONDS = 0.5
# 这些 kind 一旦出现就本任务熔断：服务真不可用时，N 张图 × 超时会把任务拖成超时。
CIRCUIT_KINDS = frozenset({"token", "permission", "quota", "transient"})


@dataclass
class EnhanceState:
    """单任务内的放大统计（每次 ``_finish_task`` new 一个，只写 ``task.runtime``）。"""

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


class BaiduEnhancer:
    """百度「图像无损放大」客户端（token 单飞缓存 + 一次放大 + 降级语义）。

    Args:
        session: 共享的 ``aiohttp.ClientSession``（由 ``main.initialize()`` 创建，
            本类**不**新建、**不**关闭）。
        config_getter: ``() -> RuntimeConfig``；用于取 ``generation_timeout`` /
            ``max_upload_bytes`` / ``proxy_url``（每次现算，与插件其它地方一致）。
        debug: ``callable(task, message, *args)``，写插件 DEBUG 日志（``main.py`` 的
            ``_scheduler_debug``：脱敏 + 前缀）。
        warn: ``callable(message, *args)``，写放大相关的 WARNING（每失败类一次）。
    """

    def __init__(self, session, config_getter, *, debug=None, warn=None) -> None:
        self.session = session
        self._config_getter = config_getter
        self._debug = debug
        self._warn = warn
        # 缓存键 -> (access_token, expires_at)；expires_at 用 time.monotonic()。
        self._tokens: dict[str, tuple[str, float]] = {}
        # 每个缓存键一把锁：并发任务只有一个协程真正打 token 接口（单飞）。
        self._locks: dict[str, asyncio.Lock] = {}
        self._warned: set[str] = set()
        # 最近一次放大响应的 log_id：只进 task.runtime["enhance"]["log_id"]（排障用），
        # 不进 INFO 汇总行；apply() 在 await 之后同步取走，不存在跨任务串值。
        self._last_log_id: int = 0

    # ------------------------------------------------------------------ token

    @staticmethod
    def _cache_key(cfg) -> str:
        """缓存键 = sha256(api_key + NUL + secret_key) 前 16 位 hex。

        **不拿 raw secret 当 dict 键**：异常 repr / traceback 里可能带出键名。
        """
        material = f"{cfg.api_key}\x00{cfg.secret_key}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()[:16]

    def invalidate(self, cfg) -> None:
        """丢掉某个账号缓存的 token（110/111/100/401 之后调用）。"""
        self._tokens.pop(self._cache_key(cfg), None)

    async def token(self, cfg, *, force: bool = False, timeout: float | None = None, runtime_cfg=None) -> str:
        """取 ``access_token``：内存缓存 + 按缓存键单飞（并发只打一次接口）。

        Args:
            cfg: 放大配置（提供 api_key / secret_key / timeout_seconds）。
            force: ``True`` 时无视缓存强制重取（110/111/100/401 之后恰好一次）。
            timeout: 单次 token 请求的超时；``None`` 用 ``cfg.timeout_seconds``。
                **必须带超时**：锁跨 await 持有，挂死的连接会锁住所有并发任务。
            runtime_cfg: 已解析的 ``RuntimeConfig``（只为取 ``proxy_url``）；
                ``apply()`` 每张图只解析一次并透传，避免逐图重复 ``load_config``。

        Returns:
            ``access_token`` 字符串。

        Raises:
            EnhanceError: ``kind="token"``（网络失败、响应非法、``invalid_client``）。
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
                async with self.session.post(enhance_logic.TOKEN_URL, params=params, **self._proxy_kwargs(cfg, runtime_cfg)) as response:
                    payload = await response.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise EnhanceError("token", f"token_request:{type(exc).__name__}") from exc
        if not isinstance(payload, dict):
            raise EnhanceError("token", "token_payload_invalid")
        token = str(payload.get("access_token") or "").strip()
        if not token:
            detail = str(payload.get("error_description") or payload.get("error") or "unknown")
            # 文案点明最常见的原因：两把 key 不在同一个应用下。只放常量与错误码。
            self._warn_once(
                "token:credentials",
                "增强 失败：用 API Key / Secret Key 换取 access_token 被拒（%s），"
                "两把 key 必须属于同一个「图像增强与特效」应用；本次任务不再放大。",
                redact(detail),
            )
            raise EnhanceError("token", f"token:{redact(detail)}")
        expires_in = _int_or(payload.get("expires_in"), DEFAULT_TOKEN_TTL)
        if expires_in <= 0:
            expires_in = DEFAULT_TOKEN_TTL
        return token, expires_in

    # ---------------------------------------------------------------- enhance

    async def enhance(self, data: bytes, cfg, *, timeout: float | None = None, runtime_cfg=None) -> tuple[bytes, str]:
        """调一次「图像无损放大」，返回 ``(放大后的字节, MIME)``。

        失败重试策略（spec §2.7）：token 族（110/111/100/HTTP 401）失效重取 + **恰好
        重试一次**；transient 族（1/2/4/216401/216630/282000、HTTP ≥400）退避 0.5 s
        重试一次；传输层失败（连接/DNS/TLS/读超时）**不重试**（时间已花掉，直接降级）。
        """
        runtime_cfg = runtime_cfg if runtime_cfg is not None else self._runtime_config()
        token = await self.token(cfg, timeout=timeout, runtime_cfg=runtime_cfg)
        try:
            payload = await self._exchange(data, token, cfg, timeout=timeout, runtime_cfg=runtime_cfg)
        except EnhanceError as exc:
            if exc.kind == "token":  # HTTP 401
                token = await self._refresh_token(cfg, timeout=timeout, runtime_cfg=runtime_cfg)
                payload = await self._exchange(data, token, cfg, timeout=timeout, runtime_cfg=runtime_cfg)
            elif exc.kind == "transient" and str(exc.detail).startswith("http_"):
                await asyncio.sleep(TRANSIENT_BACKOFF_SECONDS)
                payload = await self._exchange(data, token, cfg, timeout=timeout, runtime_cfg=runtime_cfg)
            else:
                raise
        code = enhance_logic.error_code_of(payload)
        if code in enhance_logic.TOKEN_ERROR_CODES:
            token = await self._refresh_token(cfg, timeout=timeout, runtime_cfg=runtime_cfg)
            payload = await self._exchange(data, token, cfg, timeout=timeout, runtime_cfg=runtime_cfg)
            code = enhance_logic.error_code_of(payload)
            if code in enhance_logic.TOKEN_ERROR_CODES:
                raise EnhanceError("token", "token_rejected_after_refresh", code=code)
        elif code in enhance_logic.TRANSIENT_ERROR_CODES:
            await asyncio.sleep(TRANSIENT_BACKOFF_SECONDS)
            payload = await self._exchange(data, token, cfg, timeout=timeout, runtime_cfg=runtime_cfg)
        decoded = enhance_logic.decode_payload(payload, expected_max_bytes=self._max_output_bytes(runtime_cfg))
        # log_id 只落 task.runtime["enhance"]（排障用），不进 INFO 汇总行也不进 WARNING。
        self._last_log_id = _int_or(payload.get("log_id"), 0) if isinstance(payload, dict) else 0
        return decoded

    async def _refresh_token(self, cfg, *, timeout: float | None, runtime_cfg=None) -> str:
        self.invalidate(cfg)
        return await self.token(cfg, force=True, timeout=timeout, runtime_cfg=runtime_cfg)

    async def _exchange(self, data: bytes, token: str, cfg, *, timeout: float | None = None, runtime_cfg=None):
        """POST 一次放大接口，返回响应 JSON（``error_code`` 未判，交上层分类）。"""
        # aiohttp 的 data=dict 自动做 application/x-www-form-urlencoded 编码；
        # 不用 json=（会被拒），不手写 Content-Type（不覆盖它）。
        form = {"image": enhance_logic.encode_image(data)}
        try:
            async with self.session.post(
                enhance_logic.ENHANCE_URL,
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
            # 传输层失败：**不重试**（UC-6 的调用序列就是 1 token + 1 enhance）。
            raise EnhanceError("transient", f"transport:{type(exc).__name__}") from exc
        if status == 401:
            raise EnhanceError("token", f"http_{status}", code=status)
        if status >= 400 or payload is None:
            raise EnhanceError("transient", f"http_{status}", code=status)
        if not isinstance(payload, dict):
            raise EnhanceError("unknown", "payload_not_object")
        return payload

    # ------------------------------------------------------------------ apply

    async def apply(self, task, value, out_dir: Path, cfg, state: EnhanceState):
        """把一张成品图放大；**永远返回一个可用的值**（失败时原样返回 ``value``）。

        Args:
            task: ``DrawTask``（只用 ``runtime`` 与 ``created_at``，**不改** state/stage）。
            value: ``materialize_result`` 的返回值；只有 ``Path`` 才会放大。
            out_dir: 任务输出目录（``PersonaStore.task_output_dir``）。
            cfg: ``EnhanceConfig``（``_finish_task`` 循环外只取一次）。
            state: 本任务的放大统计（``EnhanceState``）。

        Returns:
            放大成功时是新落盘的 ``Path``；关闭/未配置/跳过/失败一律返回入参。
        """
        # Z0：开关关闭时连 runtime 键都不建、零网络、零日志。
        if not cfg.enabled:
            return value
        if not cfg.configured:
            # 已开启但 key 不全 ⇒ 视为未配置：零网络，整插件生命周期只告警一次。
            self._warn_once(
                "unconfigured",
                "增强 已在配置里开启，但 API Key / Secret Key 未填全，本轮不放大（发送原图）。",
            )
            return value
        if state.circuit_open or not isinstance(value, Path):
            return value
        # 每张图只解析一次运行时配置（generation_timeout / max_upload_bytes /
        # proxy_url 都从这里取），避免逐图重复 load_config（main.py:1220 同一教训）。
        runtime_cfg = self._runtime_config()
        try:
            data = value.read_bytes()
        except OSError:
            state.skipped.append("read_failed")
            self._record(task, state)
            self._debug_log(task, "增强 跳过 reason=read_failed")
            return value
        ok, reason, detail = enhance_logic.plan(data, cfg, remaining=self._remaining(task, runtime_cfg, cfg))
        if not ok:
            state.skipped.append(reason)
            self._record(task, state)
            self._debug_log(task, "增强 跳过 index=%d reason=%s %s", len(state.skipped), reason, detail)
            return value
        state.attempted += 1
        budget = self._budget(task, runtime_cfg, cfg)
        started = time.monotonic()
        try:
            async with asyncio.timeout(budget):
                enhanced, mime = await self.enhance(data, cfg, timeout=budget, runtime_cfg=runtime_cfg)
        except EnhanceSkipped as exc:
            state.skipped.append(exc.reason)
            self._record(task, state)
            self._debug_log(task, "增强 跳过 index=%d reason=%s %s", len(state.skipped), exc.reason, redact(exc.detail))
            return value
        except EnhanceError as exc:
            state.failures.append(f"{exc.kind}:{exc.code or '-'}")
            if exc.kind in CIRCUIT_KINDS:
                state.circuit_open = True
            self._record(task, state)
            self._debug_log(task, "增强 失败 index=%d kind=%s code=%s detail=%s", state.attempted, exc.kind, exc.code or "-", redact(exc.detail))
            self._warn_for(exc)
            return value
        except TimeoutError:
            # 单图预算耗尽：不再重试，本任务熔断（N 张图 × 超时会把任务拖超时）。
            state.failures.append("timeout")
            state.circuit_open = True
            self._record(task, state)
            self._debug_log(task, "增强 失败 index=%d kind=timeout budget=%.1fs", state.attempted, budget)
            self._warn_once("timeout", "增强 超时（单张预算 %.0f s），本次任务改为发送原图。", budget)
            return value
        except Exception as exc:
            # 兜底：任何未预期异常都降级，绝不拖累出图。CancelledError 会绕过本分支。
            state.failures.append(type(exc).__name__)
            self._record(task, state)
            self._debug_log(task, "增强 失败 index=%d kind=exception type=%s detail=%s", state.attempted, type(exc).__name__, redact(str(exc)))
            return value
        # 走到这里说明放大成功返回了解码后的字节与 MIME（失败分支都已 return）。
        state.log_id = self._last_log_id
        max_output_bytes = self._max_output_bytes(runtime_cfg)
        if max_output_bytes > 0 and len(enhanced) > max_output_bytes:
            # 输出超过存储上限：跳过（不是失败），发原图。
            state.skipped.append("output_oversize")
            self._record(task, state)
            self._debug_log(task, "增强 跳过 index=%d reason=output_oversize bytes=%d limit=%d", state.attempted, len(enhanced), max_output_bytes)
            return value
        suffix = mime_suffix(mime).lstrip(".")
        out = out_dir / f"{uuid.uuid4().hex}.{suffix}"
        try:
            out.write_bytes(enhanced)
        except OSError as exc:
            state.failures.append("write")
            self._record(task, state)
            self._debug_log(task, "增强 失败 index=%d kind=write type=%s", state.attempted, type(exc).__name__)
            return value
        state.succeeded += 1
        self._record(task, state)
        self._debug_log(
            task,
            "增强 完成 index=%d in_bytes=%d out_bytes=%d cost=%.1fs log_id=%s",
            state.succeeded,
            len(data),
            len(enhanced),
            time.monotonic() - started,
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
        """``proxy=`` 关键字参数（与 ``_ProxySession`` 同语义）；默认直连。

        代理地址**绝不进日志**：这里只做透传，不打任何字段。
        """
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
        """单图预算：``min(timeout_seconds, remaining - 余量)``，至少 1 s。"""
        remaining = self._remaining(task, runtime_cfg, cfg)
        return max(1.0, min(float(cfg.timeout_seconds), remaining - enhance_logic.BUDGET_MARGIN_SECONDS))

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

    def _warn_for(self, exc: EnhanceError) -> None:
        code = exc.code or "-"
        if exc.kind == "permission":
            if exc.code == 6:
                self._warn_once(
                    "permission:6",
                    "增强 失败：控制台应用未勾选「图像无损放大」（error_code=6），本次任务改为发送原图。",
                )
            else:
                self._warn_once(
                    f"permission:{code}",
                    "增强 失败：应用无权访问该能力（error_code=%s），本次任务改为发送原图。",
                    code,
                )
        elif exc.kind == "quota":
            self._warn_once(
                f"quota:{code}",
                "增强 失败：百度接口额度已用尽（error_code=%s：日限额/QPS/总量），本次任务改为发送原图。",
                code,
            )
        elif exc.kind == "token":
            self._warn_once(
                "token",
                "增强 失败：access_token 不可用（%s），本次任务改为发送原图。",
                redact(exc.detail),
            )
        elif exc.kind == "transient":
            self._warn_once(
                f"transient:{code}",
                "增强 失败：百度接口暂时不可用（%s），本次任务改为发送原图。",
                code if code != "-" else redact(exc.detail),
            )
        elif exc.kind == "config":
            self._warn_once(
                f"config:{code}",
                "增强 失败：请求参数被拒（error_code=%s），本次任务改为发送原图。",
                code,
            )
        else:
            self._warn_once(
                f"unknown:{code}",
                "增强 失败：未知错误（error_code=%s），本次任务改为发送原图。",
                code,
            )

    def _record(self, task, state: EnhanceState) -> None:
        """把统计写进 ``task.runtime["enhance"]``（**不碰** ``task.errors``）。"""
        try:
            task.runtime["enhance"] = {
                "attempted": state.attempted,
                "succeeded": state.succeeded,
                "skipped": list(state.skipped),
                "failures": list(state.failures),
                "circuit_open": state.circuit_open,
                "log_id": state.log_id,
            }
        except Exception:  # pragma: no cover - 防御：统计失败不能影响出图
            pass


def mime_suffix(mime: str) -> str:
    """放大结果的落盘后缀：只认 png/jpg，其余按 png（与 ``materialize_result`` 同风格）。"""
    return {"image/png": ".png", "image/jpeg": ".jpg"}.get(mime, ".png")
