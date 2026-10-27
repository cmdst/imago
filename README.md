# IMAGO·FUSION

IMAGO·FUSION 是 **IMAGO·映相** 骨架与 **大香蕉（astrbot_plugin_big_banana）AstrBot 原生 Provider 桥接** 的融合插件：保留映相的提示词优化、Persona 出镜、绘图额度、多节点 fallback 与素材管理，新增「走 AstrBot 已配置 Provider（反代 / 网页端模型）出图」的原生路由。

- 版本：`1.1.0`（`metadata.yaml` / `__init__.py` / `main.py` 的 `@register` / 本行**四处一致**；更新内容见 `CHANGELOG.md` 顶部的 1.1.0 节）
- AstrBot：`>=4.16,<5`
- 依赖：`aiohttp>=3.9,<4`（不引入 Pillow：插件侧不做格式转换）

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

## 图像无损放大（可选，**默认关闭**）

出图成功后，把每张成品图送**百度「图像无损放大」**放大 2 倍（长宽各 2 倍）后再发送到群聊。配置在插件配置页的「图像无损放大（百度）」组（`upscale_config`）。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enable` | **false** | 总开关。开启后**每张成品图**多一次百度调用（按次计费） |
| `api_key` / `secret_key` | 空 | 「图像增强与特效」应用的两把 key；任一为空视为未配置，**不发任何请求** |
| `max_image_mb` | 4 | 本地预检的 base64 上限（百度硬限 4M；原图上限 = 4 MiB × 3/4） |
| `max_pixels` | 4000000 | 本地预检的长宽乘积上限（2000×2000） |
| `timeout_seconds` | 20 | 单张超时（同时受任务总超时约束，剩余预算不足会自动跳过） |
| `enable_proxy` | false | 放大请求走插件顶层的「出站代理地址」；百度是境内公网，一般不需要 |

- **端点**：`https://aip.baidubce.com/rest/2.0/image-process/v1/image_quality_enhance`（`access_token` 走查询参数，表单体 `image=<base64>`，不带图片头）。鉴权：`oauth/2.0/token` 用两把 key 换 `access_token`（30 天有效，内存缓存，插件重载后重取一次）。
- **限制**：base64 ≤ 4M、长宽乘积 ≤ 2000×2000、支持 jpg/png/bmp。超限在本地先跳过（服务端超限也会计费），并记一条 DEBUG。
- **失败降级**：放大失败、超时、超限、额度用尽、应用未勾选该能力（`error_code=6`）等情况**一律发送原图**，任务仍判成功、额度结算不变，日志只留一条 WARNING（每类一次）。放大统计只写 `task.runtime["upscale"]`。
- **原图保留**：放大图另存为 `task_cache/<任务 ID>/outputs/` 下的新文件（命名规则与结果图落盘一致：uuid hex + 按魔数定的后缀），原图仍在同目录（对账/回滚用，`task.json` 的 `outputs` 记的是放大后的文件，缓存清理按目录整体进行），缓存上限沿用 `storage_config.temp_cache_mb`。
- **零副作用**：`enable=false` 时出图链路与 **v1.0.0** 逐字节等价——零网络、零新文件、零新增日志（当前版本见文首「版本」行）。

> 注意：「图像无损放大」与「图像清晰度增强」（`image_definition_enhance`，尺寸不变、上限 10M/最长边 5000px）是百度同产品下的**两个不同接口**，控制台需要**分别勾选**。本插件只实现前者；未勾选时调用返回 `error_code=6`。

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
- **`upscale_config.api_key` / `secret_key` 是明文存在插件配置里的**（与 `providers[].api_keys` 同等待遇，AstrBot 的 schema 没有 password 类型）：禁止贴群、贴日志或截图；插件侧日志与任务记录永不打印这两个值、`access_token`、base64 载荷与代理地址，`access_token` 的缓存键是 `sha256(api_key + NUL + secret_key)` 的前 16 位（不拿密钥当字典键）。
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
