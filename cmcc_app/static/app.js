"use strict";
const $ = (selector) => document.querySelector(selector);
const state = { csrf: "", timezone: "Asia/Shanghai", accounts: [], runs: [], editing: null, runAccount: null, logId: null, deleting: null, authenticated: false };
const labels = { queued: "等待执行", running: "正在执行", success: "执行完成", failed: "执行失败", interrupted: "已停止" };
const tasks = { sign: "每日签到", wed: "周三拼图", rate: "评价有礼", fee: "话费查询" };
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
}
function dateText(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: state.timezone, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}
function isPending(run) { return ["queued", "running"].includes(run.status); }
function renderAccounts() {
  const grid = $("#account-grid");
  grid.replaceChildren();
  $("#empty-accounts").hidden = state.accounts.length > 0;
  $("#stat-accounts").replaceChildren(document.createTextNode(state.accounts.length), node("small", "个"));
  $("#stat-enabled").replaceChildren(document.createTextNode(state.accounts.filter(a => a.enabled).length), node("small", "个"));
  $("#account-count").textContent = state.accounts.length;
  for (const account of state.accounts) {
    const card = node("article", undefined, "account-card");
    const top = node("div", undefined, "account-top");
    const title = node("div", undefined, "account-title");
    title.append(node("h3", account.name), node("span", account.config.phone.replace(/^(\d{3})\d{4}/, "$1 **** "), "account-phone"));
    top.append(node("span", account.name.slice(0, 1), "avatar"), title, node("span", account.enabled ? "自动签到" : "手动运行", `badge ${account.enabled ? "enabled" : ""}`));
    const schedule = node("div", undefined, "schedule-info");
    schedule.append(node("span", "每日计划"), node("strong", account.enabled ? `每天 ${account.schedule_time}` : "未开启"));
    const tags = node("div", undefined, "card-tags");
    tags.append(node("span", "每日签到", "card-tag"));
    for (const [key, text] of Object.entries({ claim: "累计奖励", tasks: "AI 豆", games: "拓展活动", wed: "周三拼图" })) {
      if (account.options[key]) tags.append(node("span", text, "card-tag"));
    }
    const actions = node("div", undefined, "card-actions");
    const pending = state.runs.some(r => r.account_id === account.id && isPending(r));
    const runButton = button(pending ? "任务执行中…" : "手动执行  →", "button primary", () => openRun(account));
    runButton.disabled = pending;
    actions.append(runButton, button("编辑", "button secondary", () => openAccount(account)), button("删除", "text-button delete-button", () => openDelete(account)));
    card.append(top, schedule, node("p", account.next_run ? `下次执行 ${dateText(account.next_run)}` : "可随时手动执行，或开启每日计划", "next-time"), tags, actions);
    grid.append(card);
  }
}
function renderRuns() {
  const list = $("#run-list");
  list.replaceChildren();
  $("#empty-runs").hidden = state.runs.length > 0;
  for (const run of state.runs) {
    const row = node("tr");
    const first = node("td");
    first.append(node("span", run.account_name, "run-name"), node("span", `${tasks[run.task] || run.task}${run.dry_run ? " · 仅查询" : ""}`, "run-task"));
    const status = node("td");
    status.append(node("span", labels[run.status] || run.status, `badge ${run.status}`));
    const detail = node("td", undefined, "align-right");
    detail.append(button("查看日志 ↗", "text-button", () => openLog(run.id)));
    row.append(first, node("td", dateText(run.created_at)), node("td", run.source === "schedule" ? "每日计划" : "手动执行"), status, detail);
    list.append(row);
  }
  const latest = state.runs[0];
  $("#stat-latest").textContent = latest ? labels[latest.status] : "暂无记录";
  $("#stat-latest-note").textContent = latest ? `${latest.account_name} · ${dateText(latest.created_at)}` : "准备好后，开始第一次签到";
}
let refreshing = false;
async function refresh(silent = false) {
  if (!state.authenticated || refreshing) return;
  refreshing = true;
  try {
    const [accounts, runs] = await Promise.all([api("/accounts"), api("/runs")]);
    if (!state.authenticated) return;
    state.accounts = accounts.accounts;
    state.runs = runs.runs;
    $("#health").textContent = accounts.healthy ? "服务运行中" : "调度器离线";
    $("#health").className = accounts.healthy ? "health" : "health offline";
    renderAccounts();
    renderRuns();
    if ($("#log-dialog").open && state.logId) await updateLog();
  } catch (error) {
    $("#health").textContent = "连接异常";
    $("#health").className = "health offline";
    if (!silent) toast(error.message, true);
  } finally { refreshing = false; }
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
    form.elements.schedule_time.value = account.schedule_time;
    form.elements.enabled.checked = account.enabled;
    for (const field of configFields) if (!secretFields.includes(field)) form.elements[field].value = account.config[field] || "";
    for (const field of secretFields) form.elements[field].placeholder = account[`has_${field}`] ? "已保存；留空保留，填写则更新" : "未设置";
    for (const [key, value] of Object.entries(account.options)) form.elements[key].checked = value;
  } else {
    form.elements.app_token.placeholder = "粘贴 appTokenLogin 请求体中的 token";
    for (const field of secretFields.filter(f => f !== "app_token")) form.elements[field].placeholder = "选填";
  }
  $("#account-dialog").showModal();
}
function openRun(account) {
  state.runAccount = account;
  $("#run-form").reset();
  $("#run-error").textContent = "";
  $("#run-account-name").textContent = `执行账号：${account.name}`;
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
    const options = Object.fromEntries(["claim", "tasks", "games", "wed"].map(field => [field, form.elements[field].checked]));
    const clear = ["serverchan_sendkey", "bark_url", "webhook_url", "webhook_token", "fee_session_cookie"].filter(field => form.elements[`clear_${field}`].checked);
    await api(state.editing ? `/accounts/${state.editing.id}` : "/accounts", state.editing ? "PUT" : "POST", {
      name: form.elements.name.value, config, options, enabled: form.elements.enabled.checked,
      schedule_time: form.elements.schedule_time.value, clear_secrets: clear,
    });
    $("#account-dialog").close();
    form.reset();
    toast("账号已保存");
    await refresh();
  });
});
$("#run-form").addEventListener("submit", event => {
  event.preventDefault();
  const form = event.currentTarget;
  submit(form, "#run-error", async () => {
    const result = await api(`/accounts/${state.runAccount.id}/run`, "POST", {
      task: form.elements.task.value, dry_run: form.elements.dry_run.checked,
    });
    $("#run-dialog").close();
    toast("任务已加入队列");
    await refresh();
    await openLog(result.run_id);
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
  if (mode === "replace" && !window.confirm("替换导入会删除当前全部账号和执行记录，确定继续吗？")) return;
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
$("#add-account").addEventListener("click", () => openAccount());
$("#empty-add").addEventListener("click", () => openAccount());
$("#refresh").addEventListener("click", () => refresh());
document.querySelectorAll("[data-close]").forEach(control => control.addEventListener("click", () => document.getElementById(control.dataset.close).close()));
$("#account-dialog").addEventListener("close", () => { $("#account-form").reset(); });
$("#log-dialog").addEventListener("close", () => { state.logId = null; });
document.querySelectorAll(".nav-link").forEach(link => link.addEventListener("click", () => {
  document.querySelectorAll(".nav-link").forEach(item => item.classList.toggle("active", item === link));
}));
setInterval(() => { if (!document.hidden) refresh(true); }, 4000);
(async () => {
  try { showApp(await api("/session")); await refresh(); }
  catch (_) { showLogin(); }
})();
