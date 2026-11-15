# IMAGO·FUSION

IMAGO·FUSION 是 **IMAGO·映相** 骨架与 **大香蕉（astrbot_plugin_big_banana）AstrBot 原生 Provider 桥接** 的融合插件：保留映相的提示词优化、Persona 出镜、绘图额度、多节点 fallback 与素材管理，新增「走 AstrBot 已配置 Provider（反代 / 网页端模型）出图」的原生路由。

- 版本：`1.4.1`（`metadata.yaml` / `__init__.py` / `main.py` 的 `@register` / 本行**四处一致**；更新内容见 `CHANGELOG.md` 顶部的 1.4.1 节）
- AstrBot：`>=4.16,<5`
- 依赖：`aiohttp>=3.9,<4`（**不新增依赖**：参考图压缩用的是 AstrBot 运行时已带的 Pillow，实测 12.2.0；缺失时自动降级为原图直发）

运行数据写入 AstrBot 分配的 `plugin_data/imago_fusion/`，**不读写** `plugin_data/imago/`（如需继承原插件的 Persona 素材，手工复制该目录）。

## 与 IMAGO·映相 的关系

| 项 | IMAGO·映相 (`imago`) | IMAGO·FUSION (`imago_fusion`) |
| --- | --- | --- |
| 命令组 | `/imago`、`/画`、`/拍照` | `/imago_fusion`、`/f画`、`/f拍照` |
| LLM 工具 | `generate_image`、`generate_persona_image` | `generate_fusion_image`、`generate_fusion_persona_image` |
| Web API | `/imago/*` | `/imago_fusion/*` |
| 数据目录 | `plugin_data/imago/` | `plugin_data/imago_fusion/` |
| 节点类型 | 5 种 HTTP 直连 | 5 种 HTTP 直连 + `astrbot_native`（原生 Provider） |
| 出图路径 | HTTP 直连 | HTTP 直连 + AstrBot provider 路由（`llm_generate` / `text_chat_stream`） |

两套命令、工具、路由、数据目录**全部不重名**，可以同时启用互不干扰；但两者语义高度重合，一般只需要启用其中一个。

## 安装

把 `imago_fusion` 放入 AstrBot 插件目录并加载，然后在管理台启用。

## 节点配置

### 1. 原生路由节点（反代 / 网页端模型）

先在 AstrBot「服务提供商」里配好一个 Chat Provider（例如 `cpa_Gemini/web-gemini-image`，它自带上游地址与密钥），再在插件里新增节点：

| 字段 | 值 |
| --- | --- |
| 节点 ID | 任意唯一标识，如 `native_web_gemini` |
| 接口类型 | `astrbot_native` |
| 原生 Provider | `cpa_Gemini/web-gemini-image`（下拉选择，`_special: select_provider`） |
| 流式 | 默认关闭；开启后走 `provider.text_chat_stream` |
| Base URL / API Key | **留空**（鉴权与地址都由 AstrBot Provider 决定） |
| 参考图总上限 | 建议显式设置，如 `14`；`0` 表示不限制 |

原生路由的图片提取分两级（与设计契约 §2.4 一致）：

1. `LLMResponse.result_chain` 里的 `Image` 组件（`url` 优先，其次 `file`）；
2. 正文里的 Markdown 图片链接（`![alt](src)`，支持 `<...>` 包裹）。

只接受 `http://`、`https://`、`data:image/`、`base64://` 四类引用；裸本地路径与 `file://` 一律跳过（避免 provider 消息链读宿主文件）。**适配器不解码、不下载、不判断格式**：引用统一交给 `core/network.py` 处理，体积上限、魔数校验与私网守卫都在那里。

**出图张数（`count`）语义**：单次聊天调用通常只出 1 张图，因此适配器会**循环调用直至凑满 `count` 张不同引用，最多 4 次**；若 AstrBot Provider id 不存在，**立即中断本轮并让位给下一个节点 fallback**（错误的 id 重试也不会变对）。单次调用失败只记日志并继续；节点部分成功时返回已拿到的图片，不把整个任务判失败。

### 2. HTTP 直连节点

| 类型 | 地址示例 | 说明 |
| --- | --- | --- |
| `openai_image` | `https://api.example.com/v1` | images API / multipart |
| `openai_chat` | `https://api.example.com/v1` | chat/completions |
| `gemini_official` | `http://127.0.0.1:8317/v1beta` | generateContent；**本地反代必须带 `/v1beta`**，否则 404 |
| `dashscope_multimodal` | 百炼完整 generation URL | MultiModal |
| `custom_endpoint` | 自定义完整 POST URL | 插件自定义 JSON |

- `api_keys` 支持逐行多个 Key，节点内**按调用顺序轮换**（不随机打乱），第 N 次尝试用第 N%K 个 Key。
- `fallback_order`：数字小的先试，同值按列表顺序；WebUI 选定的主节点永远第一个尝试。
- `enable_proxy`：仅 HTTP 直连节点生效，走全局「出站代理地址」；原生路由节点该开关无效（代理由 AstrBot provider_source 决定）。本地反代 `127.0.0.1` 不要开。

调用顺序：

```text
主节点默认模型 → 主节点备选模型 → 其他节点默认模型 → 其他节点备选模型
```

### 3. `模板名/模型名` 覆写（默认关闭，**不推荐开启**）

`template_override` 开启后，主节点可以写成 `模板名/模型名` 覆写该模板的模型。判据要求 `/` 前**确实是已配置的模板名**，因此 AstrBot 原生 id（`cpa_Gemini/web-gemini-image`）不会被误当作覆写语法 —— 这是与大香蕉原实现的关键差异：原实现先按模板名查表再 `partition("/")`，只要用户建了一个与原生 id 前缀同名的模板，原生路由就会被静默换成「模板 base_url + 模板 key + 覆写模型名」。

**不推荐开启**：该语法与 AstrBot 原生 provider id 形态相同（都含 `/`），一旦命名碰撞就会改变主节点的实际去向。插件已加碰撞守卫——若 `/` 前的模板名与某个 `astrbot_native` 节点 id **完全相同**，会记一条 WARNING 并**跳过覆写**，把引用留给原生节点解析（例如模板名写成 `cpa_Gemini` 而原生节点 id 就是 `cpa_Gemini`）。守卫只覆盖完全同名的情况，语义上仍建议用节点的 `available_models` 做模型回退，而不是依赖本开关。

## 参考图压缩（默认开启，`reference_compress_config`，7 键）

**为什么**：发往绘图节点的参考图决定了上传耗时与失败率。1.4.1 起，凡是**发往 provider 的参考图副本**都会先压缩一次；本地 `inputs/` 里的原素材、出图结果、增强/放大产物**完全不参与**压缩。

**覆盖面（同一个入口，七类来源一视同仁）**：①消息链里的 `Image` 组件；②`data:image/...;base64`；③`base64://`；④`http(s)` 链接（含引用消息与正文 Markdown 链接）；⑤引用消息里的图片（deferred 解析）；⑥后台 deferred 的链接；⑦Persona 参考图（含 `ref-upload` 上传的素材）。压缩发生在「参考图全部就绪之后、第一次请求节点之前」，每任务只算一次，重试与切换节点不会重复有损。

| 键 | 默认 | 限制 | 说明 |
| --- | --- | --- | --- |
| `enable` | **true** | bool | 关闭后**零解码、零新增文件、零新增日志**，发送字节与 1.4.0 逐字节一致（超时修复与回吐守卫**不受影响**） |
| `max_edge` | 1536 | 0–4096 | 最长边上限，**只缩不放**；`0` = 完全不缩放（含未达标后的缩边轮次） |
| `jpeg_quality` | 85 | 40–95 | JPEG 起始质量；未达标按 10 递减，下限 40 |
| `target_kb` | 128 | 32–2048 | 目标体积，**1 KB = 1024 字节** |
| `min_bytes` | 32768 | 0–1048576 | 小于该字节数直接原样发送（零解码）；`0` = 全部参与 |
| `keep_alpha` | false | bool | 关闭时透明区域合成白底再按 JPEG 输出；开启时保留 alpha 并输出 PNG |
| `max_pixels` | 40000000 | 1000000–100000000 | 超过该像素数不参与压缩（不解码，避免超大图耗尽内存） |

**算法**：先看体积（低于 `min_bytes` 不动）→ 只读图片头取尺寸（超过 `max_pixels` 直接不解码就放弃）→ 解码首帧 + EXIF 方向归一 → 透明处理 → 长边缩放 → 质量阶梯。最多 9 次编码（3 个质量档 × 3 个尺寸档），达标即停；**最终只接受"比原图更小"的候选**，否则原样发送（`no_gain`）；达不到 `target_kb` 时保留最小候选并标 `target_unmet`，不会为了达标把图压花。

**产物与命名**：默认输出 `image/jpeg`，文件名后缀随之改成 `.jpg`；`keep_alpha=true` 且确实带 alpha 时输出 `image/png` + `.png`。压缩只影响发给节点的副本，`task.json` 的 `inputs[]` 仍是**原始字节与原始 sha256**。

**诊断（不含图片内容 / URL / base64）**：

- INFO：`参考图压缩 in=<字节>B out=<字节>B <长边>px q<质量> <耗时>ms`（未达标追加 ` target_unmet`）
- DEBUG：`参考图压缩 skip reason=<under_min_bytes|too_many_pixels|no_gain> bytes=<n>`
- WARNING（每任务至多一条）：`参考图压缩失败，回退原图: <异常类型与消息>`；Pillow 不可用时为 `参考图压缩不可用：Pillow 未安装，参考图按原样发送`
- `task.json` 的 `reference_compress` 记 `in_bytes / out_bytes / compressed / target_unmet / skipped / failed` 的有界摘要；每次尝试实际发出的字节数记在运行期的 `task.runtime["attempt_meta"]["references"][]`。

**体积口径**：`storage_config.max_upload_mb` 是**入站**上限，按 1 MB = 1024×1024 字节算，默认 20 MB = **20971520 字节**；压缩发生在该判定**之后**，所以不会把一张超限的入站图片"救回来"。

## 回吐守卫（默认开启，无配置键）

模型偶尔会把参考图**原样**当成"成品"返回（反代 / 网页端模型常见）。1.4.1 在"成品图已落盘、尚未增强/放大"的位置加了一道闸门：拿这张成品与**本次成功尝试实际发出去的那组参考图**（含压缩后的副本与被采样的 Persona 图）比对，命中即丢弃这张"成品"。

**命中判据（精确，不近似）**：

1. 字节完全相同；或
2. EXIF 方向归一后**同尺寸**，且解码出的 RGBA 像素**逐像素完全相等**。

不做感知哈希、灰度缩略图或"相似度"阈值；任一侧解码失败、超出比较上限（4000 万像素 / 64 MiB）、或取不到成功尝试的快照时，**一律放行**并只记 `unknown`。

**结果分类**：全部被拦 → `NO_OUTPUT` + `no_change`，用户收到固定文案「这次没有生成新图，模型把参考图原样送了回来。请重试或换个节点。」；部分被拦 → `PARTIAL_SUCCESS`，只发有效图；未命中 → 照旧。**不自动重试、不额外调用节点**（零新增费用），被拦的 provider 原件保留在 `outputs/`（但不进 `task.json` 的 `outputs[]`）。

**诊断**：INFO `回吐闸 检查=<n> 拦下=<m> 放行=<k>`；`task.json` 的 `echo_guard` 记 `checked / blocked / unknown / no_change` 与被拦文件的相对名（不含图片内容）。

## 三层超时（1.4.1 起语义分开）

| 层 | 位置 | 默认 | 到期结果 |
| --- | --- | --- | --- |
| L1 节点 | `providers[].timeout`（每节点） | 180 s | 该节点判 `ProviderTimeout`，记进 `attempts` 后**继续 fallback**，任务不结束 |
| L2 任务 | `task_config.generation_timeout`（从任务创建起算） | 300 s | 任务 `TIMED_OUT`，`errors` 必有 `timeout`，`runtime.last_provider_error=任务超时`，**不退款**（额度口径不变） |
| L3 上传窗口 | 内部常量 | ≈15 s | 只影响单张参考图上传；超出即按该节点失败处理 |

L1 到期**不会**再被结算成"任务超时"（1.4.0 曾出现"单节点 117 s 超时 → 任务判 timed_out 且 errors 为空"）；反过来，L2 真到期也不会被误判成节点失败。取消（`CANCELLED`）保持原样向上传播并退款。适配器网络异常额外记录 `phase`（`connect` / `upload` / `wait_response` / `read_body` / `unknown`）、`elapsed`、`up_bytes`、`timeout`，其中 `up_bytes` 来自 aiohttp 分片回调的真实计数，**不按图片大小猜**。

## 图像清晰度增强（可选，**默认关闭**）

出图成功后，把每张成品图送**百度「图像清晰度增强」**（去噪 + 优化纹理细节，**输出尺寸与输入相同**，**不是**放大）后再发送到群聊。配置在插件配置页的「图像清晰度增强（百度）」组（`enhance_config`）。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enable` | **false** | 总开关。开启后**每张成品图**多一次百度调用（按次计费） |
| `api_key` / `secret_key` | 空 | 「图像增强与特效」应用的两把 key；任一为空视为未配置，**不发任何请求** |
| `max_image_mb` | 10 | 本地预检的 **base64** 上限（百度硬限 10M；原图上限 = 10 MiB × 3/4 = 7 864 320 B） |
| `max_edge` | 5000 | 本地预检的**最长边**上限（px） |
| `max_aspect` | 4.0 | 本地预检的**长宽比**上限（长边 ÷ 短边，恰好 4:1 允许） |
| `timeout_seconds` | 20 | 单张超时（同时受任务总超时约束，剩余预算不足会自动跳过） |
| `enable_proxy` | false | 增强请求走插件顶层的「出站代理地址」；百度是境内公网，一般不需要 |

最短边 ≥10px 是官方硬下限，作为常量实现（`core/enhance.MIN_EDGE_PX`），**不占配置键**——所以本组是 8 键。

- **端点**：`https://aip.baidubce.com/rest/2.0/image-process/v1/image_definition_enhance`（`access_token` 走查询参数，表单体 `image=<base64>`，不带图片头）。鉴权：`oauth/2.0/token` 用两把 key 换 `access_token`（30 天有效，内存缓存，插件重载后重取一次）。
- **限制**：base64 ≤ 10M（原图约 8M 以内）、最短边 ≥ 10px、最长边 ≤ 5000px、长宽比 ≤ 4:1、支持 jpg/png/bmp。超限在本地先跳过（服务端超限也会计费），并记一条 DEBUG（`reason=` 见下）。
- **失败降级**：增强失败、超时、超限、额度用尽、应用未勾选该能力（`error_code=6`）等情况**一律发送原图**，任务仍判成功、额度结算不变，日志只留一条 WARNING（每类一次）。增强统计只写 `task.runtime["enhance"]`。
- **原图保留**：增强图另存为 `task_cache/<任务 ID>/outputs/` 下的新文件（命名规则与结果图落盘一致：uuid hex + 按魔数定的后缀），原图仍在同目录（对账/回滚用，`task.json` 的 `outputs` 记的是增强后的文件，缓存清理按目录整体进行），缓存上限沿用 `storage_config.temp_cache_mb`。
- **本地跳过的 `reason`**（不是失败、不花钱）：`empty` / `format`（webp/gif/未知）/ `too_large` / `edge_too_short` / `edge_too_long` / `aspect_too_wide` / `budget` / `read_failed` / `output_oversize`。
- **零副作用**：`enable=false` 时出图链路与 **v1.0.0** 逐字节等价——零网络、零新文件、零新增日志（当前版本见文首「版本」行）。

> 注意：上面这一级是**图像清晰度增强**（`image_definition_enhance`：尺寸不变、只变清晰），**不是**「图像无损放大」——后者是 v1.2.0 新增的**另一级**，见下一节。两者在百度控制台**分别勾选**；没勾当前接口时调用返回 `error_code=6`，插件会降级发原图。

## 图像无损放大（可选，**默认关闭**，与增强可叠加）

出图成功、并走完（可选的）清晰度增强之后，再把成品图送**百度「图像无损放大」**（`image_quality_enhance`）——**长宽方向各放大两倍**（面积 ×4），然后才发送到群聊。配置在插件配置页的「图像无损放大（百度）」组（`upscale_config`，**7 键**）。

**两级顺序（写死、不可交换）**：

```text
出图结果 → 落盘 → [第 1 级] 图像清晰度增强（尺寸不变） → [第 2 级] 图像无损放大（2×） → 发送
```

先增强后放大：反过来的话是「先 2× 再送增强」，等于把 4 倍像素量喂给增强（费用与耗时都更差），而且放大后的图会顶爆增强接口的 5000px 上限。**每一级各判自己的限值**（判据是「该级实际要上传的字节」），放大拿的是**增强产物**（增强没成功/没开时是原图）。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enable` | **false** | 总开关。开启后**每张成品图**多一次百度调用（按次计费） |
| `api_key` / `secret_key` | 空 | 「图像增强与特效」应用的两把 key；任一为空视为未配置，**不发任何请求** |
| `max_image_mb` | 4 | 本地预检的 **base64** 上限（百度硬限 4M ⇒ 原图上限 = 4 MiB × 3/4 = **3 145 728 B**）；范围 1-4，**调不高** |
| `max_pixels` | 4000000 | 本地预检的**长宽乘积**上限（百度要求 ≤ 2000×2000 = 4 000 000 px）；`4000×1000` 合法、`2001×2000` 超限 |
| `timeout_seconds` | 20 | 单张超时（同时受任务总超时约束，剩余预算不足会自动跳过） |
| `enable_proxy` | false | 放大请求走插件顶层的「出站代理地址」；百度是境内公网，一般不需要 |

最短边 16px 是**本地省钱守卫**（官方该接口没有最短边要求），作为常量实现（`core/upscale.UPSCALE_MIN_EDGE_PX`），**不占配置键**——所以本组是 7 键。

- **端点**：`https://aip.baidubce.com/rest/2.0/image-process/v1/image_quality_enhance`（`access_token` 走查询参数，表单体 `image=<base64>`，不带图片头）。鉴权与增强同产品：`oauth/2.0/token` 换 `access_token`，内存缓存（30 天 − 5 分钟余量），`110/111/100/HTTP 401` 失效重取 + 恰好重试一次；**缓存与增强互不共享**（同应用时多一次 oauth 调用 / 30 天，换两级彻底解耦）。
- **限制**：base64 ≤ 4M、长宽**乘积** ≤ 2000×2000、支持 jpg/png/bmp；另有本地最短边 16px 守卫。超限在本地先跳过（服务端超限也会计费），并记一条 DEBUG。
- **失败降级（与增强同口径，但级间互不牵连）**：放大失败、超时、超限、额度用尽、应用未勾选该能力（`error_code=6`）等情况**一律保留上一级结果**（增强图或原图，**不回退**到原图重试），任务仍判成功、额度结算不变，日志只留一条 WARNING（每类一次，前缀 `放大 失败：`）。统计只写 `task.runtime["upscale"]`（`attempted/succeeded/skipped/failures/circuit_open/log_id` 六个字段，不落 `task.json`）。
- **两套熔断互相独立**：放大连续遇到 token/permission/quota/transient 或单图超时会**只**熔断本任务的放大（剩余图不再放大、只保留上一级产物），增强那一级继续照跑；反之亦然。
- **产物**：放大图另存为 `task_cache/<任务 ID>/outputs/` 下的新文件（uuid hex + 按响应魔数定的后缀），原图与增强图都保留（count=1 时目录里最多 3 个文件）；`task.json` 的 `outputs` 只登记**最终交付**的那一个。响应字节**原样落盘**：插件不缩放、不裁剪、不转码，`2×` 是服务端语义。
- **本地跳过的 `reason`**（不是失败、不花钱）：`format` / `too_large` / `pixels_too_large` / `edge_too_short` / `budget` / `read_failed` / `output_oversize`（**7 条，与增强的 9 条互不冒充**）。
- **零副作用**：`enable=false` 时出图链路与 **v1.1.0** 逐字节等价——零网络、零新文件、零新增日志。
- **日志口径**：每任务一条 `放大 成功=N 跳过=M 失败=K`（**独立成行**，紧跟 `增强 成功=…` 那一行之后；既有增强行的格式一个字节都没改）。判「有没有真的走放大」就抓这一行。

> **上线前必须做**：登录百度智能云控制台 →「图像增强与特效」→ 找到 `upscale_config.api_key` 所属的那个应用 → **编辑应用** → 勾选 **「图像无损放大」**（可以和「图像清晰度增强」同时勾选，两项互不排斥）→ 保存 → 等 `access_token` 缓存自然过期（最长 30 天）或重载插件一次。**没勾选时每次调用都返回 `error_code=6`**，日志会出现 `放大 失败：控制台应用未勾选「图像无损放大」（error_code=6）…`——那是控制台没勾，**不是插件缺陷**（勾了增强 ≠ 勾了放大）。

## 参考图关系声明注入开关（`task_config.inject_reference_note`，默认开启）

消息附图/引用图等**显式参考图**存在时，插件会在最终 prompt 末尾追加一段低优先级的英文关系声明（要求图片模型把参考图当作**源素材**、保持主体与构图一致），人设固定图并存时还会补一段「前 N 张是用户参考、其余是人设身份」的位置说明。

这个行为现在可以在「后台任务」组里关掉：`task_config.inject_reference_note`（**bool，默认 `true` = 与 v1.1.0 逐字节相同，无需迁移**）。关闭后**两段一起消失**，最终 prompt 与优化后的画面描述**逐字节相等**——不留空行、不删既有文本（若模型自己把类似措辞写进了画面描述，那属于模型产物，本插件不负责删除，见 `core/prompting.py` 的说明）。参考图计数（`task.runtime["current_*_reference_count"]`）开关真假都照记。

配置每次现算，改完不必重启插件。

## 参考图文字降级（`reference_fallback_config`，默认关闭）

当**某个节点**在带参考图的那次请求上被上游**明确以安全/内容合规原因拒绝**（结构化证据优先：响应体业务码、`finish_reason`、`block_reason`；HTTP 200 响应体内的业务拒绝同样算），插件可以在**该节点内**把这次被拒的参考图改写成一段中性文字说明，然后按用户原文 + 这段说明重试纯文字生成。它**不**放宽、也**不**绕过上游内容策略：原图不会再发，说明里也不许重建被拒内容。

配置组 5 个键（`reference_fallback_config`）：

| 键 | 默认 | 范围 | 说明 |
| --- | --- | --- | --- |
| `enable` | `false` | bool | 总开关。**关闭时零额外调用、零新增文件、零新增日志、零费用**（连 `task.runtime["reference_fallback"]` 都不会建）。 |
| `max_retries_per_node` | `1` | `0..3` | 语义是**该节点纯文字生成调用的总次数**（不是「额外次数」，所以 `1` = 只重试一次，不是两次）。`0` = 关闭。次数在发起前占用，失败也计一次。 |
| `caption_max_chars` | `200` | `80..400` | 识图说明的长度上限，与「外观摘要」的 400 字上限相互独立。 |
| `vision_timeout_seconds` | `20` | `5..60` | 单次识图调用的上限；同时受任务剩余预算约束（见下）。 |
| `notify_user` | `true` | bool | 最终成品**来自纯文字重试**时，在结果消息末尾追加一句说明（只追加一次、只在干净成功时追加）。 |

执行口径（都在 `services/scheduler.py` 内，逐节点）：

- **触发条件全部满足才算**：开关开、`max_retries_per_node >= 1`、被拒的那次请求确实带了参考图、判定结果是 `safety` 且带证据、识图 Provider 已配置且可用、参考图数量不超过识图上限（`CAPTION_MAX_IMAGES = 4`）、任务未取消、剩余预算够（`vision_timeout_seconds + 15 s 余量 + 20 s 收尾`）。
- **不触发**：图片格式不支持、图片本身无效、纯提示词拒绝、未知原因、超时、429 限流、401/403 鉴权、5xx、网络错误、参数错误。这些一律按普通失败前进，**不产生任何识图或文字调用**。
- **每节点至多识图一次**，说明只生成一次并在本节点内逐字节复用；**识图失败 → 该节点 0 次文字调用**（不伪造成功）。
- **失败后的走向**：只有安全拒绝会进入文字阶段；文字阶段里的失败（含网络/超时）**不会**消耗剩余次数继续撞墙——按普通失败结束该节点，**下一个节点重新带原图正常尝试**（它有自己的参考图采样与关系声明）。
- **预算与取消**：整条链路共用任务原有的 `generation_timeout`，**不延长**；取消或超时直接终止任务，按既有 `CANCELLED` / `TIMED_OUT` 结算。
- **失败不写 `task.errors`**：降级只写 `task.runtime["reference_fallback"]` 与 `task.runtime["attempt_errors"]`，所以「A 节点文字重试失败、B 节点带图成功」仍是干净成功（不会变成「部分图片绘制失败」）。
- **用户告知**：`notify_user=true` 且最终成品来自纯文字重试时，追加一句「（本次参考图未被使用：原图被内容安全策略拒绝，已改按文字描述重绘。）」；后续节点带图成功、或任务失败/部分失败时都不追加。

### 从 v1.3.0 升级

v1.3.0 的 `reference_fallback_config.max_extra_attempts`（`0..1`）被 `max_retries_per_node`（`0..3`）取代，**实验性遮挡相关的配置组 `experimental_reference_config` 已整组删除**（功能本身也删干净了：运行分支、类型、API、UI、产物依赖都不再存在）。

**必须先迁移配置、再换代码**：新版 schema 里已经没有 `experimental_reference_config`，核心的配置完整性检查会把 schema 之外的键当作 stale 删掉。插件启动时有一个幂等的防御迁移（`main.py::_migrate_reference_fallback_keys()`）：

- 有旧键 `max_extra_attempts`、没有新键 → 取值按下表转换后写入 `max_retries_per_node`（`0`/`1` 一一对应，其它值 clamp 到 `0..3`）；
- 新键已存在 → **新键优先**，旧键只删不改；
- 两个都没有 → 不写键（交给 schema 默认值）；
- `experimental_reference_config` 整组丢弃；
- 文件用 `utf-8-sig` 读、`ensure_ascii=False` + 2 空格缩进写回，**BOM 与其余所有组的叶值逐字节保留**；没有变化时不重写文件；落盘失败只在日志里留一条 warning（本次运行仍按迁移后的内存值工作）。

回滚要**成对**：只回滚代码或只回滚配置都会让两侧口径不一致（旧代码读不到 `max_retries_per_node`、新代码读不到 `max_extra_attempts`）。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/imago_fusion help` | 查看按用途和当前权限分组的帮助卡片 |
| `/imago_fusion draw <提示词>`、`/f画 <提示词>` | 普通绘图 |
| `/imago_fusion photo <画面要求>`、`/f拍照 <画面要求>` | 当前 Persona 出镜 |
| `/imago_fusion status` | 查看自己的任务 |
| `/imago_fusion quota help/show/sign` | 查看、查询或签到 |
| `/imago_fusion quota add/del/set <用户 ID> <整数>` | 管理员调额 |
| `/imago_fusion ref-upload` | 上传 Persona 参考图 |
| `/imago_fusion summary-show/rebuild/set` | 查看、重建或设置外观摘要 |
| `/imago_fusion provider-primary <节点 ID>` | 指定主节点（也可在 WebUI 设置） |

LLM 工具：`generate_fusion_image`（普通绘图）、`generate_fusion_persona_image`（Persona 出镜）。

## 安全边界

- **参考图与结果图的下载**统一经 `core/network.py`：手动逐跳重定向（≤5 跳）、逐跳私网校验、体积上限、魔数校验。`storage_config.block_private_networks` 只约束这里，**不**约束节点 `base_url`，所以本地反代仍然可用。
- 日志与用户可见文案统一经 `core/security.py` 脱敏：`api_key=`、裸 `key=`、`?key=` / `?apiKey=` 查询参数、`data:image/...` 载荷、完整 URL、长数字标识都会被掩码或替换。
- 原生适配器不下载 provider 返回的引用，因此不存在「把宿主任意文件提交给上游」的路径。
- **`enhance_config.api_key` / `secret_key` 是明文存在插件配置里的**（与 `providers[].api_keys` 同等待遇，AstrBot 的 schema 没有 password 类型）：禁止贴群、贴日志或截图；插件侧日志与任务记录永不打印这两个值、`access_token`、base64 载荷与代理地址，`access_token` 的缓存键是 `sha256(api_key + NUL + secret_key)` 的前 16 位（不拿密钥当字典键）。
- Agent 结果图的 `fileUri` 只在**与节点 `base_url` 同域**时才补 `key` 查询参数（上游 v1.3.0 的安全修复）：中转把结果图放到第三方对象存储时，补 key 等于把密钥交给对方日志。

## 已知边界（非目标）

- 不移植大香蕉的直连 HTTP 栈、`@provider_type` 注册表、smart_retry、随机 key 打乱、图床/视频/白名单/冷却等绘图管线。
- 不引入 Gemini 原生协议的 `system_prompt` / `responseModalities` 之外的参数面：原生路由走 OpenAI 兼容协议，这些字段不会被 AstrBot Provider 带出；需要时用提示词表达。**例外**：`gemini_official` HTTP 节点会把节点 `default_size`（或请求 `size`）约分成 `generationConfig.imageConfig.aspectRatio`，让尺寸配置真正生效（识别不出比例时不写该键，请求体与旧版一致）；`imageSize` 与 `custom_params` 不引入。
- `enable_proxy` 只覆盖 HTTP 直连节点；为原生路由加代理需要在 AstrBot Provider 侧配置。

## 测试

纯逻辑测试不需要 AstrBot 运行环境（原生适配器用假 `LLMResponse`/`MessageChain` 打桩）：

```bash
cd data/plugins/imago_fusion/test
PYTHONPATH=/root/AstrBot/data/plugins /root/AstrBot/.venv/bin/python test_native_adapter.py
PYTHONPATH=/root/AstrBot/data/plugins /root/AstrBot/.venv/bin/python test_scheduler.py
```
