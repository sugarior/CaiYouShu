"use strict";

const $ = (id) => document.getElementById(id);
const state = { account: "", view: "mail", query: "", folder: "", kind: "all", page: 1,
  selected: "", sequence: 0, detailSequence: 0, detailKey: "", listKey: "", tasksKey: "", outboxKey: "", rows: [] };
const labels = {
  analyzed: "AI 已解析", pending: "等待解析", control_processed: "任务已处理",
  control_rejected_sender: "控制者未授权", control_rejected_token: "控制口令未通过",
  control_invalid_format: "控制格式有误", historical_control_archived_only: "历史控制邮件",
  archived_only_self_sent: "自己发送", archived_only_outgoing_or_unverified_time: "仅归档",
  sent: "SMTP 已接受", sent_partial: "部分地址已接受", sending: "正在发送", preview: "预览",
  uncertain: "结果待确认", blocked: "已阻止", cancelled: "已取消"
};
const folderLabels = { INBOX: "收件箱" };
const kindLabels = { incoming: "外部来信", control: "控制邮件", agent: "Agent 邮件", outgoing: "自己发送" };
let refreshing = false;
let searchTimer;
let latestService = {};
let networkError = "";

function element(tag, className = "", value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (value !== undefined && value !== null) node.textContent = String(value);
  return node;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
function badge(text, color = "", symbol) {
  const node = element("span", `badge ${color}`);
  if (symbol) node.append(icon(symbol));
  node.append(document.createTextNode(text));
  return node;
}
function empty(title, description, symbol = "inbox") {
  const node = element("div", "empty");
  const graphic = element("span", "empty-icon");
  graphic.append(icon(symbol));
  node.append(graphic, element("h3", "", title), element("p", "", description));
  return node;
}
function formatDate(value, short = false) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const current = new Date();
  if (short && date.toDateString() === current.toDateString()) return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  return date.toLocaleString("zh-CN", short
    ? { month: "2-digit", day: "2-digit", ...(date.getFullYear() !== current.getFullYear() ? { year: "numeric" } : {}) }
    : { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function sizeLabel(bytes) {
  if (bytes < 1024) return `${bytes || 0} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function apiBase(account = state.account) { return `/api/accounts/${encodeURIComponent(account)}`; }
async function api(path) {
  const response = await fetch(path, { cache: "no-store", signal: AbortSignal.timeout(12000) });
  if (!response.ok) throw new Error(response.status === 404 ? "记录不存在，可能已从本地移除。" : "暂时无法读取归档，请稍后刷新。");
  return response.json();
}
function showNotice() {
  const service = latestService;
  const messages = [];
  if (networkError) messages.push(networkError);
  else if (service.state === "waiting_configuration") messages.push("收信服务正在等待配置，请在本机 config.yml 中补齐邮箱、AI 和控制者信息。");
  else if (!service.online) messages.push("收信服务当前离线或心跳已过期。已有归档仍可查看，重新运行 start.ps1 可启动服务。");
  if (service.last_error) messages.push(`最近一次收信错误：${service.last_error}`);
  if (service.sync_errors?.length) messages.push(`部分文件夹同步异常：${service.sync_errors.map((v) => typeof v === "string" ? v : JSON.stringify(v)).join("；")}`);
  $("notice").hidden = !messages.length;
  $("notice").textContent = messages.join(" ");
}
function renderOverview(data) {
  latestService = data.service;
  const accountsKey = JSON.stringify(data.accounts);
  const select = $("account-select");
  if (select.dataset.key !== accountsKey) {
    select.replaceChildren(...data.accounts.map((account) => {
      const option = element("option", "", account.address);
      option.value = account.id;
      return option;
    }));
    if (!data.accounts.length) {
      const option = element("option", "", "暂无邮箱归档");
      option.value = "";
      select.append(option);
    }
    select.dataset.key = accountsKey;
  }
  if (!data.accounts.some((account) => account.id === state.account)) changeAccount(data.active_account);
  select.value = state.account;
  const service = data.service;
  const online = service.online && service.state !== "waiting_configuration" && !(service.last_error || service.sync_errors?.length);
  $("service-pill").className = `service-pill ${online ? "online" : "warning"}`;
  $("service-pill").lastElementChild.textContent = online ? "收信服务在线" : service.state === "waiting_configuration" ? "等待配置" : service.online ? "收信服务重试中" : "收信服务离线";
  $("service-pill").title = `最后心跳：${formatDate(service.heartbeat_at)}；待解析：${service.pending_analysis ?? 0}`;
  showNotice();
}
function resetContent() {
  state.selected = "";
  state.detailKey = state.listKey = state.tasksKey = state.outboxKey = "";
  state.detailSequence++;
  state.rows = [];
  $("mail-workbench").classList.remove("detail-open");
}
function changeAccount(account) {
  state.account = account || "";
  state.page = 1;
  state.folder = "";
  resetContent();
}
function renderFolders(folders) {
  const nav = $("folder-nav");
  const key = JSON.stringify([folders, state.folder]);
  if (nav.dataset.key === key) return;
  nav.dataset.key = key;
  nav.replaceChildren(...["", ...folders].map((name) => {
    const button = element("button", `folder-button ${state.folder === name ? "active" : ""}`);
    button.append(icon(name ? "folder" : "inbox"), element("span", "", name ? folderLabels[name] || name : "全部文件夹"));
    button.setAttribute("aria-pressed", String(state.folder === name));
    button.addEventListener("click", () => {
      state.folder = name;
      state.page = 1;
      setView("mail");
      loadContent();
    });
    return button;
  }));
}
function statusBadge(status, outbox = false) {
  if (outbox) return badge(status === "pending" ? "等待发送" : labels[status] || status || "未知状态",
    status === "sent" ? "green" : ["uncertain", "sent_partial", "preview"].includes(status) ? "amber" : status === "blocked" ? "red" : "");
  return badge(labels[status] || (status?.includes("control") ? "控制邮件已归档" : "已归档"), status === "analyzed" ? "green" : status?.includes("rejected") || status?.includes("invalid") ? "amber" : "", status === "analyzed" ? "spark" : undefined);
}
function renderMail(data) {
  for (const [key, id] of [["all", "stat-all"], ["incoming", "stat-incoming"], ["analyzed", "stat-analyzed"], ["matched", "stat-matched"]]) $(id).textContent = data.counts[key].toLocaleString("zh-CN");
  $("nav-mail-count").textContent = data.counts.all;
  $("list-total").textContent = `${data.total} 封`;
  $("list-title").textContent = state.folder ? folderLabels[state.folder] || state.folder : "全部邮件";
  $("page-label").textContent = `第 ${data.page} / ${data.pages} 页`;
  $("prev-page").disabled = data.page <= 1;
  $("next-page").disabled = data.page >= data.pages;
  state.page = data.page;
  state.rows = data.items;
  renderFolders(data.folders);
  if (!data.items.some((row) => row.id === state.selected)) {
    state.selected = data.items[0]?.id || "";
    state.detailKey = "";
    $("mail-workbench").classList.remove("detail-open");
  }
  const key = JSON.stringify([data.items, state.selected]);
  if (key !== state.listKey) {
    const scroll = $("mail-list").scrollTop;
    $("mail-list").replaceChildren(...data.items.map((row) => {
      const button = element("button", `mail-item ${state.selected === row.id ? "selected" : ""}`);
      button.dataset.id = row.id;
      button.setAttribute("aria-pressed", String(state.selected === row.id));
      const sender = row.from.join(", ") || "未知发件人";
      button.append(element("span", "mail-avatar", sender[0].toUpperCase()));
      const content = element("div", "mail-item-content");
      const meta = element("div", "mail-meta");
      meta.append(element("span", "mail-sender", sender), element("time", "mail-date", formatDate(row.received_at, true)));
      const badges = element("div", "mail-badges");
      badges.append(statusBadge(row.status));
      if (row.matches) badges.append(badge(`命中 ${row.matches}`, "amber"));
      if (row.attachments) { const clip = element("span", "attachment-indicator"); clip.append(icon("clip"), document.createTextNode(row.attachments)); badges.append(clip); }
      content.append(meta, element("h3", "mail-subject", row.subject), element("p", "mail-snippet", row.summary || row.snippet || "（无正文）"), badges);
      button.append(content);
      button.addEventListener("click", () => selectMail(row.id));
      return button;
    }));
    if (!data.items.length) $("mail-list").append(empty(state.query || state.folder || state.kind !== "all" ? "没有找到匹配邮件" : "归档中还没有邮件", state.query || state.folder || state.kind !== "all" ? "试试其他关键词、文件夹或邮件类型。" : "服务收到并归档邮件后，会自动出现在这里。"));
    $("mail-list").scrollTop = scroll;
    state.listKey = key;
  }
  if (state.selected) loadDetail(state.selected);
  else { state.detailSequence++; $("mail-detail").replaceChildren(empty("在这里读懂每一封来信", "选择一封邮件，查看正文、AI 摘要与任务匹配结果。", "mail")); }
}
function selectMail(id) {
  state.selected = id;
  state.detailKey = "";
  state.listKey = "";
  for (const button of $("mail-list").querySelectorAll(".mail-item")) {
    const selected = button.dataset.id === id;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  }
  $("mail-workbench").classList.add("detail-open");
  loadDetail(id);
}
async function loadDetail(id) {
  const sequence = ++state.detailSequence;
  const account = state.account;
  if (!state.detailKey) $("mail-detail").replaceChildren(empty("正在读取邮件", "加载正文与解析结果。", "mail"));
  try {
    const data = await api(`${apiBase(account)}/mail/${encodeURIComponent(id)}`);
    if (sequence !== state.detailSequence || id !== state.selected || account !== state.account) return;
    const key = JSON.stringify(data);
    if (key === state.detailKey) return;
    const scroll = state.detailKey ? $("mail-detail").scrollTop : 0;
    renderDetail(data);
    state.detailKey = key;
    $("mail-detail").scrollTop = scroll;
  } catch (error) {
    if (sequence === state.detailSequence) $("mail-detail").replaceChildren(empty("邮件暂时无法打开", error.message, "info"));
  }
}
function renderDetail(data) {
  const parsed = data.parsed;
  const root = $("mail-detail");
  const toolbar = element("div", "detail-toolbar");
  const crumb = element("div", "detail-breadcrumb");
  const back = element("button", "mobile-back", "邮件列表");
  back.prepend(icon("back"));
  back.addEventListener("click", () => $("mail-workbench").classList.remove("detail-open"));
  crumb.append(back, icon("folder"), document.createTextNode((data.sources || []).map((s) => folderLabels[s.folder] || s.folder).filter((v, i, all) => all.indexOf(v) === i).join(" · ") || "本地归档"));
  const raw = element("a", "text-link", "下载原始邮件");
  raw.prepend(icon("download")); raw.href = `${apiBase()}/mail/${data.id}/raw`; raw.setAttribute("download", "");
  toolbar.append(crumb);
  if (data.original_available) toolbar.append(raw);
  const content = element("div", "detail-content");
  content.append(element("h2", "detail-title", parsed.subject || "（无主题）"));
  const sender = (parsed.from || []).join(", ") || "未知发件人";
  const senderBlock = element("div", "sender-block");
  const senderInfo = element("div", "sender-info");
  senderInfo.append(element("strong", "", sender), element("p", "", `发送至 ${(parsed.to || []).join(", ") || "—"}`), element("p", "", `收信时间 ${formatDate(data.received_at)}`));
  senderBlock.append(element("span", "mail-avatar", sender[0].toUpperCase()), senderInfo, element("time", "", formatDate(data.received_at, true)));
  content.append(senderBlock);
  const summary = element("section", "summary-card");
  const summaryHeader = element("div", "summary-header");
  const summaryTitle = element("h3", "", "AI 内容摘要"); summaryTitle.prepend(icon("spark"));
  summaryHeader.append(summaryTitle, element("span", "", "POWERED BY DEEPSEEK"));
  const processing = data.processing || {};
  let summaryText = data.analysis?.summary;
  if (!summaryText) summaryText = processing.status === "pending" ? (processing.attempts ? "邮件已归档。AI 解析暂未完成，服务会稍后重试。" : "邮件已归档，正在等待 AI 解析。") : processing.status?.includes("control") ? "这是一封控制邮件，可在下方查看处理结果。" : "此邮件仅归档，没有生成 AI 摘要。";
  summary.append(summaryHeader, element("p", "", summaryText));
  content.append(summary);
  for (const match of data.analysis?.matches || []) {
    const card = element("section", "match-card");
    const title = element("h3", "", `匹配任务 · ${match.task_id}`); title.prepend(icon("task"));
    card.append(title, element("p", "", match.reason), element("p", "", `通知收件人：${(match.recipients || []).join(", ")}`));
    const details = element("details", "metadata");
    details.append(element("summary", "", "查看生成的通知内容"), element("p", "", match.subject), element("p", "", match.body));
    card.append(details); content.append(card);
  }
  content.append(element("h3", "body-heading", "邮件正文"), element("pre", "message-body", parsed.text || "（无可展示的纯文本正文）"));
  if (data.control_result) content.append(element("h3", "body-heading", "控制执行结果"), element("pre", "control-result", JSON.stringify(data.control_result, null, 2)));
  if (parsed.attachments?.length) {
    const attachments = element("section", "attachments");
    const title = element("h3", "", `${parsed.attachments.length} 个附件`); title.prepend(icon("clip"));
    const links = element("div", "attachment-list");
    parsed.attachments.forEach((attachment, index) => {
      const link = element(attachment.original_available ? "a" : "div", "attachment");
      if (attachment.original_available) {
        link.href = `${apiBase()}/mail/${data.id}/attachments/${index}`; link.setAttribute("download", "");
      }
      const info = element("div");
      info.append(element("strong", "", attachment.filename || `附件 ${index + 1}`), element("small", "", `${sizeLabel(attachment.size)} · ${attachment.content_type || "文件"}`));
      link.append(icon("clip"), info);
      if (attachment.original_available) link.append(icon("download"));
      else info.append(element("small", "", "仅保存提取内容"));
      links.append(link);
      const extraction = attachment.extraction;
      if (extraction) {
        const details = element("details", "metadata");
        const names = {ok: "完整提取", truncated: "部分提取", error: "解析失败"};
        details.append(element("summary", "", `${attachment.filename || "表格"} · ${names[extraction.status] || extraction.status} · ${extraction.extracted_rows || 0} 行（含表头）`));
        if (extraction.error) details.append(element("p", "", extraction.error));
        for (const sheet of extraction.sheets || []) {
          const text = sheet.rows.map(row => row.map(value => /[",\r\n]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value).join(",")).join("\n");
          details.append(element("h4", "", sheet.name), element("pre", "message-body", text));
        }
        for (const note of extraction.notes || []) details.append(element("p", "", note));
        links.append(details);
      }
    });
    attachments.append(title, links); content.append(attachments);
  }
  const metadata = element("details", "metadata");
  const pairs = [["处理状态", labels[processing.status] || processing.status], ["本地归档", formatDate(data.archived_at)], ["AI 模型", data.analysis?.model || "—"], ["解析时间", formatDate(data.analysis?.analyzed_at)], ["邮件标识", parsed.message_id || data.id]];
  if (processing.last_error_type) pairs.push(["上次解析错误", processing.last_error_type]);
  if (parsed.parse_error) pairs.push(["正文解析错误", parsed.parse_error]);
  const list = element("dl");
  for (const [key, value] of pairs) list.append(element("dt", "", key), element("dd", "", value || "—"));
  metadata.append(element("summary", "", "查看归档与解析信息"), list); content.append(metadata);
  root.replaceChildren(toolbar, content);
}
function renderTasks(data) {
  $("nav-task-count").textContent = data.total + (data.reply_once ? 1 : 0);
  const key = JSON.stringify(data);
  if (state.tasksKey === key) return;
  state.tasksKey = key;
  $("task-list").replaceChildren(...data.items.map((task) => {
    const card = element("article", "task-card");
    const header = element("header");
    header.append(element("h3", "", task.name), badge(task.enabled ? "已启用" : "已暂停", task.enabled ? "green" : ""));
    card.append(header, element("p", "task-id", `${task.id} · v${task.version}`));
    for (const [label, value] of [["触发条件", task.condition], ["通知收件人", (task.recipients || []).join("\n")], ["通知内容要求", task.instruction]]) card.append(element("h4", "", label), element("p", "", value));
    card.append(element("footer", "", `控制者 ${task.owner || "—"} · 更新于 ${formatDate(task.updated_at)}`));
    return card;
  }));
  if (data.reply_once) {
    const task = data.reply_once;
    const names = { armed: "等待下一封来信", queued: "已选中来信", pending: "等待发送", sending: "正在发送", completed: "已完成 · 自动失效", uncertain: "发送结果待核实", cancelled: "已取消", blocked: "已阻止", preview: "仅预览" };
    const card = element("article", "task-card");
    const header = element("header");
    header.append(element("h3", "", "一次性收信测试"), badge(names[task.status] || task.status, ["armed", "completed"].includes(task.status) ? "green" : "amber"));
    card.append(header, element("p", "task-id", "本地授权 · 仅执行一次"), element("h4", "", "触发条件"), element("p", "", task.mode === "csv" ? "启用后收到的第一封带 CSV 附件的普通来信。其他来信不消耗本次测试。" : "启用后收到的第一封普通来信。历史邮件、控制邮件、自发邮件和自动通知不触发。"));
    card.append(element("h4", "", "回复内容"), element("p", "", task.mode === "csv" ? "向原发件人回复：DeepSeek 摘要及 CSV 提取原文。表格内的邮箱不会成为收件人。" : "向原发件人回复：收到"));
    if (task.mode === "csv" && task.status === "pending" && !task.recipients?.length) card.append(element("p", "", "已选中 CSV 来信，等待 AI 分析完成。"));
    if (task.recipients?.length) card.append(element("h4", "", "实际收件人"), element("p", "", task.recipients.join(", ")));
    if (task.status === "uncertain") card.append(element("p", "", "发送结果不明确，不会自动重发或选择下一封邮件。"));
    card.append(element("footer", "", `启用时间 ${formatDate(task.armed_at)}${task.sent_at ? ` · SMTP 接受时间 ${formatDate(task.sent_at)}` : ""}`));
    $("task-list").prepend(card);
  }
  if (!data.items.length && !data.reply_once) $("task-list").append(empty("让第一条规则开始工作", "从白名单邮箱发送一封控制邮件，创建你的自动化任务。", "task"));
}
function renderOutbox(data) {
  $("nav-outbox-count").textContent = data.total;
  $("outbox-total").textContent = `${data.total} 条发送记录`;
  const key = JSON.stringify(data);
  if (state.outboxKey === key) return;
  state.outboxKey = key;
  const open = new Set([...$("outbox-list").querySelectorAll("details[open]")].map((v) => v.dataset.id));
  $("outbox-list").replaceChildren(...data.items.map((job) => {
    const details = element("details", "outbox-item");
    details.dataset.id = job.id; details.open = open.has(job.id);
    const summary = element("summary");
    const graphic = element("span", "outbox-icon"); graphic.append(icon("send"));
    const info = element("div", "outbox-info");
    info.append(element("strong", "", job.subject || "（无主题）"), element("p", "", `${job.kind === "receipt" ? "控制回执" : job.kind === "reply_once" ? "一次性测试回信" : `任务 ${job.task_id || ""}`} → ${(job.recipients || []).join(", ")}`));
    summary.append(graphic, info, element("time", "", formatDate(job.created_at)), statusBadge(job.status, true));
    const content = element("div", "outbox-content");
    if (job.reason) content.append(element("p", "", `触发原因：${job.reason}`));
    if (job.detail) content.append(element("p", "", job.detail));
    if (job.status === "uncertain") content.append(element("p", "", "发送结果不明确，服务不会自动重发。请核实实际投递情况后再处理。"));
    if (job.sent_at) content.append(element("p", "", `SMTP 接受时间：${formatDate(job.sent_at)}。接受不代表最终投递。`));
    if (job.last_error_type) content.append(element("p", "", `上次错误：${job.last_error_type}；尝试次数 ${job.attempts || 0}`));
    content.append(element("pre", "", job.body || "（无正文）"));
    details.append(summary, content); return details;
  }));
  if (!data.items.length) $("outbox-list").append(empty("还没有发送记录", "任务通知和控制回执会在这里记录，点击记录可查看完整内容。", "send"));
}
function queryKey() { return JSON.stringify([state.account, state.query, state.folder, state.kind, state.page]); }
async function loadContent() {
  const sequence = ++state.sequence;
  if (!state.account) {
    renderMail({ items: [], page: 1, pages: 1, total: 0, counts: { all: 0, incoming: 0, analyzed: 0, matched: 0 }, folders: [] });
    renderTasks({ items: [], total: 0, enabled: 0 }); renderOutbox({ items: [], total: 0 });
    return;
  }
  const key = queryKey();
  const params = new URLSearchParams({ q: state.query, folder: state.folder, kind: state.kind, page: state.page });
  try {
    const [mail, tasks, outbox] = await Promise.all([api(`${apiBase()}/mail?${params}`), api(`${apiBase()}/tasks`), api(`${apiBase()}/outbox`)]);
    if (sequence !== state.sequence || key !== queryKey()) return;
    renderMail(mail); renderTasks(tasks); renderOutbox(outbox);
    networkError = "";
    $("updated-at").textContent = `${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })} 已更新 · 每 15 秒同步`;
    showNotice();
  } catch (error) {
    if (sequence !== state.sequence) return;
    networkError = `${error.name === "TimeoutError" ? "读取超时" : error.message} 页面会自动重试。`;
    showNotice();
    if (!state.rows.length) $("mail-list").replaceChildren(empty("暂时无法加载邮件", "请确认本地面板仍在运行，然后点击刷新。", "info"));
  }
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  $("refresh").classList.add("refreshing");
  $("refresh").setAttribute("aria-busy", "true");
  try { renderOverview(await api("/api/overview")); await loadContent(); }
  catch (error) { networkError = "无法连接本地面板，请运行 start-dashboard.ps1 后重试。"; showNotice(); }
  finally { refreshing = false; $("refresh").classList.remove("refreshing"); $("refresh").removeAttribute("aria-busy"); }
}
function setView(view) {
  state.view = view;
  const descriptions = {
    mail: ["邮件归档", "所有来信，清晰有序。让 AI 帮你抓住重点。"],
    tasks: ["自动化任务", "用一封邮件设定规则，让重要消息自动抵达。"],
    outbox: ["发件记录", "从条件匹配到邮件发送，每一步都有迹可循。"]
  };
  $("page-title").textContent = descriptions[view][0];
  $("page-description").textContent = descriptions[view][1];
  for (const name of ["mail", "tasks", "outbox"]) $(name === "mail" ? "mail-workbench" : `${name}-view`).hidden = view !== name;
  for (const button of document.querySelectorAll("[data-view]")) {
    button.classList.toggle("active", button.dataset.view === view);
    if (button.dataset.view === view) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  }
}

// All archive and model text is inserted with textContent; no email HTML is rendered.
for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click", () => setView(button.dataset.view));
$("refresh").addEventListener("click", refresh);
$("search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.query = $("search").value.trim(); state.page = 1; loadContent(); }, 300); });
$("kind-filter").addEventListener("change", () => { state.kind = $("kind-filter").value; state.page = 1; loadContent(); });
$("prev-page").addEventListener("click", () => { state.page--; loadContent(); });
$("next-page").addEventListener("click", () => { state.page++; loadContent(); });
$("account-select").addEventListener("change", () => { changeAccount($("account-select").value); loadContent(); });
$("task-help-toggle").addEventListener("click", () => { $("task-help").hidden = !$("task-help").hidden; $("task-help-toggle").setAttribute("aria-expanded", String(!$("task-help").hidden)); });
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) && state.view === "mail") { event.preventDefault(); $("search").focus(); }
  if (event.key === "Escape") $("mail-workbench").classList.remove("detail-open");
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
$("mail-detail").append(empty("在这里读懂每一封来信", "选择一封邮件，查看正文、AI 摘要与任务匹配结果。", "mail"));
refresh();
