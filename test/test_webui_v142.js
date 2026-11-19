#!/usr/bin/env node
/**
 * IMAGO·FUSION v1.4.2 管理台前端回归（无第三方依赖）。
 *
 * 复现并守住 t2 的真实页面事件（`pages/webui/app.js`，基线 a1056441）：
 *   「选 A → 点击上传 → FileReader 挂起 → 切到 B → 放行」
 * 基线 3/3 会把 A 的图 POST 给 B（`persona_id` 在读文件**之后**才读活值
 * `state.current`），且迟到的完成回调无条件清空文件选择、无条件重载详情。
 *
 * 本回归断言的是**修好后的不变量**：
 *   1. 绑定的目标在**点击那一瞬**锁定，FileReader 期间与之后切换 Persona 都不影响；
 *   2. 迟到回调不清空"已经不是它"的文件选择，也不重载/覆盖新选中的界面；
 *   3. 目标未变时行为与历史一致（清空文件选择 + 重载详情）。
 *
 * 同一份用例可在基线上跑出红：把 `IMAGO_WEBUI_APP` 指向基线那份 app.js 即可
 * （例如 `git show a1056441:pages/webui/app.js > /tmp/app-base.js`）。
 * 由 `test/test_webui_v142.py` 以子进程调用，也可单独运行：
 * `node test/test_webui_v142.js`。
 */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const APP = process.env.IMAGO_WEBUI_APP
  ? path.resolve(process.env.IMAGO_WEBUI_APP)
  : path.join(ROOT, 'pages', 'webui', 'app.js');

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

const document = {
  querySelector(selector) {
    if (selector.startsWith('#')) return elements[selector.slice(1)] || null;
    return null;
  },
  querySelectorAll() { return []; },
  createElement(tag) { return makeElement(tag); },
  addEventListener() {},
  documentElement: { dataset: {} },
  body: { classList: { set: new Set(), toggle() {} } }
};

/* ------------------------------------------------------------ 假 Bridge + 夹具 */
const PERSONAS = ['persona-a', 'persona-b'];
const posted = [];
const detailCalls = [];

const window = {
  addEventListener() {},
  document,
  AstrBotPluginPage: {
    ready: async () => {},
    onContext() {},
    apiGet: async (endpoint, params) => {
      const query = params || {};
      if (endpoint === 'personas') {
        return { success: true, personas: PERSONAS.map(id => ({ id })) };
      }
      if (endpoint === 'detail') {
        detailCalls.push(query.persona_id);
        return {
          success: true,
          persona_id: query.persona_id,
          prompt: 'prompt of ' + query.persona_id,
          summary: '',
          summary_manual: false,
          references: []
        };
      }
      if (endpoint === 'preview') {
        return { success: true, mime_type: 'image/png', base64_data: 'AA==' };
      }
      // 节点与额度不是本专题的关注点：返回失败让初始化提前收尾，不制造假配置。
      return { success: false, message: '本用例不提供 ' + endpoint };
    },
    apiPost: async (endpoint, body) => {
      posted.push({ endpoint, body });
      if (endpoint === 'upload') return { success: true, message: '上传成功' };
      return { success: true, message: 'ok' };
    }
  }
};
window.window = window;

/* 可控 FileReader：真正的上传竞态就发生在"读文件这段异步窗口"里。 */
const readers = [];
class GateFileReader {
  constructor() {
    readers.push(this);
    this.result = '';
    this.onload = null;
    this.onerror = null;
    this.file = null;
  }

  readAsDataURL(file) {
    this.file = file;
    this.armed = true;
  }

  /** 放行：模拟浏览器读完文件后回调 `onload`。 */
  release() {
    if (!this.armed) throw new Error('FileReader 尚未 readAsDataURL');
    this.armed = false;
    this.result = 'data:image/png;base64,' + Buffer.from('bytes:' + this.file.name).toString('base64');
    if (this.onload) this.onload();
  }
}

const sandbox = {
  window, document, console, setTimeout, Promise, JSON, Set, Map, Number, String, Boolean,
  Math, Array, Object, Error, RegExp, Buffer, encodeURIComponent, decodeURIComponent,
  FileReader: GateFileReader
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(APP, 'utf8'), sandbox, { filename: APP });
const ui = window.__imagoWebui;

function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

async function flush(rounds = 12) {
  for (let index = 0; index < rounds; index += 1) await sleep(0);
}

async function waitFor(fn, label) {
  for (let index = 0; index < 400; index += 1) {
    if (fn()) return;
    await sleep(1);
  }
  throw new Error('等待超时：' + label);
}

/** 触发某个元素上注册的监听器（可选假事件），并按需等待返回的 Promise。 */
async function fire(id, event) {
  const handlers = (elements[id].listeners || {})[Object.keys(elements[id].listeners || {})[0]] || [];
  for (const handler of handlers) {
    const result = handler(event || { target: elements[id] });
    if (result && typeof result.then === 'function') await result;
  }
}

/**
 * 触发 click 监听器但**不等待**它返回的 Promise。
 *
 * 基线 a1056441 的 `#upload` 处理器是 `async () => {…await readFile…}`：在 FileReader
 * 放行前那个 Promise 一直挂着。若这里 `await` 它，事件循环会彻底空转、Node 直接以 0
 * 退出（静默漏测）。所以只调用、只吞掉 rejection，进度靠 `waitFor` 轮询推进。
 */
function clickFire(id) {
  const handlers = (elements[id].listeners || {}).click || [];
  for (const handler of handlers) {
    const result = handler({ target: elements[id] });
    if (result && typeof result.then === 'function') result.catch(() => {});
  }
}

async function waitReady() {
  await waitFor(() => ui && ui.state && ui.state.current === 'persona-a', '页面初始化到 persona-a');
  await flush();
}

/** 复位：回到"选中 A、没有待处理上传、没有待放行 FileReader"的干净起点。 */
function reset() {
  posted.length = 0;
  detailCalls.length = 0;
  elements['reference-files'].value = '';
  elements['reference-files'].files = [];
  elements['persona-title'].textContent = '';
  ui.state.current = 'persona-a';
  const leftover = readers.filter(reader => reader.armed);
  assert.strictEqual(leftover.length, 0, '上一轮有未放行的 FileReader，用例自身不干净');
}

async function switchTo(personaId) {
  elements['persona-select'].value = personaId;
  await fire('persona-select', { target: { value: personaId } });
  await flush();
}

/**
 * 跑一轮真实竞态：点击上传 → 等 FileReader 挂起 → 切到 B → 放行 → 收尾。
 * 返回这一次 upload POST 的载荷与本轮读到的 FileReader。
 */
async function runRace({ round, fileForA = 'a.png', fileForB = null }) {
  reset();
  elements['reference-files'].files = [{ name: fileForA, type: 'image/png' }];
  elements['reference-files'].value = 'C:\\fakepath\\' + fileForA;
  const before = readers.length;
  await clickFire('upload');
  await waitFor(() => readers.length > before, '第 ' + round + ' 轮 FileReader 被创建');
  const reader = readers[readers.length - 1];
  assert.ok(reader.armed, '第 ' + round + ' 轮：上传应停在读文件处（这正是竞态窗口）');
  assert.strictEqual(posted.filter(item => item.endpoint === 'upload').length, 0,
    '第 ' + round + ' 轮：读文件期间不该已经发出 upload');
  await switchTo('persona-b');
  if (fileForB) {
    // 切到 B 之后用户重新挑了 B 的图：迟到回调**不得**清掉它。
    elements['reference-files'].files = [{ name: fileForB, type: 'image/png' }];
    elements['reference-files'].value = 'C:\\fakepath\\' + fileForB;
  }
  reader.release();
  await flush(24);
  const uploads = posted.filter(item => item.endpoint === 'upload');
  return { uploads, reader };
}

(async () => {
  await waitReady();

  check('初始状态：Persona 列表第一项被选中并渲染', () => {
    assert.strictEqual(ui.state.current, 'persona-a');
    assert.strictEqual(elements['persona-title'].textContent, 'persona-a');
  });

  check('红证/绿证：选 A 点击上传→读文件挂起→切 B→放行，3/3 仍绑定 A', async () => {
    for (const round of [1, 2, 3]) {
      const { uploads } = await runRace({ round });
      assert.strictEqual(uploads.length, 1, '第 ' + round + ' 轮应恰好发出 1 次 upload');
      assert.strictEqual(uploads[0].body.persona_id, 'persona-a',
        '第 ' + round + ' 轮：绑定的目标必须在点击那一瞬锁定，不能跟着 state.current 漂移');
      assert.strictEqual(uploads[0].body.images.length, 1, '第 ' + round + ' 轮：应只带 1 张图');
      assert.ok(uploads[0].body.images[0].data, '第 ' + round + ' 轮：应带上读到的 base64');
    }
  });

  check('对照：不切换时 3/3 也发 A（证明上一条不是"恒发 A"）', async () => {
    for (const round of [1, 2, 3]) {
      reset();
      elements['reference-files'].files = [{ name: 'a.png', type: 'image/png' }];
      elements['reference-files'].value = 'C:\\fakepath\\a.png';
      ui.state.current = 'persona-b';
      const before = readers.length;
      await clickFire('upload');
      await waitFor(() => readers.length > before, '对照轮 FileReader');
      readers[readers.length - 1].release();
      await flush(24);
      const uploads = posted.filter(item => item.endpoint === 'upload');
      assert.strictEqual(uploads.length, 1);
      assert.strictEqual(uploads[0].body.persona_id, 'persona-b',
        '对照轮：点击时选中的就是 B，必须发 B');
      await switchTo('persona-a');
    }
  });

  check('迟到的完成回调不得清空"已经不是它"的文件选择', async () => {
    const { uploads } = await runRace({ round: 1, fileForB: 'b.png' });
    assert.strictEqual(uploads[0].body.persona_id, 'persona-a', '绑定目标仍应是点击时的 A');
    assert.strictEqual(elements['reference-files'].value, 'C:\\fakepath\\b.png',
      '切到 B 后新选的文件不能被 A 的迟到回调清空');
    assert.deepStrictEqual(elements['reference-files'].files.map(file => file.name), ['b.png']);
  });

  check('迟到的完成回调不得把 A 的详情重载到 B 的界面', async () => {
    const { uploads } = await runRace({ round: 1 });
    assert.strictEqual(uploads[0].body.persona_id, 'persona-a');
    assert.strictEqual(elements['persona-title'].textContent, 'persona-b',
      '界面上当前选中的是 B，标题不得被 A 的迟到回调改写');
    assert.strictEqual(elements['persona-prompt'].textContent, 'prompt of persona-b',
      '详情内容必须仍属于 B');
    assert.strictEqual(detailCalls[detailCalls.length - 1], 'persona-b',
      '最后一次详情请求必须属于 B（迟到回调不得替 A 再拉一次并覆盖界面）');
    assert.ok(!detailCalls.slice(-1).includes('persona-a'),
      '迟到回调不允许再请求 A 的详情');
  });

  check('目标未变时保持历史行为：清空文件选择并重载详情', async () => {
    reset();
    elements['reference-files'].files = [{ name: 'a.png', type: 'image/png' }];
    elements['reference-files'].value = 'C:\\fakepath\\a.png';
    const before = readers.length;
    await clickFire('upload');
    await waitFor(() => readers.length > before, 'FileReader');
    readers[readers.length - 1].release();
    await flush(24);
    const uploads = posted.filter(item => item.endpoint === 'upload');
    assert.strictEqual(uploads.length, 1);
    assert.strictEqual(uploads[0].body.persona_id, 'persona-a');
    assert.strictEqual(elements['reference-files'].value, '', '上传成功后应清空文件选择');
    assert.strictEqual(elements['persona-title'].textContent, 'persona-a');
    assert.ok(detailCalls.includes('persona-a'), '成功后应重载详情');
  });

  check('没有选中 Persona 时不上传、不发空 persona_id', async () => {
    reset();
    ui.state.current = '';
    elements['reference-files'].files = [{ name: 'a.png', type: 'image/png' }];
    const before = readers.length;
    await clickFire('upload');
    await flush(8);
    assert.strictEqual(readers.length, before, '没有 Persona 时不该开始读文件');
    assert.strictEqual(posted.filter(item => item.endpoint === 'upload').length, 0);
    assert.ok(elements['notice'].textContent.includes('Persona'), '应提示先选 Persona');
    ui.state.current = 'persona-a';
  });

  check('没有选图片时不上传', async () => {
    reset();
    elements['reference-files'].files = [];
    await clickFire('upload');
    await flush(8);
    assert.strictEqual(posted.filter(item => item.endpoint === 'upload').length, 0);
    assert.ok(elements['notice'].textContent.includes('图片'));
  });

  check('上传失败时不清空文件选择、不重载详情（用户可直接重试）', async () => {
    reset();
    const originalPost = window.AstrBotPluginPage.apiPost;
    window.AstrBotPluginPage.apiPost = async (endpoint, body) => {
      posted.push({ endpoint, body });
      if (endpoint === 'upload') return { success: false, message: '上传数据或图片格式无效' };
      return { success: true };
    };
    try {
      elements['reference-files'].files = [{ name: 'a.png', type: 'image/png' }];
      elements['reference-files'].value = 'C:\\fakepath\\a.png';
      const before = readers.length;
      await clickFire('upload');
      await waitFor(() => readers.length > before, 'FileReader');
      readers[readers.length - 1].release();
      await flush(24);
      assert.strictEqual(elements['reference-files'].value, 'C:\\fakepath\\a.png');
      assert.ok(elements['notice'].textContent.includes('无效'));
    } finally {
      window.AstrBotPluginPage.apiPost = originalPost;
    }
  });

  check('测试钩子导出上报路径（供 Node 回归直接调用）', () => {
    for (const name of ['uploadReferences', 'sameFiles', 'readFile', 'loadPersonaDetail']) {
      assert.strictEqual(typeof ui[name], 'function', '缺少导出：' + name);
    }
  });

  check('静态守卫：upload 的 persona_id 不得直读活值 state.current', () => {
    const source = fs.readFileSync(APP, 'utf8');
    assert.ok(!/apiPost\(\s*'upload'\s*,\s*\{\s*persona_id\s*:\s*state\.current/.test(source),
      'upload 载荷必须用点击时锁定的快照，不能把 live state.current 直接塞进 payload');
    assert.ok(/persona_id\s*:\s*personaId/.test(source), 'upload 载荷应使用锁定的 personaId');
  });

  check('静态守卫：迟到回调只在目标未变时才清空/重载', () => {
    const source = fs.readFileSync(APP, 'utf8');
    assert.ok(/state\.current\s*!==\s*personaId/.test(source), '需要显式的"目标是否已变"判定');
    assert.ok(/sameFiles\(/.test(source), '清空文件选择前必须比较文件集是否还是原来那批');
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
