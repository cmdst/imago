# 更新日志

## 1.4.1（未发布开发候选）

**一句话**：把三处已证实的链路缺陷（参考图不压缩 → 上传慢且易踩超时、节点超时被误判成任务总超时、模型把参考图原样回吐当成品）一并修掉，并把"为什么失败"写进脱敏、有界、可诊断的记录；配置面只新增一组（7 键，默认开启）。

### 新增

- **参考图压缩**（配置组 `reference_compress_config`，7 键，默认开启）：显式参考图、引用消息图片、`data:` / `base64://` / `http(s)` 链接、后台 deferred、Persona 参考图**共用同一个入口**，在"参考图全部就绪之后、第一次请求节点之前"压缩一次；`inputs/` 原素材与出图/增强/放大产物完全不参与。只缩不放、EXIF 方向归一、透明按需白底、质量档带 40 下限、最多 9 次编码、最终只接受"比原图更小"的候选（否则 `no_gain`）；达不到 `target_kb` 时保留最小候选并标 `target_unmet`。Pillow 缺失或任何异常一律回退原图，**不写 `task.errors`**、不新增任务失败。`enable=false` 时零解码、零新增文件、零新增日志，发送字节与 1.4.0 逐字节一致。
- **回吐守卫**：在"成品图已落盘、尚未增强/放大"的位置，把成品与**本次成功尝试实际发出的参考图**（含压缩副本与采样到的 Persona 图）比对；命中判据只有两条——**字节完全相同**，或 **EXIF 方向归一后同尺寸且 RGBA 逐像素完全相等**。全部命中 → `NO_OUTPUT` + `no_change` + 固定文案「这次没有生成新图，模型把参考图原样送了回来。请重试或换个节点。」；部分命中 → `PARTIAL_SUCCESS` 且只发有效图。不解码/超限/无快照一律放行（`unknown`），不做感知哈希、不自动重试、不额外调用节点；被拦的 provider 原件保留在 `outputs/`，但不进 `task.json` 的 `outputs[]`。全部命中时**不发起后置配文**（冻结文案也不会被配文覆盖）；若开了 `task_config.llm_caption_pregen`，预生成配文可能在参考图准备阶段就已发起、随后被取消——**取消不等于没花过**，所以这里只承诺「闸门不新增调用」，不承诺「整个任务零配文费用」。
- **超时语义分两层**：L1 节点超时（`providers[].timeout`，默认 180 s、最小 10 s）在 `_generate` 出口统一包成 `ProviderTimeout` 并继续 fallback；L2 任务预算（`task_config.generation_timeout`，从 `created_at` 起算，默认 300 s、最小 30 s）真实到期才判 `TIMED_OUT`。**没有第三个「上传窗口」定时器**：1.4.1 开发期间文档里出现过的「≈15 s 上传窗口」是早期链路观测值，不是代码里的阈值；`aiohttp.ClientTimeout(total=None, connect=15)` 只限制连接建立。`TIMED_OUT` 现在**必然**带 `errors` 里的 `timeout` 与 `runtime.last_provider_error=任务超时`；准备参考图、配文、发送等阶段的内部 `TimeoutError` 按普通失败结算，不冒充总超时。
- **可观测（脱敏、有界）**：适配器网络异常开始记录 `phase`（保守三值 **connect / read_body / unknown**——只有明确的连接类异常才记 `connect`，其余一律 `unknown`，不按异常名推断阶段）、`elapsed`、`up_bytes`（**客户端分片回调发出的字节**，不是对端接收计数；没有回调就写 `unknown`，不补零）、`timeout`；`task.json` 新增 `reference_compress` / `echo_guard` / `attempts` 三块摘要，运行期另有 `attempt_diagnostics`（≤12 条）。日志新增 `参考图压缩 …`、`回吐闸 检查=… 拦下=… 放行=…`、`绘图模型失败 node=… model=… phase=… …` 三类行，均不含图片内容、完整 URL 或凭据。

### 变更

- `_request_for_attempt()` 改为优先使用"发往节点的副本"（`runtime["send_references"]` / `["send_persona_references"]`）；压缩关闭或无参考图时退回原图路径，行为与 1.4.0 一致。关系声明注入与 `reference_image_limit` 采样语义不变。
- `aiohttp.ClientSession` 挂上 `TraceConfig`（只写内存计数），用于给上面的 `up_bytes` / `phase` 提供**事实依据**，不改任何 provider 协议或请求体。
- 配置 schema 由 11 组变 12 组（`reference_compress_config` 插在 `reference_fallback_config` 之后）；旧配置启动时自动补齐，其余组与 BOM 不变。

### 修复

- 1.4.0 出现的"任务 `timed_out` 且 `errors=-`"（本地任务 `1299a517…`，单节点 117 s 后任务被判超时）：根因是节点级超时直接从 `_execute` 逃逸成任务级结算。现在节点超时被包装并继续 fallback，任务级超时则必然带原因。
- 参考图不经压缩直接上传（单张数百 KB～数 MB 的原图）导致的准备阶段耗时与上传阶段超时。

### 未验证（留 fix 侧真实链路）

- 真实 `295454` 字节样本的压缩后体积、真实网络下单张参考图 ≤7 s 的上传耗时（**观测目标，不是插件里的阈值**）、以及真实模型回吐的复现：本站只用合成夹具验证判定器与管道本身，**不作为真实链路已验证**，也不代表已上线或已过真实模型验收。
- 增强 / 放大两级与压缩的叠加顺序只做离线断言，未打真实百度接口。

## 1.4.0 — 2026-11-15

**一句话**：删掉 v1.3.0 的**实验性遮挡重试**（整组配置、运行分支、类型、Web API/WebUI 控件与产物依赖一并移除），只保留「参考图被**明确安全/内容合规拒绝**后在**该节点内**做一次识图、再用中性文字说明重试」这一条降级路径，并把重试次数改成**逐节点、可配 0..3**。

### 移除

- **实验性遮挡重试整体删除**：配置组 `experimental_reference_config`（6 键）、`DEFAULT_OCCLUSION_PROMPT`、`ExperimentalReferenceConfig`、`RuntimeConfig.experimental_reference`、`main.py` 的 `_occlude_references()` / `_fallback_invoke()` / `_reference_fallback()` 与相关常量、`services/scheduler.py` 的任务级末尾救援（`rescue=` 注入、`_maybe_reference_fallback()`、`fallback_locked` / `fallback_request` 全局锁）、`integrations/web_api.py` 的实验分组定义与 `occluder_ready` / `dependency_ready` 派生字段及 `experimental_without_fallback` / `occluder_unavailable` 两条保存告警、`pages/webui/app.js` 的遮挡节点下拉与实验分组渲染。
- **产物依赖**：不再写入 `inputs/proc-*` 遮挡产物；`inputs/NN_<sha16>.<ext>` 的原始参考图落盘与识图读取不变。
- **旧键 `max_extra_attempts`（`0..1`）**：schema、模型、配置、WebUI、WebAPI 里全部消失（不留别名），只在新版的配置迁移路径与历史文档里出现。

### 变更

- **触发口径收窄**：可降级的原因从 `{图片不支持, 图片无效, 安全}` 收窄为**只有明确的安全/内容合规拒绝**（`kind == "safety"` 且带结构化证据）。图片格式不支持、图片无效、纯提示词拒绝、未知原因、超时、429/401/403、5xx、网络与参数错误**都不触发**，一律按普通失败前进（零识图、零文字调用）。HTTP 状态码单独仍不构成依据；HTTP 200 响应体内的结构化业务拒绝仍可判定。
- **作用域从任务级改为节点级**：安全拒绝后立刻在**该节点、该模型**内处理；文字阶段失败就结束本节点，**下一个节点重新带原图正常尝试**（有自己的参考图采样与关系声明）。不再有「任务首次拒绝锁」和「所有节点都失败后再整体救援」。
- **次数语义改为逐节点总数**：`reference_fallback_config.max_retries_per_node`（`int`，默认 `1`，`0..3`）表示**该节点纯文字生成调用的总次数**（不是 N+1）。次数在发起前占用、失败也计一次，额度按节点私有、不按模型刷新；`0` = 关闭。每节点**至多识图一次**，说明只生成一次并逐字节复用。
- **原文来源收紧**：纯文字重试只用 `_prepare()` 入口保存的**用户原文**（`task.runtime["original_user_prompt"]`，在任何人设/关系声明/自动识图改写之前写入）重建 prompt；该值不可证明时直接 `prompt_unrebuildable` 终止，**零调用**。不再从被拒请求的 prompt 里剥离关系声明——那会带上旧的人格提示、自动识图摘要与优化器产物。
- **用户告知收紧**：`notify_user` 为真且最终成品确实来自纯文字重试时，才在结果末尾追加一句；后续节点带图成功、或任务失败/部分失败时都不追加。
- **预算与取消**：整条降级链路共用任务原有的 `generation_timeout`（**不延长**）；进入文字阶段前要求剩余预算 ≥ `vision_timeout_seconds + 15 s 余量 + 20 s 收尾`；取消与总超时直接终止任务，按既有 `CANCELLED` / `TIMED_OUT` 结算，不新增 sleep/轮询/退避。
- **结算不变**：降级尝试不写 `task.errors`（避免把「A 节点文字失败、B 节点成功」错判成 `PARTIAL_SUCCESS` 并给用户发「部分图片绘制失败」），不额外扣额度、不改 `usable_output_count`；`NO_OUTPUT` / `TIMED_OUT` / `FAILED` / `CANCELLED` 与退款口径一字未改；最终成品仍只做一次「增强 → 放大」。
- **配置迁移**：升级前必须先迁移配置再换代码（核心的完整性检查会删掉 schema 之外的键）。插件启动时执行幂等迁移 `main.py::_migrate_reference_fallback_keys()`：旧键 `max_extra_attempts` → 新键 `max_retries_per_node`（clamp `0..3`）、**新键优先**、两键都无则交给默认值、`experimental_reference_config` 整组丢弃；`utf-8-sig` 保 BOM、`ensure_ascii=False` + 2 空格缩进、顶层顺序与其它组叶值逐字节保留。回滚要**代码 + 配置成对**。
- **版本号 `1.4.0` 四处同步**（`metadata.yaml` / `__init__.py` / `main.py` 的 `@register` / `README.md` 版本行）。配置顶层组 12 → **11 个**（`reference_fallback_config` 由 5 键替换其中的次数键）。

### 保留

- 7 种 `api_type`（含 `xai_image` 与 `astrbot_native`）、节点启停、`custom_params` 类型化参数、`thinking` 六值、质量/尺寸、主节点（stored/effective）、密钥轮转与清空、代理、原生实际子请求计数、模型回退顺序、`inject_reference_note`、`llm_caption*`、`EXCLUDED_STATUSES` 与 `classify_rejection()` 的判定逻辑（**逐字节未改**，只改写了一处注释）、`enhance_config` 8 键与 `upscale_config` 7 键、两级图后处理与汇总日志行。

## 1.3.0 — 2026-11-14

> **v1.4.0 已移除**：本节里「实验性遮挡重试」相关的能力（配置组、运行分支、类型、Web API/WebUI 控件、遮挡产物）已在 1.4.0 **整组删除**；「参考图文字降级」也已在 1.4.0 收窄为**仅安全/内容合规拒绝**触发、改为逐节点额度。以下保留 1.3.0 当时的事实，不再更新。

**一句话**：选择性吸收上游 imago v1.3.x 的三项能力（xAI 图像渠道、节点启停、类型化节点参数），并单独实现与质量分离的思考控制；新增**默认关闭**的参考图文字降级与**实验性**遮挡重试，二者只在节点明确拒绝图片输入时触发，绝不放宽或绕过上游内容策略。

### 新增

- **xAI 图像渠道（`xai_image`，第 7 种 `api_type`）**：继承 OpenAI 图像适配器，`POST /v1/images/generations` 无参考图、`POST /v1/images/edits` 有参考图；单图 `image:{"url": data-URI}`、多图 `images:[…]`（互斥）、`<IMAGE_0>` 指代；把 `size`（比例原样 / 像素按 gcd 折算）落到 `aspect_ratio`，`resolution` 支持 1k/1.5k/2k，`edit_mode=multipart` 走兼容中转。官方 OpenAPI 请求体没有 `quality`，本版不生成该字段（显式写在节点参数里时原样透传并告警）。
- **节点启停（`enabled`，默认 true）**：停用节点完全不参与路由、不占主节点候选，配置与密钥保留；全部停用或配置不完整时在**扣额度之前**拒绝创建任务。
- **类型化节点参数（`custom_params`，每行 key=value）**：按协议为每个 `api_type` 出控件（尺寸/比例、`quality`、`image_size`、`resolution`、`edit_mode`、百炼 `prompt_extend`/`watermark`/`seed`/`negative_prompt`）。参数优先级：调用级参数 > 节点参数 > `default_size` > 适配器默认；保留键永不覆盖；非法行只跳过并记录，不影响节点可用性。
- **思考控制（`thinking`，6 值，与质量/分辨率独立）**：`off/low/medium/high/max` 按协议映射到 OpenAI 图像 `complexity`、OpenAI Chat `reasoning_effort`、Gemini `thinkingConfig.thinkingLevel`（仅能力表内模型，图像模型默认开启且不可关闭）；xAI/百炼/原生不自动发送。被厂商拒绝时只上抛一次、不剥参数重发。
- **参考图文字降级（`reference_fallback_config`，5 键，默认关闭）**：只在可判定的图片拒绝（结构化证据优先，其次图片相关文本）时触发一次：对**该次失败尝试实际使用的图片集合**做识图说明，去掉全部参考图后按原文重绘；普通 400/鉴权/限流/网络/超时/未知错误不触发。
- **实验性遮挡重试（`experimental_reference_config`，6 键，默认关闭）**：被拒后调用指定的遮挡节点，对同一图片集合做不透明遮挡编辑，再用**处理产物 + 与产物一致的摘要**对原目标节点重试一次；处理产物不发送、不进增强/放大、不重复扣额度；失败按契约收束，绝不回退原图、不恢复被遮挡内容。
- **用户告知**：降级/遮挡成功或失败时在结果末尾追加一句 Plain 说明，不改变图片数量与顺序。

### 变更

- 配置升到 **12 个顶层组**（新增两组，默认关闭），节点模板 13 → **16 键**；旧配置由核心按 schema 自动补齐，缺组行为与 v1.2.0 完全一致。
- 版本号 `1.3.0` 四处同步（`metadata.yaml` / `__init__.py` / `main.py` 的 `@register` / `README.md` 版本行）。
### 修复（独立评审 round 2）

- **降级调用额度**：`max_extra_attempts=0` 时零调用；最终生成在调用**前**占用唯一一次额度，失败不再补发第二次；遮挡编辑按锁定顺序**逐图一次**，任何一步失败都不取前 N 张凑数；降级识图固定零隐藏重试；原生节点内部子请求单独计数。
- **预算与取消**：每次编辑/识图/最终生成前都重算同一任务 deadline，检查取消、15 s 余量与目标节点可用性；取消后不再发起新请求。
- **终态与额度**：最终降级生成失败原样上抛，按既有语义落 `NO_OUTPUT` / `TIMED_OUT` / `FAILED`；只有 `FAILED`/`CANCELLED` 退额度，不再把降级失败改写为成功或重复退款。
- **拒绝判定**：先排除超时/网络/鉴权/限流/404/5xx 与无证据响应；上游业务码按字段语义解释，`block_reason=OTHER` 单独视为未知；参数级 400（`quality`/`thinking`/分辨率等）不再误判为图片被拒；HTTP 200 的结构化内容拒绝可判定，但保留真实状态码与来源。
- **遮挡产物**：只接受魔数校验通过的真实图片，`max_images` 为容量上限而非"取前 N 张"；编辑提示追加固定后缀「只输出图片，不要文字说明。」；实验摘要使用本组 `caption_max_chars`。
- **告知与原文**：`notify_user=false` 时成功/失败/容量提示全部静默；降级 prompt 只从 prepare 入口保存的用户原文重建，原文不可证明时 `prompt_unrebuildable` 终止、零调用。
- **提交入口**：无可用节点（全部停用或配置不完整）在扣额度、创建排队任务之前拒绝；`_conf_schema.json` 的 `api_type` 补齐第 7 型 `xai_image`。

### 修复（独立评审 round 3）

- **原生子请求计数（R2-01）**：`scheduler.invoke` 现在把适配器实例与任务关联，`finally` 记录成功/异常/超时/取消，遮挡编辑与最终降级共用同一计数器；`_generate` 自身计数不变，两条路径不重叠、不双算，原生节点内部 count≤4 与部分成功语义保持不变。
- **拒绝分类冻结表（R2-02）**：补齐 `moderation_blocked`/`prohibited_content`/`invalid_image_format`/`IMAGE_SAFETY` 等冻结别名；结构化字段按 code/finishReason/blockReason 各自语义解释；无结构化证据的 prompt blocked 文本判 `text_rejected` 且优先于 safety；`block_reason=OTHER` 单独视为未知，仅与 prompt 证据同现才判 `text_rejected`。
- **原生包装状态（R2-03）**：流式/非流式失败保留上游可信 `status`（含 SDK `response.status`，经 int/范围校验），超时与连接异常以 `source=transport` 保留排除证据；未知状态仍为 None，绝不从业务字符串伪造。
- **HTTP 200 业务拒绝（R2-04）**：DashScope 无图 `{code: DataInspectionFailed}` 改抛结构化 `NoOutputError`（保留 200/code/source），Gemini 仅有 `finishReason`（如 `IMAGE_SAFETY`）的分支补 `200 + source=body`；这类已完成计费的请求按 `NO_OUTPUT` 结算、不退款。


## 1.2.0 — 2026-10-28

**一句话**：在既有的百度「图像清晰度增强」之后，再挂一级百度**「图像无损放大」**（`image_quality_enhance`，长宽方向各放大两倍、面积 4 倍），并给参考图关系声明加一个**注入开关**（`task_config.inject_reference_note`，默认开启 = 行为零变化）。两个百度接口各自一套配置组、各自一套预检与熔断；增强那一级**行为零改动**。

### 新增

- **百度「图像无损放大」（`upscale_config`，7 键，默认关闭）**：出图成功、并走完（可选的）清晰度增强之后，把成品图送 `image_quality_enhance` —— **长宽各 2 倍**（面积 ×4）后再交付群聊；放大图另存为任务缓存目录下的新文件，**原图与增强图都保留**。端点、调用形状（`application/x-www-form-urlencoded` + urlencode 后的 base64，`access_token` 走查询参数，不带图片头）、本地预检（base64 ≤4M、长宽**乘积** ≤2000×2000、jpg/png/bmp，另加本地最短边 16px 省钱守卫）与失败降级全部按百度文档口径实现。
- **两级链路（顺序写死）**：`materialize → 增强（尺寸不变） → 放大（2×） → 发送`。每级**独立预检**（判据 = 该级实际要上传的字节），放大拿的是**增强产物**（增强未成功/未开时是原图）；**任何一级的失败/跳过都保留上一级产物**，绝不回退原图。两套 `State` **各自熔断**（放大熔断不影响增强，反之亦然），预算余量复用 `core/enhance.BUDGET_MARGIN_SECONDS = 15`。
- **配置组 `upscale_config`（7 键）**：`enable`（默认 **false**）/ `api_key` / `secret_key`（默认空）/ `max_image_mb`=4（clamp 1-4 ⇒ 原图上限 3 145 728 B）/ `max_pixels`=4000000（clamp 100000-4000000）/ `timeout_seconds`=20（clamp 5-60）/ `enable_proxy`=false。最短边 16px 是本地守卫，做成常量、不占配置键。键由 AstrBot 核心在插件加载时按 schema 自动补齐，**不需要手工迁移**。
- **`task_config.inject_reference_note`（第 8 键，bool，默认 `true`）**：关闭后参考图关系声明与「前 N 张用户参考 / 其余人设身份」的位置补充段**一起不再追加**，最终 prompt 与 `task.request.prompt` **逐字节相等**（不留空行、不删既有文本）。默认 `true` ⇒ 升级到 1.2.0 后**行为零变化、不需要迁移**。
- **日志**：放大汇总 INFO **独立成行**（`放大 成功=N 跳过=M 失败=K`，紧跟既有 `增强 成功=…` 行之后，**既有增强行格式一个字节没改**）；WARNING 每失败类一次、全部带 `放大 失败：` 前缀（marker 与增强不共用）；DEBUG 每图一条（`放大 完成/跳过/失败`）。放大统计只写 `task.runtime["upscale"]`（6 个字段：`attempted/succeeded/skipped/failures/circuit_open/log_id`），**不落 `task.json`**、不进 `task.errors`。

### 变更

- **测试伴奏（唯一触碰增强测试的地方）**：`test/test_enhance.py` 的 E11 端点守卫扫描面从 `core/*.py` glob **收窄成显式白名单**（不含 `core/upscale.py` / `core/upscale_client.py`）。断言一条没删、没跳过、没放松；放大侧的反向守卫（放大两模块里不得出现增强端点常量标识符）由新增的 `test/test_upscale.py` 承担。
- 版本号 `1.2.0` 四处同步（`metadata.yaml` / `__init__.py` / `main.py` 的 `@register` / `README.md` 版本行）。

### 非目标（本版不做）

- **不改增强行为**：`core/enhance.py` / `core/enhance_client.py` / `enhance_config` 8 键 / `task.runtime["enhance"]` / 增强日志文案一字不动（唯一例外是上面那条测试扫描面收窄）。
- 不做多级放大（4× / 8×）、不做自定义文案、不做「失败回退原图」、不用 `url` 参数、不引入 Pillow、不抽公共 `baidu_token.py`。
- 不写生产、不重启服务、不 push / 不合并 / 不打 tag、**不真调百度接口**（全部用例走 `FakeSession`）。

### 上线前必读

百度控制台要在**同一个应用**里额外勾选 **「图像无损放大」**（可以和「图像清晰度增强」同时勾，两项互不排斥）；没勾选时每次调用都返回 `error_code=6`，日志出现 `放大 失败：控制台应用未勾选「图像无损放大」（error_code=6）…` —— 那是控制台没勾，**不是插件缺陷**（勾了增强 ≠ 勾了放大）。勾选后等 `access_token` 缓存自然过期（最长 30 天）或重载插件一次即可生效。

## 1.1.0 — 2026-10-27

**版本口径**：本版走融合版自己的 `1.1.0` 线，**不跟随上游 imago 的 minor**（上游此时是 `1.3.0`）；只从上游 `v1.2.2…v1.3.0` 的 diff 里吸收**两项 bug 修复**，并**明确排除**其删除默认模型模板/适配器（`openai_chat`、`dashscope_multimodal`）与 `custom_params` 结构重写。

**接口口径（变更令 v2）**：百度能力从草案里的「图像无损放大」（`image_quality_enhance`，长宽各放大 2 倍）**改判为「图像清晰度增强」**（`image_definition_enhance`，尺寸不变、去噪变清晰）——运营方的应用只勾选了后者；两接口在控制台分别勾选，未勾选返回 `error_code=6`。代码/配置/文档里的 `upscale`→`enhance` 命名重构与本改判同批落地（`v1.1.0` 从未发布，故**不跳版本号**）。

### 新增

- **百度「图像清晰度增强」（`enhance_config`，默认关闭）**：出图成功后把每张成品图送 `image_definition_enhance`（去噪 + 优化纹理细节，**输出尺寸与输入相同**）再交付群聊，增强图另存为任务缓存目录下的新文件、**原图保留**兜底。**不是「图像无损放大」**：本插件不把图放大 2 倍。端点、调用形状（`application/x-www-form-urlencoded` + urlencode 后的 base64，`access_token` 走查询参数）、本地预检（base64 ≤10M、最短边 ≥10px、最长边 ≤5000px、长宽比 ≤4:1、jpg/png/bmp）与失败降级全部按百度文档口径实现；`access_token` 用两把 key 换取，内存缓存（30 天 − 5 分钟时钟余量），`110/111/100/HTTP 401` 失效重取 + **恰好重试一次**，并发按缓存键单飞（只打一次 token 接口）。
- 配置组 `enhance_config` **8 键**：`enable`（默认 **false**）/ `api_key` / `secret_key`（默认空）/ `max_image_mb`=10 / `max_edge`=5000 / `max_aspect`=4.0（float）/ `timeout_seconds`=20 / `enable_proxy`=false。最短边 10px 是官方硬下限，做成常量、不占配置键。键由 AstrBot 核心在插件加载时按 schema 自动补齐，**不需要手工迁移**（生产从未部署过旧组名，见 spec v2.11）。
- 增强相关的 DEBUG（每图 + 本地跳过的 `reason`）+ INFO（每任务一条 `增强 成功=N 跳过=N 失败=N`）+ WARNING（每失败类一次，含 `error_code=6` 时点明「控制台应用未勾选『图像清晰度增强』」）；`log_id` 只落 `task.runtime["enhance"]`，不进 INFO 汇总行。

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
