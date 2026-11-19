#!/usr/bin/env node
/**
 * IMAGO·FUSION v1.4.2 定向收尾回归（无第三方依赖）：详情已换、旧预览仍在写窗口。
 *
 * 现场事件（t25，承 t11 R2-05 / t19 R3）：
 *   A 的详情与图库已渲染（卡片、选集都是 A 的）→ 用户切到 B →
 *   **B 的 detail 已经返回**（`state.personaDetail=B`、标题/摘要已是 B），
 *   但 `renderGallery` 还卡在逐张 `preview` 的 await 上，DOM 里仍是 A 的图卡与 A 的选集。
 *   此时 `renderedPersonaTarget()` 已经返回 B，于是
 *     - 点旧卡「删除这张图」→ POST `persona_id=B, filename=A 的图`（同名图会被真删）
 *     - 点「重建外观摘要」→ POST `persona_id=B, reference_names=A 的选集`
 *   这是 R3 之后仍留的第二个窗口：目标对了，**内容却还是别人家的**；filename 不能当归属证明。
 *
 * 修好后的不变量：
 *   1. 作用在"已渲染内容"上的写操作，除了要确认详情代次，还必须确认**图库归属**
 *      （DOM 里这批卡片与选集属于哪个人格）与本次目标是同一个；
 *   2. 窗口内删除 / 选用 / 重建一律不提交，并给出可重试提示（不静默丢弃）；
 *   3. 窗口关闭（B 的图库真正提交）后，B 的删除/选用/重建照常工作，且只作用于 B；
 *   4. 单纯的 detail-pending（R3 已覆盖）、A→B→A 正常路径、上传点击时绑定都不回退。
 *
 * 基线红证（把 `IMAGO_WEBUI_APP` 指向未修版本）：
 *   `git show f63787c:pages/webui/app.js > /tmp/app-close-base.js`
 *   `IMAGO_WEBUI_APP=/tmp/app-close-base.js node test/test_webui_v142_close.js`
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

/* ------------------------------------------------------------ 最小 DOM 桩（与 r3 回归同款） */
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
const holds = [];
// A 与 B 各有一张**同名**图：用来反证"filename 不能当归属证明"。
const detailRefs = {
  'persona-a': [{ name: 'shared.png' }, { name: 'a-only.png' }],
  'persona-b': [{ name: 'shared.png' }]
};

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
      if (endpoint === 'preview') {
        const holder = takeHold('preview', query);
        if (holder) await holder.promise;
        return { success: true, mime_type: 'image/png', base64_data: 'AA==' };
      }
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

/** 图卡上的「用于本次识图」勾选（走 #gallery 的 change 委托）。 */
function selectCard(name, checked = true) {
  const target = { dataset: { reference: name }, checked, closest: () => null };
  return fire('gallery', 'change', { target });
}

function switchTo(personaId) {
  elements['persona-select'].value = personaId;
  const pending = fire('persona-select', 'change');
  return Promise.all(pending.map(promise => promise.catch(() => {})));
}

function writes(endpoint) {
  return posted.filter(entry => entry.endpoint === endpoint);
}

async function settle(promises) {
  await Promise.all(promises.map(promise => promise.catch(() => {})));
  await flush();
}

async function renderA() {
  await switchTo('persona-a');
  await waitFor(() => elements['gallery'].innerHTML.includes('a-only.png'), 'A 的图库');
}

async function renderB() {
  await switchTo('persona-b');
  await waitFor(() => elements['persona-title'].textContent === 'persona-b'
    && elements['gallery'].innerHTML.includes('shared.png')
    && !elements['gallery'].innerHTML.includes('a-only.png'), 'B 的图库');
}

/** 打开问题窗口：屏障一 = B 的 detail 已就位；屏障二 = B 的 preview 还没回。 */
async function openBWindow() {
  const hold = holdNext({ endpoint: 'preview', personaId: 'persona-b' });
  const switching = switchTo('persona-b');
  await waitFor(() => ui.state.personaDetail && ui.state.personaDetail.personaId === 'persona-b',
    'B 的详情已就位');
  assert.ok(elements['gallery'].innerHTML.includes('a-only.png'),
    '屏障二：DOM 里必须仍是 A 的图卡（否则测的不是这个窗口）');
  return { hold, switching };
}

/* ------------------------------------------------------------ 用例 */
check('初始渲染：A 的图库含与 B 同名的 shared.png', async () => {
  await waitFor(() => elements['gallery'].innerHTML.includes('a-only.png'), '初始化渲染');
  assert.ok(elements['gallery'].innerHTML.includes('shared.png'));
  assert.strictEqual(ui.state.current, 'persona-a');
});

check('窗口内删除旧卡：即使与 B 的图同名，也不得写到 B', async () => {
  await renderA();
  posted.length = 0;
  const { hold, switching } = await openBWindow();
  const pending = fireDocumentClick(deleteTarget('shared.png'));
  await flush();
  assert.deepStrictEqual(writes('delete'), [],
    'filename 不能当归属证明：窗口内不得把整张卡的删除写到 B');
  assert.ok(elements['notice'].textContent.includes('稍后重试'), '应给出可重试提示');
  hold.release();
  await settle([switching, ...pending]);
  await renderB();
});

check('窗口内选用旧卡：不得进入本次识图选集', async () => {
  await renderA();
  posted.length = 0;
  selectCard('a-only.png', true);
  assert.deepStrictEqual([...ui.state.selectedReferences], ['a-only.png'], 'A 的选集');
  const { hold, switching } = await openBWindow();
  const pending = selectCard('shared.png', true);
  await flush();
  assert.deepStrictEqual([...ui.state.selectedReferences], ['a-only.png'],
    '窗口内勾选旧卡不得改变选集（它属于 A，不该被当成 B 的识图依据）');
  hold.release();
  await settle([switching, ...pending]);
  await renderB();
});

check('窗口内重建摘要：不得拿 A 的选集重建 B', async () => {
  await renderA();
  posted.length = 0;
  selectCard('a-only.png', true);
  const { hold, switching } = await openBWindow();
  const pending = fire('rebuild', 'click');
  await flush();
  assert.deepStrictEqual(writes('rebuild'), [], '选集属于 A，不能拿它重建 B');
  hold.release();
  await settle([switching, ...pending]);
  await renderB();
});

check('窗口关闭后 B 的删除落到 B 自己（同名图也是 B 自己的）', async () => {
  await renderB();
  posted.length = 0;
  const pending = fireDocumentClick(deleteTarget('shared.png'));
  await flush();
  assert.strictEqual(writes('delete').length, 1, '图库就绪后应正常提交');
  // 逐字段断言：POST 体是在 vm 上下文里造的对象，跨 realm 的 deepStrictEqual 会因原形不同而误报。
  assert.strictEqual(writes('delete')[0].body.persona_id, 'persona-b');
  assert.strictEqual(writes('delete')[0].body.filename, 'shared.png');
  await settle(pending);
  await renderB();
});

check('窗口关闭后 B 的选用与重建作用于 B', async () => {
  await renderB();
  posted.length = 0;
  const pendingSelect = selectCard('shared.png', true);
  await settle(pendingSelect);
  assert.deepStrictEqual([...ui.state.selectedReferences], ['shared.png']);
  const pending = fire('rebuild', 'click');
  await flush();
  assert.strictEqual(writes('rebuild').length, 1);
  assert.strictEqual(writes('rebuild')[0].body.persona_id, 'persona-b');
  assert.deepStrictEqual(Array.from(writes('rebuild')[0].body.reference_names), ['shared.png']);
  await settle(pending);
  await renderB();
});

check('单纯 detail-pending 仍然拒绝（R3 行为不回退）', async () => {
  await renderA();
  posted.length = 0;
  const hold = holdNext({ endpoint: 'detail', personaId: 'persona-b' });
  const switching = switchTo('persona-b');
  await flush();
  const pendingDelete = fireDocumentClick(deleteTarget('a-only.png'));
  const pendingRebuild = fire('rebuild', 'click');
  await flush();
  assert.deepStrictEqual(writes('delete'), [], '详情未就绪时不得提交删除');
  assert.deepStrictEqual(writes('rebuild'), [], '详情未就绪时不得提交重建');
  hold.release();
  await settle([switching, ...pendingDelete, ...pendingRebuild]);
  await renderB();
});

check('A→B→A 正常路径不回退：回到 A 后删除落到 A', async () => {
  await renderB();
  await renderA();
  posted.length = 0;
  const pending = fireDocumentClick(deleteTarget('a-only.png'));
  await flush();
  assert.strictEqual(writes('delete').length, 1);
  assert.strictEqual(writes('delete')[0].body.persona_id, 'persona-a');
  assert.strictEqual(writes('delete')[0].body.filename, 'a-only.png');
  await settle(pending);
  await renderA();
});

check('上传点击时绑定不回退：读文件期间切换仍发点击时的 A', async () => {
  await renderA();
  posted.length = 0;
  elements['reference-files'].files = [{ name: 'up.png', type: 'image/png' }];
  readers.length = 0;
  const uploading = fire('upload', 'click');
  await waitFor(() => readers.length === 1, 'FileReader 已启动');
  const switching = switchTo('persona-b');
  await flush();
  readers[0].release();
  await settle([...uploading, switching]);
  assert.strictEqual(writes('upload').length, 1);
  assert.strictEqual(writes('upload')[0].body.persona_id, 'persona-a');
  elements['reference-files'].files = [];
  await renderB();
});

check('静态守卫：图库归属必须单独登记，写操作按代次 + 归属双重校验', async () => {
  const source = fs.readFileSync(APP, 'utf8');
  assert.ok(source.includes('function renderedGalleryTarget('), '必须存在"已渲染图库目标"helper');
  assert.ok((source.match(/state\.galleryPersona\s*=/g) || []).length >= 2,
    '图库归属必须在两处提交点（空库 / 有卡片）登记');
  for (const action of ['删除参考图', '重建外观摘要', '选用参考图']) {
    assert.ok(source.includes("renderedGalleryTarget('" + action + "')"),
      action + ' 必须按"已渲染图库目标"校验');
  }
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
