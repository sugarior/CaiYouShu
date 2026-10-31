const app = document.querySelector('#app');
const cap = document.querySelector('#account-cap');
const logoutButton = document.querySelector('#logout');
let state = null;
let currentUser = null;
let activePane = 'inbox';
let refreshTimer = null;
const esc = (value = '') => String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));

function toast(message, bad = false) {
  const node = document.querySelector('#toast');
  node.textContent = message;
  node.className = `toast visible${bad ? ' bad' : ''}`;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.className = 'toast', 4200);
}

async function api(path, options = {}) {
  const response = await fetch(path, {credentials:'same-origin', ...options, headers:{...(options.body ? {'Content-Type':'application/json'} : {}), ...(options.headers || {})}});
  const result = response.headers.get('Content-Type')?.includes('application/json') ? await response.json() : {};
  if (!response.ok) throw new Error(result.error || `请求失败 (${response.status})`);
  return result;
}

function collectControllers(prefix = '') {
  return [1, 2, 3].map(index => document.querySelector(`#${prefix}controller-${index}`)?.value.trim() || '').filter(Boolean);
}

function renderSetup() {
  cap.textContent = '本机设置';
  logoutButton.classList.add('hidden');
  app.innerHTML = `
    <section class="auth-shell">
      <div class="auth-heading"><span class="eyebrow">LOCAL DEMO</span><h1>连接 AI 服务</h1><p>为本机三个演示账号设置统一的 DeepSeek API Key。</p></div>
      <form id="setup-form" class="form-stack">
        <label>DeepSeek API Key<input name="api_key" type="password" autocomplete="off" required minlength="12"></label>
        <button class="button button-primary" type="submit">验证并保存</button>
        <p class="form-note">密钥保存在本机，不会展示给注册用户。</p>
        <p class="form-error" id="form-error"></p>
      </form>
    </section>`;
  document.querySelector('#setup-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button');
    button.disabled = true; button.textContent = '正在验证';
    try { await api('/api/setup', {method:'POST', body:JSON.stringify({api_key:new FormData(event.currentTarget).get('api_key')})}); await bootstrap(); }
    catch (error) { document.querySelector('#form-error').textContent = error.message; button.disabled = false; button.textContent = '验证并保存'; }
  });
}

function renderClaim(user) {
  cap.textContent = `${state.registered} / ${state.limit} 个演示账号`;
  logoutButton.classList.add('hidden');
  app.innerHTML = `<section class="auth-shell"><div class="auth-heading"><span class="eyebrow">原邮箱账号</span><h1>认领现有 Agent</h1><p>${esc(user.agent_email)} 的邮箱设置和模型配置已载入本机演示环境。</p></div><form id="claim-form" class="form-stack"><label>设置工作台密码<input name="password" type="password" minlength="10" maxlength="256" autocomplete="new-password" required></label><button class="button button-primary" type="submit">验证原邮箱并继续</button><p class="form-note">系统会验证原邮箱的 IMAP/SMTP 登录；邮箱授权码不会显示。</p><p id="form-error" class="form-error"></p></form></section>`;
  document.querySelector('#claim-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('[type="submit"]');
    button.disabled = true; button.textContent = '正在验证邮箱';
    try {
      await api('/api/claim-original', {method:'POST',body:JSON.stringify({password:new FormData(event.currentTarget).get('password')})});
      await bootstrap();
    } catch (error) {
      document.querySelector('#form-error').textContent = error.message;
      button.disabled = false; button.textContent = '验证原邮箱并继续';
    }
  });
}

function renderAuth(register = false, message = '') {
  currentUser = null;
  logoutButton.classList.add('hidden');
  cap.textContent = `${state.registered} / ${state.limit} 个演示账号`;
  const full = state.registered >= state.limit;
  if (register && full) register = false;
  app.innerHTML = `
    <section class="auth-shell auth-wide">
      <div class="auth-heading"><span class="eyebrow">PURCHASING AUTOMATION</span><h1>${register ? '创建演示账号' : '登录工作台'}</h1><p>最多开放三个相互隔离的演示账号。</p></div>
      <div class="auth-tabs"><button type="button" data-auth="login" class="${register ? '' : 'selected'}">登录</button><button type="button" data-auth="register" class="${register ? 'selected' : ''}" ${full ? 'disabled title="演示账号名额已满"' : ''}>注册 <span>${state.registered}/${state.limit}</span></button></div>
      ${register ? registerForm() : loginForm(full)}
      <p class="form-error" id="form-error">${esc(message)}</p>
    </section>`;
  document.querySelectorAll('[data-auth]').forEach(button => button.addEventListener('click', () => renderAuth(button.dataset.auth === 'register')));
  const form = document.querySelector('#auth-form');
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('[type="submit"]');
    button.disabled = true;
    button.textContent = register ? '正在验证邮箱并创建账号' : '正在登录';
    try {
      const payload = new FormData(form);
      const body = register ? {
        login_email:payload.get('login_email'), password:payload.get('password'),
        agent_email:payload.get('agent_email'), authorization_code:payload.get('authorization_code'),
        controllers:collectControllers(), supplier_email:payload.get('supplier_email')
      } : {login_email:payload.get('login_email'), password:payload.get('password')};
      await api(register ? '/api/register' : '/api/login', {method:'POST', body:JSON.stringify(body)});
      await bootstrap();
    } catch (error) {
      document.querySelector('#form-error').textContent = error.message;
      button.disabled = false;
      button.textContent = register ? '验证邮箱并创建账号' : '登录';
    }
  });
}

function registerForm() {
  return `<form id="auth-form" class="form-stack">
    <div class="form-grid">
      <label>登录邮箱<input name="login_email" type="email" autocomplete="email" required></label>
      <label>登录密码<input name="password" type="password" autocomplete="new-password" required minlength="10" maxlength="256"></label>
      <label>Agent 收件邮箱<input name="agent_email" type="email" autocomplete="email" required></label>
      <label>邮箱客户端授权码<input name="authorization_code" type="password" autocomplete="new-password" required></label>
      <label>演示供应商收件邮箱<input name="supplier_email" type="email" value="xyinchina@163.com" required></label>
      <div class="field-hint">新建的演示供应商将统一使用此地址</div>
    </div>
    <fieldset class="controller-fields"><legend>控制邮箱 <span>最多 3 个</span></legend>
      ${[1,2,3].map(index=>`<label class="controller-row"><span>${index}</span><input id="controller-${index}" type="email" ${index === 1 ? 'required' : ''} placeholder="邮箱地址" autocomplete="email"></label>`).join('')}
    </fieldset>
    <button class="button button-primary" type="submit">验证邮箱并创建账号</button>
    <p class="form-note">创建时会实际验证 Agent 邮箱的 IMAP 和 SMTP 登录。授权码仅保存在本机账号目录。</p>
  </form>`;
}

function loginForm(full) {
  return `<form id="auth-form" class="form-stack">
    <label>登录邮箱<input name="login_email" type="email" autocomplete="username" required></label>
    <label>登录密码<input name="password" type="password" autocomplete="current-password" required></label>
    <button class="button button-primary" type="submit">登录</button>
    ${full ? '<p class="form-note">当前演示账号名额已满。</p>' : '<p class="form-note">新用户可注册；邮箱信息保存后可在设置中修改。</p>'}
  </form>`;
}

function renderWorkspace(user, agent) {
  currentUser = user;
  logoutButton.classList.remove('hidden');
  cap.textContent = `${state.registered} / ${state.limit} 个演示账号`;
  app.innerHTML = `
    <div class="workspace-head"><div><span class="eyebrow">${esc(user.login_email)}</span><h1>采购邮件工作台</h1></div><div id="agent-status" class="status-pill ${agent?.online ? 'online' : 'offline'}"><span></span>${agent?.online ? 'Agent 运行中' : 'Agent 正在连接'}</div></div>
    <div class="workspace-layout">
      <nav class="side-nav" aria-label="工作区"><button data-pane="inbox" class="${activePane === 'inbox' ? 'active' : ''}">收件箱</button><button data-pane="settings" class="${activePane === 'settings' ? 'active' : ''}">邮箱设置</button><div class="nav-rule"></div><div class="tenant-summary"><span>Agent 邮箱</span><strong>${esc(user.agent_email)}</strong><span>已归档邮件</span><strong id="archive-count">${Number(agent?.archived_count || 0)}</strong></div></nav>
      <section id="workspace-pane" class="pane"></section>
    </div>`;
  document.querySelectorAll('[data-pane]').forEach(button => button.addEventListener('click', () => { activePane = button.dataset.pane; renderWorkspace(currentUser, state.agent); }));
  document.querySelector('#logout').onclick = async () => { try { await api('/api/logout', {method:'POST',body:'{}'}); } finally { await bootstrap(); } };
  if (activePane === 'settings') renderSettings(user, agent); else renderInbox();
  clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshWorkspace, 15000);
}

async function refreshWorkspace() {
  if (!currentUser) return;
  try {
    const data = await api('/api/bootstrap');
    if (!data.user) return bootstrap();
    state = data;
    currentUser = data.user;
    const status = document.querySelector('#agent-status');
    if (status) {
      status.className = `status-pill ${data.agent?.online ? 'online' : 'offline'}`;
      status.innerHTML = `<span></span>${data.agent?.online ? 'Agent 运行中' : 'Agent 正在连接'}`;
    }
    const count = document.querySelector('#archive-count');
    if (count) count.textContent = Number(data.agent?.archived_count || 0);
  } catch (error) { toast(error.message, true); }
}

function renderSettings(user, agent) {
  const pane = document.querySelector('#workspace-pane');
  pane.innerHTML = `<div class="pane-title"><div><h2>邮箱设置</h2><p>修改后会重新验证 IMAP 和 SMTP 登录。</p></div></div>
    <form id="settings-form" class="settings-form">
      <div class="form-grid">
        <label>Agent 收件邮箱<input name="agent_email" type="email" value="${esc(user.agent_email)}" required></label>
        <label>邮箱客户端授权码<input name="authorization_code" type="password" autocomplete="new-password" placeholder="留空保持当前授权码"></label>
        <label>演示供应商收件邮箱<input name="supplier_email" type="email" value="${esc(user.supplier_email)}" required></label>
        <div class="field-hint">修改后会同步更新此账号全部演示供应商邮箱。</div>
      </div>
      <fieldset class="controller-fields"><legend>控制邮箱 <span>1 到 3 个</span></legend>
        ${[1,2,3].map((index)=>`<label class="controller-row"><span>${index}</span><input id="edit-controller-${index}" type="email" value="${esc(user.controllers[index-1] || '')}" ${index === 1 ? 'required' : ''} placeholder="邮箱地址"></label>`).join('')}
      </fieldset>
      <div class="form-actions"><button class="button button-primary" type="submit">验证并保存</button><button id="agent-toggle" class="button button-secondary" type="button">${agent?.online ? '停止 Agent' : '启动 Agent'}</button></div>
      <p id="form-error" class="form-error"></p>
    </form>`;
  document.querySelector('#settings-form').addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('[type="submit"]');
    button.disabled = true; button.textContent = '正在验证邮箱';
    const payload = new FormData(event.currentTarget);
    try {
      await api('/api/mail-settings', {method:'POST',body:JSON.stringify({agent_email:payload.get('agent_email'),authorization_code:payload.get('authorization_code'),supplier_email:payload.get('supplier_email'),controllers:collectControllers('edit-')})});
      toast('邮箱设置已验证并保存');
      await bootstrap();
    } catch (error) {
      document.querySelector('#form-error').textContent = error.message;
      button.disabled = false; button.textContent = '验证并保存';
    }
  });
  document.querySelector('#agent-toggle').addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await api(agent?.online ? '/api/agent/stop' : '/api/agent/start', {method:'POST',body:'{}'});
      await bootstrap();
    } catch (error) { toast(error.message, true); button.disabled = false; }
  });
}

async function renderInbox() {
  const pane = document.querySelector('#workspace-pane');
  if (!pane) return;
  pane.innerHTML = `<div class="pane-title"><div><h2>收件箱</h2><p>Agent 邮箱归档</p></div><button id="refresh-inbox" class="icon-button" type="button" title="刷新" aria-label="刷新">↻</button></div>
    <div class="inbox-tools"><input id="mail-search" type="search" placeholder="搜索主题、发件人或正文"><select id="mail-kind" aria-label="邮件类型"><option value="all">全部类型</option><option value="incoming">收件</option><option value="control">控制</option><option value="outgoing">发件</option><option value="agent">Agent</option></select></div>
    <div class="mail-layout"><div id="mail-list" class="mail-list"><div class="empty">正在读取归档</div></div><article id="mail-detail" class="mail-detail"><div class="detail-placeholder">选择一封邮件</div></article></div>`;
  document.querySelector('#refresh-inbox').onclick = loadInbox;
  document.querySelector('#mail-search').oninput = debounce(loadInbox, 250);
  document.querySelector('#mail-kind').onchange = loadInbox;
  await loadInbox();
}

async function loadInbox() {
  const list = document.querySelector('#mail-list');
  if (!list || !currentUser) return;
  list.innerHTML = '<div class="empty">正在读取归档</div>';
  try {
    const query = new URLSearchParams({q:document.querySelector('#mail-search')?.value || '',kind:document.querySelector('#mail-kind')?.value || 'all'});
    const result = await api('/api/inbox?' + query);
    if (!result.items.length) { list.innerHTML = '<div class="empty">暂无归档邮件</div>'; return; }
    list.innerHTML = result.items.map(mail => `<button class="mail-row" data-mail="${esc(mail.id)}"><span class="mail-meta"><strong>${esc(mail.subject)}</strong><time>${esc(formatDate(mail.received_at))}</time></span><span class="mail-sender">${esc(mail.from.join(', '))}</span><span class="mail-snippet">${esc(mail.summary || mail.snippet || ' ')}</span><span class="mail-label">${esc(mail.kind)} · ${esc(mail.status)}</span></button>`).join('');
    list.querySelectorAll('[data-mail]').forEach(button => button.onclick = () => loadMail(button.dataset.mail, button));
  } catch (error) { list.innerHTML = `<div class="empty error">${esc(error.message)}</div>`; }
}

async function loadMail(id, button) {
  document.querySelectorAll('.mail-row.selected').forEach(row => row.classList.remove('selected'));
  button.classList.add('selected');
  const detail = document.querySelector('#mail-detail');
  detail.innerHTML = '<div class="detail-placeholder">正在读取邮件</div>';
  try {
    const result = await api('/api/mail/' + encodeURIComponent(id));
    const parsed = result.parsed || {};
    detail.innerHTML = `<div class="detail-head"><h3>${esc(parsed.subject || '（无主题）')}</h3><dl><div><dt>发件人</dt><dd>${esc((parsed.from || []).join(', '))}</dd></div><div><dt>收件人</dt><dd>${esc((parsed.to || []).join(', '))}</dd></div><div><dt>时间</dt><dd>${esc(formatDate(result.received_at))}</dd></div></dl></div><pre class="mail-body">${esc(parsed.text || '无纯文本正文')}</pre>${parsed.attachments?.length ? `<div class="attachment-list">${parsed.attachments.map(item=>`<span>${esc(item.filename)} · ${Number(item.size || 0)} B</span>`).join('')}</div>` : ''}${result.control_result?.reply_body ? `<section class="reply-result"><h4>Agent 处理结果</h4><pre>${esc(result.control_result.reply_body)}</pre></section>` : ''}`;
  } catch (error) { detail.innerHTML = `<div class="detail-placeholder error">${esc(error.message)}</div>`; }
}

function formatDate(value) {
  if (!value) return '';
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(parsed);
}

function debounce(fn, delay) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); };
}

async function bootstrap() {
  try {
    state = await api('/api/bootstrap');
    if (!state.configured) renderSetup();
    else if (!state.user && state.claimable) renderClaim(state.claimable);
    else if (!state.user) renderAuth(false);
    else renderWorkspace(state.user, state.agent);
  } catch (error) { app.innerHTML = `<section class="auth-shell"><h1>本地工作台未启动</h1><p>${esc(error.message)}</p></section>`; }
}

bootstrap();
