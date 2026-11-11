// Run with node tests/web_ui.cjs (requires jsdom in the test environment).
const {JSDOM} = require("jsdom");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const assets = path.join(__dirname, "../src/broker/web_assets");
const settle = () => new Promise(resolve => setTimeout(resolve, 25));

function setup(saved = {}, preferences = {}, storageBlocked = false, initialState = null) {
  const dom = new JSDOM(fs.readFileSync(path.join(assets, "index.html"), "utf8"), {
    url: "http://localhost/ui/", runScripts: "outside-only", pretendToBeVisual: true,
  });
  const w = dom.window, d = w.document;
  const env = {dom, w, d, calls: [], now: 10000, offline: false, confirmed: false, logs: "one\ntwo", revision: 1};
  env.state = {
    version: "test", executors: [{executor_id: "a"}, {executor_id: "b"}],
    tasks: [
      {task_id: "t", executor_id: "a", command: "echo <img src=x>", status: "running", submitted_at: 1},
      {task_id: "new", executor_id: "a", command: "new command", status: "failed", submitted_at: 2},
      {task_id: "old", executor_id: "offline", command: "old command", status: "completed", submitted_at: 0},
    ], services: [{service_id: "s", executor_id: "a", name: "server", command: "serve", status: "ready", submitted_at: 3}],
  };
  if (initialState) env.state = initialState;
  for (const [key, value] of Object.entries(saved)) w.sessionStorage.setItem(key, value);
  for (const [key, value] of Object.entries(preferences)) w.localStorage.setItem(key, value);
  if (storageBlocked) Object.defineProperty(w, "localStorage", {get() {throw Error("Storage disabled");}});
  w.AbortSignal = AbortSignal;
  w.Date.now = () => env.now;
  w.setInterval = callback => { env.tick = callback; };
  w.confirm = () => env.confirmed;
  Object.defineProperty(w.navigator, "clipboard", {value: {writeText: async text => {env.copied = text;}}});
  w.URL.createObjectURL = value => {env.blob = value; return "blob:test";};
  w.URL.revokeObjectURL = () => {};
  w.HTMLAnchorElement.prototype.click = function () {env.filename = this.download;};
  w.fetch = async (url, options) => {
    env.calls.push({url, options});
    if (env.offline) throw Error("offline");
    if (env.unauthorized || options.headers.Authorization !== "Bearer key") return {ok: false, status: 401};
    let data;
    if (url.endsWith("state")) data = structuredClone(env.state);
    else if (url.includes("/logs")) data = url.endsWith("revision=" + env.revision) ? {unchanged: true} :
      {text: env.logs, revision: String(env.revision), truncated: false};
    else if (url.endsWith("history/clear")) {
      const name = JSON.parse(options.body).executor_id;
      env.state.tasks = env.state.tasks.filter(record => record.executor_id !== name);
      data = {tasks_deleted: 1, services_deleted: 0};
    } else if (url.endsWith("/stop")) data = {status: "cancelling"};
    else throw Error("Unexpected endpoint: " + url);
    return {ok: true, status: 200, json: async () => data};
  };
  w.eval(fs.readFileSync(path.join(assets, "app.js"), "utf8"));
  env.login = async () => {
    d.getElementById("key").value = "key";
    d.getElementById("login-form").dispatchEvent(new w.Event("submit", {cancelable: true}));
    await settle();
  };
  return env;
}

async function viewAndLogs() {
  const env = setup(), {w, d} = env;
  try {
    await env.login();
    assert.match(d.querySelector("summary").textContent, /new command/); // newest first
    let card = [...d.querySelectorAll("details")].find(node => node.textContent.includes("echo <img"));
    assert.equal(d.querySelectorAll("img").length, 0);
    card.open = true; await settle();
    card.querySelector(".log-search").value = "two";
    card.querySelector(".log-search").dispatchEvent(new w.Event("input"));
    assert.equal(card.querySelector("pre").textContent, "two");
    assert.match(card.querySelector(".log-matches").textContent, /1 行/);
    card.querySelectorAll("button")[2].click(); await settle();
    assert.equal(env.copied, "one\ntwo"); // full retained log, not filtered lines
    card.querySelectorAll("button")[3].click(); await settle();
    assert(env.blob); assert.equal(env.filename, "tasks-t.txt");
    card.querySelectorAll("button")[1].click();
    card.querySelector("pre").scrollTop = 123;
    d.querySelector("main").scrollTop = 50;
    d.getElementById("services-tab").click();
    d.getElementById("tasks-tab").click(); await settle();
    card = [...d.querySelectorAll("details")].find(node => node.textContent.includes("echo <img"));
    assert(card.open); assert.equal(card.querySelectorAll("button")[1].textContent, "恢复跟随");
    assert.equal(card.querySelector("pre").scrollTop, 123);
    assert.equal(d.querySelector("main").scrollTop, 50);
    env.logs = "changed"; env.revision++; await env.tick(); await settle();
    assert.equal(card.querySelector("pre").textContent, "two"); // paused content stable
    w.dispatchEvent(new w.Event("pagehide"));
    const saved = Object.fromEntries(Object.keys(w.sessionStorage).map(key => [key, w.sessionStorage.getItem(key)]));
    const restored = setup(saved);
    try {
      await settle();
      const current = [...restored.d.querySelectorAll("details")].find(node => node.textContent.includes("echo <img"));
      assert(current.open);
      assert.equal(current.querySelectorAll("button")[1].textContent, "恢复跟随");
      assert.equal(current.querySelector("pre").scrollTop, 123);
      assert.equal(current.querySelector(".log-search").value, "two");
    } finally {restored.w.close();}
    const filter = d.getElementById("status-filter");
    filter.value = "failed"; filter.dispatchEvent(new w.Event("change"));
    assert.equal([...d.querySelectorAll("details")].filter(node => !node.hidden).length, 1);
    d.getElementById("run-search").value = "no match";
    d.getElementById("run-search").dispatchEvent(new w.Event("input"));
    assert.equal([...d.querySelectorAll("details")].filter(node => !node.hidden).length, 0);
    env.unauthorized = true; await env.tick(); await settle();
    assert.equal(w.sessionStorage.length, 0);
  } finally {w.close();}
}

async function reconnectAndCleanup() {
  const env = setup(), {w, d} = env;
  try {
    await env.login();
    assert.match(d.getElementById("last-update").textContent, /最后更新/);
    env.offline = true; await env.tick(); await settle();
    assert(d.getElementById("dashboard").classList.contains("stale"));
    assert.match(d.getElementById("connection").textContent, /1 秒后重连/);
    const count = env.calls.length; await env.tick(); assert.equal(env.calls.length, count);
    env.now += 1000; await env.tick();
    assert.match(d.getElementById("connection").textContent, /2 秒后重连/);
    env.offline = false; env.now += 2000; await env.tick();
    assert(!d.getElementById("dashboard").classList.contains("stale"));
    Object.defineProperty(d, "hidden", {configurable: true, value: true});
    const before = env.calls.length; await env.tick(); assert.equal(env.calls.length, before);
    Object.defineProperty(d, "hidden", {configurable: true, value: false});
    [...d.querySelectorAll("nav button")].find(button => button.textContent.includes("offline")).click();
    assert.equal(d.getElementById("clear-history").hidden, false);
    d.getElementById("clear-history").click(); await settle();
    assert(!env.calls.some(call => call.url.endsWith("history/clear"))); // cancelled confirmation
    env.confirmed = true; d.getElementById("clear-history").click(); await settle();
    assert(env.calls.some(call => call.url.endsWith("history/clear") && call.options.method === "POST"));
    assert(![...d.querySelectorAll("nav button")].some(button => button.textContent.includes("offline")));
  } finally {w.close();}
}

async function themes() {
  const env = setup(), {w, d} = env;
  try {
    const toggle = d.getElementById("theme-toggle");
    assert.equal(d.documentElement.dataset.theme, "dark");
    assert.equal(toggle.getAttribute("aria-pressed"), "false");
    toggle.click();
    assert.equal(d.documentElement.dataset.theme, "light");
    assert.equal(toggle.textContent, "深色主题");
    assert.equal(toggle.getAttribute("aria-pressed"), "true");
    assert.equal(w.localStorage.getItem("broker.web.theme"), "light");
    await env.login();
    const card = d.querySelector("details"); card.open = true;
    toggle.click(); assert.equal(card.open, true);
    toggle.click(); env.unauthorized = true; await env.tick(); await settle();
    assert.equal(d.documentElement.dataset.theme, "light");
    assert.equal(w.localStorage.getItem("broker.web.theme"), "light");
    const restored = setup({}, {"broker.web.theme": "light"});
    try {assert.equal(restored.d.documentElement.dataset.theme, "light");} finally {restored.w.close();}
    const blocked = setup({}, {}, true);
    try {
      assert.equal(blocked.d.documentElement.dataset.theme, "dark");
      blocked.d.getElementById("theme-toggle").click();
      assert.equal(blocked.d.documentElement.dataset.theme, "light");
    } finally {blocked.w.close();}
  } finally {w.close();}
}

async function serviceNotifications() {
  const env = setup(), {w, d} = env;
  try {
    env.state.services.push({service_id: "historical", executor_id: "b", name: "old", command: "old", status: "stopped"});
    await env.login();
    assert.equal(d.getElementById("unread-total").textContent, "0"); // initial history is quiet
    env.state.tasks[0].status = "completed";
    env.state.tasks[0].returncode = 0;
    await env.tick();
    assert.equal(d.getElementById("unread-total").textContent, "0"); // tasks never notify
    assert.equal(d.getElementById("service-announcement").hidden, true);
    env.state.services[0].status = "failed";
    env.state.services[0].finished_at = 10;
    await env.tick();
    assert.equal(d.getElementById("unread-total").textContent, "1");
    assert.match(d.title, /^\(1\)/);
    assert.match(d.getElementById("service-unread").textContent, /1 未读/);
    assert.match(d.querySelector("nav button .unread-badge").textContent, /1/);
    assert.match(d.getElementById("service-announcement").textContent, /server.*失败/);
    await env.tick(); assert.equal(d.getElementById("unread-total").textContent, "1"); // no duplicates
    const saved = Object.fromEntries(Object.keys(w.sessionStorage).map(key => [key, w.sessionStorage.getItem(key)]));
    saved["broker.web.views"] = JSON.stringify({selected: "a", category: "services", views: {
      '["a","services"]': {cards: [{id: "s", open: true, following: true}]},
    }});
    const reload = setup(saved, {}, false, env.state);
    try {
      await settle();
      assert.equal(reload.d.getElementById("unread-total").textContent, "1");
      assert.equal(reload.d.getElementById("service-announcement").hidden, true); // no repeated toast on reload
    } finally {reload.w.close();}
    // Switching back to an auto-restored open card must not mark it read.
    d.getElementById("services-tab").click();
    d.querySelector("details").open = true;
    await settle();
    d.getElementById("tasks-tab").click();
    d.getElementById("services-tab").click();
    await settle();
    assert.equal(d.getElementById("unread-total").textContent, "1");
    d.getElementById("notifications-toggle").click();
    assert.equal(d.getElementById("notifications").hidden, false);
    d.querySelector(".notification-item").click(); await settle();
    assert.equal(d.getElementById("services-tab").getAttribute("aria-selected"), "true");
    assert(d.querySelector("details").open);
    assert.equal(d.getElementById("unread-total").textContent, "0");
    assert(!d.title.startsWith("("));
    // A new short service may finish entirely between two polls.
    env.state.services.push({service_id: "short", executor_id: "b", name: "<img src=x>", command: "exit 0", status: "stopped"});
    await env.tick();
    assert.equal(d.getElementById("unread-total").textContent, "1");
    assert.equal(d.querySelectorAll("img").length, 0);
    d.getElementById("read-all").click();
    assert.equal(d.getElementById("unread-total").textContent, "0");
    env.state.services.push({service_id: "gone", executor_id: "b", name: "gone", command: "exit 1", status: "failed"});
    await env.tick(); assert.equal(d.getElementById("unread-total").textContent, "1");
    env.state.services = env.state.services.filter(item => item.service_id !== "gone");
    await env.tick(); assert.equal(d.getElementById("unread-total").textContent, "0");
    // A service ending while its details are already open stays unread until
    // explicitly acknowledged, even after another poll.
    env.state.services.push({service_id: "open-service", executor_id: "a", name: "open", command: "serve", status: "ready"});
    await env.tick();
    const openCard = [...d.querySelectorAll("details")].find(node => node.textContent.includes("open-service"));
    openCard.open = true; await settle();
    env.state.services.find(item => item.service_id === "open-service").status = "stopped";
    await env.tick(); await settle();
    await env.tick();
    assert.equal(d.getElementById("unread-total").textContent, "1");
    openCard.querySelector(".mark-read").click();
    assert.equal(d.getElementById("unread-total").textContent, "0");
    env.state.services.push({service_id: "click-service", executor_id: "a", name: "click", command: "serve", status: "failed"});
    await env.tick(); await settle();
    const clickedCard = [...d.querySelectorAll("details")].find(node => node.textContent.includes("click-service"));
    clickedCard.querySelector("summary").click(); await settle();
    assert.equal(d.getElementById("unread-total").textContent, "0");
    env.unauthorized = true; await env.tick(); await settle();
    assert.equal(w.sessionStorage.length, 0);
    assert.equal(d.getElementById("notifications-toggle").hidden, true);
  } finally {w.close();}
}

async function serviceNotificationsAfterBackgroundReload() {
  const env = setup();
  try {
    await env.login();
    Object.defineProperty(env.d, "hidden", {configurable: true, value: true});
    env.state.services[0].status = "stopped";
    await env.tick();
    assert.equal(env.d.getElementById("unread-total").textContent, "0");
    const saved = Object.fromEntries(Object.keys(env.w.sessionStorage).map(key => [key, env.w.sessionStorage.getItem(key)]));
    const reload = setup(saved, {}, false, env.state);
    try {
      await settle();
      assert.equal(reload.d.getElementById("unread-total").textContent, "1");
    } finally {reload.w.close();}
  } finally {env.w.close();}
}

async function operationIcons() {
  const env = setup(), {d, w} = env;
  try {
    const examples = [
      ["git status", "git"], ["rg -n pattern src", "search"],
      ["/env/bin/python3.12 train.py", "python"], ["env TOKEN=hidden python -c 'pass'", "python"],
      ["MODE=test python script.py", "python"], ["cat README.md", "file"],
      ["ping 127.0.0.1", "network"], ["bash run.sh", "terminal"],
      ["echo 'python git rg'", "terminal"], ["printf '<svg onload=alert(1)>'", "terminal"],
    ];
    env.state.tasks = examples.map(([command], index) => ({task_id: String(index), executor_id: "a", command, status: "completed"}));
    await env.login();
    const icons = [...d.querySelectorAll("summary .operation-icon")];
    assert.deepEqual(icons.map(icon => icon.dataset.operation), examples.map(([, kind]) => kind));
    for (const icon of icons) {assert.equal(icon.namespaceURI, "http://www.w3.org/2000/svg"); assert(icon.getAttribute("aria-label"));}
    assert.equal(d.querySelectorAll("[onload]").length, 0);
    d.getElementById("services-tab").click();
    assert.equal(d.querySelector("summary .operation-icon").dataset.operation, "service");
  } finally {w.close();}
}

(async () => {
  await viewAndLogs();
  await reconnectAndCleanup();
  await themes();
  await serviceNotifications();
  await serviceNotificationsAfterBackgroundReload();
  await operationIcons();
  console.log("Web UI regression passed: restore, logs, filters, reconnect, offline cleanup, themes and service notifications.");
})().catch(error => {console.error(error); process.exitCode = 1;});
