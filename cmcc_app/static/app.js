"use strict";
const $ = (selector) => document.querySelector(selector);
const state = { csrf: "", timezone: "Asia/Shanghai", accounts: [], runs: [], editing: null, runAccount: null, logId: null, deleting: null, authenticated: false,
  runsTotal: 0, runsPages: 1, runsPage: 1, runsPageSize: 20, runsLatest: null, runsCounts: {},
  runsScanned: null, selected: new Set(), runsLoading: false, runsLoaded: false, pendingAccounts: new Set(), pendingTasks: new Set(),
  accountQuery: "", accountStatus: "", rerunTask: null, activities: [], editingActivity: null, deletingActivity: null };
const weekdayFullLabels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const labels = { queued: "等待执行", running: "正在执行", success: "执行完成", partial: "部分成功", failed: "执行失败", interrupted: "已停止" };
const tasks = { sign: "每日签到", wed: "周三拼图", rate: "评价有礼", fee: "话费查询", seckill: "假期秒杀" };
const taskOrder = ["sign", "wed", "rate", "fee"];          // 账号编辑里的四个计划（秒杀按活动管理）
const taskKeys = ["sign", "wed", "rate", "fee", "seckill"];  // 与后端 TASK_ORDER 一致：执行记录与一键执行涵盖的任务
const weekdayLabels = ["一", "二", "三", "四", "五", "六", "日"];
const runTaskLabels = { sign: "每日签到（使用账号中保存的选项）", wed: "周三充值日拼图",
  rate: "评价有礼（提交 10 分评价，不兑换）", fee: "话费余额查询", seckill: "假期秒杀（按场次自动等待并抢券）" };
const defaultSchedule = { enabled: false, time: "08:23", frequency: "daily", weekdays: [] };
const configFields = ["phone", "app_token", "province_code", "city_code", "serverchan_sendkey", "bark_url", "webhook_url", "webhook_token", "fee_session_cookie", "activity_id", "channel_id", "app_version_code", "carrier_operator"];
const secretFields = ["app_token", "serverchan_sendkey", "bark_url", "webhook_url", "webhook_token", "fee_session_cookie"];
let toastTimer;

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function button(text, className, action) {
  const element = node("button", text, className);
  element.type = "button";
  element.addEventListener("click", action);
  return element;
}
function toast(message, error = false) {
  clearTimeout(toastTimer);
  $("#toast").textContent = message;
  $("#toast").className = error ? "toast error-toast" : "toast";
  $("#toast").hidden = false;
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 4500);
}
async function api(path, method = "GET", body) {
  const response = await fetch(`/api${path}`, {
    method, credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/login") showLogin();
    throw new Error(result.error || "操作失败，请稍后重试");
  }
  return result;
}
function showLogin() {
  state.authenticated = false;
  state.csrf = "";
  state.accounts = [];
  state.runs = [];
  state.logId = null;
  document.querySelectorAll("dialog[open]").forEach(d => d.close());
  $("#app-view").hidden = true;
  $("#login-view").hidden = false;
  $("#loading").hidden = true;
}
function showApp(session) {
  state.csrf = session.csrf;
  state.timezone = session.timezone;
  state.authenticated = true;
  $("#password-warning").hidden = !session.weak_password;
  $("#timezone-label").textContent = `${state.timezone} 时区`;
  $("#schedule-timezone").textContent = state.timezone;
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  $("#loading").hidden = true;
  showView(window.location.hash.slice(1) || "accounts", false);
}
function dateText(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: state.timezone, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}
function isPending(run) { return ["queued", "running"].includes(run.status); }
function scheduleText(entry) {
  if (!entry || !entry.enabled) return "未开启";
  if (entry.frequency === "weekly") {
    const days = (entry.weekdays || []).map(day => weekdayLabels[day]).join("/") || "三";
    return `每周${days} ${entry.time}`;
  }
  return `每天 ${entry.time}`;
}
function accountSchedules(account) {
  const stored = (account && account.schedules) || {};
  // 必须覆盖全部任务（含秒杀）：账号卡片按后端返回的 tasks 渲染，缺键会取到 undefined 而报错。
  return Object.fromEntries(taskKeys.map(task => [task, { ...defaultSchedule, ...(stored[task] || {}) }]));
}
function buildScheduleRows(account) {
  const schedules = accountSchedules(account);
  const container = $("#task-schedules");
  container.replaceChildren();
  for (const task of taskOrder) {
    const entry = schedules[task];
    const row = node("div", undefined, "task-schedule");
    row.dataset.task = task;
    const head = node("div", undefined, "task-schedule-head");
    const toggle = node("label", undefined, "switch");
    const enabled = node("input");
    enabled.type = "checkbox";
    enabled.checked = entry.enabled;
    enabled.dataset.role = "enabled";
    enabled.setAttribute("aria-label", `开启${tasks[task]}计划`);
    toggle.append(enabled, node("span"));
    head.append(node("strong", tasks[task]), toggle);
    const fields = node("div", undefined, "task-schedule-fields");
    const time = node("input");
    time.type = "time";
    time.value = entry.time;
    time.dataset.role = "time";
    const frequency = node("select");
    frequency.dataset.role = "frequency";
    for (const [value, label] of [["daily", "每天"], ["weekly", "每周"]]) {
      const option = node("option", label);
      option.value = value;
      frequency.append(option);
    }
    frequency.value = entry.frequency;
    fields.append(node("label", "时间"), time, node("label", "频率"), frequency);
    const weekdays = node("div", undefined, "weekday-row");
    weekdays.dataset.role = "weekdays";
    weekdayLabels.forEach((label, index) => {
      const wrapper = node("label", undefined, "checkbox");
      const box = node("input");
      box.type = "checkbox";
      box.dataset.day = String(index);
      box.checked = (entry.weekdays || []).includes(index);
      wrapper.append(box, node("span", label));
      weekdays.append(wrapper);
    });
    const sync = () => {
      fields.hidden = !enabled.checked;
      weekdays.hidden = !enabled.checked || frequency.value !== "weekly";
    };
    enabled.addEventListener("change", sync);
    frequency.addEventListener("change", sync);
    row.append(head, fields, weekdays);
    container.append(row);
    sync();
  }
}
function readSchedules() {
  const schedules = {};
  for (const row of document.querySelectorAll(".task-schedule")) {
    const weekdays = Array.from(row.querySelectorAll('[data-role="weekdays"] input:checked'))
      .map(box => Number(box.dataset.day));
    schedules[row.dataset.task] = {
      enabled: row.querySelector('[data-role="enabled"]').checked,
      time: row.querySelector('[data-role="time"]').value || "08:23",
      frequency: row.querySelector('[data-role="frequency"]').value,
      weekdays,
    };
  }
  return schedules;
}
function accountMatches(account, query) {
  const text = query.trim().toLowerCase();
  if (!text) return true;
  const phone = account.config.phone || "";
  const haystack = `${account.name} ${phone} ${phone.slice(0, 3)}****${phone.slice(-4)}`.toLowerCase();
  if (haystack.includes(text)) return true;
  const digits = text.replace(/\D/g, "");
  return Boolean(digits) && haystack.replace(/\*/g, "").includes(digits);
}
function visibleAccounts() {
  return state.accounts.filter(account => accountMatches(account, state.accountQuery)
    && (!state.accountStatus || (account.summary?.state || "unknown") === state.accountStatus));
}
const healthLabels = {normal: "运行正常", running: "执行中", queued: "排队中", warning: "任务异常", invalid: "凭证失效", unknown: "尚未验证"};
const taskLabels = {success: "已完成", partial: "部分成功", failed: "执行失败", interrupted: "已停止", running: "执行中", queued: "排队中", waiting: "等待执行", missed: "计划未执行", unexecuted: "未执行", unknown: "详情未记录", query: "仅查询", skipped: "已跳过"};
const taskIcons = {success: "✓", partial: "!", failed: "!", interrupted: "—", running: "↻", queued: "◷", waiting: "◷", missed: "!", unexecuted: "○", unknown: "—", query: "⌕", skipped: "—"};
function renderStatusFilters() {
  const group = $("#account-status-filters");
  for (const [key, label] of [["", "全部账号"], ...Object.entries(healthLabels)]) {
    let chip = Array.from(group.children).find(el => el.dataset.status === key);
    if (!chip) {
      chip = button("", "status-filter", () => { state.accountStatus = key; renderAccounts(); });
      chip.dataset.status = key;
      group.append(chip);
    }
    const count = state.accounts.filter(a => accountMatches(a, state.accountQuery) && (!key || (a.summary?.state || "unknown") === key)).length;
    chip.replaceChildren(node("span", label), node("span", count, "filter-count"));
    chip.setAttribute("aria-pressed", String(state.accountStatus === key));
    chip.className = `status-filter ${key} ${state.accountStatus === key ? "selected" : ""}`;
  }
}
function renderAccounts() {
  const grid = $("#account-grid");
  const focused = grid.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
  grid.replaceChildren();
  renderStatusFilters();
  const matches = visibleAccounts();
  const total = state.accounts.length;
  const searching = Boolean(state.accountQuery.trim() || state.accountStatus);
  $("#empty-accounts").hidden = total > 0;
  $("#no-match-accounts").hidden = !(searching && total > 0 && matches.length === 0);
  $("#account-search-note").textContent = searching ? `匹配 ${matches.length} / 共 ${total} 个账号` : "凭证仅保存在服务端";
  $("#account-search-clear").hidden = !state.accountQuery;
  $("#account-search").value = state.accountQuery;
  $("#stat-accounts").replaceChildren(document.createTextNode(state.accounts.length), node("small", "个"));
  $("#stat-enabled").replaceChildren(document.createTextNode(state.accounts.filter(a => a.enabled).length), node("small", "个"));
  $("#account-count").textContent = matches.length;
  for (const account of matches) {
    const card = node("article", undefined, "account-card");
    const summary = account.summary || {state: "unknown", label: "尚未验证", tasks: []};
    card.dataset.status = summary.state;
    const top = node("div", undefined, "account-top");
    const title = node("div", undefined, "account-title");
    title.append(node("h3", account.name), node("span", account.config.phone.replace(/^(\d{3})\d{4}/, "$1 **** "), "account-phone"));
    top.append(node("span", account.name.slice(0, 1), "avatar"), title, node("span", account.enabled ? "自动运行" : "手动运行", `badge ${account.enabled ? "enabled" : ""}`));
    const health = node("div", undefined, "account-health-line");
    const indicator = node("span", undefined, `account-health ${summary.state}`);
    const dot = node("span", undefined, "status-dot"); dot.setAttribute("aria-hidden", "true");
    indicator.append(dot, document.createTextNode(summary.label));
    health.append(indicator, node("span", summary.checked_at ? `最近检查 ${dateText(summary.checked_at)}` : "首次执行后更新状态", "checked-at"));
    card.append(top, health);
    if (summary.state === "invalid") card.append(node("p", summary.issue_detail || "请更新凭证后重试", "account-issue invalid"));
    if (summary.issue && ["running", "queued"].includes(summary.state)) {
      card.append(node("p", summary.issue === "invalid" ? "上次凭证失效 · 本次结果返回后更新" : "上次任务异常 · 本次结果返回后更新", "account-issue"));
    }
    const schedules = accountSchedules(account);
    const taskList = node("div", undefined, "account-task-list");
    for (const entry of summary.tasks) {
      const schedule = schedules[entry.task] || defaultSchedule;
      const row = node("div", undefined, "account-task-row");
      const line = node("div", undefined, "account-task-heading");
      let label = taskLabels[entry.state] || entry.state;
      if (entry.state === "success" && schedule.enabled) label = schedule.frequency === "daily" ? "今日已完成" : schedule.weekdays.length === 1 ? "本周已完成" : "本次已完成";
      const text = `${taskIcons[entry.state] || "○"} ${label}`;
      const badge = entry.run_id ? button(text, `task-status ${entry.state}`, () => openLog(entry.run_id)) : node("span", text, `task-status ${entry.state}`);
      badge.title = entry.run_id ? `${entry.detail} · 点击查看${entry.current || isPending({status:entry.state}) ? "本次" : "上次"}日志` : entry.detail;
      badge.setAttribute("aria-label", `${tasks[entry.task]}：${label}${entry.run_id ? "，查看日志" : ""}`);
      badge.dataset.focusKey = `${account.id}-${entry.task}`;
      line.append(node("strong", tasks[entry.task]), badge);
      row.append(line, node("p", schedule.enabled ? `${scheduleText(schedule)}${schedule.next_run ? " · 下次 " + dateText(schedule.next_run) : ""}` : "未开启计划 · 可手动执行", "task-timing"));
      if (["failed", "partial", "interrupted"].includes(entry.state)) {
        row.append(node("p", entry.detail, `task-detail ${entry.state}`));
      } else if (entry.last_at) {
        row.append(node("p", `${entry.current && entry.state === "success" ? "完成于" : "上次执行"} ${dateText(entry.last_at)}${entry.current && entry.state === "success" ? "" : " · " + (taskLabels[entry.last_state] || "详情未记录")}${entry.detail === "详情未记录" ? " · 旧记录详情未记录" : ""}`, "task-last"));
      }
      if (entry.state === "missed") row.append(node("p", "计划时间已过，未找到本次执行记录", "task-detail partial"));
      if (entry.state === "success" && entry.detail === "已完成，无需重复操作") row.append(node("p", entry.detail, "task-last"));
      if (entry.query_id) {
        const query = button(`最近仅查询 ${dateText(entry.query_at)} ↗`, "task-query", () => openLog(entry.query_id));
        query.dataset.focusKey = `${account.id}-${entry.task}-query`;
        row.append(query);
      }
      if (entry.parts.length) {
        const parts = node("div", undefined, "task-parts");
        for (const part of entry.parts) {
          const name = {claim: "累计奖励", tasks: "AI 豆", games: "拓展活动"}[part.key];
          // 需要真实操作而跳过的任务属于正常情况，用中性的「部分跳过」而不是黄色告警。
          const skipped = part.state === "success" && part.code === "skipped";
          const partState = skipped ? "skipped" : part.state;
          const label = skipped ? "部分跳过" : taskLabels[part.state] || "未执行";
          const tag = node("span", `${taskIcons[partState] || "○"} ${name} · ${label}`, `task-part ${partState}`);
          tag.title = part.detail;
          parts.append(tag);
        }
        row.append(parts);
      }
      taskList.append(row);
    }
    const actions = node("div", undefined, "card-actions");
    const runButton = button("手动执行  →", "button primary", () => openRun(account));
    if (state.pendingAccounts.has(account.id)) {
      runButton.title = "该账号已有任务在执行或排队：同一任务不能重复提交，其他任务可以继续提交并自动排队。";
    }
    const runAllButton = button("一键执行", "button secondary", () => openRunAll(account));
    runAllButton.title = "提交该账号下所有未在执行或排队的任务";
    actions.append(runButton, runAllButton, button("编辑", "button secondary", () => openAccount(account)), button("删除", "text-button delete-button", () => openDelete(account)));
    Array.from(actions.children).forEach((el, i) => { el.dataset.focusKey = `${account.id}-action-${i}`; });
    card.append(taskList, actions);
    grid.append(card);
  }
  if (focused) Array.from(grid.querySelectorAll("[data-focus-key]")).find(el => el.dataset.focusKey === focused)?.focus({preventScroll: true});
}

function renderRuns() {
  const list = $("#run-list");
  list.replaceChildren();
  $("#empty-runs").hidden = state.runs.length > 0;
  $("#run-count").textContent = state.runsTotal;
  const pendingIds = new Set(state.runs.filter(isPending).map(run => run.id));
  for (const run of state.runs) {
    const row = node("tr");
    if (state.selected.has(run.id)) row.className = "selected";
    const check = node("td", undefined, "check-cell");
    const box = node("input");
    box.type = "checkbox";
    box.checked = state.selected.has(run.id);
    box.setAttribute("aria-label", `选择 ${run.account_name} 的记录`);
    box.addEventListener("change", () => {
      if (box.checked) state.selected.add(run.id); else state.selected.delete(run.id);
      row.classList.toggle("selected", box.checked);
      syncSelection();
    });
    check.append(box);
    const first = node("td");
    first.append(node("span", run.account_name, "run-name"), node("span", `${tasks[run.task] || run.task}${run.dry_run ? " · 仅查询" : ""}`, "run-task"));
    const status = node("td");
    status.append(node("span", labels[run.status] || run.status, `badge ${run.status}`));
    const detail = node("td", undefined, "align-right");
    detail.append(button("查看日志 ↗", "text-button", () => openLog(run.id)));
    row.append(check, first, node("td", dateText(run.created_at), "run-time"),
               node("td", run.source === "schedule" ? "计划任务" : run.source === "import" ? "导入记录" : "手动执行"),
               status, detail);
    if (pendingIds.has(run.id)) row.classList.add("running-row");
    list.append(row);
  }
  state.runsLatest = state.runsLatest || state.runs[0] || null;
  const latest = state.runsLatest;
  $("#stat-latest").textContent = latest ? labels[latest.status] || latest.status : "暂无记录";
  $("#stat-latest-note").textContent = latest ? `${latest.account_name} · ${dateText(latest.created_at)}` : "准备好后，开始第一次签到";
  syncSelection();
}
function syncSelection() {
  const visible = state.runs.map(run => run.id);
  const chosen = visible.filter(id => state.selected.has(id));
  $("#selection-note").textContent = state.selected.size
    ? `已选 ${state.selected.size} 条（本页 ${chosen.length} 条）`
    : "勾选记录后可批量删除";
  $("#delete-selected").disabled = state.selected.size === 0;
  const selectAll = $("#select-page");
  selectAll.checked = visible.length > 0 && chosen.length === visible.length;
  selectAll.indeterminate = chosen.length > 0 && chosen.length < visible.length;
}
function renderPager() {
  const pages = Math.max(1, state.runsPages);
  state.runsPage = Math.min(Math.max(1, state.runsPage), pages);
  $("#page-info").textContent = `第 ${state.runsPage} / ${pages} 页 · 共 ${state.runsTotal} 条`;
  $("#page-prev").disabled = state.runsPage <= 1;
  $("#page-next").disabled = state.runsPage >= pages;
}
function renderRunSummary(payload) {
  const counts = state.runsCounts || {};
  const parts = [`共 ${state.runsTotal} 条`];
  for (const [status, label] of [["success", "完成"], ["partial", "部分成功"], ["failed", "失败"], ["interrupted", "已停止"], ["running", "运行中"], ["queued", "排队中"]]) {
    if (counts[status]) parts.push(`${label} ${counts[status]}`);
  }
  if (payload && payload.keyword) {
    parts.push(payload.truncated ? `关键字仅搜索最近 ${payload.scanned} 条` : `关键字命中 ${state.runsTotal} 条（已搜索最近 ${payload.scanned} 条）`);
  }
  parts.push(`更新于 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
  $("#history-summary").textContent = parts.join(" · ");
}
function currentFilters() {
  return {
    account_id: $("#filter-account").value,
    task: $("#filter-task").value,
    status: $("#filter-status").value,
    source: $("#filter-source").value,
    date_from: $("#filter-from").value,
    date_to: $("#filter-to").value,
    keyword: $("#filter-keyword").value.trim(),
  };
}
function runQuery() {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(currentFilters())) if (value) query.set(key, value);
  query.set("page", state.runsPage);
  query.set("page_size", state.runsPageSize);
  return query;
}
async function loadRuns(silent = false) {
  if (state.runsLoading) return;
  state.runsLoading = true;
  try {
    const query = runQuery();
    const payload = await api(`/runs?${query.toString()}`);
    state.runs = payload.runs;
    state.runsTotal = payload.total;
    state.runsPages = payload.pages;
    state.runsPage = payload.page;
    state.runsPageSize = payload.page_size;
    state.runsLatest = payload.latest;
    state.runsCounts = payload.counts || {};
    const visible = new Set(state.runs.map(run => run.id));
    for (const id of Array.from(state.selected)) if (!visible.has(id)) state.selected.delete(id);
    state.runsLoaded = true;
    renderRuns();
    renderPager();
    renderRunSummary(payload);
  } catch (error) {
    $("#history-summary").textContent = "记录加载失败";
    if (!silent) toast(error.message, true);
  } finally {
    state.runsLoading = false;
  }
}
let refreshing = false;
async function refresh(silent = false) {
  if (!state.authenticated || refreshing) return;
  refreshing = true;
  try {
    const accounts = await api("/accounts");
    if (!state.authenticated) return;
    state.accounts = accounts.accounts;
    state.pendingTasks = new Set((accounts.pending_tasks || []).map(item => `${item.account_id}:${item.task}`));
    state.pendingAccounts = new Set((accounts.pending_tasks || []).map(item => item.account_id));
    $("#health").textContent = accounts.preview ? "审核预览" : accounts.healthy ? "服务运行中" : "调度器离线";
    $("#health").className = accounts.preview || accounts.healthy ? "health" : "health offline";
    syncAccountFilter();
    renderAccounts();
  } catch (error) {
    $("#health").textContent = "连接异常";
    $("#health").className = "health offline";
    if (!silent) toast(error.message, true);
  } finally {
    refreshing = false;
  }
  // 账号总览也要显示「最近执行」，首次加载取一次记录；之后只在记录页刷新列表。
  if (state.view === "history" || !state.runsLoaded) await loadRuns(silent);
  if ($("#log-dialog").open && state.logId) await updateLog();
}
function syncAccountFilter(force = false) {
  const select = $("#filter-account");
  const signature = state.accounts.map(account => `${account.id}:${account.name}`).join("|");
  if (!force && select.dataset.signature === signature) return;
  const current = select.value;
  const options = [node("option", "全部账号")];
  options[0].value = "";
  for (const account of state.accounts) {
    const label = `${account.name}（${account.config.phone.replace(/^(\d{3})\d{4}/, "$1****")}）`;
    const option = node("option", label);
    option.value = account.id;
    options.push(option);
  }
  select.replaceChildren(...options);
  select.dataset.signature = signature;
  select.value = state.accounts.some(account => account.id === current) ? current : "";
}
function openAccount(account = null) {
  state.editing = account;
  $("#notify-test-result").replaceChildren();
  $("#notify-test-row").hidden = !account;
  const form = $("#account-form");
  form.reset();
  $("#account-error").textContent = "";
  $("#account-dialog-title").textContent = account ? "编辑账号" : "添加账号";
  $("#token-hint").textContent = account ? "App Token 已保存。留空保留原值，填写新值即可更新。" : "从移动 App「签到领流量」页面抓包获取，见部署说明。";
  form.elements.phone.readOnly = Boolean(account);
  form.elements.app_token.required = !account;
  $("#clear-secret-options").hidden = !account;
  if (account) {
    form.elements.name.value = account.name;
    for (const field of configFields) if (!secretFields.includes(field)) form.elements[field].value = account.config[field] || "";
    for (const field of secretFields) form.elements[field].placeholder = account[`has_${field}`] ? "已保存；留空保留，填写则更新" : "未设置";
    for (const [key, value] of Object.entries(account.options)) if (form.elements[key]) form.elements[key].checked = value;
  } else {
    form.elements.app_token.placeholder = "粘贴 appTokenLogin 请求体中的 token";
    for (const field of secretFields.filter(f => f !== "app_token")) form.elements[field].placeholder = "选填";
  }
  buildScheduleRows(account);
  $("#account-dialog").showModal();
}
function accountRunTasks(account) {
  return runnableTaskKeys(account);
}
function batchToast(result) {
  const queued = (result.queued || []).length;
  const skipped = (result.skipped || []).length;
  if (!queued) return skipped ? `没有可提交的任务，跳过 ${skipped} 个` : "没有可提交的任务";
  return `已提交 ${queued} 个任务${skipped ? `，跳过 ${skipped} 个` : ""}，可在执行记录中查看`;
}
async function loadActivities(silent = false) {
  try {
    const payload = await api("/activities");
    if (!state.authenticated) return;
    state.activities = payload.activities;
    state.accounts = payload.accounts;
    renderActivities();
  } catch (error) {
    if (!silent) toast(error.message, true);
  }
}
function renderActivities() {
  const grid = $("#activity-grid");
  grid.replaceChildren();
  $("#empty-activities").hidden = state.activities.length > 0;
  $("#activity-count").textContent = state.activities.length;
  for (const activity of state.activities) {
    const card = node("article", undefined, "account-card");
    const top = node("div", undefined, "account-top");
    const title = node("div", undefined, "account-title");
    title.append(node("h3", activity.name), node("span", `假期秒杀 · ${activity.account_count} 个账号`, "account-phone"));
    top.append(node("span", "◈", "avatar activity-avatar"), title,
               node("span", activity.enabled ? "已启用" : "已停用", `badge ${activity.enabled ? "enabled" : ""}`));
    const config = node("div", undefined, "schedule-info");
    const days = (activity.weekdays || []).map(day => weekdayFullLabels[day]).join("、") || "每天";
    config.append(node("span", "开抢时间"), node("strong",
      `${activity.time}（提前 ${activity.config.lead} 秒 · 每 ${activity.config.interval} 秒一发，最多 ${activity.config.max_attempts} 发）`));
    const days_line = node("div", undefined, "schedule-info");
    days_line.append(node("span", "活动日"), node("strong", days));
    const counts = (activity.results || {});
    const totals = Object.values(counts);
    const okCount = totals.filter(item => item && item.success).length;
    const failedCount = totals.filter(item => item && !item.success && !item.pending).length;
    const pendingCount = totals.filter(item => item && item.pending).length;
    const summary = node("p", `${activity.account_count} 个账号 · 成功 ${okCount} · 失败 ${failedCount}`
      + (pendingCount ? ` · 进行中 ${pendingCount}` : ""), "next-time");
    const next = node("p", activity.enabled ? `下次执行 ${dateText(activity.next_run)}` : "已停用，可手动执行", "next-time");
    const actions = node("div", undefined, "card-actions");
    actions.append(button("立即执行  →", "button primary", () => runActivity(activity)),
                   button("账号结果", "button secondary", () => openActivityResult(activity)),
                   button("编辑", "button secondary", () => openActivity(activity)),
                   button("删除", "text-button delete-button", () => openDeleteActivity(activity)));
    card.append(top, config, days_line, summary, next, actions);
    grid.append(card);
  }
}
function resultTone(item) {
  if (!item) return "none";
  if (item.pending) return item.status === "queued" ? "queued" : "running";
  return item.success ? "success" : "failed";
}
function resultLabel(item) {
  if (!item) return "未执行";
  if (item.pending) return item.status === "queued" ? "排队中" : "正在抢";
  if (item.success) return "秒杀成功";
  return item.message || "失败";
}
function openActivityResult(activity) {
  $("#activity-result-title").textContent = activity.name;
  const container = $("#activity-result-list");
  const byId = Object.fromEntries(state.accounts.map(item => [item.id, item]));
  const rows = [];
  for (const [account_id, item] of Object.entries(activity.results || {})) {
    const account = byId[account_id];
    rows.push({ account, item, tone: resultTone(item), label: resultLabel(item) });
  }
  for (const account of state.accounts.filter(item => activity.accounts.includes(item.id))) {
    if (!rows.some(row => row.account && row.account.id === account.id)) {
      rows.push({ account, item: null, tone: "none", label: "未执行" });
    }
  }
  const order = { success: 0, failed: 1, running: 2, queued: 3, none: 4 };
  rows.sort((a, b) => order[a.tone] - order[b.tone] || (a.account?.name || "").localeCompare(b.account?.name || ""));
  const success = rows.filter(row => row.tone === "success");
  const failed = rows.filter(row => row.tone === "failed");
  const summary = $("#activity-result-summary");
  summary.replaceChildren();
  const rate = rows.length ? `${success.length}/${rows.length}` : "—";
  summary.append(node("span", `成功率 ${rate}`, "success-rate"),
                 node("span", `成功 ${success.length}`, "pill pill-success"),
                 node("span", `失败 ${failed.length}`, "pill pill-failed"),
                 button("复制结果", "task-query copy-results", () => copyActivityResult(rows)));
  container.replaceChildren();
  for (const row of rows) {
    // 整行即按钮：账号名着色区分状态，点击打开日志。
    const line = button("", `account-result ${row.tone}`, () => {
      if (row.item && row.item.run_id) openLog(row.item.run_id);
    });
    line.setAttribute("aria-label", `${row.account?.name || "未知账号"}：${row.label}${row.item?.run_id ? "，查看日志" : ""}`);
    if (row.item && row.item.run_id) line.title = `${row.label} · 点击查看日志`;
    line.append(node("strong", row.account ? row.account.name : "未知账号"));
    container.append(line);
  }
  $("#activity-result-dialog").showModal();
}
async function copyActivityResult(rows) {
  const names = tone => rows.filter(row => row.tone === tone).map(row => row.account?.name || "未知账号");
  const lines = [`成功（${names("success").length}）`, ...names("success"),
                 `失败（${names("failed").length}）`, ...names("failed")];
  const text = lines.join("\n");
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制成功与失败的账号名单");
  } catch (error) {
    const area = document.createElement("textarea");
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
    toast("已复制成功与失败的账号名单");
  }
}
function readActivityForm(form) {
  return {
    name: form.elements.name.value,
    time: form.elements.time.value,
    lead: Number(form.elements.lead.value),
    interval: Number(form.elements.interval.value),
    max_attempts: Number(form.elements.max_attempts.value),
    weekdays: Array.from(document.querySelectorAll('#activity-weekdays input:checked')).map(box => Number(box.dataset.day)),
    accounts: [],
    enabled: form.elements.enabled.checked,
  };
}
function openActivity(activity) {
  state.editingActivity = activity || null;
  const form = $("#activity-form");
  form.reset();
  $("#activity-error").textContent = "";
  $("#activity-dialog-title").textContent = activity ? "编辑活动" : "添加活动";
  if (activity) {
    form.elements.name.value = activity.name;
    form.elements.time.value = activity.time;
    form.elements.lead.value = activity.config.lead;
    form.elements.interval.value = activity.config.interval;
    form.elements.max_attempts.value = activity.config.max_attempts;
    form.elements.enabled.checked = activity.enabled;
  }
  const weekdays = $("#activity-weekdays");
  weekdays.replaceChildren();
  const selected = activity ? (activity.weekdays || [0,1,2,3,4,5,6]) : [0,1,2,3,4,5,6];
  for (let day = 0; day < 7; day += 1) {
    const wrap = node("label", undefined, "checkbox");
    const box = node("input");
    box.type = "checkbox";
    box.dataset.day = day;
    box.checked = selected.includes(day);
    wrap.append(box, document.createTextNode(weekdayFullLabels[day]));
    weekdays.append(wrap);
  }
  $("#activity-dialog").showModal();
}
async function runActivity(activity) {
  if (!await askConfirm({
    title: "立即执行活动",
    message: `执行活动「${activity.name}」？${activity.account_count} 个账号将依次排队秒杀。`,
    confirmText: "开始执行",
  })) return;
  try {
    const result = await api(`/activities/${activity.id}/run`, "POST", {});
    toast(batchToast(result));
    await refresh();
    await loadActivities(true);
  } catch (error) { toast(error.message, true); }
}
function openDeleteActivity(activity) {
  state.deletingActivity = activity;
  $("#delete-activity-name").textContent = `确定删除「${activity.name}」？`;
  $("#delete-activity-error").textContent = "";
  $("#activity-delete-dialog").showModal();
}
$("#add-activity").addEventListener("click", () => openActivity());
$("#empty-add-activity").addEventListener("click", () => openActivity());
$("#activity-form").addEventListener("submit", event => {
  event.preventDefault();
  const form = event.currentTarget;
  submit(form, "#activity-error", async () => {
    const payload = readActivityForm(form);
    if (state.editingActivity) await api(`/activities/${state.editingActivity.id}`, "PUT", payload);
    else await api("/activities", "POST", payload);
    $("#activity-dialog").close();
    form.reset();
    toast("活动已保存");
    await loadActivities(true);
    await refresh();
  });
});
$("#confirm-delete-activity").addEventListener("click", async () => {
  const control = $("#confirm-delete-activity");
  control.disabled = true;
  try {
    await api(`/activities/${state.deletingActivity.id}`, "DELETE");
    $("#activity-delete-dialog").close();
    toast("活动已删除");
    await loadActivities(true);
  } catch (error) { $("#delete-activity-error").textContent = error.message; }
  finally { control.disabled = false; }
});
function taskMark(account, task) {
  const entry = runTaskEntry(account, task);
  if (task === "seckill") {
    // 秒杀没有账号级计划：这里按最近一次结果判断，与后端 select_tasks 的 pending 语义保持一致。
    if (state.pendingTasks.has(`${account.id}:${task}`)) {
      return { skip: true, tone: "muted", label: entry.state === "queued" ? "排队中，跳过" : "执行中，跳过" };
    }
    if (entry.state === "success") {
      return { skip: false, tone: "repeat", done: true, label: "重复执行（最近一次已成功）" };
    }
    return { skip: false, tone: "new", label: entry.run_id ? "按最近场次自动等待并抢券" : "尚未执行过" };
  }
  if (task === "fee" && !account.has_fee_session_cookie) {
    return { skip: true, tone: "muted", label: "未配置话费会话，跳过" };
  }
  if (state.pendingTasks.has(`${account.id}:${task}`)) {
    return { skip: true, tone: "muted", label: entry.state === "queued" ? "排队中，跳过" : "执行中，跳过" };
  }
  const last = entry.last_state || null;
  if (entry.state === "success") {
    return { skip: false, tone: "repeat", done: true, label: "重复执行（最近一次已成功）" };
  }
  if (last === "failed") return { skip: false, tone: "problem", label: "上次执行失败" };
  if (last === "partial") return { skip: false, tone: "problem", label: "上次部分成功" };
  if (last === "interrupted") return { skip: false, tone: "problem", label: "上次被停止" };
  if (entry.state === "missed") return { skip: false, tone: "problem", label: "本次计划未执行" };
  if (last) return { skip: false, tone: "new", label: "上次已执行，本周期未完成" };
  return { skip: false, tone: "new", label: entry.query_id ? "只查询过一次，尚未真正执行" : "尚未执行过" };
}
function planFor(account, mode) {
  const schedules = accountSchedules(account);
  const rows = [];
  for (const task of taskKeys) {
    // 秒杀没有账号级计划，勾选「只执行已开启计划」时与后端一样跳过它。
    if (mode === "enabled" && !schedules[task].enabled) continue;
    const mark = taskMark(account, task);
    const repeat = Boolean(mark.done) && mode === "all";
    const run = !mark.skip && (mode === "all" || !mark.done);
    rows.push({ task, run, tone: run ? mark.tone : "skipped",
                label: run ? `将执行 · ${mark.label}` : mark.skip ? mark.label : "已执行成功，本次跳过" });
  }
  return rows;
}
function planCounts(rows) {
  const running = rows.filter(row => row.run);
  return { run: running.length, repeat: running.filter(row => row.tone === "repeat").length,
           skip: rows.length - running.length };
}
function renderPlan(container, rows) {
  for (const row of rows) {
    const item = node("div", undefined, `run-all-item ${row.run ? row.tone : "skipped"}`);
    item.append(node("span", row.run ? "✓" : "—", "run-all-mark"), document.createTextNode(tasks[row.task]));
    item.append(node("span", row.label, "run-all-note"));
    container.append(item);
  }
}
function planHint(counts, tail) {
  if (!counts.run) return `没有需要提交的任务${counts.skip ? `（${counts.skip} 个已跳过）` : ""}。`;
  const repeat = counts.repeat ? `，其中 ${counts.repeat} 个是重复执行` : "";
  const skip = counts.skip ? `，另有 ${counts.skip} 个会跳过` : "";
  return `将提交 ${counts.run} 个任务${repeat}${skip}${tail}。提交后按发起顺序串行执行。`;
}
function runAllMode() {
  return $("#run-all-form").elements.mode.value;
}
function renderRunAllTasks(account) {
  const container = $("#run-all-tasks");
  const rows = planFor(account, runAllMode());
  container.replaceChildren();
  renderPlan(container, rows);
  const counts = planCounts(rows);
  $("#run-all-hint").textContent = planHint(counts, counts.repeat ? "，重复执行会重新签到或再次消耗活动次数" : "");
  $("#run-all-submit").disabled = counts.run === 0;
}
function openRunAll(account) {
  state.runAllAccount = account;
  $("#run-all-form").reset();
  $("#run-all-error").textContent = "";
  $("#run-all-account").textContent = `执行账号：${account.name}`;
  renderRunAllTasks(account);
  $("#run-all-dialog").showModal();
}
function renderBatchPlan(mode) {
  const container = $("#batch-tasks");
  container.replaceChildren();
  const total = { run: 0, repeat: 0, skip: 0 };
  for (const account of state.accounts) {
    const rows = planFor(account, mode);
    const counts = planCounts(rows);
    if (!counts.run) continue;
    total.run += counts.run;
    total.repeat += counts.repeat;
    total.skip += counts.skip;
    container.append(node("p", account.name, "batch-account"));
    renderPlan(container, rows);
  }
  if (!total.run) container.append(node("p", "当前没有需要提交的任务。", "batch-empty"));
  return total;
}
function syncBatchDialog() {
  const mode = $("#batch-form").elements.mode.value;
  const counts = renderBatchPlan(mode);
  const tail = mode === "enabled" ? "，范围为各账号已开启计划的任务"
    : mode === "all" ? "，包含最近已执行成功的任务" : "，已执行成功的任务本次不再执行";
  $("#batch-hint").textContent = planHint(counts, tail);
  $("#batch-submit").disabled = counts.run === 0;
}
function runnableTaskKeys(account) {
  return taskKeys.filter(task => task !== "fee" || account.has_fee_session_cookie);
}
function runTaskEntry(account, task) {
  return ((account.summary && account.summary.tasks) || []).find(entry => entry.task === task) || {};
}
function buildRunTasks(account) {
  const select = $("#run-task");
  select.replaceChildren();
  for (const task of taskKeys) {
    const busy = state.pendingTasks.has(`${account.id}:${task}`);
    const label = runTaskLabels[task] || tasks[task];
    const option = node("option", busy ? `${label}（已在队列中，不能重复提交）` : label);
    option.value = task;
    option.disabled = busy;
    select.append(option);
  }
  const available = Array.from(select.options).find(option => !option.disabled);
  if (available) select.value = available.value;
}
function syncRunDialog() {
  const account = state.runAccount;
  if (!account) return;
  const task = $("#run-task").value;
  const entry = runTaskEntry(account, task);
  const busy = state.pendingTasks.has(`${account.id}:${task}`);
  const done = !busy && task !== "fee" && entry.state === "success";
  const hint = $("#run-hint");
  if (busy) {
    hint.textContent = `${tasks[task]}正在${entry.state === "running" ? "执行" : "排队"}，同一任务不能重复提交；可以改选其他任务，提交后会自动排队。`;
  } else if (done) {
    hint.textContent = `${tasks[task]}最近一次已执行成功${entry.last_at ? `（${dateText(entry.last_at)}）` : ""}。是否再次执行？已默认勾选「仅查询状态」，不会重复签到或消耗活动次数。`;
  } else {
    hint.textContent = "";
  }
  hint.hidden = !hint.textContent;
  $("#run-dry-run").checked = done;
  $("#run-submit").disabled = busy;
}
function openRun(account) {
  state.runAccount = account;
  $("#run-form").reset();
  $("#run-error").textContent = "";
  $("#run-account-name").textContent = `执行账号：${account.name}`;
  buildRunTasks(account);
  syncRunDialog();
  $("#run-dialog").showModal();
}
async function openLog(runId) {
  state.logId = runId;
  $("#log-content").textContent = "正在读取日志…";
  $("#log-title").textContent = "执行日志";
  $("#log-status").textContent = "";
  $("#cancel-run").hidden = true;
  $("#log-dialog").showModal();
  try { await updateLog(); } catch (error) { $("#log-content").textContent = error.message; }
}
async function updateLog() {
  const selectedId = state.logId;
  const result = await api(`/runs/${selectedId}/log`);
  if (state.logId !== selectedId) return;
  $("#log-title").textContent = `${result.run.account_name} · ${tasks[result.run.task]}`;
  $("#log-status").textContent = `${labels[result.run.status]}${result.run.message ? " · " + result.run.message : ""}`;
  $("#log-content").textContent = result.log || "任务已进入队列，执行后会在这里显示日志。";
  $("#cancel-run").hidden = !isPending(result.run);
}
function openDelete(account) {
  state.deleting = account;
  $("#delete-name").textContent = `确定删除「${account.name}」？`;
  $("#delete-error").textContent = "";
  $("#delete-dialog").showModal();
}
async function submit(form, errorId, action) {
  const control = form.querySelector("[type=submit]");
  control.disabled = true;
  $(errorId).textContent = "";
  try { await action(); } catch (error) { $(errorId).textContent = error.message; }
  finally { control.disabled = false; }
}
$("#login-form").addEventListener("submit", event => {
  event.preventDefault();
  submit(event.currentTarget, "#login-error", async () => {
    const session = await api("/login", "POST", { password: $("#password").value });
    $("#password").value = "";
    showApp(session);
    await refresh();
  });
});
$("#account-form").addEventListener("submit", event => {
  event.preventDefault();
  const form = event.currentTarget;
  submit(form, "#account-error", async () => {
    const config = Object.fromEntries(configFields.map(field => [field, form.elements[field].value]));
    const options = Object.fromEntries(["claim", "tasks", "games"].map(field => [field, form.elements[field].checked]));
    const clear = ["serverchan_sendkey", "bark_url", "webhook_url", "webhook_token", "fee_session_cookie"].filter(field => form.elements[`clear_${field}`].checked);
    await api(state.editing ? `/accounts/${state.editing.id}` : "/accounts", state.editing ? "PUT" : "POST", {
      name: form.elements.name.value, config, options, schedules: readSchedules(), clear_secrets: clear,
    });
    $("#account-dialog").close();
    const added = !state.editing;
    form.reset();
    toast("账号已保存");
    if (added && state.accountQuery) setAccountQuery("");
    await refresh();
  });
});
$("#run-task").addEventListener("change", syncRunDialog);
$("#run-all-form").addEventListener("change", () => renderRunAllTasks(state.runAllAccount));
$("#run-all-form").addEventListener("submit", event => {
  event.preventDefault();
  const form = event.currentTarget;
  submit(form, "#run-all-error", async () => {
    const result = await api(`/accounts/${state.runAllAccount.id}/run-all`, "POST", { mode: runAllMode() });
    $("#run-all-dialog").close();
    toast(batchToast(result));
    await refresh();
  });
});
$("#batch-run").addEventListener("click", () => {
  $("#batch-form").reset();
  $("#batch-error").textContent = "";
  syncBatchDialog();
  $("#batch-dialog").showModal();
});
$("#batch-form").addEventListener("change", syncBatchDialog);
$("#batch-form").addEventListener("submit", event => {
  event.preventDefault();
  const form = event.currentTarget;
  submit(form, "#batch-error", async () => {
    const result = await api("/run-batch", "POST", { mode: form.elements.mode.value });
    $("#batch-dialog").close();
    toast(batchToast(result));
    await refresh();
  });
});
async function runTask(task, dryRun) {
  const result = await api(`/accounts/${state.runAccount.id}/run`, "POST", { task, dry_run: dryRun });
  $("#run-dialog").close();
  toast("任务已加入队列");
  showView("history");
  await refresh();
  await openLog(result.run_id);
}
function askConfirm({ title = "请确认", message = "", confirmText = "确定", danger = false, eyebrow = "PLEASE CONFIRM" }) {
  return new Promise(resolve => {
    const dialog = $("#confirm-dialog");
    const form = $("#confirm-form");
    const submitButton = $("#confirm-submit");
    $("#confirm-eyebrow").textContent = eyebrow;
    $("#confirm-title").textContent = title;
    $("#confirm-message").textContent = message;
    submitButton.textContent = confirmText;
    submitButton.className = danger ? "button primary danger-button" : "button primary";
    $("#confirm-error").textContent = "";
    const finish = answer => {
      form.removeEventListener("submit", onSubmit);
      dialog.removeEventListener("close", onDismiss);
      if (dialog.open) dialog.close();
      resolve(answer);
    };
    const onSubmit = event => { event.preventDefault(); finish(true); };
    const onDismiss = () => finish(false);
    form.addEventListener("submit", onSubmit);
    dialog.addEventListener("close", onDismiss);
    dialog.showModal();
  });
}
function openRerun(task) {
  const entry = runTaskEntry(state.runAccount, task);
  state.rerunTask = task;
  $("#rerun-message").textContent = `「${tasks[task]}」最近一次已执行成功${entry.last_at ? `（${dateText(entry.last_at)}）` : ""}。`;
  $("#rerun-error").textContent = "";
  $("#rerun-dialog").showModal();
}
$("#run-form").addEventListener("submit", event => {
  event.preventDefault();
  const form = event.currentTarget;
  const task = form.elements.task.value;
  const dryRun = form.elements.dry_run.checked;
  if (!dryRun && task !== "fee" && runTaskEntry(state.runAccount, task).state === "success") {
    openRerun(task);
    return;
  }
  submit(form, "#run-error", () => runTask(task, dryRun));
});
$("#rerun-form").addEventListener("submit", event => {
  event.preventDefault();
  const form = event.currentTarget;
  submit(form, "#rerun-error", async () => {
    const task = state.rerunTask;
    const dryRun = $("#run-dry-run").checked;
    $("#rerun-dialog").close();
    await runTask(task, dryRun);
  });
});
$("#confirm-delete").addEventListener("click", async () => {
  const control = $("#confirm-delete");
  control.disabled = true;
  try {
    await api(`/accounts/${state.deleting.id}`, "DELETE");
    $("#delete-dialog").close();
    toast("账号已删除");
    await refresh();
  } catch (error) { $("#delete-error").textContent = error.message; }
  finally { control.disabled = false; }
});
$("#cancel-run").addEventListener("click", async () => {
  try { await api(`/runs/${state.logId}/cancel`, "POST", {}); toast("已请求停止任务"); await refresh(); }
  catch (error) { toast(error.message, true); }
});
function openPassword() {
  const form = $("#password-form");
  form.reset();
  $("#password-error").textContent = "";
  $("#password-dialog").showModal();
}
$("#change-password").addEventListener("click", openPassword);
$("#warning-change-password").addEventListener("click", openPassword);
$("#password-form").addEventListener("submit", event => {
  event.preventDefault();
  const form = event.currentTarget;
  submit(form, "#password-error", async () => {
    const current = form.elements.current_password.value;
    const next = form.elements.new_password.value;
    if (next !== form.elements.confirm_password.value) throw new Error("两次输入的新密码不一致");
    const result = await api("/password", "POST", { current_password: current, new_password: next });
    state.csrf = result.csrf;
    $("#password-dialog").close();
    form.reset();
    $("#password-warning").hidden = !result.weak_password;
    toast("管理密码已修改");
  });
});
function backupReport(items) {
  $("#backup-result").replaceChildren(...items.map(([text, kind]) => node("span", text, kind)));
}
function backupBusy(control, busy, label) {
  control.disabled = busy;
  control.textContent = busy ? label : control.dataset.label;
}
function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = node("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
function resetBackupForm() {
  ["#export-password", "#export-passphrase", "#export-passphrase-confirm", "#import-passphrase"].forEach(selector => { $(selector).value = ""; });
  $("#import-file").value = "";
  $("#import-mode").value = "merge";
  $("#import-password").checked = true;
  $("#import-history").checked = true;
  $("#export-include-password").checked = true;
  backupReport([["导出文件包含全部账号凭证，请只保存在可信位置。", "pending"]]);
}
async function exportBackup() {
  const control = $("#export-backup");
  const password = $("#export-password").value;
  const passphrase = $("#export-passphrase").value;
  if (!password) return backupReport([["请输入当前管理密码。", "fail"]]);
  if (passphrase !== $("#export-passphrase-confirm").value) return backupReport([["两次输入的备份密码不一致。", "fail"]]);
  if (passphrase.length < 8) return backupReport([["备份密码至少 8 位。", "fail"]]);
  control.dataset.label = control.dataset.label || control.textContent;
  backupBusy(control, true, "正在生成备份…");
  backupReport([["正在加密导出，请稍候…", "pending"]]);
  try {
    const response = await fetch("/api/backup/export", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": state.csrf },
      body: JSON.stringify({
        password, passphrase,
        include_password: $("#export-include-password").checked,
        include_history: true, include_cache: true,
      }),
    });
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      if (response.status === 401) showLogin();
      throw new Error(result.error || "导出失败，请稍后重试");
    }
    const blob = await response.blob();
    const match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
    downloadBlob(blob, match ? match[1] : "cmcc-backup.json");
    $("#export-password").value = "";
    backupReport([["备份已下载，请连同备份密码一起保存；忘记密码将无法导入。", "ok"]]);
    toast("加密备份已下载");
  } catch (error) {
    backupReport([[error.message, "fail"]]);
  } finally {
    backupBusy(control, false, "");
  }
}
async function importBackup() {
  const control = $("#import-backup");
  const file = $("#import-file").files[0];
  const passphrase = $("#import-passphrase").value;
  const mode = $("#import-mode").value;
  if (!file) return backupReport([["请先选择备份文件。", "fail"]]);
  if (!passphrase) return backupReport([["请输入备份密码。", "fail"]]);
  if (mode === "replace" && !await askConfirm({
    title: "替换导入备份",
    message: "替换导入会删除当前全部账号和执行记录，确定继续吗？",
    confirmText: "替换导入",
    danger: true,
  })) return;
  control.dataset.label = control.dataset.label || control.textContent;
  backupBusy(control, true, "正在导入…");
  backupReport([["正在校验并导入备份…", "pending"]]);
  try {
    const form = new FormData();
    form.append("file", file);
    form.append("passphrase", passphrase);
    form.append("mode", mode);
    form.append("restore_password", $("#import-password").checked ? "true" : "false");
    form.append("import_history", $("#import-history").checked ? "true" : "false");
    const response = await fetch("/api/backup/import", {
      method: "POST", credentials: "same-origin", headers: { "X-CSRF-Token": state.csrf }, body: form,
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401) showLogin();
      throw new Error(result.error || "导入失败，请检查备份密码");
    }
    if (result.csrf) state.csrf = result.csrf;
    $("#password-warning").hidden = !result.weak_password;
    const lines = [
      [`导入完成：新增 ${result.added} 个账号` + (result.skipped_total ? `，跳过 ${result.skipped_total} 个已存在账号` : "") + "。", "ok"],
      [`执行记录 ${result.runs} 条、日志 ${result.logs} 个、JWT 缓存 ${result.cache} 个。`, "ok"],
      [result.password_restored ? "管理密码已按备份恢复，请使用备份所属实例的密码登录。" : "保留了当前实例的管理密码。", "ok"],
    ];
    if (result.skipped && result.skipped.length) lines.push([`已跳过：${result.skipped.join("、")}`, "pending"]);
    backupReport(lines);
    $("#import-passphrase").value = "";
    $("#import-file").value = "";
    toast("备份导入完成");
    await refresh();
  } catch (error) {
    backupReport([[error.message, "fail"]]);
  } finally {
    backupBusy(control, false, "");
  }
}
$("#backup").addEventListener("click", () => { resetBackupForm(); $("#backup-dialog").showModal(); });
$("#export-backup").addEventListener("click", exportBackup);
$("#import-backup").addEventListener("click", importBackup);
$("#backup-dialog").addEventListener("close", () => { $("#import-passphrase").value = ""; $("#import-file").value = ""; });
$("#logout").addEventListener("click", async () => {
  try { await api("/logout", "POST", {}); showLogin(); } catch (error) { toast(error.message, true); }
});
$("#test-notify").addEventListener("click", async () => {
  const form = $("#account-form");
  const result = $("#notify-test-result");
  const control = $("#test-notify");
  control.disabled = true;
  result.replaceChildren(node("span", "正在发送测试通知…", "pending"));
  try {
    const payload = Object.fromEntries(["serverchan_sendkey", "bark_url", "webhook_url", "webhook_token"]
      .map(field => [field, form.elements[field].value]));
    const response = await api(`/accounts/${state.editing.id}/notify-test`, "POST", payload);
    result.replaceChildren(...response.results.map(item => node(
      "span", `${item.channel}：${item.ok ? "发送成功" : "发送失败"} · ${item.message}`, item.ok ? "ok" : "fail")));
  } catch (error) {
    result.replaceChildren(node("span", error.message, "fail"));
  } finally {
    control.disabled = false;
  }
});
function debounce(action, wait) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => action(...args), wait);
  };
}
function applyFilters() {
  state.runsPage = 1;
  state.selected.clear();
  return loadRuns();
}
async function deleteRuns(ids, message) {
  const targets = (ids || []).filter(Boolean);
  if (!targets.length) return;
  if (!await askConfirm({ title: "删除执行记录", message, confirmText: "删除", danger: true })) return;
  try {
    const result = await api("/runs/delete", "POST", { ids: targets });
    if (state.logId && targets.includes(state.logId)) $("#log-dialog").close();
    for (const id of targets) state.selected.delete(id);
    const parts = [`已删除 ${result.deleted} 条记录`];
    if (result.skipped) parts.push(`${result.skipped} 条正在执行，已保留`);
    toast(parts.join("，"));
    await loadRuns();
  } catch (error) {
    toast(error.message, true);
  }
}
async function deleteFilteredRuns() {
  if (!state.runsTotal) { toast("当前筛选没有可删除的记录", true); return; }
  if (!await askConfirm({
    title: "删除筛选结果",
    message: `删除当前筛选条件下的 ${state.runsTotal} 条记录？排队或运行中的记录会保留。`,
    confirmText: "删除",
    danger: true,
  })) return;
  try {
    const result = await api("/runs/delete", "POST", { scope: "filtered", filters: currentFilters() });
    state.selected.clear();
    toast(result.deleted ? `已删除 ${result.deleted} 条记录` : "没有可删除的记录");
    await loadRuns();
  } catch (error) {
    toast(error.message, true);
  }
}
for (const selector of ["#filter-account", "#filter-task", "#filter-status", "#filter-source",
                        "#filter-from", "#filter-to"]) {
  $(selector).addEventListener("change", applyFilters);
}
$("#page-size").addEventListener("change", () => {
  state.runsPageSize = Number($("#page-size").value) || 20;
  applyFilters();
});
$("#filter-keyword").addEventListener("input", debounce(applyFilters, 350));
$("#filter-keyword").addEventListener("keydown", event => {
  if (event.key === "Enter") { event.preventDefault(); applyFilters(); }
});
$("#filter-reset").addEventListener("click", () => {
  for (const selector of ["#filter-account", "#filter-task", "#filter-status", "#filter-source",
                          "#filter-from", "#filter-to", "#filter-keyword"]) {
    $(selector).value = "";
  }
  state.selected.clear();
  toast("已重置筛选条件");
  applyFilters();
});
$("#select-page").addEventListener("change", event => {
  for (const run of state.runs) {
    if (event.currentTarget.checked) state.selected.add(run.id); else state.selected.delete(run.id);
  }
  renderRuns();
});
$("#delete-selected").addEventListener("click", () => deleteRuns([...state.selected],
  `删除所选 ${state.selected.size} 条记录？日志文件会一并删除。`));
$("#delete-filtered").addEventListener("click", deleteFilteredRuns);
$("#page-prev").addEventListener("click", () => { if (state.runsPage > 1) { state.runsPage -= 1; loadRuns(); } });
$("#page-next").addEventListener("click", () => { if (state.runsPage < state.runsPages) { state.runsPage += 1; loadRuns(); } });
function setAccountQuery(value) {
  state.accountQuery = value;
  renderAccounts();
}
$("#account-search").addEventListener("input", event => setAccountQuery(event.currentTarget.value));
$("#account-search").addEventListener("keydown", event => {
  if (event.key === "Escape") { event.preventDefault(); setAccountQuery(""); }
});
$("#account-search-clear").addEventListener("click", () => { setAccountQuery(""); $("#account-search").focus(); });
$("#no-match-clear").addEventListener("click", () => { state.accountStatus = ""; setAccountQuery(""); $("#account-search").focus(); });
$("#add-account").addEventListener("click", () => openAccount());
$("#empty-add").addEventListener("click", () => openAccount());
$("#refresh").addEventListener("click", () => refresh());
document.querySelectorAll("[data-close]").forEach(control => control.addEventListener("click", () => document.getElementById(control.dataset.close).close()));
$("#account-dialog").addEventListener("close", () => { $("#account-form").reset(); });
$("#log-dialog").addEventListener("close", () => { state.logId = null; });
$("#delete-run").addEventListener("click", () => deleteRuns([state.logId], "删除这条执行记录？日志文件会一并删除。"));
const viewTitles = { accounts: "账号总览", activities: "秒杀任务", history: "执行记录" };
function showView(name, updateHash = true) {
  const view = viewTitles[name] ? name : "accounts";
  state.view = view;
  $("#accounts-view").hidden = view !== "accounts";
  $("#activities-view").hidden = view !== "activities";
  $("#history-view").hidden = view !== "history";
  $("#breadcrumb-view").textContent = viewTitles[view];
  document.querySelectorAll("[data-view]").forEach(link => link.classList.toggle("active", link.dataset.view === view));
  if (updateHash && window.location.hash !== `#${view}`) window.history.replaceState(null, "", `#${view}`);
  if (view === "history" && state.authenticated) loadRuns(true);
  if (view === "activities" && state.authenticated) loadActivities(true);
  window.scrollTo({ top: 0, behavior: "smooth" });
}
document.querySelectorAll("[data-view]").forEach(link => {
  link.addEventListener("click", event => { event.preventDefault(); showView(link.dataset.view); });
});
window.addEventListener("hashchange", () => showView(window.location.hash.slice(1), false));
setInterval(() => { if (!document.hidden) refresh(true); }, 4000);
(async () => {
  try { showApp(await api("/session")); await refresh(); }
  catch (_) { showLogin(); }
})();
