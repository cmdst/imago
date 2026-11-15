#!/usr/bin/env node
/**
 * IMAGO·FUSION v1.4.1 管理台前端回归（无第三方依赖）。
 *
 * 用 Node 内置 `vm` 载入真实的 `pages/webui/app.js`，配最小 DOM 桩与假 Bridge，
 * 验证 `reference_compress_config` 这一组是**纯数据驱动**地出现在「参考图策略」里：
 * catalog 说什么就渲染什么、保存载荷按 catalog 字段表装配、`false` / `0` 原样往返、
 * 只读派生字段不回流、开关带 `role=switch`/`aria-checked`、数值控件带 min/max/step。
 * 由 `test/test_webui_v141.py` 以子进程调用，也可单独运行：`node test/test_webui_v141.js`。
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
      if (on) this.set.add(name);
      else this.set.delete(name);
      return on;
    },
    add(name) { this.set.add(name); },
    remove(name) { this.set.delete(name); },
    contains(name) { return this.set.has(name); }
  };
  el.addEventListener = (type, handler) => { (el.listeners[type] = el.listeners[type] || []).push(handler); };
  el.setAttribute = (key, value) => { el[key] = value; };
  el.getAttribute = key => (key in el ? el[key] : null);
  el.focus = () => {};
  el.matches = () => false;
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

const documentListeners = {};
const document = {
  querySelector(selector) {
    if (selector.startsWith('#')) return elements[selector.slice(1)] || null;
    return null;
  },
  querySelectorAll() { return []; },
  createElement(tag) { return makeElement(tag); },
  addEventListener(type, handler) { (documentListeners[type] = documentListeners[type] || []).push(handler); },
  documentElement: { dataset: {} },
  body: { classList: { set: new Set(), toggle() {} } }
};

/* ------------------------------------------------------------ 夹具：两组都在 catalog 里 */
function field(key, label, control, type, extra) {
  return Object.assign(
    { key, label, control, type, default: '', options: [], placeholder: '', help: '说明', max_length: null },
    extra || {}
  );
}

const COMPRESS_KEYS = ['enable', 'max_edge', 'jpeg_quality', 'target_kb', 'min_bytes', 'keep_alpha', 'max_pixels'];
const FALLBACK_KEYS = ['enable', 'max_retries_per_node', 'caption_max_chars', 'vision_timeout_seconds', 'notify_user'];

const COMPRESS_GROUP = {
  key: 'reference_compress_config',
  payload: 'reference_compress',
  label: '参考图压缩',
  description: '发往节点的参考图副本先压缩；inputs/ 原素材与出图结果不变。',
  fields: [
    { key: 'enable', label: '启用参考图压缩', control: 'switch', type: 'bool', default: true, help: '关闭后零解码、零新增文件、零新增日志' },
    { key: 'max_edge', label: '最长边上限（像素）', control: 'number', type: 'int', default: 1536, min: 0, max: 4096, help: '0 = 完全不缩放' },
    { key: 'jpeg_quality', label: 'JPEG 起始质量', control: 'number', type: 'int', default: 85, min: 40, max: 95, help: '40-95，未达标按 10 递减' },
    { key: 'target_kb', label: '目标体积（KB）', control: 'number', type: 'int', default: 128, min: 32, max: 2048, help: '1 KB = 1024 字节；目标不是硬限制' },
    { key: 'min_bytes', label: '压缩起点（字节）', control: 'number', type: 'int', default: 32768, min: 0, max: 1048576, help: '小于该字节数原样发送' },
    { key: 'keep_alpha', label: '保留透明通道', control: 'switch', type: 'bool', default: false, help: '关闭时白底 JPEG；开启时 PNG' },
    { key: 'max_pixels', label: '像素总数上限', control: 'number', type: 'int', default: 40000000, min: 1000000, max: 100000000, help: '超过不参与压缩' }
  ]
};

const FALLBACK_GROUP = {
  key: 'reference_fallback_config',
  payload: 'reference_fallback',
  label: '参考图文字降级',
  description: '仅在节点明确以安全/内容合规原因拒绝参考图时改走文字重绘。',
  fields: [
    { key: 'enable', label: '启用文字降级', control: 'switch', type: 'bool', default: false, help: '默认关闭' },
    { key: 'max_retries_per_node', label: '每节点纯文字重试次数', control: 'number', type: 'int', default: 1, min: 0, max: 3, help: '0 = 关闭' },
    { key: 'caption_max_chars', label: '描述长度上限（字符）', control: 'number', type: 'int', default: 200, min: 80, max: 400, help: '80-400' },
    { key: 'vision_timeout_seconds', label: '识图超时（秒）', control: 'number', type: 'int', default: 20, min: 5, max: 60, help: '单次识图上限' },
    { key: 'notify_user', label: '降级时告知用户', control: 'switch', type: 'bool', default: true, help: '默认告知' }
  ]
};

function baseCatalog() {
  return {
    version: 1,
    api_types: [
      {
        value: 'openai_image',
        label: 'OpenAI 图像接口',
        param_mode: 'typed',
        fields: [field('size', '尺寸 / 比例', 'text', 'str', { placeholder: '1024x1024' })],
        capabilities: { quality: 'native', thinking: 'complexity', references: 'edits' }
      }
    ],
    thinking_values: ['', 'off', 'low', 'high'],
    thinking_labels: { '': '跟随默认（不发送）', off: '关闭', low: '低', high: '高' },
    groups: JSON.parse(JSON.stringify([FALLBACK_GROUP, COMPRESS_GROUP]))
  };
}

function baseSnapshot() {
  return {
    success: true,
    ok: true,
    providers: [
      {
        id: 'main', api_type: 'openai_image', enabled: true, thinking: 'high', custom_params: '',
        params: {}, param_errors: [], param_mode: 'typed',
        capabilities: { quality: 'native', thinking: 'complexity', references: 'edits' },
        model: 'gpt-image', reference_image_limit: 0, available_models: [], base_url: 'https://a.invalid',
        api_keys_configured: true, default_size: '1024x1024', timeout: 180, astrbot_provider_id: '',
        stream: false, enable_proxy: false, fallback_order: 0, complete: true, primary: true,
        primary_disabled: false
      }
    ],
    catalog: baseCatalog(),
    reference_fallback: {
      enable: false, max_retries_per_node: 1, caption_max_chars: 200, vision_timeout_seconds: 20,
      notify_user: true, vision_provider_id: '', vision_provider_ready: false
    },
    // 关键夹具：从配置里读出来就是 false / 0 —— 前端必须原样往返，不能按 truthy 覆盖。
    reference_compress: {
      enable: false, max_edge: 0, jpeg_quality: 85, target_kb: 128,
      min_bytes: 32768, keep_alpha: false, max_pixels: 40000000
    },
    primary_provider_id: 'main',
    stored_primary_provider_id: 'main',
    chat_providers: [],
    optimizer_provider_id: '',
    vision_provider_id: ''
  };
}

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
      if (endpoint === 'providers') return baseSnapshot();
      if (endpoint === 'quotas') return { success: true, quotas: [] };
      return { success: true };
    },
    apiPost: async (endpoint, body) => {
      posted.push({ endpoint, body });
      return {
        success: true,
        ok: true,
        message: '节点配置已保存',
        warnings: [
          {
            code: 'param_invalid_value',
            key: 'reference_compress_config.target_kb',
            message: '参考图压缩的「目标体积（KB）」取值无效，已保留原值。'
          }
        ],
        data: baseSnapshot()
      };
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

function groupCards() {
  return elements['config-groups'].innerHTML;
}

/** 造一个位于某张组卡片内的假控件。 */
function fakeGroupInput(payload, key, value, type) {
  const card = makeElement('article');
  card.dataset.payload = payload;
  const target = makeElement('input');
  target.dataset.groupField = key;
  target.type = type || 'text';
  target.value = value;
  target.parent = card;
  target.closest = selector => (selector === '[data-payload]' ? card : null);
  return target;
}

(async () => {
  await waitReady();

  check('两张组卡片都渲染出来（catalog 驱动，不写死单组）', () => {
    const html = groupCards();
    assert.ok(html.includes('data-group="reference_fallback_config"'), '缺少文字降级卡片');
    assert.ok(html.includes('data-group="reference_compress_config"'), '缺少参考图压缩卡片');
    const cards = html.match(/class="card config-card/g) || [];
    assert.strictEqual(cards.length, 2, '应渲染 2 张组卡片，实际 ' + cards.length);
  });

  check('标题区泛化为「参考图策略」且说明与增强/放大分离', () => {
    const html = groupCards();
    const head = html.slice(0, html.indexOf('<article'));
    assert.ok(head.includes('参考图策略'), '标题区应泛化为参考图策略');
    assert.ok(!head.includes('<h2>参考图文字降级</h2>'), '标题区不得写死单组名');
    assert.ok(head.includes('增强') && head.includes('放大'), '需要说明与增强/放大是两回事');
    assert.ok(html.includes('参考图压缩'), '压缩组卡片标题');
  });

  check('压缩组按 catalog 渲染 7 个控件', () => {
    const html = groupCards();
    COMPRESS_KEYS.forEach(key => {
      assert.ok(html.includes('data-group-field="' + key + '"'), '缺少 ' + key);
    });
    const compressCard = html.split('data-group="reference_compress_config"')[1] || '';
    assert.ok(compressCard.includes('只压缩发往节点') || compressCard.includes('副本'), '卡片需说明只影响副本');
  });

  check('开关带 role=switch 且 aria-checked 反映真实值', () => {
    const html = groupCards();
    assert.ok(html.includes('role="switch"'), '开关必须有 role=switch');
    const card = html.split('data-group="reference_compress_config"')[1] || '';
    const enableInput = card.split('data-group-field="enable"')[1] || '';
    assert.ok(enableInput.slice(0, 120).includes('aria-checked="false"'), 'enable=false 必须 aria-checked="false"');
    const alphaInput = card.split('data-group-field="keep_alpha"')[1] || '';
    assert.ok(alphaInput.slice(0, 120).includes('aria-checked="false"'), 'keep_alpha=false 必须 aria-checked="false"');
  });

  check('数值控件带 min/max/step（单位与钳制可见）', () => {
    const card = (groupCards().split('data-group="reference_compress_config"')[1] || '');
    const expect = {
      max_edge: ['min="0"', 'max="4096"'],
      jpeg_quality: ['min="40"', 'max="95"'],
      target_kb: ['min="32"', 'max="2048"'],
      min_bytes: ['min="0"', 'max="1048576"'],
      max_pixels: ['min="1000000"', 'max="100000000"']
    };
    Object.keys(expect).forEach(key => {
      const at = card.indexOf('data-group-field="' + key + '"');
      assert.ok(at > 0, '缺少控件 ' + key);
      const chunk = card.slice(Math.max(0, at - 90), at + 90);
      expect[key].forEach(token => assert.ok(chunk.includes(token), key + ' 缺少 ' + token));
      assert.ok(chunk.includes('step="1"'), key + ' 缺少 step=1');
    });
  });

  check('保存载荷含压缩组全部 7 键（catalog 字段表装配）', () => {
    const payload = ui.buildProviderPayload();
    assert.ok(payload.reference_compress, '载荷缺少 reference_compress_config');
    assert.strictEqual(
      JSON.stringify(Object.keys(payload.reference_compress)),
      JSON.stringify(COMPRESS_KEYS)
    );
  });

  check('false 与 max_edge=0 原样往返（不被 truthy 覆盖）', () => {
    const values = ui.buildProviderPayload().reference_compress;
    assert.strictEqual(values.enable, false, 'enable=false 不能被感叹号/默认值覆盖');
    assert.strictEqual(values.max_edge, 0, 'max_edge=0 是有效值（禁止缩放）');
    assert.strictEqual(values.keep_alpha, false);
    assert.strictEqual(values.target_kb, 128);
  });

  check('载荷不夹带只读派生字段', () => {
    const payload = ui.buildProviderPayload();
    assert.ok(!('vision_provider_id' in payload.reference_fallback));
    assert.ok(!('vision_provider_ready' in payload.reference_fallback));
    assert.deepStrictEqual(Object.keys(payload.reference_fallback).sort(), FALLBACK_KEYS.slice().sort());
    assert.ok(!('vision_provider_id' in payload.reference_compress));
  });

  check('catalog 增字段无需改前端（新增键自动出现）', () => {
    const snapshot = baseSnapshot();
    snapshot.catalog.groups[1].fields.push(
      { key: 'future_knob', label: '未来开关', control: 'switch', type: 'bool', default: true, help: '未改前端也能渲染' }
    );
    snapshot.reference_compress.future_knob = false;
    ui.applySnapshot(snapshot);
    assert.ok(groupCards().includes('data-group-field="future_knob"'), '新字段未渲染');
    assert.strictEqual(ui.buildProviderPayload().reference_compress.future_knob, false, '新字段未进入载荷');
    ui.applySnapshot(baseSnapshot());
    assert.strictEqual(
      JSON.stringify(Object.keys(ui.buildProviderPayload().reference_compress)),
      JSON.stringify(COMPRESS_KEYS)
    );
  });

  check('切换开关即时同步 aria-checked 并进入载荷', () => {
    const target = fakeGroupInput('reference_compress', 'enable', '', 'checkbox');
    target.checked = true;
    ui.syncGroupField(target);
    assert.strictEqual(ui.state.dirty, true);
    assert.strictEqual(target.getAttribute('aria-checked'), 'true', '开关必须同步 aria-checked');
    assert.strictEqual(ui.buildProviderPayload().reference_compress.enable, true);
    const off = fakeGroupInput('reference_compress', 'keep_alpha', '', 'checkbox');
    off.checked = true;
    ui.syncGroupField(off);
    assert.strictEqual(ui.buildProviderPayload().reference_compress.keep_alpha, true);
  });

  check('数值输入照常写入；清空不清掉已存值', () => {
    const target = fakeGroupInput('reference_compress', 'max_edge', '1024', 'number');
    ui.syncGroupField(target);
    assert.strictEqual(ui.buildProviderPayload().reference_compress.max_edge, 1024);
    const blank = fakeGroupInput('reference_compress', 'target_kb', '', 'number');
    ui.syncGroupField(blank);
    assert.strictEqual(
      ui.buildProviderPayload().reference_compress.target_kb,
      128,
      '清空数字框不应把目标写坏（保留原值）'
    );
    assert.strictEqual(blank.value, '128', '清空后应回填原值，避免用户误以为已改为 0');
  });

  check('0 不会被当作“空”而丢失', () => {
    const target = fakeGroupInput('reference_compress', 'max_edge', '0', 'number');
    ui.syncGroupField(target);
    assert.strictEqual(ui.state.groupValues.reference_compress.max_edge, 0);
    assert.strictEqual(ui.buildProviderPayload().reference_compress.max_edge, 0);
  });

  check('保存后 warnings 可见且指向具体组字段', async () => {
    elements['save-config'].listeners.click[0]();
    await sleep(60);
    assert.ok(posted.length >= 1, '未发出保存请求');
    const last = posted[posted.length - 1];
    assert.strictEqual(last.endpoint, 'providers/save');
    assert.ok(last.body.reference_compress, '保存请求必须带压缩组');
    const warnings = elements['provider-warnings'].innerHTML;
    assert.ok(warnings.includes('目标体积'), '警告应显示具体字段：' + warnings);
  });

  check('快照切换后两组数值都跟着更新（不缓存旧值）', () => {
    const snapshot = baseSnapshot();
    snapshot.reference_compress = {
      enable: true, max_edge: 2048, jpeg_quality: 70, target_kb: 512,
      min_bytes: 0, keep_alpha: true, max_pixels: 8000000
    };
    ui.applySnapshot(snapshot);
    const html = groupCards();
    const card = html.split('data-group="reference_compress_config"')[1] || '';
    assert.ok((card.split('data-group-field="max_edge"')[1] || '').slice(0, 120).includes('value="2048"'));
    assert.strictEqual(ui.buildProviderPayload().reference_compress.keep_alpha, true);
    assert.strictEqual(ui.buildProviderPayload().reference_compress.min_bytes, 0);
    ui.applySnapshot(baseSnapshot());
  });

  check('app.js 不复制压缩组字段表（纯数据驱动）', () => {
    const source = fs.readFileSync(APP, 'utf8');
    ['max_pixels', 'target_kb', 'min_bytes', 'jpeg_quality', 'keep_alpha', 'max_edge'].forEach(key => {
      assert.ok(
        !source.includes("'" + key + "'") && !source.includes('"' + key + '"'),
        'app.js 不得硬编码字段名 ' + key
      );
    });
    assert.ok(!source.includes('reference_compress_config'), 'app.js 不得硬编码组 key');
    assert.ok(!source.includes('reference_fallback_config'), 'app.js 不得硬编码组 key');
  });

  check('样式仍只有一个 max-width 断点，组字段在窄屏单列', () => {
    const css = fs.readFileSync(CSS, 'utf8');
    assert.strictEqual((css.match(/@media\s*\(max-width/g) || []).length, 1);
    assert.ok(css.includes('@media(max-width:760px)'));
    const mobile = css.slice(css.indexOf('@media(max-width:760px)'));
    assert.ok(mobile.includes('.group-fields'));
    assert.ok(mobile.includes('grid-template-columns:1fr'));
    assert.ok(
      /\.field-grid\{[^}]*grid-template-columns:repeat\(2,minmax\(0,1fr\)\)[^}]*min-width:0/.test(css),
      '宽屏两列且允许收缩'
    );
    assert.ok(/\.group-fields\{[^}]*margin-top:12px/.test(css), '组字段容器沿用正式版间距');
  });

  check('index.html 仍无内联脚本，样式只来自 style.css', () => {
    const source = fs.readFileSync(HTML, 'utf8');
    assert.ok(source.includes('id="config-groups"'));
    assert.ok(!/<script(?![^>]*src=)/.test(source), '不得出现内联脚本');
    assert.ok(!/\son[a-z]+=/.test(source), '不得出现内联事件');
  });

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
