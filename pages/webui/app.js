/* IMAGO·FUSION v1.4.0 管理台（原生 JS，无构建步骤） */
function getBridge(){return window.AstrBotPluginPage||null}
async function waitBridge(){for(let i=0;i<30;i++){const b=getBridge();if(b&&typeof b.ready==='function'){await b.ready();return}await new Promise(r=>setTimeout(r,100))}throw new Error('AstrBot Plugin Page Bridge 未就绪')}
async function apiGet(endpoint,params={}){const b=getBridge();if(!b||typeof b.apiGet!=='function')throw new Error('Bridge 未就绪');return b.apiGet(endpoint,params)}
async function apiPost(endpoint,body={}){const b=getBridge();if(!b||typeof b.apiPost!=='function')throw new Error('Bridge 未就绪');return b.apiPost(endpoint,body)}
const $=s=>document.querySelector(s);

const state={
  personas:[],current:'',providers:[],primary:'',storedPrimary:'',
  primaryChosen:false,originalIds:new Set(),originalPrimary:'',
  catalog:{version:1,api_types:[],groups:[],thinking_values:[],thinking_labels:{}},
  referenceFallback:{},chatProviders:[],
  optimizerProviderId:'',visionProviderId:'',
  dirty:false,quotaDirty:false,quotas:[],quotaPolicy:{},quotaSort:{key:'user_id',direction:'asc'},
  rebuilding:false,selectedReferences:new Set(),expanded:new Set(),warnings:[]
};

/* ---------------------------------------------------------------- 基础工具 */
function notify(message,error=false){const el=$('#notice');el.textContent=message||'';el.style.color=error?'var(--danger)':'var(--primary)'}
function escapeHtml(value){const el=document.createElement('div');el.textContent=String(value);return el.innerHTML}
function attr(value){return escapeHtml(value).replace(/"/g,'&quot;')}
function lineList(value){const values=Array.isArray(value)?value:String(value||'').split(/\r?\n/);return values.map(item=>String(item).trim()).filter(Boolean)}
function hasApiKeys(item){return lineList(item.api_keys).length>0||Boolean(item.api_keys_configured)}
function updateDirtyClass(){document.body.classList.toggle('provider-dirty',state.dirty);document.body.classList.toggle('quota-dirty',state.quotaDirty)}
function setDirty(value=true){state.dirty=value;$('#save-config').disabled=!value;updateDirtyClass()}
function setQuotaDirty(value=true){state.quotaDirty=value;$('#save-quotas').disabled=!value;updateDirtyClass()}

/* ---------------------------------------------------------------- 共享目录 catalog */
function catalogEntry(apiType){return (state.catalog.api_types||[]).find(item=>item.value===apiType)||null}
function catalogLabel(apiType){const entry=catalogEntry(apiType);return entry&&entry.label?entry.label:apiType}
function paramFields(apiType){const entry=catalogEntry(apiType);return entry&&Array.isArray(entry.fields)?entry.fields:[]}
function capabilitiesOf(apiType){const entry=catalogEntry(apiType);return entry&&entry.capabilities?entry.capabilities:{}}
function thinkingValues(){return Array.isArray(state.catalog.thinking_values)?state.catalog.thinking_values:['']}
function thinkingLabel(value){const labels=state.catalog.thinking_labels||{};return labels[value]||({ '':'跟随模型默认（不发送）',off:'关闭思考',low:'低',medium:'中',high:'高',max:'最高' })[value]||value}
function optionLabel(value){
  const map={auto:'自动',low:'低',medium:'中',high:'高',xhigh:'更高',max:'最高',standard:'标准（已退役）',hd:'高清（已退役）',blur:'模糊',mosaic:'马赛克',solid:'纯色覆盖',json:'JSON（官方）',multipart:'multipart（中转兼容）','1k':'1K','1.5k':'1.5K','2k':'2K','4K':'4K'};
  return map[value]||value;
}
function qualityText(value){return ({native:'原生支持（quality 参数）',none:'不适用',undocumented:'未文档化（写了会透传）'})[value]||(value===true?'支持（旧形状）':value)||'不适用'}
function thinkingCapText(value){return ({complexity:'映射为 complexity',reasoning_effort:'映射为 reasoning_effort',thinking_level:'映射为 thinkingLevel（仅能力声明模型）',none:'不发送思考字段'})[value]||value||'不发送'}
function referencesText(value){return ({edits:'images/edits 或 JSON 图像字段',inline:'内联图像（inlineData/contents）',array:'references[]',none:'不发送参考图'})[value]||(value===true?'支持（旧形状）':value)||'不发送'}
function reasonLabel(reason){return ({invalid_key:'键名不合法',reserved_key:'保留键，已跳过',invalid_value:'取值无效',duplicate_key:'重复键（首个生效）',unknown_for_type:'当前接口不支持（会保留）',invalid_line:'无法解析的行',parse_failed:'解析失败'})[reason]||reason}

/* ---------------------------------------------------------------- Persona（保持 v1.2.0 行为） */
function filteredPersonas(){const q=$('#persona-search').value.trim().toLowerCase();return state.personas.filter(id=>id.toLowerCase().includes(q))}
function renderPersonaOptions(){const items=filteredPersonas();$('#persona-select').innerHTML=items.map(id=>`<option value="${attr(id)}"${id===state.current?' selected':''}>${escapeHtml(id)}</option>`).join('')}
async function loadPersonas(){const result=await apiGet('personas');if(!result.success){state.personas=[];renderPersonaOptions();notify(result.message||'读取 Persona 列表失败',true);return}state.personas=(result.personas||[]).map(item=>item.id);renderPersonaOptions();if(state.personas.length){state.current=state.personas[0];renderPersonaOptions();await loadPersonaDetail()}}
async function loadPersonaDetail(){
  if(!state.current)return;
  notify('正在读取 Persona……');
  const result=await apiGet('detail',{persona_id:state.current});
  if(!result.success)throw new Error(result.message||'读取失败');
  $('#persona-title').textContent=state.current;
  $('#persona-prompt').textContent=result.prompt||'空 prompt';
  $('#persona-summary').value=result.summary||'';
  updateSummaryCount();
  $('#summary-mode').textContent=result.summary?result.summary_manual?'手工覆写':'自动摘要':'';
  $('#rebuild').disabled=state.rebuilding;
  $('#upload').disabled=false;
  $('#save-summary').disabled=false;
  await renderGallery(result.references||[]);
  notify('');
}
async function renderGallery(refs){
  const gallery=$('#gallery');
  state.selectedReferences.clear();
  if(!refs.length){gallery.innerHTML='<p class="empty">尚无参考图</p>';return}
  const cards=[];
  for(const ref of refs){
    try{
      const preview=await apiGet('preview',{persona_id:state.current,filename:ref.name});
      cards.push(`<div class="image-card"><label class="reference-select"><input type="checkbox" data-reference="${attr(ref.name)}"><span>用于本次识图</span></label>${preview.success?`<img src="${attr(preview.data_url||`data:${preview.mime_type};base64,${preview.base64_data}`)}" alt="Persona 参考图">`:`<div class="preview-error">${escapeHtml(preview.message||'预览失败')}</div>`}<button type="button" data-delete="${attr(ref.name)}">删除这张图</button></div>`);
    }catch(error){
      cards.push(`<div class="image-card"><label class="reference-select"><input type="checkbox" data-reference="${attr(ref.name)}"><span>用于本次识图</span></label><div class="preview-error">预览请求失败</div><button type="button" data-delete="${attr(ref.name)}">删除这张图</button></div>`);
    }
  }
  gallery.innerHTML=cards.join('');
}
function readFile(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file)})}
function updateSummaryCount(){const area=$('#persona-summary');$('#summary-count').textContent=`${area.value.length} 字符`}
function setRebuildLoading(active){state.rebuilding=active;const button=$('#rebuild');button.disabled=active||!state.current;button.classList.toggle('is-loading',active);button.setAttribute('aria-busy',active?'true':'false');button.textContent=active?'正在重建…':'重建外观摘要'}

/* ---------------------------------------------------------------- 节点渲染 */
function chevron(){return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="m6 9 6 6 6-6"/></svg>'}
function normalizeProvider(item){
  return {
    ...item,
    enabled:item.enabled!==false,
    thinking:String(item.thinking||''),
    custom_params:String(item.custom_params||''),
    params:item.params&&typeof item.params==='object'?item.params:{},
    param_errors:Array.isArray(item.param_errors)?item.param_errors:[],
    param_mode:item.param_mode||'ignored',
    capabilities:item.capabilities||{},
    available_models:lineList(item.available_models),
    api_keys:[],
    clear_api_keys:false,
    isNew:item.isNew===true
  };
}
function providerBadges(item,index){
  const badges=[];
  if(item.primary)badges.push('<span class="badge badge-primary">主节点</span>');
  if(item.primary_disabled)badges.push('<span class="badge badge-warn">主节点已停用</span>');
  if(item.enabled===false)badges.push('<span class="badge badge-off">已停用</span>');
  if(item.complete===false)badges.push('<span class="badge badge-idle">配置不完整</span>');
  return badges.join('');
}
function providerMeta(item,index){
  const parts=[`顺序 ${index+1}`,catalogLabel(item.api_type||'')];
  if(item.model)parts.push(item.model);
  const params=item.params||{};
  if(params.quality)parts.push(`质量 ${params.quality}`);
  if(item.thinking)parts.push(`思考 ${thinkingLabel(item.thinking)}`);
  return parts.map(part=>escapeHtml(part)).join(' · ');
}
function renderNativeFields(item,index){
  if(item.api_type!=='astrbot_native')return '';
  const options=state.chatProviders.map(p=>`<option value="${attr(p.id)}" ${p.id===item.astrbot_provider_id?'selected':''}>${escapeHtml(p.id)}${p.model?` · ${escapeHtml(p.model)}`:''}</option>`).join('');
  return `<label>原生 Provider<select data-field="astrbot_provider_id"><option value="">未选择</option>${options}</select><span class="hint">由 AstrBot 托管鉴权与地址，Base URL / API Key 可留空。</span></label>
  <label class="check-line"><input type="checkbox" data-field="stream" ${item.stream?'checked':''}> 流式输出（仅原生）</label>`;
}
function renderThinkingField(item){
  const capability=capabilitiesOf(item.api_type).thinking||'none';
  const options=thinkingValues().map(value=>`<option value="${attr(value)}" ${value===item.thinking?'selected':''}>${escapeHtml(thinkingLabel(value))}</option>`).join('');
  const hint=capability==='none'
    ? '当前接口不会自动发送思考字段；此设置只保存，切换接口后仍保留。'
    : `保存后按 ${escapeHtml(item.api_type)} 协议${escapeHtml(thinkingCapText(capability))}；留空 = 不发送。`;
  return `<label>思考控制<select data-field="thinking">${options}</select><span class="hint">${hint}</span></label>`;
}
function renderParamControl(field,item){
  const value=item.params?item.params[field.key]:'';
  const key=field.key;
  const help=field.help?`<span class="hint">${escapeHtml(field.help)}</span>`:'';
  if(field.control==='select'){
    const options=(field.options||[]).map(option=>`<option value="${attr(option)}" ${String(option)===String(value??'')?'selected':''}>${escapeHtml(option===''?'未设置（不发送）':optionLabel(option))}</option>`).join('');
    return `<label>${escapeHtml(field.label||key)}<select data-param="${attr(key)}" data-control="select">${options}</select>${help}</label>`;
  }
  if(field.control==='bool3'){
    const current=value===true?'true':value===false?'false':'';
    const options=['','true','false'].map(option=>`<option value="${option}" ${option===current?'selected':''}>${option===''?'未设置（不发送）':option==='true'?'开':'关'}</option>`).join('');
    return `<label>${escapeHtml(field.label||key)}<select data-param="${attr(key)}" data-control="bool3">${options}</select>${help}</label>`;
  }
  if(field.control==='int'||field.control==='float'){
    const step=field.control==='int'?'1':'any';
    const attrs=[field.min!=null?`min="${attr(field.min)}"`:'',field.max!=null?`max="${attr(field.max)}"`:''].filter(Boolean).join(' ');
    return `<label>${escapeHtml(field.label||key)}<input type="number" step="${step}" ${attrs} data-param="${attr(key)}" data-control="${field.control}" value="${attr(value??'')}" placeholder="${attr(field.placeholder||'')}">${help}</label>`;
  }
  return `<label>${escapeHtml(field.label||key)}<input data-param="${attr(key)}" data-control="text" value="${attr(value??'')}" placeholder="${attr(field.placeholder||'')}" ${field.max_length?`maxlength="${attr(field.max_length)}"`:''}>${help}</label>`;
}
function renderParamErrors(item){
  const errors=(item.param_errors||[]).map(error=>`<li><span class="param-line">第 ${escapeHtml(error.line||'?')} 行</span>${error.key?` · ${escapeHtml(error.key)}`:''} · ${escapeHtml(reasonLabel(error.reason))}${error.message?`（${escapeHtml(error.message)}）`:''}</li>`).join('');
  return errors?`<ul class="param-errors">${errors}</ul>`:'';
}
function renderProvider(item,index){
  const expanded=state.expanded.has(index);
  const primaryTouch=item.primary?'':'title="设置为主节点"';
  const toggleClass=item.primary?'active':item.complete?'complete':'incomplete';
  const keyHint=item.api_keys_configured?' · 已配置（留空 = 保留）':' · 未配置';
  const clearKeys=item.api_keys_configured?`<label class="check-line warn-line"><input type="checkbox" data-field="clear_api_keys" ${item.clear_api_keys?'checked':''}> 清空已保存的 API Key（保存前二次确认；HTTP 节点清空后会由服务端强制停用，原生节点只清休眠 key）</label>`:'';
  const idField=item.isNew
    ? `<label>节点 ID<input data-field="id" value="${attr(item.id||'')}"><span class="hint">新增节点可连续键入；ID 需唯一。</span></label>`
    : `<label>节点 ID<input data-field="id" value="${attr(item.id||'')}" readonly aria-readonly="true"><span class="hint">已有节点的 ID 只读：要换名请新增节点并显式删除旧节点，避免密钥与历史配置错位。</span></label>`;
  const apiOptions=(state.catalog.api_types||[]).map(entry=>`<option value="${attr(entry.value)}" ${entry.value===item.api_type?'selected':''}>${escapeHtml(entry.label||entry.value)}</option>`).join('');
  const advancedFields=paramFields(item.api_type).map(field=>renderParamControl(field,item)).join('');
  const modeHint=item.param_mode==='typed'
    ? '上方控件会写入下方“高级参数原文”；未知行原样保留。'
    : item.param_mode==='free'
      ? '自定义端点：整包参数透传（未知键会发送）。'
      : '当前接口忽略高级参数（不会发送）。';
  return `<article class="provider-node ${expanded?'expanded':''}" data-index="${index}" data-api-type="${attr(item.api_type||'')}">
    <div class="node-main">
      <button class="primary-toggle ${toggleClass}" data-primary="${index}" ${primaryTouch} aria-label="${item.primary?'主节点':'设置为主节点'}">${item.primary?'★':'●'}</button>
      <div class="node-title">
        <div class="provider-title-line"><strong>${escapeHtml(item.id)}</strong>${providerBadges(item,index)}</div>
        <div class="node-meta">${providerMeta(item,index)}</div>
      </div>
      <div class="node-controls">
        <label class="switch-label" title="停用后不参与任何调用，配置保留"><input type="checkbox" data-field="enabled" ${item.enabled!==false?'checked':''}> 启用</label>
        <button class="expand-button" data-expand="${index}" aria-label="展开节点配置">${chevron()}</button>
      </div>
    </div>
    <div class="node-fields">
      <section class="node-section">
        <h4>基础</h4>
        <div class="field-grid">
          ${idField}
          <label>接口类型<select data-field="api_type">${apiOptions}</select></label>
          <label>接口地址（Base URL）<input data-field="base_url" value="${attr(item.base_url||'')}" placeholder="https://…"></label>
          <label>API Key（每行一个，节点内轮换）${escapeHtml(keyHint)}<textarea rows="3" data-field="api_keys" placeholder="留空 = 保留已保存的 Key" spellcheck="false"></textarea></label>
          ${clearKeys}
          ${renderNativeFields(item,index)}
          <label class="check-line"><input type="checkbox" data-field="enable_proxy" ${item.enable_proxy?'checked':''}> 走插件出站代理</label>
        </div>
      </section>
      <section class="node-section">
        <h4>模型与调用</h4>
        <div class="field-grid">
          <label>主模型<input data-field="model" value="${attr(item.model||'')}" placeholder="留空 = 服务端默认"></label>
          <label>备用模型（每行一个）<textarea rows="3" data-field="available_models">${escapeHtml((item.available_models||[]).join('\n'))}</textarea></label>
          <label>回退顺序（小者先试）<input type="number" step="1" data-field="fallback_order" value="${Math.trunc(Number(item.fallback_order)||0)}"></label>
          <label>单次超时（秒）<input type="number" min="10" step="1" data-field="timeout" value="${Number(item.timeout)||180}"></label>
          <label>参考图上限（0 = 不限）<input type="number" min="0" step="1" data-field="reference_image_limit" value="${Math.max(0,Number(item.reference_image_limit)||0)}"></label>
          <label>旧版默认尺寸<input data-field="default_size" value="${attr(item.default_size||'')}" placeholder="1024x1024；留空 = 不发送"><span class="hint">仅当高级参数里没有 size 时兜底。</span></label>
        </div>
      </section>
      <details class="node-advanced">
        <summary>高级参数（质量 / 分辨率 / 思考 / 自定义）</summary>
        <p class="hint">${escapeHtml(modeHint)}</p>
        <div class="field-grid">
          ${renderThinkingField(item)}
          ${advancedFields||'<p class="hint">当前接口没有声明参数。</p>'}
        </div>
        <label class="wide">高级参数原文（key=value 或 key: value，每行一条；保留键由后端校验并在保存结果里提示）<textarea rows="4" data-field="custom_params" spellcheck="false" placeholder="size=1536x1024&#10;quality=high">${escapeHtml(item.custom_params||'')}</textarea><span class="hint">空行与 # 开头的行忽略；非法行只提示，不会让节点失效。</span></label>
        ${renderParamErrors(item)}
        <p class="hint">能力：质量 ${escapeHtml(qualityText(capabilitiesOf(item.api_type).quality))} · 思考 ${escapeHtml(thinkingCapText(capabilitiesOf(item.api_type).thinking))} · 参考图 ${escapeHtml(referencesText(capabilitiesOf(item.api_type).references))}</p>
      </details>
    </div>
  </article>`;
}
function renderProviders(){
  const root=$('#provider-list');
  const summary=$('#provider-summary');
  if(!state.providers.length){
    root.innerHTML='<p class="empty">尚未添加图片生成节点。</p>';
  }else{
    root.innerHTML=state.providers.map((item,index)=>renderProvider(item,index)).join('');
  }
  if(!state.providers.length){
    summary.innerHTML='<p class="summary-empty">尚未添加图片生成节点，提交绘图会被拒绝。</p>';
  }else if(!state.primary){
    summary.innerHTML='<p class="summary-error">没有可用的绘图节点（全部停用或配置不完整），提交绘图会被拒绝。</p>';
  }else if(state.storedPrimary&&state.storedPrimary!==state.primary){
    summary.innerHTML=`<p class="summary-warn">存储主节点「${escapeHtml(state.storedPrimary)}」已停用或配置不完整，当前生效：「${escapeHtml(state.primary)}」。</p>`;
  }else{
    summary.innerHTML=`<p class="summary-ok">当前主节点：「${escapeHtml(state.primary)}」；其余启用节点按回退顺序尝试。</p>`;
  }
  renderWarnings();
}
function renderWarnings(){
  const root=$('#provider-warnings');
  const warnings=state.warnings||[];
  if(!warnings.length){root.innerHTML='';return}
  const items=warnings.slice(0,8).map(item=>`<li>${escapeHtml(item.message||item.code||'')}</li>`).join('');
  root.innerHTML=`<div class="warning-box"><strong>保存成功，但有 ${warnings.length} 条提示</strong><ul>${items}</ul>${warnings.length>8?'<p class="hint">其余提示已省略。</p>':''}</div>`;
}

/* ---------------------------------------------------------------- 配置组（数据驱动，字段来自 catalog.groups） */
function groupState(payload){const values={reference_fallback:state.referenceFallback};return values[payload]||{}}
function groupBlocked(group){
  const depends=group.depends_on;
  if(!depends)return false;
  const source=groupState(depends.payload)||{};
  return !Boolean(source[depends.field]);
}
function groupCard(group){
  const values=groupState(group.payload)||{};
  const blocked=groupBlocked(group);
  const fields=(group.fields||[]).map(field=>renderGroupField(group,field,values[field.key],blocked)).join('');
  const defaultOff=(group.fields||[]).some(field=>field.key==='enable'&&field.default===false);
  const vision=group.payload==='reference_fallback'?renderFallbackVision():'';
  const dependency=blocked&&group.depends_on?`<p class="group-dependency">${escapeHtml(group.depends_on.message||'依赖未满足，实验功能不会生效。')}</p>`:'';
  const body=fields||vision?`<div class="field-grid group-fields${blocked?' is-blocked':''}">${fields}${vision}</div>`:'<p class="hint">该组暂不提供控件；后端默认关闭，不会产生任何调用。</p>';
  return `<article class="card config-card${blocked?' is-blocked':''}" data-group="${attr(group.key)}" data-payload="${attr(group.payload)}">
    <div class="card-title"><div><h3>${escapeHtml(group.label)}${defaultOff?' <span class="pill">默认关闭</span>':''}</h3><p>${escapeHtml(group.description||'')}</p></div></div>
    ${dependency}
    ${body}
  </article>`;
}
function renderGroupField(group,field,value,blocked=false){
  const label=escapeHtml(field.label||field.key);
  const help=field.help?`<span class="hint">${escapeHtml(field.help)}</span>`:'';
  const data=`data-group-field="${attr(field.key)}"`;
  const disabled=blocked?'disabled':'';
  if(field.control==='switch'||field.type==='bool'){
    return `<label class="check-line">${label}<input type="checkbox" ${data} ${value?'checked':''} ${disabled}>${help}</label>`;
  }
  if(field.control==='textarea'){
    return `<label class="wide">${label}<textarea rows="3" ${data} ${field.max_length?`maxlength="${attr(field.max_length)}"`:''} ${disabled} placeholder="${attr(field.placeholder||'')}">${escapeHtml(value??'')}</textarea>${help}</label>`;
  }
  if(field.control==='select'){
    const options=(field.options||[]).map(option=>`<option value="${attr(option)}" ${String(option)===String(value??'')?'selected':''}>${escapeHtml(optionLabel(option))}</option>`).join('');
    return `<label>${label}<select ${data} ${disabled}>${options}</select>${help}</label>`;
  }
  if(field.type==='int'||field.type==='float'){
    const attrs=[field.min!=null?`min="${attr(field.min)}"`:'',field.max!=null?`max="${attr(field.max)}"`:''].filter(Boolean).join(' ');
    return `<label>${label}<input type="number" step="${field.type==='int'?'1':'any'}" ${attrs} ${data} value="${attr(value??'')}" ${disabled}>${help}</label>`;
  }
  return `<label>${label}<input ${data} value="${attr(value??'')}" ${disabled}>${help}</label>`;
}
function renderFallbackVision(){
  const options=state.chatProviders.map(item=>`<option value="${attr(item.id)}" ${item.id===state.visionProviderId?'selected':''}>${escapeHtml(item.id)}${item.model?` · ${escapeHtml(item.model)}`:''}</option>`).join('');
  const ready=Boolean(state.visionProviderId)&&state.chatProviders.some(item=>item.id===state.visionProviderId);
  return `<label>识图 Provider（降级依赖）<select data-fallback-vision><option value="">未配置</option>${options}</select><span class="hint">${ready?'已配置；为空时降级不会发生（只记一条警告）。':'未配置识图 Provider，即使开启降级也不会发生。'}</span></label>`;
}
function renderGroups(){
  const root=$('#config-groups');
  const groups=Array.isArray(state.catalog.groups)?state.catalog.groups:[];
  if(!groups.length){root.innerHTML='';return}
  root.innerHTML=`<div class="groups-head"><p class="eyebrow">参考图策略</p><h2>参考图文字降级</h2><p>仅在节点明确以安全/内容合规原因拒绝参考图时，才在该节点内改用文字说明重试；默认关闭，关闭时不产生任何额外请求、文件或费用。</p></div>${groups.map(groupCard).join('')}`;
}

/* ---------------------------------------------------------------- 节点同步与保存 */
function syncProviderField(target){
  const node=target.closest('.provider-node');
  if(!node)return;
  const item=state.providers[Number(node.dataset.index)];
  const field=target.dataset.field;
  const oldId=item.id;
  let value=target.type==='checkbox'?Boolean(target.checked):target.value;
  if(['available_models','api_keys'].includes(field))value=lineList(target.value);
  else if(field==='clear_api_keys')value=Boolean(target.checked);
  else if(field==='id')value=String(target.value).trim();
  else if(['reference_image_limit','timeout','fallback_order'].includes(field))value=target.value===''?0:Number(target.value);
  if(field==='id'&&!item.isNew){notify('已有节点的 ID 只读：如需换名，请新增节点并显式删除旧节点',true);return}
  item[field]=value;
  if(field==='id'){
    if(state.primary===oldId)state.primary=item.id;
    // FF-07：只改标题，不替换正在输入的 input（否则连续键入会丢字符与光标）。
    const title=node.querySelector('.provider-title-line strong');
    if(title)title.textContent=item.id||'（未命名）';
  }
  item.complete=Boolean(item.id&&(item.api_type==='astrbot_native'?item.astrbot_provider_id:(item.base_url&&hasApiKeys(item))));
  setDirty(true);
  if(field==='api_type'||field==='enabled')renderProviders();
}
function setParamValue(item,key,value,control){
  const lines=String(item.custom_params||'').split(/\r?\n/).filter(line=>{
    const match=line.trim().match(/^([a-zA-Z][a-zA-Z0-9_-]{0,31})\s*[=:]/);
    return !(match&&match[1]===key);
  });
  const empty=value===''||value===null||value===undefined;
  if(!empty)lines.unshift(`${key}=${value===true?'true':value===false?'false':value}`);
  item.custom_params=lines.filter(line=>line.trim()!=='').join('\n');
  item.params={...(item.params||{}),[key]:value};
}
function syncParam(target){
  const node=target.closest('.provider-node');
  if(!node)return;
  const item=state.providers[Number(node.dataset.index)];
  const control=target.dataset.control;
  let value=target.value;
  if(control==='bool3')value=value===''?'':value==='true';
  else if(control==='int')value=value===''?'':Math.trunc(Number(value));
  else if(control==='float')value=value===''?'':Number(value);
  setParamValue(item,target.dataset.param,value,control);
  const textarea=node.querySelector('[data-field="custom_params"]');
  if(textarea)textarea.value=item.custom_params;
  item.param_errors=[];
  setDirty(true);
}
function syncCustomParams(target){
  const node=target.closest('.provider-node');
  if(!node)return;
  const item=state.providers[Number(node.dataset.index)];
  item.custom_params=target.value;
  item.param_errors=[];
  setDirty(true);
}
function syncGroupField(target){
  const card=target.closest('[data-payload]');
  if(!card)return;
  const payload=card.dataset.payload;
  const values=groupState(payload);
  const fieldKey=target.dataset.groupField;
  const group=(state.catalog.groups||[]).find(item=>item.payload===payload);
  const field=group?(group.fields||[]).find(item=>item.key===fieldKey):null;
  let value=target.type==='checkbox'?Boolean(target.checked):target.value;
  if(field&&(field.type==='int'||field.type==='float')){
    value=target.value===''?0:Number(target.value);
    if(field.type==='int')value=Math.trunc(value);
  }
  values[fieldKey]=value;
  setDirty(true);
  if(fieldKey==='enable'||(field&&field.control==='provider_select'))renderGroups();
}
function buildProviderPayload(){
  const payload={
    optimizer_provider_id:state.optimizerProviderId,
    vision_provider_id:state.visionProviderId,
    providers:state.providers.map(item=>{
      const entry={
        id:item.id,
        api_type:item.api_type,
        enabled:item.enabled!==false,
        base_url:item.base_url||'',
        api_keys:lineList(item.api_keys),
        model:item.model||'',
        available_models:lineList(item.available_models),
        reference_image_limit:Math.max(0,Math.trunc(Number(item.reference_image_limit)||0)),
        default_size:item.default_size||'',
        timeout:Math.max(10,Math.trunc(Number(item.timeout)||180)),
        astrbot_provider_id:item.api_type==='astrbot_native'?String(item.astrbot_provider_id||''):'',
        stream:item.api_type==='astrbot_native'?Boolean(item.stream):false,
        enable_proxy:Boolean(item.enable_proxy),
        fallback_order:Math.trunc(Number(item.fallback_order)||0),
        thinking:String(item.thinking||''),
        custom_params:String(item.custom_params||'')
      };
      if(item.clear_api_keys)entry.clear_api_keys=true;
      return entry;
    })
  };
  // FF-01：只有用户明确点选主节点时才提交 primary_provider_id；
  // 启停、改参数等普通保存省略该项，后端保留存储主节点，避免把“当前生效”误存为“已选”。
  if(state.primaryChosen)payload.primary_provider_id=state.primary;
  // FF-03：删除必须是显式名单；只提交旧快照中存在、当前列表已不存在的 ID。
  const originalIds=state.originalIds instanceof Set?state.originalIds:new Set();
  const currentIds=state.providers.map(item=>String(item.id||'').trim()).filter(Boolean);
  const deletedIds=[...originalIds].filter(id=>!currentIds.includes(id));
  if(deletedIds.length)payload.deleted_provider_ids=deletedIds;
  for(const group of (state.catalog.groups||[])){
    const groupKey=group.key;
    if(groupKey==='reference_fallback_config'){
      // 后端快照在这个组里附带了只读的派生字段（识图 Provider 及其可用性），
      // 提交时必须剥掉，否则会被当成配置值写回。
      const values={...(groupState(group.payload)||{})};
      delete values.vision_provider_id;
      delete values.vision_provider_ready;
      payload[group.payload]=values;
    }
  }
  return payload;
}
function pendingClearNodes(){return state.providers.filter(item=>item.clear_api_keys&&item.api_keys_configured)}
function cancelClearFlags(){
  for(const item of state.providers)item.clear_api_keys=false;
  renderProviders();
}
function clearConfirmMessage(nodes){
  // FF-R2-01：确认文案必须区分 HTTP 与 native —— 前者清空后会被强制停用，
  // 后者只清休眠的 HTTP key，原生鉴权、节点启停与流式设置都不变。
  const http=nodes.filter(item=>item.api_type!=='astrbot_native').map(item=>item.id);
  const native=nodes.filter(item=>item.api_type==='astrbot_native').map(item=>item.id);
  const lines=[];
  if(http.length)lines.push(`HTTP 节点（${http.join('、')}）：将清空已保存的 API Key，并由服务端强制停用该节点；停用后无法发起调用。`);
  if(native.length)lines.push(`原生节点（${native.join('、')}）：只清空休眠的 HTTP API Key；原生 Provider 鉴权、节点启停与流式设置保持不变。`);
  return `${lines.join('\n')}\n\n清空后不可恢复，确认继续？`;
}
function confirmClear(nodes){
  const confirmFn=typeof window.confirm==='function'?window.confirm:null;
  if(!confirmFn)return false;
  return Boolean(confirmFn(clearConfirmMessage(nodes)));
}
async function saveProviderConfig(){
  // FF-R2-01：确认后保留 clear_api_keys 到 POST；成功回包前不本地清空/改启停。
  // 取消则复位临时标志并零 POST。
  const clearing=pendingClearNodes();
  if(clearing.length&&!confirmClear(clearing)){
    cancelClearFlags();
    notify('已取消清空密钥；未发送任何清空请求',false);
    return;
  }
  const result=await apiPost('providers/save',buildProviderPayload());
  if(!result.success){notify(result.message||'保存失败',true);return}
  state.warnings=Array.isArray(result.warnings)?result.warnings:[];
  if(result.data)applySnapshot(result.data);
  else await loadProviders();
  setDirty(false);
  const warningCount=state.warnings.length;
  notify(warningCount?`节点配置已保存（${warningCount} 条提示见下方）`:'节点配置已保存',false);
  renderWarnings();
}

/* ---------------------------------------------------------------- 快照应用与加载 */
function renderChatProviderOptions(items){
  const options=['<option value="">留空</option>',...items.map(item=>`<option value="${attr(item.id)}">${escapeHtml(item.id)}${item.model?` · ${escapeHtml(item.model)}`:''}</option>`)].join('');
  $('#optimizer-provider').innerHTML=options;
  $('#vision-provider').innerHTML=options;
  $('#optimizer-provider').value=state.optimizerProviderId;
  $('#vision-provider').value=state.visionProviderId;
}
function applySnapshot(result){
  state.providers=Array.isArray(result.providers)?result.providers.map(normalizeProvider):[];
  state.primary=result.primary_provider_id||'';
  state.storedPrimary=result.stored_primary_provider_id||'';
  state.originalPrimary=state.storedPrimary;
  state.originalIds=new Set(state.providers.map(item=>item.id));
  state.primaryChosen=false;
  state.catalog=result.catalog||{version:1,api_types:[],groups:[],thinking_values:[],thinking_labels:{}};
  state.referenceFallback=result.reference_fallback||{};
  state.optimizerProviderId=result.optimizer_provider_id||'';
  state.visionProviderId=result.vision_provider_id||'';
  state.chatProviders=Array.isArray(result.chat_providers)?result.chat_providers:[];
  renderChatProviderOptions(state.chatProviders);
  renderGroups();
  renderProviders();
}
async function loadProviders(){
  const result=await apiGet('providers');
  if(!result.success)throw new Error(result.message||'读取节点配置失败');
  state.warnings=[];
  state.expanded.clear();
  applySnapshot(result);
  setDirty(false);
}

/* ---------------------------------------------------------------- 额度（保持 v1.2.0 行为） */
function quotaStatusText(item){return item.blocked?'黑名单':item.unlimited?'无限额度':'普通用户'}
function quotaStatus(item){const label=quotaStatusText(item);const kind=item.blocked?'blocked':item.unlimited?'unlimited':'';return `<span class="${kind}">${label}</span>`}
function quotaSortValue(item,key){if(key==='status')return quotaStatusText(item);if(key==='quota')return Math.max(0,Number(item.quota)||0);return String(item[key]||'')}
function sortedQuotaEntries(){const {key,direction}=state.quotaSort;const factor=direction==='desc'?-1:1;return state.quotas.map((item,index)=>({item,index})).sort((left,right)=>{const a=quotaSortValue(left.item,key);const b=quotaSortValue(right.item,key);let result=typeof a==='number'&&typeof b==='number'?a-b:String(a).localeCompare(String(b),'zh-CN',{numeric:true,sensitivity:'base'});if(!result)result=String(left.item.user_id||'').localeCompare(String(right.item.user_id||''),'zh-CN',{numeric:true,sensitivity:'base'});return result?result*factor:left.index-right.index})}
function renderQuotaSortHeaders(){document.querySelectorAll('[data-quota-sort]').forEach(button=>{const active=button.dataset.quotaSort===state.quotaSort.key;button.classList.toggle('active',active);button.setAttribute('aria-label',`按${button.firstChild.textContent.trim()}${active&&state.quotaSort.direction==='asc'?'降序':'升序'}排列`);const indicator=button.querySelector('.sort-indicator');if(indicator)indicator.textContent=active?(state.quotaSort.direction==='asc'?'▲':'▼'):'↕'})}
function renderQuotaPolicy(){const p=state.quotaPolicy;$('#quota-policy').innerHTML=[`<span>额度限制：${p.quota_enabled?'开启':'关闭'}</span>`,`<span>每日刷新：${p.daily_refresh_enabled?`重置为 ${Number(p.daily_quota_target)||0}`:'关闭'}</span>`,`<span>签到：${p.checkin_enabled?`${Number(p.checkin_quota_min)||0}–${Number(p.checkin_quota_max)||0}`:'关闭'}</span>`].join('')}
function renderQuotas(){const root=$('#quota-list');renderQuotaSortHeaders();if(!state.quotas.length){root.innerHTML='<p class="empty" style="padding:14px">暂无额度记录，可点击“新增 ID”。</p>';$('#quota-count').textContent='0 条记录';return}root.innerHTML=sortedQuotaEntries().map(({item,index})=>`<div class="quota-row" role="row" data-quota-index="${index}"><input data-quota-field="user_id" value="${attr(item.user_id||'')}" placeholder="用户 ID" ${item.isNew?'':'readonly'}><div class="quota-status">${quotaStatus(item)}</div><div class="number-stepper"><input type="number" min="0" step="1" data-quota-field="quota" value="${Math.max(0,Number(item.quota)||0)}"><div class="step-buttons"><button type="button" data-quota-step="1" aria-label="增加额度">▲</button><button type="button" data-quota-step="-1" aria-label="减少额度">▼</button></div></div><span>${escapeHtml(item.last_refresh_date||'—')}</span><span>${escapeHtml(item.last_checkin_date||'—')}</span></div>`).join('');$('#quota-count').textContent=`${state.quotas.length} 条记录`}
async function loadQuotas(){const result=await apiGet('quotas');if(!result.success)throw new Error(result.message||'读取额度失败');state.quotas=(result.quotas||[]).map(item=>({...item,isNew:false}));state.quotaPolicy=result;renderQuotaPolicy();renderQuotas();setQuotaDirty(false)}
function syncQuotaField(target){const row=target.closest('[data-quota-index]');if(!row)return;const item=state.quotas[Number(row.dataset.quotaIndex)];if(target.dataset.quotaField==='quota'){item.quota=Math.max(0,Math.trunc(Number(target.value)||0));target.value=item.quota}else item.user_id=target.value.trim();setQuotaDirty(true)}
async function saveQuotas(){const ids=state.quotas.map(item=>String(item.user_id||'').trim());if(ids.some(id=>!id)){notify('用户 ID 不能为空',true);return}if(new Set(ids).size!==ids.length){notify('用户 ID 不能重复',true);return}const result=await apiPost('quotas/save',{quotas:state.quotas.map(item=>({user_id:String(item.user_id).trim(),quota:Math.max(0,Math.trunc(Number(item.quota)||0))}))});notify(result.message,!result.success);if(result.success)await loadQuotas()}

/* ---------------------------------------------------------------- 事件绑定 */
document.addEventListener('click',async event=>{
  const tab=event.target.closest('[data-tab]');
  if(tab){
    document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===tab));
    document.querySelectorAll('.panel').forEach(x=>x.classList.toggle('active',x.id===`tab-${tab.dataset.tab}`));
    $('#save-config').hidden=tab.dataset.tab==='quotas';
    if(tab.dataset.tab==='providers'&&!state.providers.length)await loadProviders().catch(e=>notify(e.message,true));
    if(tab.dataset.tab==='quotas'&&!state.quotas.length)await loadQuotas().catch(e=>notify(e.message,true));
    return;
  }
  const sort=event.target.closest('[data-quota-sort]');
  if(sort){const key=sort.dataset.quotaSort;if(state.quotaSort.key===key)state.quotaSort.direction=state.quotaSort.direction==='asc'?'desc':'asc';else state.quotaSort={key,direction:'asc'};renderQuotas();return}
  const step=event.target.closest('[data-quota-step]');
  if(step){const row=step.closest('[data-quota-index]');const item=state.quotas[Number(row.dataset.quotaIndex)];item.quota=Math.max(0,(Number(item.quota)||0)+Number(step.dataset.quotaStep));row.querySelector('[data-quota-field="quota"]').value=item.quota;setQuotaDirty(true);return}
  const primary=event.target.closest('[data-primary]');
  if(primary){const index=Number(primary.dataset.primary);state.primary=state.providers[index].id;state.primaryChosen=true;setDirty(true);if(state.providers[index].enabled===false)notify('该节点已停用：保存后不会参与调用，启用后才会生效',false);renderProviders();return}
  const expand=event.target.closest('[data-expand]');
  if(expand){const index=Number(expand.dataset.expand);if(state.expanded.has(index))state.expanded.delete(index);else state.expanded.add(index);expand.closest('.provider-node').classList.toggle('expanded');return}
  const deleteButton=event.target.closest('[data-delete]');
  if(deleteButton){deleteButton.disabled=true;const r=await apiPost('delete',{persona_id:state.current,filename:deleteButton.dataset.delete});notify(r.message,!r.success);if(r.success)await loadPersonaDetail();else deleteButton.disabled=false}
});
$('#provider-list').addEventListener('input',event=>{
  if(event.target.dataset.param){syncParam(event.target);return}
  if(event.target.dataset.field==='custom_params'){syncCustomParams(event.target);return}
  if(event.target.dataset.field)syncProviderField(event.target);
});
$('#provider-list').addEventListener('change',event=>{
  if(event.target.dataset.param){syncParam(event.target);return}
  if(event.target.dataset.field)syncProviderField(event.target);
});
$('#config-groups').addEventListener('change',event=>{
  if(event.target.dataset.groupField)syncGroupField(event.target);
  if(event.target.matches('[data-fallback-vision]')){
    state.visionProviderId=event.target.value;
    $('#vision-provider').value=state.visionProviderId;
    setDirty(true);
    renderGroups();
  }
});
$('#save-config').addEventListener('click',()=>saveProviderConfig().catch(e=>notify(e.message,true)));
$('#add-provider').addEventListener('click',()=>{
  let number=state.providers.length+1;
  let id=`provider_${number}`;
  const ids=new Set(state.providers.map(item=>item.id));
  while(ids.has(id)){number+=1;id=`provider_${number}`}
  const entry=catalogEntry('openai_image');
  state.providers.push(normalizeProvider({isNew:true,id,api_type:'openai_image',enabled:true,model:'',reference_image_limit:0,available_models:[],base_url:'',api_keys:[],api_keys_configured:false,default_size:'1024x1024',timeout:180,astrbot_provider_id:'',stream:false,enable_proxy:false,fallback_order:0,complete:false,thinking:'',custom_params:'',params:{},param_errors:[],param_mode:entry?entry.param_mode:'typed',capabilities:entry?entry.capabilities:{}}));
  if(!state.primary){state.primary=id;state.primaryChosen=true}
  state.expanded.add(state.providers.length-1);
  setDirty(true);
  renderProviders();
});
$('#quota-list').addEventListener('input',event=>{if(event.target.dataset.quotaField)syncQuotaField(event.target)});
$('#quota-list').addEventListener('change',event=>{if(event.target.dataset.quotaField)syncQuotaField(event.target)});
$('#add-quota-row').addEventListener('click',()=>{const index=state.quotas.length;state.quotas.push({user_id:'',quota:0,blocked:false,unlimited:false,last_refresh_date:'',last_checkin_date:'',isNew:true});renderQuotas();setQuotaDirty(true);const input=document.querySelector(`[data-quota-index="${index}"] [data-quota-field="user_id"]`);if(input)input.focus()});
$('#save-quotas').addEventListener('click',()=>saveQuotas().catch(e=>notify(e.message,true)));
$('#gallery').addEventListener('change',event=>{const name=event.target.dataset.reference;if(!name)return;if(event.target.checked)state.selectedReferences.add(name);else state.selectedReferences.delete(name)});
$('#optimizer-provider').addEventListener('change',event=>{state.optimizerProviderId=event.target.value;setDirty(true)});
$('#vision-provider').addEventListener('change',event=>{state.visionProviderId=event.target.value;setDirty(true);renderGroups()});
$('#persona-search').addEventListener('input',renderPersonaOptions);
$('#persona-select').addEventListener('change',async event=>{state.current=event.target.value;await loadPersonaDetail().catch(e=>notify(e.message,true))});
$('#rebuild').addEventListener('click',async()=>{if(state.rebuilding||!state.current)return;const personaId=state.current;setRebuildLoading(true);try{const r=await apiPost('rebuild',{persona_id:personaId,reference_names:[...state.selectedReferences]});if(!r.success){notify(r.message||'重建失败',true);return}if(state.current===personaId)await loadPersonaDetail();notify(r.message||'外观摘要已重建')}catch(error){notify(error.message||'重建失败',true)}finally{setRebuildLoading(false)}});
$('#persona-summary').addEventListener('input',updateSummaryCount);
$('#save-summary').addEventListener('click',async()=>{const summary=$('#persona-summary').value.trim();if(!summary){notify('外观摘要不能为空',true);return}const r=await apiPost('summary',{persona_id:state.current,summary});notify(r.message,!r.success);if(r.success)await loadPersonaDetail()});
$('#upload').addEventListener('click',async()=>{const files=[...$('#reference-files').files];if(!files.length){notify('请先选择图片',true);return}const images=[];for(const file of files)images.push({mime_type:file.type,data:await readFile(file)});const r=await apiPost('upload',{persona_id:state.current,images});notify(r.message,!r.success);if(r.success){$('#reference-files').value='';await loadPersonaDetail()}});
window.addEventListener('beforeunload',event=>{if(!state.dirty&&!state.quotaDirty)return;event.preventDefault();event.returnValue=''});

/* 测试钩子：Node vm 回归直接调用真实渲染/装配逻辑（对线上无副作用）。 */
window.__imagoWebui={state,catalogEntry,catalogLabel,paramFields,capabilitiesOf,qualityText,thinkingCapText,referencesText,thinkingValues,thinkingLabel,normalizeProvider,renderProviders,renderGroups,buildProviderPayload,applySnapshot,syncProviderField,syncGroupField,setParamValue,clearConfirmMessage};

(async()=>{
  try{
    await waitBridge();
    const bridge=getBridge();
    if(bridge&&typeof bridge.onContext==='function')bridge.onContext(context=>{if(typeof context?.isDark==='boolean')document.documentElement.dataset.theme=context.isDark?'dark':'light'});
    await Promise.all([loadPersonas(),loadProviders(),loadQuotas()]);
  }catch(error){
    notify(error.message||'页面初始化失败',true);
  }
})();
