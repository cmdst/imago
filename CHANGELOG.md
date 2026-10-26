# 更新日志

## 1.1.0 — 2026-10-27

**版本口径**：本版走融合版自己的 `1.1.0` 线，**不跟随上游 imago 的 minor**（上游此时是 `1.3.0`）；只从上游 `v1.2.2…v1.3.0` 的 diff 里吸收**两项 bug 修复**，并**明确排除**其删除默认模型模板/适配器（`openai_chat`、`dashscope_multimodal`）与 `custom_params` 结构重写。

### 新增

- **百度「图像无损放大」（`upscale_config`，默认关闭）**：出图成功后把每张成品图送 `image_quality_enhance` 放大 2 倍（长宽各 2 倍）再交付群聊，放大图另存为任务缓存目录下的新文件、**原图保留**兜底。端点、调用形状（`application/x-www-form-urlencoded` + urlencode 后的 base64，`access_token` 走查询参数）、本地预检（base64 ≤4M、长宽乘积 ≤2000×2000、jpg/png/bmp）与失败降级全部按百度文档口径实现；`access_token` 用两把 key 换取，内存缓存（30 天 − 5 分钟时钟余量），`110/111/100/HTTP 401` 失效重取 + **恰好重试一次**，并发按缓存键单飞（只打一次 token 接口）。
- 配置组 `upscale_config` 7 键：`enable`（默认 **false**）/ `api_key` / `secret_key`（默认空）/ `max_image_mb`=4 / `max_pixels`=4000000 / `timeout_seconds`=20 / `enable_proxy`=false。键由 AstrBot 核心在插件加载时按 schema 自动补齐，**不需要手工迁移**。
- 放大相关的 DEBUG（每图）+ INFO（每任务一条 `放大 成功=N 跳过=N 失败=N`）+ WARNING（每失败类一次）；`log_id` 只落 `task.runtime["upscale"]`。

### 修复

- **P0：原生节点失败不再被记成 `TypeError`**。`main.py` 把 3 参 `_provider_error(provider, exc, task)` 绑给 `runtime.configure(error_reporter=…)`，而 `runtime.report_provider_error()` 按 2 参调用 ⇒ 每次原生节点失败都在「上报失败」处抛 `TypeError`，真实原因（超时 / 4xx-5xx / 无输出）被吞掉、`attempt_errors` 从未写入（终态日志只剩 `errors=TypeError attempts=-`）、多节点 fallback 被整段跳过。线上自 2026-10-23 起累计 8 次。修法：`task` 改为可缺省（`task=None`），并新增 `test/test_runtime_hook.py` 两条守卫（签名层 + 真实 `report_provider_error()` 2 参驱动）。
- **吸收上游 v1.3.0 的安全修复（F1）**：Gemini 结果图的 `fileUri` **只在同域**才补 `?key=`。旧实现把 key 追加到任意 URI 上，中转把结果图放到自己的对象存储时等于把用户的 Gemini key 交给第三方日志。`_file_uri_with_key(uri, api_key, api_host)` 新增 `api_host`（节点 `base_url` 的 netloc）且不给默认值；融合版既有的三处边界处理（按参数名判 `key`/`apiKey`、fragment 留在最后、无值 `?key`）全部保留。
- **吸收上游 v1.3.0 的功能缺陷修复（F2，裁剪版）**：Gemini 节点的尺寸真正生效。v1.2.2 的 `generationConfig` 只有 `responseModalities`，节点上填的 `default_size` 对 Gemini **静默无效**（写 3:4 出来 1:1）。现在把 `size` / `default_size` 约分成 `generationConfig.imageConfig.aspectRatio`；识别不出比例时**不写**该键（请求体与旧版逐字节一致），不引入 `imageSize` 与 `custom_params`。

> 上游 `1.3.0` 的变更日志只写「新增/变更」，**没有**把上面两项标成修复；F1/F2 是按 diff 语义判定的（F3「调试日志里的 size 打印错」与 `custom_params` 强耦合，本版不吸收）。

### 不进本版（明确排除）

- 上游删除 `providers/openai_chat.py` / `providers/dashscope.py` 及其在 `API_TYPES` / schema `api_type.options` / WebUI 校验白名单里的联动：**`openai_chat` 与 `dashscope_multimodal` 必须保留**（`test/test_core.py` 有守卫用例）。
- 上游的 `xai_image` 适配器、`core/params.py`、`custom_params` 取代 `default_size`、配置页参数控件与 WebUI 渲染：属破坏性配置变更，本版是 bug 修复线。
- 百炼（`dashscope_multimodal`）同类的「尺寸不生效」检查：登记为遗留，不扩面。

## 1.0.0 — 2026-10-16

首次以 **IMAGO·FUSION**（注册名 `imago_fusion`）发布：以 **IMAGO·映相 v1.2.2** 为骨架，融合「大香蕉」的 **AstrBot 原生 Provider 桥接**——出图不再只能走 HTTP 直连，也可以走 AstrBot 里已经配好的 Chat Provider（反代 / 网页端模型）。

### 新增

- **`astrbot_native` 节点类型**（`providers/native.py`，405 行）：节点只填一个 AstrBot Provider id（`astrbot_provider_id`，面板下拉选择），地址与鉴权由已配置的 `provider_source` 决定，`base_url` / `api_keys` 留空合法。适配器**只搬运图片引用**，不下载、不解码、不嗅探格式——体积上限、魔数校验与私网守卫仍由 `core/network.py` 统一负责，因此不存在「把宿主任意文件提交给上游」的路径。
- **原生结果的两级提取**：先取 `LLMResponse.result_chain` 里的 `Image` 组件（`url` 优先、`file` 次之），再从正文的 Markdown 图片链接兜底；只接受 `http://`、`https://`、`data:image/`、`base64://` 四类引用，裸本地路径与 `file://` 一律丢弃。响应完全没有图片时，把模型自己的文字原因带进 `NoOutputError`，便于判断是内容安全、额度还是协议不兼容。
- **流式开关**（节点字段 `stream`，默认关闭）：开启后走 `provider.text_chat_stream`，逐帧提取并按引用去重（流式 provider 的末帧通常重复整份结果）。
- **单次调用凑 `count`**：一次聊天调用通常只出 1 张图，适配器最多循环 4 次以凑满请求张数；AstrBot Provider id 不存在时**立即中断**并把机会让给下一个节点（错误 id 重试也不会变对），单次失败只记日志、继续下一次；节点部分成功时返回已拿到的图片。
- **独立命名空间**（可与 IMAGO·映相同装互不干扰）：命令组 `/imago_fusion`、`/f画`、`/f拍照`；LLM 工具 `generate_fusion_image` / `generate_fusion_persona_image`；Web API `/imago_fusion/*`；运行数据 `plugin_data/imago_fusion/`（不读写 `plugin_data/imago/`）。
- **`core/runtime.py`**：把 AstrBot `Context`、DEBUG 日志与「模型失败」行集中注入，避免把 context 穿进调度器构造适配器的既有契约（`ADAPTERS[api_type](config)`，测试与第三方按此签名打桩）。
- **`template_override` 碰撞守卫**：`模板名/模型名` 覆写只在 `/` 前**确实是已配置模板名**时生效；若该名字与某个 `astrbot_native` 节点 id 完全相同，记 WARNING 并跳过覆写，把引用留给原生节点解析（原生 id 本身带 `/`，如 `cpa_Gemini/web-gemini-image`，不设守卫就会被静默劫持到模板的 base_url + key）。
- **`DESIGN.md`**：迁移审计与设计契约（模块清单、NativeAdapter 接口、配置 schema、bug/优化清单、逐文件处置与非目标），用于解释「为什么这样分模块」。

### 变更（相对 IMAGO·映相 v1.2.2）

- 插件注册名 `imago` → `imago_fusion`；`metadata.yaml`、`__init__.py`、`main.py` 的 `@register` 三处版本统一为 `1.0.0`。
- `providers` 模板扩到 **13 个字段**（新增 `astrbot_provider_id`、`stream` 等），`api_type` 增第 6 个选项 `astrbot_native`（其余 5 种 HTTP 直连保持不变）。
- 配置读取仍统一走 `core.config.load_config()`：伪节点（缺 id、id 重复、未知 `api_type`、原生节点缺 provider id）**静默丢弃**，所以「配置里填了」不等于运行时存在该节点。
- 测试从 4 个文件扩到 5 个：新增 `test/test_native_adapter.py`（610 行），覆盖两级提取、跨帧去重、引用白名单、无图时透出模型原因、`API_TYPES` 与 `ADAPTERS` 同步。

### 已知问题

- **原生 Provider 失败会被记成 `TypeError`**：`main.py` 把 3 参的 `_provider_error(provider, exc, task)` 绑给 `runtime.configure(error_reporter=…)`，而 `runtime.report_provider_error()` 按 2 参调用 ⇒ 每次原生节点失败都在错误上报处抛 `TypeError`，真正的失败原因（超时 / HTTP 4xx-5xx / 无输出）被吞掉，且多节点 fallback 会被整段跳过。线上自 2026-10-23 起累计 8 次（终态日志只剩 `errors=TypeError attempts=-`）。**已在 1.1.0 修复**（`task` 改为可缺省 + 两条守卫测试）。
