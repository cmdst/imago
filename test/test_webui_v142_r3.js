#!/usr/bin/env node
/**
 * IMAGO·FUSION v1.4.2 R3 前端回归（无第三方依赖）：详情未就绪时的跨人格写入。
 *
 * 复现的现场事件（t11 R2-05）：界面上仍是 A 的图卡与 A 的摘要文本，用户切到 B，
 * 而 B 的详情请求还没回来；此时
 *   - 点旧卡上的「删除这张图」→ POST `persona_id=B, filename=A 的图`（删他人的图）
 *   - 点「保存外观摘要」→ POST `persona_id=B, summary=A 的摘要`（覆盖他人的摘要）
 * 因为这几处写操作都直接读活值 `state.current`，而界面内容来自**上一次已渲染的详情**。
 *
 * 本回归断言修好后的不变量：
 *   1. 写操作的目标只来自「当前已渲染的那一代详情」，绝不用 `state.current` 猜；
 *   2. 详情尚未就绪（没有已渲染详情 / 已有一代在途）时，写被拒绝并给出可重试提示，
 *      既不写到即将变成别人的对象上，也不写到旧对象上；
 *   3. 详情回来后写操作恢复正常，目标是新渲染的那个人格；
 *   4. A→B→A 快速切换同样按代次判定，不会把第一代 A 的界面当成第三代 A；
 *   5. 正在读文件的旧上传仍绑定点击瞬间的 A（该路径不改成渲染目标）。
 *
 * 基线红证：把 `IMAGO_WEBUI_APP` 指向未修版本即可（本目录 a2d94a8 的 app.js）：
 *   `git show a2d94a8:pages/webui/app.js > /tmp/app-r3-base.js`
 *   `IMAGO_WEBUI_APP=/tmp/app-r3-base.js node test/test_webui_v142_r3.js`
 * 由 `test/test_v142_persona_r3.py` 以子进程调用，也可单独运行。
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
    tagName: tag, id: '', innerHTML: '', textContent: '', value: '', files: [],
    disabled: false, hidden: false, checked: false, type: 'text', style: {},
    dataset: {}, parent: null, listeners: {}, children: []
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
  el.getAttribute = key => (key in el ? el[key] : null);
  el.focus = () => {};
  el.matches = () => false;
  el.querySelector = () => null;
  el.querySelectorAll = () => [];
  el.closest = () => el.parent;
  el.appendChild = child => { child.parent = el; el.children.push(child); };
  Object.defineProperty(el, 'textContent', {
    get() { return el._text || ''; },
    set(value) { el._text = String(value); el._html = String(value); }
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

const documentClicks = [];
const document = {
  querySelector(selector) { return selector.startsWith('#') ? (elements[selector.slice(1)] || null) : null; },
  querySelectorAll() { return []; },
  createElement(tag) { return makeElement(tag); },
  addEventListener(type, handler) { if (type === 'click') documentClicks.push(handler); },
  documentElement: { dataset: {} },
  body: { classList: { set: new Set(), toggle() {} } }
};

/* ------------------------------------------------------------ 假 Bridge + 夹具 */
const PERSONAS = ['persona-a', 'persona-b'];
const posted = [];
const detailCalls = [];
const holds = [];
const detailRefs = { 'persona-a': [{ name: 'a-1.png' }], 'persona-b': [{ name: 'b-1.png' }] };
let detailGeneration = 0;

function holdNext(match) {
  let release;
  const promise = new Promise(resolve => { release = resolve; });
  const entry = Object.assign({ promise }, match);
  entry.release = () => release();
  holds.push(entry);
  return entry;
}

function takeHold(endpoint, query) {
  const index = holds.findIndex(entry => entry.endpoint === endpoint
    && (!entry.personaId || entry.personaId === query.persona_id)
    && (!entry.filename || entry.filename === query.filename));
  if (index < 0) return null;
  return holds.splice(index, 1)[0];
}

const window = {
  addEventListener() {},
  document,
  AstrBotPluginPage: {
    ready: async () => {},
    onContext() {},
    apiGet: async (endpoint, params) => {
      const query = params || {};
      if (endpoint === 'personas') return { success: true, personas: PERSONAS.map(id => ({ id })) };
      if (endpoint === 'detail') {
        detailCalls.push(query.persona_id);
        detailGeneration += 1;
        const holder = takeHold('detail', query);
        if (holder) await holder.promise;
        return {
          success: true,
          persona_id: query.persona_id,
          prompt: 'prompt of ' + query.persona_id,
          summary: 'summary of ' + query.persona_id,
          summary_manual: false,
          references: detailRefs[query.persona_id] || []
        };
      }
      if (endpoint === 'preview') return { success: true, mime_type: 'image/png', base64_data: 'AA==' };
      return { success: false, message: '本用例不提供 ' + endpoint };
    },
    apiPost: async (endpoint, body) => {
      posted.push({ endpoint, body });
      return { success: true, message: 'ok' };
    }
  }
};
window.window = window;

const readers = [];

class GateFileReader {
  constructor() { this.result = ''; this.onload = null; this.onerror = null; this.file = null; }
  readAsDataURL(file) { this.file = file; this.armed = true; readers.push(this); }
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

/* ------------------------------------------------------------ 事件驱动小工具 */
function sleep(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

async function flush(rounds = 20) {
  for (let index = 0; index < rounds; index += 1) await sleep(0);
}

async function waitFor(fn, label) {
  for (let index = 0; index < 400; index += 1) {
    if (fn()) return;
    await sleep(1);
  }
  throw new Error('等待超时：' + label);
}

/** 触发元素上的监听器；返回监听器返回的 Promise（调用方决定要不要等）。 */
function fire(id, type, event) {
  const handlers = (elements[id].listeners || {})[type] || [];
  return handlers.map(handler => {
    let result;
    try {
      result = handler(event || { target: elements[id] });
    } catch (error) {
      return Promise.reject(error);
    }
    return result && typeof result.then === 'function' ? result : Promise.resolve();
  });
}

/** 点击委托：`document` 上的 click 监听器（参考图卡删除走这里）。 */
function fireDocumentClick(target) {
  return documentClicks.map(handler => {
    let result;
    try {
      result = handler({ target });
    } catch (error) {
      return Promise.reject(error);
    }
    return result && typeof result.then === 'function' ? result : Promise.resolve();
  });
}

function deleteTarget(filename) {
  return {
    dataset: { delete: filename },
    disabled: false,
    closest(selector) { return selector === '[data-delete]' ? this : null; }
  };
}

/** 切换人格；返回的 Promise 在详情响应落 UI 之后才 resolve（可被 hold 挂住）。 */
function switchTo(personaId) {
  elements['persona-select'].value = personaId;
  const pending = fire('persona-select', 'change');
  return Promise.all(pending.map(promise => promise.catch(() => {})));
}

async function renderA() {
  await switchTo('persona-a');
  await waitFor(() => elements['persona-title'].textContent === 'persona-a', '渲染 A');
}

function writes(endpoint) {
  return posted.filter(entry => entry.endpoint === endpoint);
}

async function settle(promises) {
  await Promise.all(promises.map(promise => promise.catch(() => {})));
  await flush();
}

/* ------------------------------------------------------------ 用例 */
check('初始化后默认渲染第一个人格', async () => {
  await waitFor(() => elements['persona-title'].textContent === 'persona-a', '初始化渲染');
  assert.ok(elements['gallery'].innerHTML.includes('a-1.png'), '图库应含 a-1.png');
  assert.strictEqual(ui.state.current, 'persona-a');
});

check('切到 B 且 B 详情未返回时，删除旧卡不得写到 B', async () => {
  await renderA();
  posted.length = 0;
  const hold = holdNext({ endpoint: 'detail', personaId: 'persona-b' });
  const switching = switchTo('persona-b');
  await flush();
  assert.strictEqual(ui.state.current, 'persona-b', '选择已经切到 B');

  const pending = fireDocumentClick(deleteTarget('a-1.png'));
  await flush();
  assert.deepStrictEqual(writes('delete'), [], '详情未就绪时不得把整张卡的删除写到任何人格');
  assert.ok(elements['notice'].textContent.includes('稍后重试'), '应给出可重试提示');

  hold.release();
  await settle([switching, ...pending]);
  await waitFor(() => elements['persona-title'].textContent === 'persona-b', '渲染 B');
});

check('切到 B 且 B 详情未返回时，保存摘要不得写到 B', async () => {
  await renderA();
  posted.length = 0;
  const hold = holdNext({ endpoint: 'detail', personaId: 'persona-b' });
  const switching = switchTo('persona-b');
  await flush();

  elements['persona-summary'].value = 'summary of persona-a';
  const pending = fire('save-summary', 'click');
  await flush();
  assert.deepStrictEqual(writes('summary'), [], '界面里还是 A 的摘要，不能把它写到 B');

  hold.release();
  await settle([switching, ...pending]);
  await waitFor(() => elements['persona-title'].textContent === 'persona-b', '渲染 B');
});

check('切到 B 且 B 详情未返回时，重建摘要不得写到 B', async () => {
  await renderA();
  posted.length = 0;
  const hold = holdNext({ endpoint: 'detail', personaId: 'persona-b' });
  const switching = switchTo('persona-b');
  await flush();

  const pending = fire('rebuild', 'click');
  await flush();
  assert.deepStrictEqual(writes('rebuild'), [], '选图集合属于 A，不能拿它重建 B');

  hold.release();
  await settle([switching, ...pending]);
  await waitFor(() => elements['persona-title'].textContent === 'persona-b', '渲染 B');
});

check('详情回来后写操作恢复正常，目标为新渲染的人格', async () => {
  await switchTo('persona-b');
  await waitFor(() => elements['persona-title'].textContent === 'persona-b', '渲染 B');
  posted.length = 0;
  elements['persona-summary'].value = 'summary of persona-b';
  const pending = fire('save-summary', 'click');
  await flush();
  assert.strictEqual(writes('summary').length, 1);
  assert.strictEqual(writes('summary')[0].body.persona_id, 'persona-b');
  await settle(pending);
});

check('A→B→A 快速切换：第一代 A 的界面不得被当成第三代 A 的写目标', async () => {
  await renderA();
  posted.length = 0;
  const hold = holdNext({ endpoint: 'detail', personaId: 'persona-a' });
  const switching = switchTo('persona-a');
  await flush();
  const pending = fireDocumentClick(deleteTarget('b-1.png'));
  await flush();
  assert.deepStrictEqual(writes('delete'), [], '界面还停在上一代，不能凭 state.current 猜测');
  hold.release();
  await settle([switching, ...pending]);
  await waitFor(() => elements['persona-title'].textContent === 'persona-a', '渲染 A');
  await waitFor(() => elements['gallery'].innerHTML.includes('a-1.png'), 'A 的图库');
});

check('无竞态时删除仍正常落到当前渲染的人格', async () => {
  await renderA();
  posted.length = 0;
  const before = detailCalls.length;
  const pending = fireDocumentClick(deleteTarget('a-1.png'));
  await flush();
  assert.strictEqual(writes('delete').length, 1);
  assert.strictEqual(writes('delete')[0].body.persona_id, 'persona-a');
  await settle(pending);
  assert.ok(detailCalls.length > before, '删除成功后应重载详情');
  assert.strictEqual(detailCalls[detailCalls.length - 1], 'persona-a');
});

check('正在读文件的旧上传仍绑定点击瞬间的 A（不改成渲染目标）', async () => {
  await renderA();
  posted.length = 0;
  readers.length = 0;
  elements['reference-files'].files = [{ name: 'up.png', type: 'image/png' }];
  const uploading = fire('upload', 'click');
  await waitFor(() => readers.length === 1, 'FileReader 已启动');
  const switching = switchTo('persona-b');
  await flush();
  readers[0].release();
  await settle([...uploading, switching]);
  assert.strictEqual(writes('upload').length, 1);
  assert.strictEqual(writes('upload')[0].body.persona_id, 'persona-a', '读文件期间切换人格不得改绑定');
  elements['reference-files'].files = [];
});

check('静态守卫：写操作不得直接使用 state.current 当目标', async () => {
  const source = fs.readFileSync(APP, 'utf8');
  assert.ok(source.includes('function renderedPersonaTarget('), '必须存在"已渲染目标"helper');
  for (const endpoint of ['delete', 'summary', 'rebuild']) {
    const pattern = new RegExp("apiPost\\('" + endpoint + "'\\s*,\\s*\\{[^}]*persona_id\\s*:\\s*state\\.current");
    assert.ok(!pattern.test(source), endpoint + ' 的 persona_id 不得直接取自 state.current');
  }
});

chain.then(() => {
  console.log('---');
  console.log(passed + ' PASS');
}, error => {
  console.error('FAIL 收尾');
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
