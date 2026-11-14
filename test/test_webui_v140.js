#!/usr/bin/env node
/**
 * IMAGO·FUSION v1.4.0 管理台前端回归（无第三方依赖）。
 *
 * 用 Node 内置 `vm` 载入真实的 `pages/webui/app.js`，配一个最小 DOM 桩与假 Bridge，
 * 断言“字段表从 catalog 来、保存载荷无损、停用/主节点/参数错误/移动端样式”等行为。
 * 由 `test/test_v140_webui.py` 以子进程调用，也可单独运行：`node test/test_webui_v140.js`。
 * v1.4.0 起实验遮挡整组删除：这里只留「参考图文字降级」单组的数据驱动渲染、
 * 保存载荷与旧快照兼容（T-U1..T-U3/T-Y1）。
 */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const APP = path.join(ROOT, 'pages', 'webui', 'app.js');
const CSS = path.join(ROOT, 'pages', 'webui', 'style.css');
const HTML = path.join(ROOT, 'pages', 'webui', 'index.html');

let passed = 0;
let chain = Promise.resolve();
function check(name, fn) {
  // 串行等待每个用例（含异步 DOM/Bridge 用例），避免并发导致的 posted 状态串台。
  chain = chain.then(async () => {
    try {
      await fn();
      passed += 1;
      console.log('PASS ' + name);
    } catch (error) {
      console.error('FAIL ' + name);
      console.error(error && error.stack ? error.stack : error);
      process.exitCode = 1;
    }
  });
  return chain;
}

/* ------------------------------------------------------------ 最小 DOM 桩 */
function makeElement(tag) {
  const el = {
    tagName: tag,
    id: '',
    innerHTML: '',
    textContent: '',
    value: '',
    files: [],
    disabled: false,
    hidden: false,
    checked: false,
    type: 'text',
    style: {},
    dataset: {},
    parent: null,
    listeners: {},
    children: []
  };
  el.classList = {
    set: new Set(),
    toggle(name, force) {
      const on = force === undefined ? !this.set.has(name) : Boolean(force);
      if (on) this.set.add(name); else this.set.delete(name);
      return on;
    },
    add(name) { this.set.add(name); },
    remove(name) { this.set.delete(name); },
    contains(name) { return this.set.has(name); }
  };
  el.addEventListener = (type, handler) => { (el.listeners[type] = el.listeners[type] || []).push(handler); };
  el.setAttribute = (key, value) => { el[key] = value; };
  el.getAttribute = key => el[key] ?? null;
  el.focus = () => {};
  el.matches = selector => selector === '[data-fallback-vision]' ? Boolean(el.dataset.fallbackVision !== undefined) : false;
  el.querySelector = () => null;
  el.querySelectorAll = () => [];
  el.closest = () => el.parent;
  el.appendChild = child => { child.parent = el; el.children.push(child); };
  Object.defineProperty(el, 'textContent', {
    get() { return el._text || ''; },
    set(value) {
      el._text = String(value);
      el._html = String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }
  });
  Object.defineProperty(el, 'innerHTML', {
    get() { return el._html || ''; },
    set(value) { el._html = String(value); }
  });
  return el;
}

const IDS = [
  'notice', 'save-config', 'save-quotas', 'persona-search', 'persona-select', 'persona-title',
  'persona-prompt', 'persona-summary', 'summary-mode', 'summary-count', 'rebuild', 'upload',
  'save-summary', 'gallery', 'provider-list', 'provider-summary', 'provider-warnings',
  'config-groups', 'add-provider', 'optimizer-provider', 'vision-provider', 'quota-list',
  'quota-policy', 'quota-count', 'add-quota-row', 'reference-files'
];
const elements = {};
for (const id of IDS) {
  elements[id] = makeElement('div');
  elements[id].id = id;
}

const document = {
  querySelector(selector) {
    if (selector.startsWith('#')) return elements[selector.slice(1)] || null;
    return null;
  },
  querySelectorAll() { return []; },
  createElement(tag) { return makeElement(tag); },
  listeners: {},
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); },
  documentElement: { dataset: {} },
  body: { classList: { set: new Set(), toggle(name, force) { if (force) this.set.add(name); else this.set.delete(name); } } }
};

/* ------------------------------------------------------------ 夹具（与后端 catalog 形状一致） */
function field(key, label, control, type, extra) {
  return Object.assign({ key, label, control, type, default: '', options: [], placeholder: '', help: '说明', max_length: null }, extra || {});
}
const CATALOG = {
  version: 1,
  api_types: [
    { value: 'openai_image', label: 'OpenAI 图像接口', param_mode: 'typed',
      fields: [field('size', '尺寸 / 比例', 'text', 'str', { placeholder: '1024x1024' }),
               field('quality', '生成质量', 'select', 'str', { options: ['', 'auto', 'low', 'medium', 'high', 'xhigh', 'max'] })],
      capabilities: { quality: 'native', thinking: 'complexity', references: 'edits' } },
    { value: 'openai_chat', label: 'OpenAI Chat 接口', param_mode: 'typed', fields: [], capabilities: { quality: 'none', thinking: 'reasoning_effort', references: 'inline' } },
    { value: 'gemini_official', label: 'Gemini 官方接口', param_mode: 'typed',
      fields: [field('size', '尺寸 / 比例', 'text', 'str'), field('image_size', '输出分辨率', 'select', 'str', { options: ['', '512', '1K', '2K', '4K'] })],
      capabilities: { quality: 'undocumented', thinking: 'thinking_level', references: 'inline' } },
    { value: 'dashscope_multimodal', label: '百炼多模态', param_mode: 'typed',
      fields: [field('size', '尺寸', 'text', 'str'), field('prompt_extend', '提示词扩写', 'bool3', 'bool'),
               field('watermark', '水印', 'bool3', 'bool'), field('seed', '随机种子', 'int', 'int'),
               field('negative_prompt', '反向提示词', 'text', 'str')],
      capabilities: { quality: 'none', thinking: 'none', references: 'array' } },
    { value: 'custom_endpoint', label: '自定义 JSON 接口', param_mode: 'free', fields: [], capabilities: { quality: 'none', thinking: 'none', references: 'array' } },
    { value: 'astrbot_native', label: 'AstrBot 原生 Provider', param_mode: 'ignored', fields: [], capabilities: { quality: 'none', thinking: 'none', references: 'inline' } },
    { value: 'xai_image', label: 'xAI 图像接口', param_mode: 'typed',
      fields: [field('size', '尺寸', 'text', 'str'), field('resolution', '分辨率档', 'select', 'str', { options: ['', '1k', '1.5k', '2k'] }),
               field('edit_mode', '编辑模式', 'text', 'str')],
      capabilities: { quality: 'undocumented', thinking: 'none', references: 'edits' } }
  ],
  thinking_values: ['', 'off', 'low', 'medium', 'high', 'max'],
  thinking_labels: { '': '跟随默认（不发送）', off: '关闭', low: '低', medium: '中', high: '高', max: '最高（协议支持时映射到 xhigh）' },
  groups: [
    { key: 'reference_fallback_config', payload: 'reference_fallback', label: '参考图文字降级',
      description: '仅在节点明确以安全/内容合规原因拒绝参考图时，才在该节点内改用文字说明重试。',
      fields: [
        { key: 'enable', label: '启用文字降级', control: 'switch', type: 'bool', default: false, help: '默认关闭' },
        { key: 'max_retries_per_node', label: '每节点文字生成次数', control: 'number', type: 'int', default: 1, min: 0, max: 3, help: '0..3，0 关闭' },
        { key: 'caption_max_chars', label: '描述长度上限', control: 'number', type: 'int', default: 200, min: 80, max: 400, help: '80..400' },
        { key: 'vision_timeout_seconds', label: '识图超时（秒）', control: 'number', type: 'int', default: 20, min: 5, max: 60, help: '5..60' },
        { key: 'notify_user', label: '向用户说明', control: 'switch', type: 'bool', default: true, help: '告知已改用文字说明重绘' }
      ] }
  ]
};
const SNAPSHOT = {
  success: true, ok: true,
  providers: [
    { id: 'main', api_type: 'openai_image', enabled: true, thinking: 'high',
      custom_params: 'size=1536x1024\nfoo=bar', params: { size: '1536x1024', foo: 'bar' },
      param_errors: [{ line: 2, key: 'foo', reason: 'unknown_for_type', message: '未知键透传' }],
      param_mode: 'typed', capabilities: { quality: 'native', thinking: 'complexity', references: 'edits' },
      model: 'gpt-image', reference_image_limit: 0, available_models: [], base_url: 'https://a.invalid',
      api_keys_configured: true, default_size: '1024x1024', timeout: 180, astrbot_provider_id: '',
      stream: false, enable_proxy: false, fallback_order: 0, complete: true, primary: true, primary_disabled: false },
    { id: 'old<主>', api_type: 'astrbot_native', enabled: false, thinking: '', custom_params: '',
      params: {}, param_errors: [], param_mode: 'ignored', capabilities: { quality: 'none', thinking: 'none', references: true },
      model: '', reference_image_limit: 0, available_models: [], base_url: '', api_keys_configured: false,
      default_size: '1024x1024', timeout: 180, astrbot_provider_id: 'cpa/x', stream: true,
      enable_proxy: false, fallback_order: 1, complete: true, primary: false, primary_disabled: true }
  ],
  catalog: CATALOG,
  reference_fallback: { enable: false, max_retries_per_node: 1, caption_max_chars: 200, vision_timeout_seconds: 20, notify_user: true, vision_provider_id: '', vision_provider_ready: false },
  primary_provider_id: 'main',
  stored_primary_provider_id: 'old<主>',
  chat_providers: [], optimizer_provider_id: '', vision_provider_id: ''
};

/* ------------------------------------------------------------ 载入 app.js */
const posted = [];
const window = {
  addEventListener() {},
  document,
  AstrBotPluginPage: {
    ready: async () => {},
    onContext() {},
    apiGet: async endpoint => {
      if (endpoint === 'personas') return { success: true, personas: [] };
      if (endpoint === 'providers') return SNAPSHOT;
      if (endpoint === 'quotas') return { success: true, quotas: [] };
      return { success: true };
    },
    apiPost: async (endpoint, body) => {
      posted.push({ endpoint, body });
      return { success: true, ok: true, message: '节点配置已保存', warnings: [{ code: 'param_invalid_value', key: 'timeout', message: '超时无效' }], data: SNAPSHOT };
    }
  }
};
window.window = window;
const sandbox = {
  window, document, console, setTimeout, Promise, JSON, Set, Map, Number, String, Boolean,
  Math, Array, Object, Error, RegExp, encodeURIComponent, decodeURIComponent,
  FileReader: class { readAsDataURL() { this.onerror(new Error('no file')); } }
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(APP, 'utf8'), sandbox, { filename: APP });
const ui = window.__imagoWebui;

function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }
async function waitReady() {
  for (let index = 0; index < 50; index += 1) {
    if (ui.state.providers.length && elements['provider-list'].innerHTML.includes('provider-node')) return;
    await sleep(10);
  }
  throw new Error('页面初始化超时：provider-list 未渲染');
}

(async () => {
  await waitReady();
  const html = elements['provider-list'].innerHTML;
  const groupsHtml = elements['config-groups'].innerHTML;

  check('初始化后渲染两个节点且主节点取自有效值', () => {
    assert.strictEqual(ui.state.providers.length, 2);
    assert.strictEqual(ui.state.primary, 'main');
    assert.strictEqual(ui.state.storedPrimary, 'old<主>');
    assert.ok(html.includes('provider-node'));
  });

  check('存储主节点停用时展示“当前生效”提示', () => {
    const summary = elements['provider-summary'].innerHTML;
    assert.ok(summary.includes('存储主节点'));
    assert.ok(summary.includes('当前生效'));
    assert.ok(summary.includes('main'));
  });

  check('节点启用开关与停用徽章存在', () => {
    assert.ok(html.includes('data-field="enabled"'));
    assert.ok(html.includes('已停用'));
    assert.ok(html.includes('badge-off'));
  });

  check('七型能力逐值映射与契约一致（quality/thinking/references）', () => {
    const expected = {
      openai_image: ['原生支持（quality 参数）', '映射为 complexity', 'images/edits 或 JSON 图像字段'],
      openai_chat: ['不适用', '映射为 reasoning_effort', '内联图像（inlineData/contents）'],
      gemini_official: ['未文档化（写了会透传）', '映射为 thinkingLevel（仅能力声明模型）', '内联图像（inlineData/contents）'],
      dashscope_multimodal: ['不适用', '不发送思考字段', 'references[]'],
      custom_endpoint: ['不适用', '不发送思考字段', 'references[]'],
      astrbot_native: ['不适用', '不发送思考字段', '内联图像（inlineData/contents）'],
      xai_image: ['未文档化（写了会透传）', '不发送思考字段', 'images/edits 或 JSON 图像字段']
    };
    for (const entry of CATALOG.api_types) {
      const want = expected[entry.value];
      assert.ok(want, `夹具缺少 ${entry.value}`);
      const caps = ui.capabilitiesOf(entry.value);
      assert.strictEqual(ui.qualityText(caps.quality), want[0], `${entry.value} quality`);
      assert.strictEqual(ui.thinkingCapText(caps.thinking), want[1], `${entry.value} thinking`);
      assert.strictEqual(ui.referencesText(caps.references), want[2], `${entry.value} references`);
    }
    // 旧形状（bool）只做单例兼容，不再是主路径
    assert.strictEqual(ui.referencesText(true), '支持（旧形状）');
    assert.strictEqual(ui.referencesText(false), '不发送');
  });

  check('节点能力行渲染真实 catalog 文案（quality/references）', () => {
    assert.ok(html.includes('原生支持（quality 参数）'));
    assert.ok(html.includes('images/edits 或 JSON 图像字段'));
    assert.ok(html.includes('内联图像（inlineData/contents）'));
  });

  check('接口类型下拉来自 catalog（7 项，含 xai_image，不硬编码）', () => {
    const select = html.match(/<select data-field="api_type">([\s\S]*?)<\/select>/)[1];
    const values = [...select.matchAll(/<option value="([^"]*)"/g)].map(match => match[1]);
    assert.deepStrictEqual(values, CATALOG.api_types.map(item => item.value));
    assert.ok(values.includes('xai_image'));
    assert.ok(html.includes('xAI 图像接口'));
  });

  check('质量与分辨率控件由字段表生成', () => {
    assert.ok(html.includes('data-param="quality"'));
    assert.ok(html.includes('data-control="select"'));
    assert.ok(html.includes('option value="xhigh"'));
    assert.ok(html.includes('data-param="size"'));
  });

  check('thinking 下拉来自 vocab（6 值）且带协议承载说明', () => {
    const select = html.match(/<select data-field="thinking">([\s\S]*?)<\/select>/)[1];
    assert.strictEqual((select.match(/<option/g) || []).length, 6);
    assert.ok(html.includes('complexity'));
    assert.ok(html.includes('留空 = 不发送'));
  });

  check('高级参数原文与参数错误可见（未知行保留）', () => {
    assert.ok(html.includes('data-field="custom_params"'));
    assert.ok(html.includes('size=1536x1024'));
    assert.ok(html.includes('foo'));
    assert.ok(html.includes('当前接口不支持'));
  });

  check('原生节点独有字段按条件渲染', () => {
    const nativeBlock = html.split('data-index="1"')[1] || '';
    assert.ok(nativeBlock.includes('data-field="astrbot_provider_id"'));
    assert.ok(nativeBlock.includes('data-field="stream"'));
    const imageBlock = html.split('data-index="0"')[1].split('data-index="1"')[0];
    assert.ok(!imageBlock.includes('data-field="astrbot_provider_id"'));
  });

  check('节点名做 HTML 转义（防注入）', () => {
    assert.ok(html.includes('old&lt;主&gt;'));
    assert.ok(!html.includes('old<主>'));
  });

  check('降级组按 catalog 渲染出 5 个控件（T-U1）', () => {
    assert.ok(groupsHtml.includes('参考图文字降级'));
    ['enable', 'max_retries_per_node', 'caption_max_chars', 'vision_timeout_seconds', 'notify_user']
      .forEach(key => assert.ok(groupsHtml.includes('data-group-field="' + key + '"'), key));
    assert.ok(groupsHtml.includes('默认关闭'));
    // 实验面彻底消失：没有遮挡节点下拉、没有遮挡文案。
    assert.ok(!groupsHtml.includes('occluder_provider_id'));
    assert.ok(!groupsHtml.includes('occlusion_prompt'));
    assert.ok(!groupsHtml.includes('实验'));
    assert.ok(!groupsHtml.includes('遮挡'));
  });


  check('无上限字段不渲染空的 maxlength 属性', () => {
    ui.renderGroups();
    assert.ok(!elements['config-groups'].innerHTML.includes('maxlength=""'));
  });



  check('新接口类型无需改前端即可出现（catalog 驱动）', () => {
    const extended = JSON.parse(JSON.stringify(CATALOG));
    extended.api_types.push({ value: 'future_type', label: '未来接口', param_mode: 'typed', fields: [], capabilities: { quality: 'none', thinking: 'none', references: false } });
    ui.applySnapshot(Object.assign({}, SNAPSHOT, { catalog: extended }));
    const rendered = elements['provider-list'].innerHTML;
    assert.ok(rendered.includes('future_type'));
    assert.ok(rendered.includes('未来接口'));
    ui.applySnapshot(SNAPSHOT);
  });

  check('typed 参数写入保留未知行与其他已知行', () => {
    const node = ui.state.providers[0];
    node.custom_params = 'size=1536x1024\nfoo=bar';
    ui.setParamValue(node, 'quality', 'high', 'select');
    assert.strictEqual(node.custom_params, 'quality=high\nsize=1536x1024\nfoo=bar');
    ui.setParamValue(node, 'quality', '', 'select');
    assert.strictEqual(node.custom_params, 'size=1536x1024\nfoo=bar');
    assert.ok(!node.custom_params.includes('quality'));
  });

  check('保存载荷含 v1.3.0 节点新键且不含只读字段', () => {
    const payload = ui.buildProviderPayload();
    const entry = payload.providers[0];
    ['enabled', 'thinking', 'custom_params', 'api_keys', 'fallback_order', 'timeout', 'default_size'].forEach(key => {
      assert.ok(Object.prototype.hasOwnProperty.call(entry, key), '缺少键 ' + key);
    });
    assert.ok(!Object.prototype.hasOwnProperty.call(entry, 'params'));
    assert.ok(!Object.prototype.hasOwnProperty.call(entry, 'capabilities'));
    assert.strictEqual(JSON.stringify(entry.api_keys), '[]');
    assert.strictEqual(entry.astrbot_provider_id, '');
    assert.strictEqual(entry.stream, false);
  });

  check('保存载荷只有降级组 5 键且不含只读字段（T-U2）', () => {
    const payload = ui.buildProviderPayload();
    assert.strictEqual(JSON.stringify(Object.keys(payload.reference_fallback).sort()),
      JSON.stringify(['caption_max_chars', 'enable', 'max_retries_per_node', 'notify_user', 'vision_timeout_seconds']));
    assert.ok(!('vision_provider_id' in payload.reference_fallback));
    assert.ok(!('vision_provider_ready' in payload.reference_fallback));
    assert.ok(!('experimental_reference' in payload));
  });



  check('改动字段会标脏并更新 payload', () => {
    const nodeElement = makeElement('article');
    nodeElement.dataset.index = '0';
    const target = makeElement('input');
    target.dataset.field = 'thinking';
    target.value = 'low';
    target.type = 'text';
    target.parent = nodeElement;
    nodeElement.closest = () => nodeElement;
    target.closest = () => nodeElement;
    ui.syncProviderField(target);
    assert.strictEqual(ui.state.providers[0].thinking, 'low');
    assert.strictEqual(ui.state.dirty, true);
    const payload = ui.buildProviderPayload();
    assert.strictEqual(payload.providers[0].thinking, 'low');
  });

  check('FF-01 未点选主节点时普通保存不提交 primary_provider_id', () => {
    assert.ok(!('primary_provider_id' in ui.buildProviderPayload()));
    const fake = makeElement('button');
    fake.dataset.primary = '0';
    fake.closest = selector => selector === '[data-primary]' ? fake : null;
    document.listeners.click[0]({ target: fake });
    assert.strictEqual(ui.state.primaryChosen, true);
    assert.strictEqual(ui.buildProviderPayload().primary_provider_id, ui.state.primary);
    ui.state.primaryChosen = false;
  });

  check('FF-07 已有节点 ID 只读；新增节点连续键入不重渲染', () => {
    assert.ok(html.includes('已有节点的 ID 只读'));
    const before = elements['provider-list'].innerHTML;
    assert.ok(/data-field="id"[^>]*readonly/.test(before));
    elements['add-provider'].listeners.click[0]();
    const index = ui.state.providers.length - 1;
    assert.strictEqual(ui.state.providers[index].isNew, true);
    const root = elements['provider-list'];
    let htmlValue = root.innerHTML;
    let renders = 0;
    Object.defineProperty(root, 'innerHTML', {
      configurable: true,
      get() { return htmlValue; },
      set(value) { renders += 1; htmlValue = String(value); }
    });
    const nodeElement = makeElement('article');
    nodeElement.dataset.index = String(index);
    nodeElement.closest = () => nodeElement;
    const fieldInput = makeElement('input');
    fieldInput.dataset.field = 'id';
    fieldInput.type = 'text';
    fieldInput.closest = () => nodeElement;
    renders = 0;
    fieldInput.value = 'my-node';
    ui.syncProviderField(fieldInput);
    fieldInput.value = 'my-node-2';
    ui.syncProviderField(fieldInput);
    assert.strictEqual(renders, 0, 'ID 输入过程中不应整体重渲染');
    assert.strictEqual(ui.state.providers[index].id, 'my-node-2');
    ui.state.providers.pop();
    ui.renderProviders();
  });

  check('FF-R2-01 确认文案区分 HTTP 强制停用与 native 保留鉴权/启停', () => {
    const httpMsg = ui.clearConfirmMessage([{ id: 'main', api_type: 'openai_image' }]);
    assert.ok(httpMsg.includes('强制停用'), 'HTTP 文案必须说明服务端强制停用');
    assert.ok(httpMsg.includes('无法发起调用'));
    const nativeMsg = ui.clearConfirmMessage([{ id: 'old<主>', api_type: 'astrbot_native' }]);
    assert.ok(nativeMsg.includes('原生 Provider 鉴权'), nativeMsg);
    assert.ok(nativeMsg.includes('节点启停'), nativeMsg);
    assert.ok(!nativeMsg.includes('强制停用该节点'), 'native 不应被描述为强制停用');
  });

  check('FF-R2-01 取消零 POST；确认后 clear flag 进入 POST 并保留到成功', async () => {
    ui.applySnapshot(SNAPSHOT);
    let released = ui.state.providers.find(item => item.id === 'main');
    released.api_keys_configured = true;
    released.clear_api_keys = true;
    const postedBefore = posted.length;
    window.confirm = () => false;
    elements['save-config'].listeners.click[0]();
    await sleep(30);
    assert.strictEqual(posted.length, postedBefore, '取消时不得发送保存请求');
    released = ui.state.providers.find(item => item.id === 'main');
    assert.strictEqual(released.clear_api_keys, false, '取消后应复位临时 flag');
    assert.strictEqual(released.enabled, true, '取消不得本地改启停');

    released.api_keys_configured = true;
    released.clear_api_keys = true;
    released.enabled = true;
    window.confirm = () => true;
    elements['save-config'].listeners.click[0]();
    await sleep(50);
    const body = posted[posted.length - 1].body;
    const entry = body.providers.find(item => item.id === 'main');
    assert.strictEqual(entry.clear_api_keys, true, '确认后必须把 clear flag 带进 POST');
    assert.strictEqual(entry.enabled, true, '服务端负责强制停用，前端不得伪造 enabled=false');
    const after = ui.state.providers.find(item => item.id === 'main');
    assert.strictEqual(after.clear_api_keys, false, '成功回包后应由 snapshot 复位临时 flag');
  });

  check('FF-03 删除只提交显式名单（旧快照有、当前列表没有的 ID）', () => {
    ui.applySnapshot(SNAPSHOT);
    ui.state.providers = ui.state.providers.filter(item => item.id !== 'old<主>');
    const payload = ui.buildProviderPayload();
    assert.strictEqual(JSON.stringify(payload.deleted_provider_ids), JSON.stringify(['old<主>']));
    ui.applySnapshot(SNAPSHOT);
  });

  check('保存后应用返回快照并展示 warnings', async () => {
    elements['save-config'].listeners.click[0]();
    await sleep(50);
    assert.ok(posted.length >= 1);
    assert.strictEqual(posted[posted.length - 1].endpoint, 'providers/save');
    assert.ok(elements['provider-warnings'].innerHTML.includes('超时无效'));
  });

  check('style.css 仅一个 max-width 断点且新字段区在窄屏单列', () => {
    const css = fs.readFileSync(CSS, 'utf8');
    const breaks = css.match(/@media\s*\(max-width/g) || [];
    assert.strictEqual(breaks.length, 1);
    assert.ok(css.includes('@media(max-width:760px)'));
    const mobile = css.slice(css.indexOf('@media(max-width:760px)'));
    assert.ok(mobile.includes('.field-grid'));
    assert.ok(mobile.includes('.group-fields'));
    assert.ok(mobile.includes('grid-template-columns:1fr'));
  });

  check('index.html 引入新增容器且不含内联脚本', () => {
    const source = fs.readFileSync(HTML, 'utf8');
    ['provider-summary', 'provider-warnings', 'config-groups', 'provider-list'].forEach(id => {
      assert.ok(source.includes('id="' + id + '"'), '缺少 #' + id);
    });
    assert.ok(!/<script(?![^>]*src=)/.test(source));
  });

  check('app.js 不复制字段表（无 7 类型硬编码数组）', () => {
    const source = fs.readFileSync(APP, 'utf8');
    assert.ok(!source.includes("'openai_image','openai_chat'"));
    assert.ok(!source.includes('"openai_image","openai_chat"'));
    assert.ok(!source.includes("quality:'high'"));
  });

  if (process.env.IMAGO_WEBUI_CATALOG) {
    check('夹具 catalog 与真实 API 输出一致（桥接）', () => {
      const live = JSON.parse(fs.readFileSync(process.env.IMAGO_WEBUI_CATALOG, 'utf8'));
      assert.strictEqual(CATALOG.version, live.version);
      const liveByValue = {};
      for (const entry of live.api_types) liveByValue[entry.value] = entry;
      assert.strictEqual(CATALOG.api_types.length, live.api_types.length);
      for (const entry of CATALOG.api_types) {
        const real = liveByValue[entry.value];
        assert.ok(real, '真实 catalog 缺少 ' + entry.value);
        assert.deepStrictEqual(entry.capabilities, real.capabilities, entry.value + ' capabilities');
        assert.strictEqual(entry.param_mode, real.param_mode, entry.value + ' param_mode');
        assert.deepStrictEqual(entry.fields.map(f => f.key), real.fields.map(f => f.key), entry.value + ' fields');
      }
    });
  }

  await chain;
  if (process.exitCode) {
    console.error('前端回归失败：' + passed + ' 项通过，存在失败项');
    process.exit(1);
  }
  console.log('PASS 全部 ' + passed + ' 项前端回归');
})().catch(error => {
  console.error('FAIL 测试运行异常');
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
