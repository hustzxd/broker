"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const terminal = new Set(["completed", "cancelled", "failed", "lost", "stopped"]);
  const keyStorage = "broker.web.key";
  let key = "", generation = 0, selected = null, category = "tasks", snapshot = null;
  let polling = false, cards = new Map();
  function rememberKey(value) {
    try {
      if (value) sessionStorage.setItem(keyStorage, value);
      else sessionStorage.removeItem(keyStorage);
    } catch {
      // Storage may be disabled; the current connection can still work.
    }
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function message(text) {
    $("message").textContent = text;
    $("message").hidden = !text;
  }
  function disconnect() {
    rememberKey("");
    generation++; key = ""; snapshot = null; selected = null;
    cards.clear(); $("runs").replaceChildren(); $("executors").replaceChildren();
    delete $("executors").dataset.signature;
    $("dashboard").hidden = true; $("logout").hidden = true; $("login").hidden = false;
    $("connection").textContent = "未连接";
  }
  async function api(path, method = "GET") {
    const current = generation;
    const response = await fetch("/ui/api/" + path, {
      method, headers: {Authorization: "Bearer " + key}, cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(10000),
    });
    if (current !== generation) throw new Error("连接已更新");
    if (response.status === 401) {
      disconnect(); throw new Error("Key 不匹配，请重新连接。");
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || "请求失败 (" + response.status + ")");
    }
    return response.json();
  }
  function duration(record) {
    if (!record.started_at) return "";
    let seconds = Math.max(0, Math.floor((record.finished_at || Date.now() / 1000) - record.started_at));
    const days = Math.floor(seconds / 86400); seconds %= 86400;
    const hours = Math.floor(seconds / 3600); seconds %= 3600;
    const minutes = Math.floor(seconds / 60); seconds %= 60;
    return (days ? days + "d " : "") + (hours || days ? hours + "h " : "") +
      (minutes || hours || days ? minutes + "m " : "") + seconds + "s";
  }
  function createCard(record) {
    const id = record.task_id || record.service_id;
    const node = element("details"), summary = element("summary");
    const title = element("span", "run-title"), status = element("span", "state"), elapsed = element("span", "duration");
    summary.append(title, status, elapsed);
    const body = element("div", "run-body"), command = element("div", "command", record.command);
    const metadata = element("div", "metadata"), controls = element("div", "controls");
    const stop = element("button", "danger", category === "tasks" ? "取消任务" : "停止服务");
    const follow = element("button", "", "暂停跟随"), truncated = element("div", "truncated");
    const output = element("pre", "", "加载日志…"); output.tabIndex = 0;
    const card = {id, kind: category, node, title, status, elapsed, metadata, stop, follow, output, truncated,
      following: true, revision: null, busy: false, controlling: false};
    stop.addEventListener("click", async () => {
      card.controlling = true; stop.disabled = true;
      try { await api(card.kind + "/" + encodeURIComponent(id) + "/stop", "POST"); await refresh(); }
      catch (error) { message(error.message); }
      finally { card.controlling = false; }
    });
    follow.addEventListener("click", () => {
      card.following = !card.following;
      follow.textContent = card.following ? "暂停跟随" : "恢复跟随";
      if (card.following) loadLogs(card);
    });
    node.addEventListener("toggle", () => { if (node.open) loadLogs(card); });
    controls.append(stop, follow); body.append(command, metadata, controls, truncated, output);
    node.append(summary, body);
    return card;
  }
  async function loadLogs(card) {
    if (!key || !card.node.open || !card.following || card.busy) return;
    const current = generation;
    card.busy = true;
    try {
      const query = card.revision === null ? "" : "?revision=" + encodeURIComponent(card.revision);
      const data = await api(card.kind + "/" + encodeURIComponent(card.id) + "/logs" + query);
      if (current !== generation || !card.following || !card.node.isConnected) return;
      if (!data.unchanged) {
        card.output.textContent = data.text || "（暂无输出）";
        card.truncated.textContent = data.truncated ? "较早的输出已被截断" : "";
        card.revision = data.revision;
        card.output.scrollTop = card.output.scrollHeight;
      }
    } catch (error) { if (current === generation) message(error.message); }
    finally { card.busy = false; }
  }
  function render() {
    const records = [...snapshot.tasks, ...snapshot.services];
    const onlineNames = new Set(snapshot.executors.map(e => e.executor_id));
    const names = [...new Set([...onlineNames, ...records.map(r => r.executor_id)])]
      .sort((a, b) => Number(!onlineNames.has(a)) - Number(!onlineNames.has(b)) || (a < b ? -1 : a > b ? 1 : 0));
    if (!names.includes(selected)) selected = names[0] || null;
    $("executor-count").textContent = names.length;
    const nav = names.map(name => {
      const online = onlineNames.has(name);
      const button = element("button", name === selected ? "active" : "");
      button.append(element("i", "dot" + (online ? "" : " offline")), element("span", "", name + (online ? "" : " · OFFLINE")));
      button.addEventListener("click", () => { selected = name; cards.clear(); $("runs").replaceChildren(); render(); });
      return button;
    });
    // Avoid replacing a focused navigation item on every poll.
    const signature = JSON.stringify([names, selected, snapshot.executors.map(e => e.executor_id)]);
    if ($("executors").dataset.signature !== signature) {
      $("executors").replaceChildren(...nav); $("executors").dataset.signature = signature;
    }
    const executor = snapshot.executors.find(e => e.executor_id === selected);
    $("executor-title").textContent = selected || "暂无执行器";
    $("executor-meta").textContent = executor ? (executor.cwd || "") + "\n" + (executor.python || "") : selected ? "OFFLINE · 显示保留的历史记录" : "启动 executor 后即可查看运行状态";
    $("version").textContent = "Broker v" + snapshot.version;
    const own = records.filter(r => r.executor_id === selected);
    $("active-count").textContent = own.filter(r => ["running", "ready", "starting", "cancelling", "stopping"].includes(r.status)).length;
    $("queued-count").textContent = own.filter(r => r.status === "submitted").length;
    $("finished-count").textContent = own.filter(r => terminal.has(r.status)).length;
    $("task-count").textContent = snapshot.tasks.filter(r => r.executor_id === selected).length;
    $("service-count").textContent = snapshot.services.filter(r => r.executor_id === selected).length;
    const shown = snapshot[category].filter(r => r.executor_id === selected);
    const ids = new Set(shown.map(r => r.task_id || r.service_id));
    for (const [id, card] of cards) if (!ids.has(id)) { card.node.remove(); cards.delete(id); }
    for (const record of shown) {
      const id = record.task_id || record.service_id;
      let card = cards.get(id);
      if (!card) { card = createCard(record); cards.set(id, card); $("runs").append(card.node); }
      const preview = record.command.replace(/\s+/g, " ");
      card.title.textContent = record.name || "$ " + (preview.length > 100 ? preview.slice(0, 99) + "…" : preview);
      card.title.title = record.command;
      card.status.textContent = record.status.toUpperCase();
      card.status.className = "state " + (terminal.has(record.status) || ["running", "ready", "starting", "submitted", "cancelling", "stopping"].includes(record.status) ? record.status : "");
      card.elapsed.textContent = duration(record);
      card.metadata.textContent = id + (record.pid ? " · PID " + record.pid : "") +
        (record.returncode !== undefined ? " · exit " + record.returncode : "") + (record.error ? "\n" + record.error : "");
      card.stop.disabled = card.controlling || terminal.has(record.status) || ["cancelling", "stopping"].includes(record.status) || !executor;
    }
    $("empty").hidden = shown.length > 0;
    $("empty").textContent = category === "tasks" ? "暂无任务。通过 MCP 提交命令后，将在这里显示。" : "暂无服务。通过 MCP 启动服务后，将在这里显示。";
  }
  async function refresh() {
    if (!key || polling) return;
    const current = generation; polling = true;
    try {
      const data = await api("state");
      if (current !== generation) return;
      snapshot = data; render();
      rememberKey(key);
      message("");
      $("dashboard").hidden = false; $("login").hidden = true; $("logout").hidden = false;
      $("connection").textContent = "已连接 · " + snapshot.executors.length + " online";
      await Promise.all([...cards.values()].map(loadLogs));
    } catch (error) { if (current === generation || !key) { message(error.message); $("connection").textContent = "连接异常"; } }
    finally { polling = false; }
  }
  $("login-form").addEventListener("submit", event => {
    event.preventDefault(); generation++; key = $("key").value; $("key").value = ""; message(""); refresh();
  });
  $("logout").addEventListener("click", () => { disconnect(); message(""); });
  for (const kind of ["tasks", "services"]) $(kind + "-tab").addEventListener("click", () => {
    category = kind; cards.clear(); $("runs").replaceChildren();
    for (const tab of ["tasks", "services"]) { $(tab + "-tab").classList.toggle("active", tab === kind); $(tab + "-tab").setAttribute("aria-selected", tab === kind); }
    if (snapshot) render();
  });
  setInterval(refresh, 1000);
  try { key = sessionStorage.getItem(keyStorage) || ""; } catch { /* Memory-only fallback. */ }
  if (key) {
    $("connection").textContent = "正在恢复连接…";
    refresh();
  }
})();
