"use strict";
(() => {
  const $ = id => document.getElementById(id);
  $("environment-toggle").addEventListener("click", () => {
    const open = $("executor-meta").hidden;
    $("executor-meta").hidden = !open;
    $("environment-toggle").setAttribute("aria-expanded", String(open));
    $("environment-toggle").textContent = open ? "环境信息 ▾" : "环境信息 ▸";
  });
  function applyTheme(theme) {
    const light = theme === "light";
    document.documentElement.dataset.theme = light ? "light" : "dark";
    $("theme-toggle").textContent = light ? "深色主题" : "浅色主题";
    $("theme-toggle").setAttribute("aria-label", light ? "切换到深色主题" : "切换到浅色主题");
    $("theme-toggle").setAttribute("aria-pressed", String(light));
  }
  let initialTheme = "dark";
  try { initialTheme = localStorage.getItem("broker.web.theme") || "dark"; } catch {}
  applyTheme(initialTheme);
  $("theme-toggle").addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    applyTheme(theme);
    try { localStorage.setItem("broker.web.theme", theme); } catch {}
  });
  const terminal = new Set(["completed", "cancelled", "failed", "lost", "stopped"]);
  const keyStorage = "broker.web.key";
  let key = "", generation = 0, selected = null, category = "tasks", snapshot = null;
  let polling = false, cards = new Map();
  const notificationStorage = "broker.web.service-notifications";
  let serviceStates = new Map(), unreadServices = new Map(), serviceBaseline = false;
  function saveNotifications() {
    try { sessionStorage.setItem(notificationStorage, JSON.stringify({
      states: [...serviceStates], unread: [...unreadServices],
    })); } catch {}
  }
  function unreadCount(executor) {
    return [...unreadServices.values()].filter(item => executor === undefined || item.executor_id === executor).length;
  }
  function updateNotifications() {
    const count = unreadServices.size;
    $("unread-total").textContent = count;
    $("notifications-toggle").setAttribute("aria-label", "服务提醒，" + count + " 条未读");
    document.title = (count ? "(" + count + ") " : "") + "Broker · 控制台";
    const own = unreadCount(selected);
    $("service-unread").hidden = !own;
    $("service-unread").textContent = own ? own + " 未读" : "";
    $("read-all").disabled = !count;
    if (!count) $("service-announcement").hidden = true;
    $("notification-empty").hidden = count > 0;
    const signature = JSON.stringify([...unreadServices]);
    if ($("notification-list").dataset.signature === signature) return;
    $("notification-list").dataset.signature = signature;
    const list = [...unreadServices.values()].reverse().map(item => {
      const button = element("button", "notification-item");
      button.append(element("strong", "", item.status === "failed" ? "服务失败" : "服务已结束"),
        element("span", "", item.executor_id + " · " + item.name),
        element("small", "", new Date(item.at * 1000).toLocaleString()));
      button.addEventListener("click", () => {
        if (!snapshot?.services.some(record => record.service_id === item.service_id)) return;
        switchView(item.executor_id, "services");
        $("run-search").value = ""; $("status-filter").value = "all"; render();
        const card = cards.get(item.service_id);
        if (card) {
          card.node.open = true;
          card.node.scrollIntoView?.({block: "nearest"});
          card.node.querySelector("summary").focus();
          loadLogs(card); markServiceRead(item.service_id);
        }
        setNotificationsOpen(false);
      });
      return button;
    });
    $("notification-list").replaceChildren(...list);
  }
  function setNotificationsOpen(open) {
    $("notifications").hidden = !open;
    $("notifications-toggle").setAttribute("aria-expanded", String(open));
  }
  function markServiceRead(id) {
    if (!unreadServices.delete(id)) return;
    saveNotifications(); updateNotifications();
    // Re-render sidebar badges without changing the selected view.
    if (snapshot) render();
  }
  function observeServices(services) {
    const retained = new Set(services.map(record => record.service_id));
    for (const id of serviceStates.keys()) if (!retained.has(id)) serviceStates.delete(id);
    for (const id of unreadServices.keys()) if (!retained.has(id)) unreadServices.delete(id);
    const fresh = [];
    for (const record of services) {
      const previous = serviceStates.get(record.service_id);
      const ended = ["stopped", "failed"].includes(record.status);
      if (serviceBaseline && ended && previous !== record.status) {
        const item = {service_id: record.service_id, executor_id: record.executor_id,
          name: record.name, status: record.status, at: record.finished_at || Date.now() / 1000};
        unreadServices.set(record.service_id, item); fresh.push(item);
      }
      serviceStates.set(record.service_id, record.status);
    }
    serviceBaseline = true; saveNotifications(); updateNotifications();
    if (fresh.length) {
      const latest = fresh[fresh.length - 1];
      $("service-announcement").textContent = latest.executor_id + " · " + latest.name +
        (latest.status === "failed" ? "：服务失败" : "：服务已结束") +
        (fresh.length > 1 ? "，共 " + fresh.length + " 条新提醒" : "");
      $("service-announcement").hidden = false;
    }
  }
  let lastSuccess = null, retryAt = 0, failures = 0;
  function updateConnection() {
    $("last-update").textContent = lastSuccess ? "最后更新 " + new Date(lastSuccess).toLocaleTimeString() : "";
    if (!key) return;
    const paused = failures > 0;
    $("dashboard").classList.toggle("stale", paused);
    $("live-status").textContent = paused ? "数据已暂停更新" : "每秒刷新";
    $("clear-history").disabled = paused;
    if (paused) {
      const remaining = Math.max(0, Math.ceil((retryAt - Date.now()) / 1000));
      $("connection").textContent = "数据已暂停更新 · " + remaining + " 秒后重连";
    } else if (snapshot) $("connection").textContent = "已连接 · " + snapshot.executors.length + " online";
    for (const card of cards.values()) card.stop.disabled = card.controlling || paused ||
      terminal.has(card.status.textContent.toLowerCase()) || ["cancelling", "stopping"].includes(card.status.textContent.toLowerCase()) ||
      !snapshot?.executors.some(item => item.executor_id === selected);
  }
  const viewStorage = "broker.web.views";
  let savedViews = {};
  function saveViews() {
    try { sessionStorage.setItem(viewStorage, JSON.stringify({selected, category, views: savedViews})); } catch {}
  }
  function viewId() { return JSON.stringify([selected, category]); }
  function captureView() {
    if (!selected) return;
    savedViews[viewId()] = {
      scroll: document.querySelector("main").scrollTop,
      query: $("run-search").value, status: $("status-filter").value,
      cards: [...cards.values()].map(card => ({id: card.id, open: card.node.open, following: card.following,
        scroll: card.output.scrollTop, horizontal: card.output.scrollLeft, search: card.search.value})),
    };
    // Keep navigation state bounded; no log content or commands are stored.
    const names = Object.keys(savedViews);
    for (const name of names.slice(0, Math.max(0, names.length - 200))) delete savedViews[name];
    saveViews();
  }
  function switchView(name, kind) {
    captureView(); selected = name; category = kind;
    $("run-search").value = savedViews[viewId()]?.query || "";
    $("status-filter").value = savedViews[viewId()]?.status || "all";
    cards.clear(); $("runs").replaceChildren();
    render(); saveViews();
  }
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
    savedViews = {};
    serviceStates.clear(); unreadServices.clear(); serviceBaseline = false;
    try { sessionStorage.removeItem(notificationStorage); } catch {}
    $("notifications-toggle").hidden = true; setNotificationsOpen(false);
    $("service-announcement").hidden = true; updateNotifications();
    lastSuccess = null; retryAt = 0; failures = 0;
    $("last-update").textContent = "";
    $("dashboard").classList.remove("stale");
    try { sessionStorage.removeItem(viewStorage); } catch {}
    cards.clear(); $("runs").replaceChildren(); $("executors").replaceChildren();
    delete $("executors").dataset.signature;
    $("dashboard").hidden = true; $("login").hidden = false;
    $("connection").textContent = "未连接";
  }
  async function api(path, method = "GET", body = null) {
    const current = generation;
    const response = await fetch("/ui/api/" + path, {
      method, headers: {Authorization: "Bearer " + key, ...(body ? {"Content-Type": "application/json"} : {})},
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(10000),
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
  function timestamps(record) {
    const labels = {submitted_at: "提交", started_at: "启动", ready_at: "就绪", finished_at: "结束"};
    return Object.entries(labels).filter(([name]) => record[name] !== null && record[name] !== undefined)
      .map(([name, label]) => label + " " + new Date(record[name] * 1000).toLocaleString(undefined, {
        year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
        hour12: false, timeZoneName: "short",
      })).join(" · ");
  }
  function operationIcon(record) {
    // Presentation hint only: never parse or execute the command as shell code.
    const words = record.command.match(/"[^"\n]*"|'[^'\n]*'|[^\s]+/g) || [];
    let index = 0;
    while (index < words.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(words[index])) index++;
    if (words[index] === "env") {
      index++;
      while (index < words.length && /^[A-Za-z_][A-Za-z0-9_]*=/.test(words[index])) index++;
    }
    const command = (words[index] || "").replace(/^["']|["']$/g, "").split("/").pop();
    let kind = "terminal";
    if (record.service_id) kind = "service";
    else if (command === "git") kind = "git";
    else if (["rg", "grep", "egrep", "fgrep", "find", "fd"].includes(command)) kind = "search";
    else if (/^python(?:\d+(?:\.\d+)*)?$/.test(command)) kind = "python";
    else if (["cat", "head", "tail", "less", "more", "sed", "awk", "ls", "pwd"].includes(command)) kind = "file";
    else if (["curl", "wget", "ping", "ping6", "ssh"].includes(command)) kind = "network";
    const icons = {
      terminal: ["终端命令", ["M4 4h16v16H4z", "m8 9 3 3-3 3", "M13 16h4"]],
      git: ["Git", ["M7 7v10", "M7 12c0-5 10 0 10-5", "M7 3a2 2 0 1 0 0 4 2 2 0 0 0 0-4", "M7 17a2 2 0 1 0 0 4 2 2 0 0 0 0-4", "M17 3a2 2 0 1 0 0 4 2 2 0 0 0 0-4"]],
      search: ["搜索", ["M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14", "m15 15 6 6"]],
      python: ["Python", ["M12 3H8a3 3 0 0 0-3 3v4h9v3H6a3 3 0 0 1-3-3V8", "M12 21h4a3 3 0 0 0 3-3v-4h-9v-3h8a3 3 0 0 1 3 3v2", "M9 6h.01", "M15 18h.01"]],
      file: ["文件查看", ["M6 3h8l4 4v14H6z", "M14 3v5h4", "M9 12h6", "M9 16h6"]],
      network: ["网络", ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18", "M3 12h18", "M12 3c-5 5-5 13 0 18 5-5 5-13 0-18"]],
      service: ["服务", ["M4 3h16v7H4z", "M4 14h16v7H4z", "M7 6.5h.01", "M7 17.5h.01", "M11 6.5h6", "M11 17.5h6"]],
    };
    const [label, paths] = icons[kind];
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("class", "operation-icon"); icon.dataset.operation = kind;
    icon.setAttribute("viewBox", "0 0 24 24"); icon.setAttribute("fill", "none");
    icon.setAttribute("stroke", "currentColor"); icon.setAttribute("stroke-width", "1.6");
    icon.setAttribute("stroke-linecap", "round"); icon.setAttribute("stroke-linejoin", "round");
    icon.setAttribute("role", "img"); icon.setAttribute("aria-label", label);
    const tooltip = document.createElementNS(icon.namespaceURI, "title"); tooltip.textContent = label; icon.append(tooltip);
    for (const data of paths) {
      const path = document.createElementNS(icon.namespaceURI, "path"); path.setAttribute("d", data); icon.append(path);
    }
    return icon;
  }
  function createCard(record) {
    const id = record.task_id || record.service_id;
    const node = element("details"), summary = element("summary");
    const title = element("span", "run-title"), status = element("span", "state"), elapsed = element("span", "duration");
    summary.append(operationIcon(record), title, status, elapsed);
    const body = element("div", "run-body"), command = element("div", "command", record.command);
    const metadata = element("div", "metadata"), controls = element("div", "controls");
    const stop = element("button", "danger", category === "tasks" ? "取消任务" : "停止服务");
    const follow = element("button", "", "暂停跟随"), truncated = element("div", "truncated");
    const output = element("pre", "", "加载日志…"); output.tabIndex = 0;
    const card = {id, kind: category, node, title, status, elapsed, metadata, stop, follow, output, truncated,
      following: true, revision: null, busy: false, controlling: false};
    const restored = savedViews[viewId()]?.cards?.find(item => item.id === id);
    if (restored) {
      node.open = Boolean(restored.open); card.following = restored.following !== false;
      card.restoreScroll = restored;
      follow.textContent = card.following ? "暂停跟随" : "恢复跟随";
    }
    stop.addEventListener("click", async () => {
      if (failures > 0) return;
      card.controlling = true; stop.disabled = true;
      try { await api(card.kind + "/" + encodeURIComponent(id) + "/stop", "POST"); await refresh(); }
      catch (error) { message(error.message); }
      finally { card.controlling = false; updateConnection(); }
    });
    follow.addEventListener("click", () => {
      card.following = !card.following;
      follow.textContent = card.following ? "暂停跟随" : "恢复跟随";
      if (card.following) loadLogs(card);
      captureView();
    });
    const copy = element("button", "", "复制日志"), download = element("button", "", "下载 .txt");
    if (category === "services") {
      const read = element("button", "mark-read", "标为已读");
      read.addEventListener("click", () => markServiceRead(id));
      card.read = read;
    }
    const search = element("input", "log-search"); search.type = "search";
    search.placeholder = "搜索日志关键词"; search.setAttribute("aria-label", "搜索日志关键词");
    const matches = element("span", "log-matches");
    card.search = search; card.matches = matches; card.text = null;
    function showLog() {
      const text = card.text || "", query = search.value.toLocaleLowerCase();
      const lines = text.split("\n");
      const shown = query ? lines.filter(line => line.toLocaleLowerCase().includes(query)) : lines;
      const next = card.text === null ? "加载日志…" : shown.join("\n") || (query ? "（无匹配内容）" : "（暂无输出）");
      if (output.textContent !== next) output.textContent = next;
      matches.textContent = query ? shown.length + " 行匹配" : "";
    }
    card.showLog = showLog;
    search.addEventListener("input", () => { showLog(); captureView(); });
    search.value = restored?.search || "";
    copy.addEventListener("click", async () => {
      try {
        if (card.text === null) await loadLogs(card);
        if (card.text === null) throw new Error("日志尚未加载，请稍后重试");
        await navigator.clipboard.writeText(card.text);
        copy.textContent = "已复制";
        setTimeout(() => { copy.textContent = "复制日志"; }, 1500);
      } catch { message("复制失败，请选择日志手动复制，或使用 HTTPS/本机地址访问。"); }
    });
    download.addEventListener("click", async () => {
      try {
        if (card.text === null) await loadLogs(card);
        if (card.text === null) throw new Error("日志尚未加载，请稍后重试");
        const text = (card.truncated.textContent ? "[较早的输出已被截断]\n" : "") + card.text;
        const url = URL.createObjectURL(new Blob([text], {type: "text/plain;charset=utf-8"}));
        const link = element("a"); link.href = url; link.download = card.kind + "-" + id + ".txt";
        link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) { message(error.message); }
    });
    // Restoring an expanded card also fires toggle. Only an explicit summary
    // interaction may acknowledge a reminder; refresh/navigation must not.
    let acknowledgeOnOpen = false;
    summary.addEventListener("click", () => { acknowledgeOnOpen = !node.open; });
    summary.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") acknowledgeOnOpen = !node.open;
    });
    node.addEventListener("toggle", () => {
      if (node.open) {
        loadLogs(card);
        if (acknowledgeOnOpen && card.kind === "services" && card.node.isConnected && !card.node.hidden && !document.hidden) markServiceRead(id);
      }
      acknowledgeOnOpen = false;
      captureView();
    });
    output.addEventListener("scroll", captureView);
    controls.append(stop, follow, copy, download);
    if (card.read) controls.append(card.read);
    const searchRow = element("div", "log-search-row"); searchRow.append(search, matches);
    body.append(command, metadata, controls, searchRow, truncated, output);
    node.append(summary, body);
    return card;
  }
  async function loadLogs(card) {
    if (!key || !card.node.open || (!card.following && card.revision !== null) || card.busy) return;
    const current = generation;
    card.busy = true;
    try {
      const query = card.revision === null ? "" : "?revision=" + encodeURIComponent(card.revision);
      const data = await api(card.kind + "/" + encodeURIComponent(card.id) + "/logs" + query);
      if (current !== generation || (!card.following && card.revision !== null) || !card.node.isConnected) return;
      if (!data.unchanged) {
        card.text = data.text;
        card.showLog();
        card.truncated.textContent = data.truncated ? "较早的输出已被截断" : "";
        card.revision = data.revision;
        if (card.restoreScroll) {
          card.output.scrollTop = card.restoreScroll.scroll;
          card.output.scrollLeft = card.restoreScroll.horizontal || 0;
          card.restoreScroll = null;
        } else if (card.following) card.output.scrollTop = card.output.scrollHeight;
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
      const unread = unreadCount(name);
      if (unread) button.append(element("span", "unread-badge", String(unread)));
      button.addEventListener("click", () => switchView(name, category));
      return button;
    });
    // Avoid replacing a focused navigation item on every poll.
    const signature = JSON.stringify([names, selected, snapshot.executors.map(e => e.executor_id), names.map(unreadCount)]);
    if ($("executors").dataset.signature !== signature) {
      $("executors").replaceChildren(...nav); $("executors").dataset.signature = signature;
    }
    const executor = snapshot.executors.find(e => e.executor_id === selected);
    $("clear-history").hidden = !selected || Boolean(executor);
    $("clear-history").disabled = failures > 0;
    for (const tab of ["tasks", "services"]) {
      $(tab + "-tab").classList.toggle("active", tab === category);
      $(tab + "-tab").setAttribute("aria-selected", tab === category);
    }
    $("executor-title").textContent = selected || "暂无执行器";
    $("executor-meta").textContent = executor ? (executor.cwd || "") + "\n" + (executor.python || "") : selected ? "OFFLINE · 显示保留的历史记录" : "启动 executor 后即可查看运行状态";
    $("version").textContent = "Broker v" + snapshot.version;
    $("task-count").textContent = snapshot.tasks.filter(r => r.executor_id === selected).length;
    $("service-count").textContent = snapshot.services.filter(r => r.executor_id === selected).length;
    const query = $("run-search").value.toLocaleLowerCase(), filter = $("status-filter").value;
    const owned = snapshot[category].filter(r => r.executor_id === selected);
    const shown = owned.filter(record => {
      const state = record.status;
      const statusMatches = filter === "all" || (filter === "active" && !terminal.has(state) && state !== "submitted") ||
        (filter === "queued" && state === "submitted") || (filter === "finished" && terminal.has(state)) ||
        (filter === "failed" && ["failed", "lost"].includes(state));
      return statusMatches && [record.name || "", record.command, record.task_id || record.service_id].join(" ").toLocaleLowerCase().includes(query);
    }).sort((a, b) => (b.submitted_at || 0) - (a.submitted_at || 0));
    const ids = new Set(owned.map(r => r.task_id || r.service_id));
    for (const [id, card] of cards) if (!ids.has(id)) { card.node.remove(); cards.delete(id); }
    for (const record of shown) {
      const id = record.task_id || record.service_id;
      let card = cards.get(id);
      if (!card) { card = createCard(record); cards.set(id, card); $("runs").append(card.node); }
      card.node.classList.toggle("unread-service", category === "services" && unreadServices.has(id));
      if (card.read) card.read.hidden = !unreadServices.has(id);
      const preview = record.command.replace(/\s+/g, " ");
      card.title.textContent = record.name || (preview.length > 100 ? preview.slice(0, 99) + "…" : preview);
      card.title.title = record.command;
      card.status.textContent = record.status.toUpperCase();
      card.status.className = "state " + (terminal.has(record.status) || ["running", "ready", "starting", "submitted", "cancelling", "stopping"].includes(record.status) ? record.status : "");
      card.elapsed.textContent = duration(record);
      card.metadata.textContent = id + (record.pid ? " · PID " + record.pid : "") +
        (record.returncode !== undefined ? " · exit " + record.returncode : "") +
        (timestamps(record) ? "\n" + timestamps(record) : "") + (record.error ? "\n" + record.error : "");
      card.stop.disabled = card.controlling || failures > 0 || terminal.has(record.status) || ["cancelling", "stopping"].includes(record.status) || !executor;
    }
    const visibleIds = new Set(shown.map(r => r.task_id || r.service_id));
    for (const card of cards.values()) card.node.hidden = !visibleIds.has(card.id);
    let previous = null;
    for (const record of shown) {
      const node = cards.get(record.task_id || record.service_id).node;
      const expected = previous ? previous.nextSibling : $("runs").firstChild;
      if (node !== expected) $("runs").insertBefore(node, expected);
      previous = node;
    }
    $("empty").hidden = shown.length > 0;
    $("empty").textContent = query || filter !== "all" ? "没有匹配的记录。" : category === "tasks" ? "暂无任务。通过 MCP 提交命令后，将在这里显示。" : "暂无服务。通过 MCP 启动服务后，将在这里显示。";
    if (document.querySelector("main").dataset.view !== viewId()) {
      document.querySelector("main").dataset.view = viewId();
      document.querySelector("main").scrollTop = savedViews[viewId()]?.scroll || 0;
    }
    updateNotifications();
  }
  async function refresh() {
    if (!key || polling || Date.now() < retryAt || document.hidden) return;
    const current = generation; polling = true;
    try {
      const data = await api("state");
      if (current !== generation) return;
      snapshot = data;
      observeServices(data.services); render();
      lastSuccess = Date.now(); failures = 0; retryAt = 0;
      rememberKey(key);
      message("");
      $("dashboard").hidden = false; $("login").hidden = true;
      $("notifications-toggle").hidden = false;
      updateConnection();
      await Promise.all([...cards.values()].filter(card => !card.node.hidden).map(loadLogs));
    } catch (error) {
      if (current === generation && key) {
        failures++; retryAt = Date.now() + Math.min(30, 2 ** Math.min(failures - 1, 5)) * 1000;
        message(error.message); updateConnection();
      } else if (!key) message(error.message);
    }
    finally { polling = false; }
  }
  $("login-form").addEventListener("submit", event => {
    event.preventDefault(); generation++; key = $("key").value; $("key").value = ""; failures = 0; retryAt = 0; message(""); refresh();
  });
  $("clear-history").addEventListener("click", async () => {
    if (!selected || !snapshot || failures > 0 || snapshot.executors.some(item => item.executor_id === selected)) return;
    const target = selected;
    if (!window.confirm("清除离线 executor “" + target + "” 的任务、服务及日志历史？这些内存记录将无法恢复，不会删除服务器文件。")) return;
    $("clear-history").disabled = true;
    try {
      await api("history/clear", "POST", {executor_id: target});
      for (const name of Object.keys(savedViews)) if (JSON.parse(name)[0] === target) delete savedViews[name];
      cards.clear(); $("runs").replaceChildren();
      retryAt = 0; await refresh(); captureView();
    } catch (error) { message(error.message); }
    finally { $("clear-history").disabled = false; }
  });
  $("notifications-toggle").addEventListener("click", () => setNotificationsOpen($("notifications").hidden));
  $("notifications-close").addEventListener("click", () => setNotificationsOpen(false));
  $("read-all").addEventListener("click", () => {
    unreadServices.clear(); saveNotifications(); updateNotifications();
    $("service-announcement").hidden = true;
    if (snapshot) render();
  });
  $("service-announcement").addEventListener("click", () => setNotificationsOpen(true));
  document.addEventListener("keydown", event => { if (event.key === "Escape") setNotificationsOpen(false); });
  for (const kind of ["tasks", "services"]) $(kind + "-tab").addEventListener("click", () => {
    if (snapshot) switchView(selected, kind);
  });
  window.addEventListener("pagehide", captureView);
  for (const name of ["run-search", "status-filter"]) $(name).addEventListener(name === "run-search" ? "input" : "change", () => {
    if (snapshot) { render(); captureView(); }
  });
  document.querySelector("main").addEventListener("scroll", captureView);
  setInterval(() => { updateConnection(); return refresh(); }, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  try {
    const stored = JSON.parse(sessionStorage.getItem(viewStorage) || "null");
    if (stored && typeof stored.views === "object" && stored.views !== null) {
      savedViews = stored.views; selected = stored.selected;
      category = stored.category === "services" ? "services" : "tasks";
      $("run-search").value = savedViews[viewId()]?.query || "";
      $("status-filter").value = savedViews[viewId()]?.status || "all";
    }
  } catch {}
  try { key = sessionStorage.getItem(keyStorage) || ""; } catch { /* Memory-only fallback. */ }
  try {
    const stored = JSON.parse(sessionStorage.getItem(notificationStorage) || "null");
    if (stored && Array.isArray(stored.states) && Array.isArray(stored.unread)) {
      serviceStates = new Map(stored.states); unreadServices = new Map(stored.unread);
      serviceBaseline = true;
    }
  } catch {}
  if (key) {
    $("connection").textContent = "正在恢复连接…";
    refresh();
  }
})();
