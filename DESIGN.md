# imago_fusion 设计契约与审计报告（任务 t1 / scout）

> 产出物性质：**只审计与设计，不含实现代码**。本文件里的代码块只有三类：接口签名（契约）、JSON schema 片段（配置）、命令与输出记录（证据）。
> 结论先行：迁移的净值 = 「imago 的任务/提示词/额度/fallback 骨架」+「大香蕉的 AstrBot 原生 provider 桥接（native.py 的三级提取）+ 反代节点配置」；**不需要**移植大香蕉的 `StandardProvider` 直连 HTTP 栈（imago 的 5 个 HTTP adapter 已覆盖同一件事，且比它更干净）。

---

## 0. 取证基线与方法

### 0.1 审计范围与只读声明

- 只读审计：`/root/AstrBot/data/plugins/imago`、`/root/AstrBot/data/plugins/astrbot_plugin_big_banana`、`/root/AstrBot/data/cmd_config.json`、`/root/AstrBot/data/config/astrbot_plugin_big_banana_config.json`、`/root/AstrBot/data/config/imago_config.json`。
- 本次**未修改**任何插件源码与 `data/config`；唯一写入是本文件（`data/plugins/imago_fusion/DESIGN.md`）。
- 复现脚本落在 `/tmp/repro_*.py`（仓外临时文件，不进仓库）。

### 0.2 版本与一致性事实

| 项 | 值 | 证据 |
| --- | --- | --- |
| imago 目录版本 | 1.2.2 | `imago/metadata.yaml:3`、`imago/__init__.py:3` |
| imago 注册版本 | **1.2.0（不一致）** | `imago/main.py:73` `@register("imago", "Wolfycz", ..., "1.2.0")` |
| 运行日志里的版本 | 1.2.2 | `data/logs/astrbot.log:13791` `Plugin imago (1.2.2) by Wolfycz` |
| big_banana 版本 | v0.2.7 | `astrbot_plugin_big_banana/metadata.yaml:3`（无 `__init__.py`，命名空间包导入可用） |
| AstrBot 依赖 | aiohttp 3.14.1 / Pillow 12.2.0（`.venv`） | `uv run` 之外的复现统一用 `/root/AstrBot/.venv/bin/python` |

### 0.3 反代链路实测（无 key 探针，未消耗额度）

```text
$ ss -ltnp | grep -E '8317|19090|19101'
LISTEN 127.0.0.1:19090  docker-proxy (pid 2912328)
LISTEN 127.0.0.1:19101  gemini-web2api-go (pid 1800346)
LISTEN *:8317           CLIProxyAPI (pid 1877347)          # 本任务的反代，注意绑定 0.0.0.0

$ curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8317/v1/models
401   {"error":"Missing API key"}
$ curl -s ... -X POST -d '{"model":"web-gemini-image",...}' http://127.0.0.1:8317/v1/chat/completions
401   {"error":"Missing API key"}
$ curl -s ... -X POST -d '{}' http://127.0.0.1:8317/v1beta/models/gemini-3.1-flash-image:generateContent
401   {"error":"Missing API key"}
$ curl -s ... -X POST -d '{}' http://127.0.0.1:8317/models/gemini-3.1-flash-image:generateContent
404                                                       # ← 无 /v1beta 前缀的路由不存在
$ curl -s ... -H 'Authorization: Bearer sk-bogus-not-real' ... /v1/chat/completions
401   {"error":"Invalid API key"}                          # 有鉴权，必须用真 key（报告中一律掩码为 sk-1d534***）
```

结论（对冒烟测试直接可用）：

1. 同一个 CLIProxyAPI 反代**同时**提供 OpenAI 协议（`/v1/chat/completions`，原生路由用）与 Gemini 协议（`/v1beta/models/<model>:generateContent`，HTTP 直连节点用）。
2. imago 的 `GeminiAdapter` 把 URL 拼成 `{base}/models/<model>:generateContent`（`imago/providers/gemini.py:29-30`），因此**HTTP 直连节点的 base_url 必须写 `http://127.0.0.1:8317/v1beta`**，写 `http://127.0.0.1:8317` 会 404（实测第 4 条探针）。这条与 `imago/_conf_schema.json:41` 的提示一致。
3. 反代绑在 `*:8317`（所有网卡）——反代进程自身的加固问题，不属于本插件范围，但建议顺手在报告里提给用户。

### 0.4 utf-8-sig（BOM）事实

两份配置文件都带 BOM，直接按 utf-8 解析会失败：

```text
cmd_config.json                               encoding=utf-8      -> JSONDecodeError: Unexpected UTF-8 BOM (decode using utf-8-sig): line 1 column 1 (char 0)
cmd_config.json                               encoding=utf-8-sig  -> OK
config/astrbot_plugin_big_banana_config.json  encoding=utf-8      -> JSONDecodeError: ... (同上)
config/astrbot_plugin_big_banana_config.json  encoding=utf-8-sig  -> OK
```

→ 任何读取这两份文件的脚本/测试（含冒烟脚本）**必须** `open(path, "rb").read().decode("utf-8-sig")` 或 `encoding="utf-8-sig"`。

### 0.5 节点配置事实（迁移与冒烟的直接输入）

原生路由（AstrBot 侧）：

| 键 | 值 | 位置 |
| --- | --- | --- |
| provider_source id | `cpa_Gemini`，`type: openai_chat_completion`，`api_base: http://127.0.0.1:8317/v1` | `data/cmd_config.json:103-114` |
| provider_source key | `sk-1d534***` | `data/cmd_config.json:105-108` |
| 原生 provider id | `cpa_Gemini/web-gemini-image`，`provider_source_id: cpa_Gemini`，`model: web-gemini-image`，`modalities: [text, image, audio, tool_use]`，`enable: true` | `data/cmd_config.json:533-545` |

反代模板（大香蕉侧）：

| 键 | 值 | 位置 |
| --- | --- | --- |
| 模板名 | `CPA_GeminiImage`（`provider_type: gemini`） | `data/config/astrbot_plugin_big_banana_config.json:17` |
| keys | `["sk-1d534***"]`（单 key） | 同上 `:20-22` |
| base_url | `http://127.0.0.1:8317` | 同上 `:23` |
| model / response_modalities / stream / enable_proxy / max_images | `gemini-3.1-flash-image` / `['IMAGE']` / `false` / `false` / `14` | 同上 `:25-29` |
| fallback_order | `1` | 同上 `:19` |
| 默认原生提供商 | `["cpa_Gemini/web-gemini-image"]` | 同上 `:51-53` |

**关键实测（`/tmp/repro_bb_order.py`）**：`default_providers == ['cpa_Gemini/web-gemini-image', 'CPA_GeminiImage']` —— 大香蕉当前的默认顺序是**原生路由优先、Gemini 反代模板兜底**（`fallback_order=1` 输给原生固定优先级 0，见 `astrbot_plugin_big_banana/core/config/provider_config.py:29-52`）。这条决定了两件事：(a) 冒烟必须同时覆盖两条路径；(b) 第 4 节 A5 的“原生参考图被静默截断到 6 张”正是打在主路径上的。

---

## 1. imago_fusion 模块清单与目录结构（以 imago 为骨架）

### 1.1 目录树（标记含义：`[复制]` 原样复制 / `[改]` 复制后按本文件改 / `[新]` 新增）

```text
data/plugins/imago_fusion/
├── metadata.yaml                  [改] name=imago_fusion, display_name=IMAGO·FUSION, version=1.0.0
├── __init__.py                    [改] __version__ = "1.0.0"（与 metadata.yaml 一致）
├── requirements.txt               [复制] aiohttp>=3.9,<4（不引 PIL：imago 侧不做格式转换）
├── README.md                      [新] 反代节点配置 + 与原 imago 的差异化说明
├── _conf_schema.json              [改] providers 模板项新增 4 字段（见 §3）
├── main.py                        [改] Star 类改名 + 传 context 给 scheduler + 3 处小修（见 §5）
├── pages/webui/{_page.json,index.html,app.js,style.css}   [复制] 管理台（节点/额度/Persona）
├── core/
│   ├── __init__.py                [复制]
│   ├── commands.py                [复制]
│   ├── config.py                  [改] API_TYPES 增 astrbot_native；native 节点放宽 base_url/api_keys
│   ├── dedup.py                   [复制]
│   ├── errors.py                  [复制]
│   ├── models.py                  [改] ProviderConfig 增 3 字段（astrbot_provider_id/stream/enable_proxy）
│   ├── network.py                 [复制] fetch_reference/materialize_result 的体积+魔数+私网守卫全部保留
│   ├── prompting.py               [复制] 副脑/风格/PERSONA 全部保留
│   ├── references.py              [复制]
│   └── security.py                [改]（可选）redact 增裸 key= 掩码
├── providers/
│   ├── __init__.py                [改] ADAPTERS 增 "astrbot_native": NativeAdapter
│   ├── base.py                    [改] __init__ 增 *, context=None；其余不动
│   ├── gemini.py                  [改] 修 _file_uri_with_key（§4 B1）
│   ├── dashscope.py               [复制]
│   ├── openai_chat.py             [复制]
│   ├── openai_image.py            [复制]
│   ├── custom.py                  [复制]
│   └── native.py                  [新] NativeAdapter（§2）
├── services/
│   ├── __init__.py                [复制]
│   ├── persona_store.py           [复制]
│   ├── quota_store.py             [复制]
│   └── scheduler.py               [改] 4 处：context 透传、api_keys 除零护栏、native 无 key 分支、fallback_order 排序
└── test/
    ├── test_core.py               [复制]
    ├── test_prompting.py          [复制]
    ├── test_reference_passthrough.py [复制]
    ├── test_scheduler.py          [改] 增 native 分支用例（空 api_keys / key 轮换）
    └── test_native_adapter.py     [新] 三级提取 + 私网守卫（用假 LLMResponse/MessageChain 打桩）
```

### 1.2 新增/改动的最小集合（4 个文件是“迁移必需”，其余是可选优化）

| 优先级 | 文件 | 原因（不做的后果） |
| --- | --- | --- |
| P0 | `providers/native.py`（新） | 没有它就没有原生路由 |
| P0 | `providers/__init__.py`、`core/config.py`、`integrations/web_api.py`、`_conf_schema.json` | 四处 `api_type` 白名单必须同步，否则节点被静默丢弃或 WebUI 拒收（§4 B3） |
| P0 | `services/scheduler.py`、`main.py` | 传 context + 空 `api_keys` 护栏（§4 B2） |
| P1 | `core/models.py`、`providers/base.py` | 承载 `astrbot_provider_id`/`stream` 与 context 注入 |
| P2 | `providers/gemini.py`、`core/security.py` | 带上诊断出的修复：§4 B1（Files API key 拼接）、§4 B5（redact 掩码）；大香蕉侧 A1/A2 另行修 |

---

## 2. NativeAdapter 接口契约

### 2.1 复用 imago 抽象（不改基类语义）

imago 现有契约（`imago/providers/base.py:11-16`）：

```python
class ProviderAdapter(ABC):
    def __init__(self, config: ProviderConfig): ...
    @abstractmethod
    async def generate(self, session, request: GenerationRequest, api_key: str) -> list[ImageResult]: ...
```

imago 的调用点（`imago/services/scheduler.py:300-320`）：

```python
attempt = replace(provider, model=model)            # 300
attempt_request = self._request_for_attempt(task, attempt)   # 301
adapter = ADAPTERS[attempt.api_type](attempt)       # 305
key = attempt.api_keys[key_index % len(attempt.api_keys)]    # 307  ← 空 key 会 ZeroDivisionError
async with asyncio.timeout(attempt.timeout):        # 319
    results = await adapter.generate(self.session, attempt_request, key)   # 320
```

**契约变更（最小，共 3 文件 4 行）**：

```python
# providers/base.py
class ProviderAdapter(ABC):
    def __init__(self, config: ProviderConfig, *, context: Any = None) -> None:
        self.config = config
        self.context = context          # 仅 astrbot_native 使用；其余 adapter 忽略

# services/scheduler.py
class TaskScheduler:
    def __init__(self, config_getter, session, runner, logger, debug=None, *, context=None): ...
    # _generate 内：
    adapter = ADAPTERS[attempt.api_type](attempt, context=self.context)

# imago/main.py:143
self.scheduler = TaskScheduler(lambda: load_config(self.raw_config), self.session,
                               self._finish_task, self._provider_error, self._scheduler_debug,
                               context=self.context)
```

**不采用**的方案（KISS 判定）：模块级全局 `_CONTEXT`（隐式状态、测试难隔离）、给 `ProviderConfig` 塞 context（配置对象混入运行时依赖）、按 api_type 分叉 `kwargs`（分支爆炸）。

> **已确认偏差（t5 补注，captain 已接受）**：实现没有走“scheduler 构造参数注入”，而是把 context 与日志回调放进 `core/runtime.py` 的模块级容器，由 `main.initialize()` 调 `runtime.configure(context=..., debug=..., error_reporter=...)` 安装、`terminate()` 调 `runtime.reset()` 清理；`NativeAdapter` 在自身没有 context 时回落到 `runtime.context()`。
>
> 偏差原因：上面“不采用模块级全局”的判断针对的是**隐式状态与测试隔离**，而落地时的约束是——`scheduler._generate` 构造适配器保持既有签名 `ADAPTERS[api_type](config)` 不变（`test/test_scheduler.py` 用只有 `config` 参数的桩适配器，第三方适配器同理），一旦改成传 `context=` 关键字就会打破该契约；同时 `providers/base.py` 仍保留 `*, context=None` 参数，两条注入路径并存：显式传参优先，未传则用容器。
>
> 功能与测试覆盖：`test/test_native_adapter.py` 覆盖“显式 context”与“runtime 容器 context”两条路径（`test_runtime_context_is_used_when_the_adapter_has_none`），`runtime.reset()` 在每个用例的 `finally` 里执行以保证隔离；`main.py` 的 configure/reset 位于 `initialize`/`terminate` 生命周期内，热重载不残留引用。

### 2.2 NativeAdapter 类契约

```python
class NativeAdapter(ProviderAdapter):
    """经 AstrBot 原生 provider 路由出图（反代/网页端模型）。"""

    async def generate(
        self, session: Any, request: GenerationRequest, api_key: str
    ) -> list[ImageResult]: ...
```

| 契约项 | 规定 |
| --- | --- |
| `session` 参数 | **忽略**。native 路由的 HTTP 由 AstrBot provider 自理；本 adapter 不新建 session、不发请求 |
| `api_key` 参数 | **忽略**（鉴权在 AstrBot provider_source 内）。上层仍需容忍空 `api_keys`（§4 B2） |
| 节点鉴权来源 | `ProviderConfig.astrbot_provider_id`（新字段）= AstrBot provider id，如 `cpa_Gemini/web-gemini-image` |
| 返回形状 | 成功时 `list[ImageResult]`，长度 ∈ [1, request.count]；`ImageResult.url` 或 `ImageResult.data` 二者其一非空 |
| `count` 语义 | `GenerationRequest.count ∈ [1,4]`（`imago/main.py:434`）。**实现（t5 修正）**：单次聊天调用通常只出 1 张图 → 循环调用直至凑满 `count` 张不同引用、**最多 4 次**；AstrBot provider id 不存在时**立即中断本轮并让位 fallback**（错误的 id 重试不会变对）；单次失败只记日志并继续，节点部分成功时返回已拿到的图片；全失败才抛错（否则 `imago/services/scheduler.py:175-176` 会记 `missing_results` 并把任务判成 `PARTIAL_SUCCESS`）。原文“任一成功即可返回”表述不准确，已按实现改写 |
| 不返回值形状 | 不允许返回空 list 表示“成功但零图”——零图必须抛 `NoOutputError`（`imago/core/errors.py:37-40`），否则任务状态机把零输出当成功（`imago/services/scheduler.py:245-252`） |
| 超时归属 | 上层 `asyncio.timeout(attempt.timeout)`（`imago/services/scheduler.py:319`）与任务总超时（`imago/services/scheduler.py:165`）双重兜底；adapter 内部不再叠加 timeout，但**必须**把 AstrBot 的重试次数收敛为 `1`（`request_max_retries=1`），避免“节点超时在即却还在内部重试 5 次” |
| 参考图入参 | `request.references: list[ImageInput]`（`imago/core/models.py:100-105`，内存 bytes + mime）→ 转 `data:{mime};base64,{b64}` 传给 `image_urls`（AstrBot `MediaResolver` 直接吃 data URL，`astrbot/core/utils/media_utils.py:608-620`） |
| 参考图数量上限 | 由图片节点的 `reference_image_limit` 复用旧机制（`imago/services/scheduler.py:112-120` 已在 attempts 前采样）；native 侧**不再**截断（大香蕉 native 的 6 张默认截断正是要修的 bug，§4 A5） |

### 2.3 调用路径（两条，按 `stream` 开关二选一）

```python
# stream=False
resp = await self.context.llm_generate(
    chat_provider_id=self.config.astrbot_provider_id,
    prompt=prompt,
    image_urls=image_urls,          # data: URL 列表
    request_max_retries=1,
)

# stream=True
prov = await self.context.provider_manager.get_provider_by_id(self.config.astrbot_provider_id)
async for resp in prov.text_chat_stream(prompt=prompt, image_urls=image_urls,
                                        request_max_retries=1):
    ...
```

- `llm_generate` 签名与异常：`astrbot/core/star/context.py:171-211`；provider 不存在时抛 `ProviderNotFoundError`（`astrbot/core/exceptions.py:8`）→ adapter 转成 `ProviderError`（带可诊断文案，不把 provider id 之外的内部信息透给用户）。
- `text_chat_stream` 签名：`astrbot/core/provider/provider.py:139-153`；**流式最后一帧是完整结果**（同文件 docstring），因此流式路径要按“逐帧提取 + 结果去重”处理，而不是取最后一帧（大香蕉 astrbot_plugin_big_banana/core/providers/native.py:65-69 就是逐帧累积）。
- `image_urls` 只在**参考图**路径使用；无参考图时不要传空列表的语义差异（传 `[]` 与 `None` 等价，二者都安全）。

### 2.4 三级提取（`result_chain` → markdown → base64/URL）精确规则

输入是 `LLMResponse`（`astrbot/core/provider/entities.py:355-388`）。

| 级别 | 数据源 | 提取规则 | 产出 `ImageResult` 形态 |
| --- | --- | --- | --- |
| L1 | `response.result_chain.chain` 中的 `Image` 组件 | `ref = comp.url or comp.file`；**只有** `http(s)://` 与 `base64://` 前缀可接受 | `ImageResult(url=ref)` |
| L2 | `response.completion_text` 的 Markdown 图片 | 复用大香蕉的正则语义 `!\[.*?\]\((.*?)\)`，剥掉 `<...>` 包裹（`astrbot_plugin_big_banana/core/providers/utils.py:28-44`） | `data:image/...` → `ImageResult(url=<data url>)`；`http(s)` → `ImageResult(url=...)` |
| L3 | L1/L2 中携带 base64 的两种形态 | 统一转成 `base64://<payload>` 交给 `imago/core/network.py` 解码（`BASE64_URL` 正则 + 魔数强制）；**不接受**“无包裹的裸 base64 猜测”，避免把正文文本当图片 | `ImageResult(url="base64://<payload>")` |
| 文本 | 无图但有文本 | 追加进“无图文本”缓冲，供 `NoOutputError` 的诊断文案使用（不要独立成返回值） | — |

**关键设计点：让 `imago/core/network.py` 承担解码、体积、魔数与私网守卫，adapter 只做“引用搬运”。**
`materialize_result` → `fetch_reference`（`imago/core/network.py:210-233`、`172-207`）已实现对三类引用的统一处理：

- `data:image/...;base64,...`：正则 + 长度预判 + 魔数优先（`imago/core/network.py:180-196`）
- `base64://...`：`BASE64_URL` + 魔数强制识别 + 尺寸上限（`imago/core/network.py:58-76`、`197-198`）
- `http(s)://...`：手动逐跳重定向（≤5）+ **私网守卫** + http 域名解析后固定 IP 直连（`imago/core/network.py:19-20`、`121-169`、`79-118`）

因此 **adapter 内部不要自己 base64 解码、不要自己下载、不要自己判断图片格式**（否则等于把 `network.py` 的守卫复制一份并迟早偏离）。

**引用白名单（必须显式拒绝）**：只接受 `http://`、`https://`、`data:image/`、`base64://` 四类前缀；其余（裸相对/绝对本地路径、`file://`、其它 scheme）**一律跳过并记 debug 日志**。理由：`fetch_reference` 对非 http/data/base64 的输入按**本地文件**处理（`imago/core/network.py:201-207`），且没有根目录限制（无心智的读盘面）——大香蕉在这一点的处理本身就是一个不一致缺陷（§4 A7）。

### 2.5 私网守卫归属（明确边界，避免重复实现）

| 阶段 | 会不会下载 | 守卫 |
| --- | --- | --- |
| 节点尝试（`scheduler._generate`） | **不下载**。adapter 只返回引用 | 无（也不需要） |
| 结果落盘与发送（`main._finish_task` → `materialize_result`） | 下载 http(s) 引用 | `block_private=cfg.block_private_networks`（`imago/main.py:1195` 传参）→ `imago/core/network.py:104-107` 拒绝非公网地址 |

补充事实：`block_private_networks` **只**约束参考图/结果图的 URL 下载，**不**约束 provider 的 `base_url`；因此把节点 base_url 设成 `http://127.0.0.1:8317/v1beta` 或依赖 AstrBot 的本地 `cpa_Gemini` 都是合法的，不会被这条配置挡住（`imago/services/scheduler.py:320` 用共享 session 直连，aiohttp 不做私网校验）。冒烟测试正需要它保持 `true`。

### 2.6 错误映射表

| 触发 | 抛 | 上层行为 |
| --- | --- | --- |
| provider id 不存在 / AstrBot 抛 `ProviderNotFoundError` | `ProviderError("原生提供商 <id> 不存在")` | 记 `attempt_errors`，走下一模型/下一节点（`imago/services/scheduler.py:331-344`） |
| LLM 调用异常（网络/鉴权/上游 5xx） | `ProviderError`，文案走 `redact()`（`main._provider_error`，`imago/main.py:233-234`） | 同上 |
| 响应里没有可用图片引用 | `NoOutputError("原生提供商未返回图片数据")` | `imago/services/scheduler.py:333-334` 记忆 `no_output_error`，全部尝试结束后按 `NO_OUTPUT` 结算（避免按 failed 误退款，`imago/services/scheduler.py:360-370`） |
| 有响应但结构非法（chain 类型异常等） | `UnsupportedResponse` | 同 `ProviderError` 分支 |

---

## 3. 反代模板配置 schema 设计

### 3.1 大香蕉原语义 → imago 现有语义映射（结论表）

| 大香蕉字段（`_conf_schema.json` provider_template items） | imago 现状 | 结论 |
| --- | --- | --- |
| `keys: list`（多 key，每次请求 `random.shuffle`） | `api_keys: list`（多 key，按 `_key_indices` **顺序轮换**，`imago/services/scheduler.py:306-308`） | **保留 imago 机制，不移植随机打乱**。随机打乱让同一次故障无法复现、且把一个节点的 key 消耗变成随机；顺序轮换可预测（第 N 次尝试用第 N%K 个 key） |
| `fallback_order: int` + `enabled_as_default` | 无。用「列表顺序 + `primary_provider_id`」表达回退（`imago/services/scheduler.py:283-289`） | **新增可选 `fallback_order`（默认 0）**；语义：主节点固定第一，其余按 `(fallback_order, 列表索引)` 稳定排序。兼容现状（全部为 0 时等价于列表顺序） |
| `template/model` 覆写（`name.partition("/")` + `dataclasses.replace(model=...)`，`astrbot_plugin_big_banana/core/config/provider_config.py:64-71`） | 无 | **不移植该语法**（原因见 §4 A4：它会劫持 AstrBot 原生 id，而原生 id 本身含 `/`）。原生节点的模型覆写 = 原生节点自己的 `_conf_schema.json` 里的 `model` + `available_models`（`imago/services/scheduler.py:293-299` 已逐模型回退） |
| `provider_type: native` + `default_astr_providers: list[select_providers]` | 无原生路由 | **新增 `api_type="astrbot_native"` + `astrbot_provider_id` 字段**；用一次 `select_provider` 体验（`imago/_conf_schema.json:94-99` 已有同类用法），避免让用户手抄 id |
| `stream: bool` | 无 | **新增 `stream: bool`（默认 false）**：false→`context.llm_generate`，true→`text_chat_stream` |
| `enable_proxy: bool` | 无 | **HTTP 节点保留字段语义（留待后续接入 per-request proxy）；native 节点上该字段无消费者**，见 §3.3 |
| `max_images: int` | `reference_image_limit: int`（0=不限，主/备共用） | **沿用 imago 字段**；原生节点必须显式设置（大香蕉原生包装默认 6，见 §4 A5） |
| `capability: image_generation` | 无（imago 只有图片节点） | 不引入（YAGNI） |
| `response_modalities` / `system_prompt` / `imageConfig` | 无 | native 路由**不引入**：`system_prompt` 与 `responseModalities` 是 Gemini 原生协议的请求体字段，native 走 OpenAI 兼容协议，AstrBot provider 侧不会带这些字段。需要时用提示词表达（KISS） |

### 3.2 `_conf_schema.json` 新增片段（design 级，不是最终文件）

在 `providers.templates.provider.items` 内新增/修改：

```json
{
  "api_type": { "type": "string",
    "options": ["openai_image","openai_chat","gemini_official","dashscope_multimodal",
                "custom_endpoint","astrbot_native"],
    "default": "openai_image" },

  "astrbot_provider_id": { "type": "string", "default": "",
    "_special": "select_provider",
    "description": "原生 Provider（仅 astrbot_native 生效）",
    "hint": "选择 AstrBot 已配置的 Chat Provider（如 cpa_Gemini/web-gemini-image）。它自带上游密钥与代理，本节点无需填 base_url/api_keys。" },

  "stream": { "type": "bool", "default": false,
    "description": "流式请求（仅 astrbot_native 生效）",
    "hint": "开启后走 provider.text_chat_stream 逐帧累积图片；部分中转的流式图片帧不兼容，失败时请关闭。" },

  "enable_proxy": { "type": "bool", "default": false,
    "description": "启用代理（HTTP 节点）",
    "hint": "仅 HTTP 直连节点生效；astrbot_native 的代理由 AstrBot provider_source 自己的 proxy 字段决定，本开关对它无效。" },

  "fallback_order": { "type": "int", "default": 0,
    "description": "回退顺序（可选）",
    "hint": "数字小的先试；留 0 表示按列表顺序。主节点（WebUI 选定）永远第一个尝试。" }
}
```

同时 `items` 里 `base_url` 与 `api_keys` 的 `hint` 追加一句：“`astrbot_native` 节点两者都可留空”。

### 3.3 关于 `enable_proxy` 的显式裁决

给 native 节点挂 `enable_proxy` 会得到一个**没有消费者**的开关，理由是三条实证：

1. native 路由的 HTTP 由 AstrBot provider 发起，代理取 `provider_sources[].proxy`（`data/cmd_config.json:110` 该字段为 `""`），插件插不上手。
2. 本 adapter 自己的网络面为零（只发 data URL 给 AstrBot）。
3. 若把结果图下载也套代理，需要给 `imago/core/network.py` 的 `fetch_reference` 加 `proxy` 参数（现在没有），并新增/复用第二个 session——这是**新功能**，不是迁移所需，且本地反代 `127.0.0.1` 走代理反而会坏（大香蕉里 `enable_proxy: false` 正是这个原因，`data/config/astrbot_plugin_big_banana_config.json:28`）。

→ 结论：schema 保留字段以便与大香蕉配置对表，但**在 UI hint 里写明 native 无效**；实现阶段不写代理分支。若队长坚持要为 native 提供代理，请把它作为独立任务（改动面 = `imago/core/network.py` 签名 + session 管理 + 2 处调用）。

### 3.4 多 key 轮换语义（与冒烟相关）

- `api_keys` 解析：`imago/core/config.py:28-29`（每行一个，支持 list 或多行文本）。
- 轮换：`imago/services/scheduler.py:306-308`，索引按 **节点 id** 累加（`attempt.id`），因此同一节点的不同模型共享 key 序列；这是**有意**的（防单模型限流打爆单 key），保持不变。
- **空 `api_keys` 必须加护栏**（见 §4 B2）：`key_index % len(...)` 在空元组上抛 `ZeroDivisionError`，而 `astrbot_native` 天然不需要 key。

---

## 4. bug / 优化点清单（每条带 file:line 证据与复现推理）

严重度：**blocker** = 迁移前置必须修；**high** = 会静默改变出图结果/丢弃输入；**medium** = 会失败但可诊断；**low** = 卫生问题。
标记：**【已复现】** 本次实测；**【代码推理】** 读码 + 逻辑推演（未跑通链路）。

### 4.1 A 组：大香蕉（原插件的 bug，顺带修）

#### A1 【high】【已复现】`_body_context_cache` 无键缓存，同一实例的第二轮请求会串掉 prompt/参考图/模型派生配置

- 证据：`astrbot_plugin_big_banana/core/providers/gemini.py:25-26`（命中即返回）、`:106`（写缓存）；缓存体包含 6 个**逐请求**输入：`self.params.get("prompt")`（`:51`）、`self.image_list`（`:30-38`）、`self.provider_config.model`（`:91`）、`params/params_config.aspect_ratio`（`:84-86`）、`params/params_config.google_search`（`:93-95`）、`params/params_config.image_size`（`:98-100`）。
- 复现（`/tmp/repro_bb_cache.py`，同一 `GeminiProvider` 实例，第二轮把 prompt 换成“一只狗”、清空 `image_list`、覆写 `model=gemini-3-pro-image`）：

  ```text
  body1 prompt: 第一轮：一只猫 | parts: 2 | imageConfig: {'imageSize': '1K'}
  body2 prompt: 第一轮：一只猫 | parts: 2 | imageConfig: {'imageSize': '1K'}     ← 全是第一轮的
  同一对象? True
  ```

- **当前是否串缓存？否**（这是本条的准确结论）：`astrbot_plugin_big_banana/core/drawing/dispatcher.py:108-115` 每次 dispatch 都 `provider_cls(...)` 新建实例，且注释明写“这个实例化过程很轻，不复用单例”；视频侧同样（`astrbot_plugin_big_banana/core/video/dispatcher.py:106`）。`replace(model)` 也是每次查找新对象（`astrbot_plugin_big_banana/core/config/provider_config.py:71`），不会改写模板。
- 但缓存的价值为零（实例活不过一次请求），风险却是实打实的：任何一次“把实例提到循环外/跨任务复用/给 key 池加共享实例”的重构都会静默用第一轮的 prompt 与参考图出图——**这类 bug 不报错、只出错的图**，是最贵的一类。
- 建议修法（最小 diff，二选一）：① 删掉 `astrbot_plugin_big_banana/core/providers/gemini.py` 内的 3 处缓存读写（`:25-26`、`:106`），保留 `astrbot_plugin_big_banana/core/providers/standard.py:31` 的属性（另外 8 个 provider 仍在用它，不能全局删）；② 若坚持缓存，键化为 `(model, prompt, aspect_ratio, image_size, google_search, 参考图 bytes 的 sha256 元组)`。推荐 ①：重建成本只是一次 `parse_response_modalities` + dict 构造。
- 同类模式还有 8 处（`astrbot_plugin_big_banana/core/providers/agnes_images.py:30`、`astrbot_plugin_big_banana/core/providers/grok_images.py:22`、`astrbot_plugin_big_banana/core/providers/minimax_images.py:21`、`astrbot_plugin_big_banana/core/providers/openai_chat.py:24`、`astrbot_plugin_big_banana/core/providers/openai_images.py:92`、`astrbot_plugin_big_banana/core/providers/openai_responses.py:24`、`astrbot_plugin_big_banana/core/providers/siliconflow_images.py:23`、`astrbot_plugin_big_banana/core/providers/vertex_ai_anonymous.py:236`）——**本次不批量改**（超出最小 diff，且 `openai_images.py` 的缓存分支还叠加了 `image_list` 前置分支，语义不同），仅在测试里按现有方式复位。

#### A2 【medium】【已复现】`_body_context_cache` 只在 `initialize()` 里出现，绕过初始化即 `AttributeError`

- 证据：`astrbot_plugin_big_banana/core/providers/standard.py:31` 是唯一的初始化点；`astrbot_plugin_big_banana/core/providers/gemini.py:25` 无条件读它。直接构造实例而不 `await initialize()`：

  ```text
  有 _body_context_cache 属性? False
  AttributeError: 'GeminiProvider' object has no attribute '_body_context_cache'   <- astrbot_plugin_big_banana/core/providers/gemini.py:25
  ```

- 旁证：仓库自己的单测不得不手工补属性——`astrbot_plugin_big_banana/tests/test_gemini.py:29` `provider._body_context_cache = None`（`:30` 才调用 `_build_body_context`）。
- 影响：生产链路 `astrbot_plugin_big_banana/core/drawing/dispatcher.py:115` 调了 `initialize()`，所以只是潜在缺陷；但任何“轻量调用/复用/单测”都会直接炸，且报错信息与真实原因无关。
- 建议修法：与 A1 方案 ① 一并解决（不再读该属性即彻底消除）；若保留缓存，则改成类级默认 + `__init__` 初始化（`BaseProvider.__init__` 里置 `None`）。

#### A3 【medium】【已复现】`replace()` 浅拷贝：模型覆写副本与模板共享同一个 `keys` 列表和 `raw_config` 字典

- 证据：`astrbot_plugin_big_banana/core/config/provider_config.py:71` `replace(template_config, model=model.strip())`；`astrbot_plugin_big_banana/core/schemas/provider.py:19`（`keys: list[str]`）、`:35`（`raw_config: dict`）都是可变默认；实测身份比较：

  ```text
  覆写副本与模板共享 keys 对象? True 共享 raw_config 对象? True
  ```

- 当前是否串缓存？否——所有消费方都先拷贝（`astrbot_plugin_big_banana/core/providers/standard.py:58` `keys = list(...) or [""]`、`astrbot_plugin_big_banana/core/providers/zhipu_videos.py:34`、`astrbot_plugin_big_banana/core/providers/grok_videos.py:68`），`raw_config` 只读（`astrbot_plugin_big_banana/core/providers/gemini.py:42/78`、`astrbot_plugin_big_banana/core/providers/vertex_ai_anonymous.py:293/391`）。
- 风险：任何将来对 `provider_config.keys` 的原地修改（append/remove/shuffle 忘了 `list()`）会同时改到模板与**同一模板的所有模型覆写副本**。
- 建议修法：`get_provider_config` 返回前深拷贝两份可变字段（`replace(template_config, model=..., keys=list(...), raw_config=dict(...))`），一行；或把 `keys` 改成 `tuple`（改动面更大，不推荐）。

#### A4 【high】【已复现】模板名阴影会静默劫持原生 provider id（`template/model` 覆写语法的结构性问题）

- 证据：`astrbot_plugin_big_banana/core/config/provider_config.py:58-71` 先查模板名，再 `partition("/")` 走覆写；`astrbot_plugin_big_banana/core/drawing/dispatcher.py:124-145` 只在模板查不到时才回退原生。实测（`/tmp/repro_bb_order.py`，在配置里加一个 `name: "cpa_Gemini"` 的模板）：

  ```text
  阴影前 get_provider_config('cpa_Gemini/web-gemini-image') -> None        # 正确：回退原生
  阴影后 get_provider_config('cpa_Gemini/web-gemini-image')
      -> {provider_type: 'gemini', name: 'cpa_Gemini', model: 'web-gemini-image',
          base_url: 'http://1.2.3.4', keys: ['sk-shadow'], enabled: True}
  ```

- 复现推理：真实配置里原生 id 一定带 `/`（`data/config/astrbot_plugin_big_banana_config.json:52` = `cpa_Gemini/web-gemini-image`，`data/cmd_config.json:534` 同）。只要用户新增一个与“id 前缀”同名的模板（`cpa_Gemini`），原生路由会被替换成「模板 base_url + 模板 key + 覆写后的模型名」，**协议从 OpenAI 兼容悄悄换成 Gemini，鉴权从 provider_source 的 key 换成模板 key，全程只有一行 INFO**。
- 建议修法：原生判断前置（先 `get_provider_by_id`，命中且非模板名再退回模板），或对“形如 `X/Y` 且 `X` 不在 `provider_configs` 且 `Y` 不是合法模型名的调用”不做覆写。**KISS 取舍：imago_fusion 侧不做 `template/model` 语法（§3.1），这条只需在原插件保留现状时记录风险。**
- 迁移意义：这条正是我们**不要**把 `template/model` 覆写搬进 imago_fusion 的原因。

#### A5 【high】【代码推理 + 配置证据】原生路由的参考图被默认截断到 6 张（而模板写的是 14）

- 证据：`astrbot_plugin_big_banana/core/drawing/dispatcher.py:141-145` 包装原生 provider 时只填 `provider_type`/`name`，其余走默认；`astrbot_plugin_big_banana/core/schemas/provider.py:33` `max_images: int = 6`；截断逻辑 `astrbot_plugin_big_banana/core/drawing/dispatcher.py:59-70`（`max_images >= 0` 即生效）。对照模板显式配置 `max_images: 14`（`data/config/astrbot_plugin_big_banana_config.json:29`）。
- 复现推理：走默认原生节点（实测默认顺序就是它优先，见 §0.5）时给 10 张参考图 → 日志出现 `[BIG BANANA] 提供商 cpa_Gemini/web-gemini-image 限制最大图片数为 6，当前有 10 张图片，已截断为 6 张`（`astrbot_plugin_big_banana/core/drawing/dispatcher.py:66-69`）→ 用户第 7-10 张参考图被无声忽略；同一插件里模板节点能吃 14 张，行为不一致。
- 建议修法：原生包装时给出可配置上限（模板/`astrbot_plugin_big_banana/_conf_schema.json` 增加“原生节点参考图上限”，或直接复用 `params_config.max_images`）；imago_fusion 的自然对应 = 节点级 `reference_image_limit`。

#### A6 【medium】【代码推理】native 的 `stream` / `enable_proxy` 现在不可达，且 `use_proxy` 两处不一致

- 证据：`astrbot_plugin_big_banana/core/providers/native.py:24` 读 `self.provider_config.stream`、`:129` 读 `self.provider_config.enable_proxy`；而原生节点的 `ProviderConfig` 由 `astrbot_plugin_big_banana/core/drawing/dispatcher.py:141-145` 现造（`stream=False`、`enable_proxy=False`、`max_images=6`），`default_astr_providers` 是**纯 id 列表**（`astrbot_plugin_big_banana/_conf_schema.json` 中 `default_astr_providers` 为 `list` + `_special: select_providers`），没有任何配置面能把它置真 → `astrbot_plugin_big_banana/core/providers/native.py:54-79` 的流式分支在现有配置下是死代码。
- 附加不一致：`astrbot_plugin_big_banana/core/providers/native.py:99-106`（chain 图片分支）**没传** `use_proxy`，而 `:127-135`（markdown 分支）传了 `use_proxy=self.provider_config.enable_proxy` —— 同一个 provider 的两个下载分支策略不同。
- 建议修法：把 stream/proxy 提升为真实的节点级配置（imago_fusion 里就是 §3.2 的 `stream` 字段），并统一两个分支的下载参数。

#### A7 【medium】【代码推理】native 下载 provider 返回的本地引用时没有根目录限制（与同仓库另一处策略不一致）

- 证据：`astrbot_plugin_big_banana/core/providers/native.py:93` `image_ref = comp.url or comp.file`；`:99-106` 调 `downloader.fetch_image(image_ref, ...)` **未传** `allowed_local_roots`/`local_base_dir`；`astrbot_plugin_big_banana/core/client/downloader.py:105-107` 提供这两个参数，`:165-179` **仅在非 None 时**做根目录校验（否则任意绝对路径/`file://` 直读）。对比同一插件的用户参考图路径：`astrbot_plugin_big_banana/core/drawing/collector.py:416-417` 明确传了 `allowed_local_roots=tuple(allowed_roots)` 与 `local_base_dir=...`。
- 复现推理：provider 消息链返回 `Image(file="file:///etc/passwd")` 或任意绝对路径 → `fetch_image` 读盘 → `handle_image` 用 PIL 打开失败返回 None（不会外泄内容），但换成真实存在的图片路径就会**把宿主任意图片文件提交给上游模型**；配合“某些中转可以自定义消息链”的部署，这是一个路径越权面。
- 建议修法：native 侧只接受 `http(s)://`/`data:`/`base64://`/`file://` 且 `file://` 需落在 AstrBot 数据/临时目录内（imago_fusion 的 §2.4 白名单直接拒绝裸路径）。

#### A8 【medium】【代码推理】native 把完整 URL（可能含凭据）写进 WARNING 日志

- 证据：`astrbot_plugin_big_banana/core/providers/native.py:110` `logger.warning(f"[BIG BANANA] 无法处理图片：{image_ref[:256]}")`、`:139-141` 同类（markdown URL）。`image_ref` 可能是带查询参数的 URL（`?key=...`）或 `base64://` 图片数据本体。对照 imago 的做法：`imago/core/security.py:26` 把整条 URL 替换为 `<url>`。
- 复现推理：中转返回 `https://host/file?key=AIza...` 且下载失败（超时/403）→ 该 URL 的前 256 字符进 WARNING 日志 → 遥测/日志外发即凭据外泄。
- 建议修法：迁移侧统一走 `redact_debug`（imago_fusion 不做无脱敏日志）；原插件侧最小改动 = 日志只留 `image_ref[:16] + "…"` 或 URL 的 host。

#### A9 【low】【代码推理】markdown 图片引用全部下载失败时，模型返回的文字被丢弃

- 证据：`astrbot_plugin_big_banana/core/providers/native.py:142-143` 的文本兜底条件是 `not images and not base64_sources and not image_urls and completion_text`——只要有引用（哪怕全部下载失败，`:126-141`），`text_response_parts` 就为空；随后 `:32` 返回 `call_result.error_message or "响应中未包含图片数据"`，而 `_missing_image_result`（`astrbot_plugin_big_banana/core/providers/base.py:46-66`）在没有 `text_response_parts` 时只会给默认文案。
- 复现推理：中转返回 `![img](http://dead.example/x.png)` → 下载失败 → 用户看到“响应中未包含图片数据”，而模型其实说了“该图被安全策略拦截”之类的原因。
- 建议修法：`:142` 的条件去掉 `not base64_sources and not image_urls`，改为“只要有文本且没拿到图就留存文本”。

#### A10 【low】【代码推理】`_build_api_url` 的 URL 拼接对“带路径的 base_url”有多种猜测分支

- 证据：`astrbot_plugin_big_banana/core/providers/gemini.py:167-195` 依次判断 `:generateContent` 结尾 / `/models/{model}` 结尾 / `/models` 结尾 / `/v1`/`/v1beta` 结尾，否则拼 `/v1beta/models/{model}:{action}`。
- 影响：本次实测反代只认 `/v1beta/models/...`（`/models/...` 404），而 base_url 写 `http://127.0.0.1:8317` 时 `:195` 分支会拼成 `/v1beta/models/...`（正确）；但写 `http://127.0.0.1:8317/v1` 时 `:193-194` 会拼成 `/v1/models/...`（预期 404）。
- 建议修法：不改代码，只在迁移文档/节点 hint 里写明“本反代必须填 `.../v1beta` 或裸 origin”。

### 4.2 B 组：imago（迁移必须处理）

#### B1 【medium】【已复现】`_file_uri_with_key` 的 key 拼接有三类边界错误

- 证据：`imago/providers/gemini.py:12-21`，判据是**子串** `"key=" in uri`（`:19`）+ 朴素拼接 `f"{uri}{'&' if '?' in uri else '?'}key=..."`（`:21`）。
- 复现（`/tmp/repro_imago_key.py`，key 用假串 `AIzaTESTKEY123`）：

  ```text
  'https://relay.local/files/abc?monkey=1'
    -> 'https://relay.local/files/abc?monkey=1'                     # ① 假阳性：误判已带 key，不补 → 401
  'https://relay.local/files/abc#frag'
    -> 'https://relay.local/files/abc#frag?key=AIzaTESTKEY123'      # ② key 落进 fragment，永不作为查询参数发出 → 401
  'https://relay.local/files/abc?alt=media#frag'
    -> 'https://relay.local/files/abc?alt=media#frag&key=...'       # ② 同上（query 之后还有 fragment）
  'https://relay.local/files/abc?key'
    -> 'https://relay.local/files/abc?key&key=AIzaTESTKEY123'       # ③ 无 '=' 的 key 参数 → 重复参数，语义未定义
  'https://relay.local/files/abc?apiKey=zz'
    -> 'https://relay.local/files/abc?apiKey=zz&key=...'            # ③ 别名参数不识别 → 双 key
  ```

  正常形态（`?alt=media` / 无 query）行为正确，`:12-21` 的 docstring 也覆盖了“空 key 不追加”。
- 建议修法：用 `urllib.parse.urlsplit/parse_qsl/urlunsplit` 处理 query（判断“是否存在名为 `key` 的参数”而非子串），fragment 原样保留在最后；或最小改动 = 判据改 `re.search(r"[?&]key=", uri)` + 拼接前先剥 fragment 再拼回。
- 迁移要求：imago_fusion 必须带上修复后的版本（这条会直接影响 `fileData.fileUri` 结果的下载成功率）。

#### B2 【blocker】【代码推理】`load_config` 强制 `base_url` + `api_keys`，且 `scheduler` 对空 key 会除零

- 证据：`imago/core/config.py:65-67`
  ```python
  api_keys = _keys(item.get("api_keys", ""))
  if not provider_id or provider_id in seen or api_type not in API_TYPES or not base_url or not api_keys:
      continue        # ← 原生节点（无 base_url/无 key）会被静默丢弃
  ```
  `imago/services/scheduler.py:306-308` `key = attempt.api_keys[key_index % len(attempt.api_keys)]` → 空元组 `ZeroDivisionError`（该行在 `try` 之前，异常冒泡到 `_execute` 的兜底 `except Exception`，任务判 FAILED 而日志只有 `ZeroDivisionError`，不可诊断）。
- 复现推理：新增 `api_type=astrbot_native` 的节点后，用户按 §3.2 hint 留空 base_url/api_keys → 要么节点消失（当前逻辑），要么放宽后第一次出图抛除零。
- 建议修法（两处必须同时改）：
  1. `imago/core/config.py:66` 的分支对 `api_type == "astrbot_native"` 放宽：只要求 `provider_id` 唯一 + `astrbot_provider_id` 非空；
  2. `imago/services/scheduler.py:307` 改为安全取值（空列表 → `key = ""`）。
- 验收方式：`test/test_scheduler.py` 增一个“native 节点 + 空 api_keys 能进 `adapter.generate` 且不抛异常”的用例。

#### B3 【high】【代码推理】`api_type` 白名单在 4 个位置各自硬编码，漏改的失败模式各不相同

| 位置 | 内容 | 漏改后果 |
| --- | --- | --- |
| `imago/core/config.py:8` | `API_TYPES = {...5 项}` | 节点被 `:66` 静默丢弃（无日志） |
| `imago/providers/__init__.py:7-13` | `ADAPTERS = {...5 项}` | `imago/services/scheduler.py:305` `ADAPTERS[...]` **KeyError**（不在 try 内）→ 任务 FAILED |
| `imago/integrations/web_api.py:207` | 同 5 项的内联集合 | WebUI 保存被整体拒绝：`return _err(f"节点 {provider_id} 的接口类型无效")`（`:208`） |
| `imago/_conf_schema.json:27-33` | `api_type.options` | 管理页下拉里没有该类型，用户只能手填 |
- 附带：`imago/integrations/web_api.py:153` 的 `complete` 判据要求 `base_url` 与 `api_keys` 同时非空（`has_keys`）→ 原生节点会显示“不完整”，UI 会把用户往错误方向引导。
- 建议修法：`API_TYPES = set(ADAPTERS)` 单一事实来源；`imago/integrations/web_api.py:207` 改成查 `ADAPTERS`；`complete` 判据按 api_type 分支（native 只看 `astrbot_provider_id`）。

#### B4 【medium】【代码推理】`ImageResult.mime_type` 默认 `image/png` 且 `materialize_result` 对 data 分支不嗅探魔数

- 证据：`imago/core/models.py:107-113` `mime_type: str = "image/png"`；`imago/core/network.py:216-217` 直接采用 `result.mime_type` 决定落盘扩展名（`:229`）；只有 `fetch_reference` 路径（URL/base64://）才做魔数校验（`:190-196`、`:58-76`）。
- 复现推理：adapter 返回 `ImageResult(data=jpeg_bytes)` 而没显式给 `mime_type` → 落盘成 `*.png` 内容是 JPEG → 发送到平台时部分实现按扩展名/声明处理，可能被拒或显示异常。
- 建议修法：native adapter **统一返回 `url=`**（把 base64 交给 `fetch_reference` 的魔数校验），或返回 `data=` 时用现成的 `imago/core/network.py:31-41 detect_image_mime` 显式填 mime。

#### B5 【low】【代码推理】`redact()` 不掩码裸 `key=` 查询参数（与 B1 的 key 拼接联动）

- 证据：`imago/core/security.py:7` `SENSITIVE = re.compile(r"(?i)(api[_-]?key|authorization|token|secret|password)\s*[:=]\s*([^\s,;}]+)")` → 裸 `key=AIza...`（既不是 `api_key` 也不是 `apikey`）不被命中；`redact_debug`（`:23-29`）靠 `:26` 的 URL 整体替换兜底，**但 `_provider_error`/`_decorate_error` 用的是 `redact` 而非 `redact_debug`**（`imago/main.py:234`、`:237`）。
- 复现推理：若将来把带 `?key=` 的下载 URL 放进异常文案（例如把 `imago/core/network.py:153` 的 `参考图 HTTP {status}` 改成带 URL），该 key 会原样出现在日志与用户可见文案里。
- 建议修法：`SENSITIVE` 增补 `([?&](?:[a-z_]*key|access_token)=)` 分支，或规定“任何 URL 进日志前必须过 `redact_debug`”。

### 4.3 C 组：优化（非阻塞）

#### C1 【low】【代码推理】`_finish_task` 在循环内重复全量解析配置

- 证据：`imago/main.py:1195` 单行内两次 `load_config(self.raw_config)`，且位于 `for result in results:` 循环（`:1193`）中 → N 张图 = 2N 次全量解析（`imago/core/config.py:56-119` 会重建全部 `ProviderConfig` 元组）。该行同时超过 200 字符。
- 建议：循环前取一次 `cfg`（`:1229` 已经又取了一次，可直接复用）。
- 顺带（同区域）：`:1197` 的 `Image.fromURL(value)` 分支不可达——`materialize_result` 三条成功路径都返回 `Path`（`imago/core/network.py:211-215`、`:224-233`）；`:1198` 的 `value.stat()` 也据此写死了 Path。要么删分支，要么让 `materialize_result` 明确返回类型。

#### C2 【low】【代码推理】`imago/core/config.py:77-80` 缩进错位（可运行但易误读）

```python
77:            # 留空是合法配置（等同"不指定"：请求里不发送 size 字段），不做兜底替换。
78:        default_size=str(item.get("default_size", "1024x1024")).strip(),
79:            timeout=max(10, int(item.get("timeout", 180) or 180)),
```

- 括号内续行所以语法合法（`ast.parse` 通过），但 `default_size` 比兄弟参数少 4 空格。恰好是 B2 要改的同一个代码块，顺手对齐。

#### C3 【low】【代码推理】imago 自身版本号三处不一致

- 证据：`imago/main.py:73` `"1.2.0"` vs `imago/metadata.yaml:3` / `imago/__init__.py:3` `1.2.2`；运行日志用 1.2.2（`data/logs/astrbot.log:13791`）。
- 建议：imago_fusion 从一开始就三处一致（`1.0.0`）；原插件这处可作为独立小修（不阻塞迁移）。

#### C4 【info】【已复现】AstrBot 配置完整性检查会丢弃未在 schema 声明的 object 子键（对 schema 设计的约束）

- 证据：`astrbot/core/config/astrbot_config.py:93`（构造时 `check_config_integrity(default_config, conf, schema=schema)`）、`:241-246`（`conf` 中不在 `refer_conf` 的键被删除）+ `:257-258`（`conf.clear(); conf.update(new_conf)`）。`template_list` 走默认值分支（`:163-164`）**不逐项裁剪**。
- 由此得出的设计约束：新配置项必须放进 `providers.templates.provider.items`（template_list 内，原样保留）或显式声明；否则管理页保存一次就丢。
- 另：`imago/integrations/web_api.py:212-225` 是插件自己写回节点列表的地方，新增字段必须在这里一起落盘，否则 WebUI 保存会把它们抹掉（这是“schema 加了但保存后消失”的根因）。

---

## 5. 逐文件处置表（改什么 / 原样复制什么）

### 5.1 原样复制（零改动：`core/` 7 个 + `providers/` 4 个 + `services/` 3 个 + `test/` 3 个 + `pages/webui/` 4 个 + `requirements.txt`）

`core/__init__.py`、`core/commands.py`、`core/dedup.py`、`core/errors.py`、`core/network.py`、`core/prompting.py`、`core/references.py`、`providers/dashscope.py`、`providers/openai_chat.py`、`providers/openai_image.py`、`providers/custom.py`、`services/__init__.py`、`services/persona_store.py`、`services/quota_store.py`、`pages/webui/*`（4 文件）、`test/test_core.py`、`test/test_prompting.py`、`test/test_reference_passthrough.py`、`requirements.txt`。

理由：这些文件与“原生路由”零耦合；`core/prompting.py`（522 行，风格/副脑/Persona 主体）和 `services/*`（额度、Persona 素材）是 imago 的核心资产，重写等于自毁。

### 5.2 需要改动（精确到点）

| 文件 | 改动点 | 关联 |
| --- | --- | --- |
| `imago/core/config.py:8` | `API_TYPES` 增 `"astrbot_native"`（或改为 `set(ADAPTERS)`） | B3 |
| `imago/core/config.py:59-67` | native 节点放宽 `base_url`/`api_keys` 必填；解析 `astrbot_provider_id`/`stream`/`enable_proxy`/`fallback_order` | B2 |
| `imago/core/config.py:77-80` | 缩进对齐（顺手） | C2 |
| `imago/core/models.py:50-61` | `ProviderConfig` 增 `astrbot_provider_id: str = ""`、`stream: bool = False`、`enable_proxy: bool = False`、`fallback_order: int = 0` | §3 |
| `imago/providers/base.py:11-16` | `__init__(self, config, *, context=None)`；`parse_common`/`_error_detail`/`response_json` 不动 | §2.1 |
| `imago/providers/__init__.py:7-13` | `ADAPTERS` 增 `"astrbot_native": NativeAdapter` | B3 |
| `imago/providers/gemini.py:12-21` | 修 `_file_uri_with_key`（子串判据 + fragment） | B1 |
| `imago/services/scheduler.py:65` | 构造参数增 `context=None` | §2.1 |
| `imago/services/scheduler.py:283-308` | `fallback_order` 稳定排序（可选）；`:305` 传 `context`；`:307` 空 `api_keys` 护栏 | §3.1/B2 |
| `imago/main.py:73` | `@register("imago_fusion", ...)` + 版本 1.0.0 | C3 |
| `imago/main.py:98-110` | 类名 `ImagoFusion`；`StarTools.get_data_dir("imago_fusion")`（**数据目录独立**，见 §7 未决问题 2） | — |
| `imago/main.py:142-143` | scheduler 传 `context=self.context` | §2.1 |
| `imago/main.py:1193-1198` | 循环前取 `cfg` 一次；删/改死分支 | C1 |
| `imago/integrations/web_api.py:151-153` | `complete` 判据按 api_type 分支 | B3 |
| `imago/integrations/web_api.py:207` | 白名单改查 `ADAPTERS` | B3 |
| `imago/integrations/web_api.py:212-225` | 落盘新增 4 字段 | C4 |
| `imago/_conf_schema.json:24-33` | `api_type.options` 增项 | B3 |
| `imago/_conf_schema.json:37-48` | `base_url`/`api_keys` hint 补“native 可留空”；新增 4 个 items | §3.2 |
| `imago/metadata.yaml` / `imago/__init__.py` | name/version 三处一致（含 `imago/main.py:73`） | C3 |

### 5.3 新增文件（3）

`providers/native.py`（§2 契约）、`test/test_native_adapter.py`、`README.md`。

---

## 6. 冒烟测试基线（复用现有真实反代配置，零新建配置）

### 6.1 两条路径都要打（大香蕉默认顺序是原生优先，§0.5）

| # | 路径 | 节点配置 | 期望 |
| --- | --- | --- | --- |
| S1 | **原生路由（新）** | `api_type=astrbot_native`，`astrbot_provider_id=cpa_Gemini/web-gemini-image`，`stream=false` | `llm_generate` 返回的 `result_chain` 无 Image（OpenAI 兼容源只产出 `Plain`，见 `astrbot/core/provider/sources/openai_source.py:862`）→ 走 L2 markdown 提取 → 拿到 1 张图 → 任务 `SUCCEEDED` |
| S2 | 原生路由流式 | 同上 + `stream=true` | 逐帧累积不重复计图；末帧完整结果被正确处理 |
| S3 | HTTP 直连 Gemini（imago 既有能力，验证不回归） | `api_type=gemini_official`，`base_url=http://127.0.0.1:8317/v1beta`，`model=gemini-3.1-flash-image`，`api_keys=[sk-1d534***]` | `{base}/models/{model}:generateContent?key=...`（`imago/providers/gemini.py:29-30`）200 且响应含 `inlineData`/`fileData` |
| S4 | 原生路由 + 参考图 | S1 + 2 张参考图（走 Persona 或消息附图） | 请求体里参考图以 `data:image/...` 形式进 AstrBot（`astrbot/core/utils/media_utils.py:1455-1478`）；不出现“截断为 6 张”类日志 |

### 6.2 冒烟脚本的硬约束

1. 读配置必须 `utf-8-sig`（§0.4）。
2. 报告中 key 一律掩码 `sk-1d534***`（真实 key 出现在 `data/cmd_config.json:106-108` 与 `data/config/astrbot_plugin_big_banana_config.json:20-22`）。
3. 无 key 探针可先跑（§0.3）确认反代在线：`/v1/models`、`/v1/chat/completions`、`/v1beta/models/<m>:generateContent` 都应 401 `Missing API key`。
4. `storage_config.block_private_networks` 保持 `true`（冒烟不要求关它，见 §2.5）。
5. **命令名冲突**：imago 注册了 `imago` 命令组、`画`、`拍照`，以及 LLM 工具 `generate_image`/`generate_persona_image`（`imago/main.py:1442`、`:1598`、`:1602`、`:1654`、`:1691`）。imago_fusion 若沿用同名，两个插件同时启用会出现双份 handler/双份任务。冒烟时必须**只启用一个**（或给 imago_fusion 换命令名）。这条要写进部署说明，否则测试结果不可信。

---

## 7. 非目标与显式边界（KISS）

1. **不移植** `StandardProvider` 的直连 HTTP 栈、`@provider_type` 注册表、`smart_retry`、随机 key 打乱、`max_refresh/recaptcha` 系列（无需求、与 imago adapter 重复）。
2. **不移植** 大香蕉的绘图管线（parser/collector/hosting/saver/tasks/cooldown/whitelist/图床/视频）——imago 的调度、额度、发送、fallback 已经覆盖，且是本次要保留的骨架。
3. **不引入** `template/model` 覆写语法（§4 A4 证明它会劫持含 `/` 的原生 id）。
4. **不新增** 为 native 服务的代理层（§3.3）。
5. **不批量修** 其余 8 个 provider 的同类缓存写法（§4 A1 末段），保持在最小 diff 内。
6. 本插件**不**改 `data/cmd_config.json`（AstrBot 主配置与 provider 列表属宿主，不在插件职责内）；插件启用/停用由用户在管理台操作。

## 8. 需队长/用户裁决的 3 个未决问题

1. **命令名策略**：imago_fusion 沿用 `画`/`拍照`/`generate_image`（要求停用 imago，语义等于“升级替换”），还是改用新命令名（可共存但要改文档与提示词）？冒烟脚本的预期结果依赖这个选择（§6.2 第 5 条）。
2. **数据目录**：`StarTools.get_data_dir("imago_fusion")`（独立、干净）还是复用 `imago`（继承已有 Persona 素材/额度状态）？实测 `data/plugin_data/imago` 只有两个空目录（`persona_references/`、`task_cache/`，0 字节、无文件），迁移成本≈0，建议**独立目录**并留一句“如需继承，手工复制该目录”。
3. **是否顺手修 A3/A4/A6/A7/A8**（超出“顺带修 bug”的最小范围）：A1/A2 是本次诊断出的核心 bug（且 A1 与 `replace(model)` 的关系需要一份结论），建议必修；其余建议只写进报告不落代码，避免碰动原插件 13.9k 行的稳定面。

---

### 附：本次审计用到的复现命令（可复跑）

```bash
# A1/A2/A3/A4：大香蕉缓存与 provider 解析
/root/AstrBot/.venv/bin/python /tmp/repro_bb_cache.py        # body1 == body2（第一轮 prompt 串到第二轮）
/root/AstrBot/.venv/bin/python /tmp/repro_bb_cache_attr.py   # AttributeError: _body_context_cache
/root/AstrBot/.venv/bin/python /tmp/repro_bb_order.py        # 默认顺序 + 模板阴影 + replace 共享对象
# B1：imago Files API key 拼接边界
/root/AstrBot/.venv/bin/python /tmp/repro_imago_key.py
# 反代在线基线（不消耗额度）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8317/v1/models
curl -s -o /dev/null -w '%{http_code}\n' -X POST -d '{}' \
  http://127.0.0.1:8317/v1beta/models/gemini-3.1-flash-image:generateContent
```
