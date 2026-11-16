// DSH Chrome Bridge — service worker
// 连本地桥接服务，收命令，在标签页里执行。
import CFG from "./config.js";

const WS_URL = `ws://127.0.0.1:${CFG.port}/ws?token=${encodeURIComponent(CFG.token)}`;
const RESTRICTED = /^(chrome|chrome-extension|edge|about|devtools|view-source|chrome-untrusted):/i;

let ws = null;
let connected = false;
let retryTimer = null;
let lastPong = 0;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function badge() {
  chrome.action.setBadgeText({ text: connected ? "on" : "" }).catch(() => {});
  chrome.action.setBadgeBackgroundColor({ color: connected ? "#2e7d32" : "#9e9e9e" }).catch(() => {});
}

function scheduleRetry(ms = 2000) {
  if (retryTimer) return;
  retryTimer = setTimeout(() => {
    retryTimer = null;
    connect();
  }, ms);
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  try {
    ws = new WebSocket(WS_URL);
  } catch {
    scheduleRetry(5000);
    return;
  }
  ws.onopen = () => {
    connected = true;
    badge();
    send({ event: "hello", version: chrome.runtime.getManifest().version });
  };
  ws.onclose = () => {
    connected = false;
    badge();
    ws = null;
    scheduleRetry(2000);
  };
  ws.onerror = () => {
    try {
      ws && ws.close();
    } catch {
      /* ignore */
    }
  };
  ws.onmessage = (ev) => handleMessage(ev.data);
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
    return true;
  }
  return false;
}

async function handleMessage(text) {
  let msg;
  try {
    msg = JSON.parse(text);
  } catch {
    return;
  }
  if (msg.event === "pong") {
    lastPong = Date.now();
    return;
  }
  if (!msg.method) return;
  const { id, method, params } = msg;
  try {
    const result = await dispatch(method, params || {});
    send({ id, ok: true, result: result === undefined ? null : result });
  } catch (e) {
    send({ id, ok: false, error: { message: String((e && e.message) || e) } });
  }
}

// ---------------------------------------------------------------- tab helpers
async function activeTab() {
  const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!t) throw new Error("没有活动标签页");
  return t;
}

async function resolveTab(tabId) {
  if (tabId === undefined || tabId === null || tabId === "") return await activeTab();
  const t = await chrome.tabs.get(Number(tabId));
  if (!t) throw new Error(`找不到标签页 ${tabId}`);
  return t;
}

function assertScriptable(tab) {
  if (RESTRICTED.test(tab.url || "")) {
    throw new Error(`这个页面不允许脚本注入：${tab.url}（chrome:// / 扩展页 / 应用商店页面都不行）`);
  }
}

function tabInfo(t) {
  return { id: t.id, windowId: t.windowId, active: t.active, url: t.url, title: t.title, status: t.status };
}

// ------------------------------------------------------------------- eval
// 页面主世界执行：返回 {ok, value} 或 {ok:false, error}
function pageEval(src) {
  return (async () => {
    const stringify = (v) => {
      try {
        return JSON.parse(JSON.stringify(v === undefined ? null : v));
      } catch {
        return String(v);
      }
    };
    try {
      let v;
      try {
        const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
        v = await new AsyncFunction("return (" + src + ")")();
      } catch (e) {
        if (e instanceof SyntaxError) v = await eval(src);
        else throw e;
      }
      return { ok: true, value: stringify(v) };
    } catch (e) {
      return { ok: false, error: String((e && e.stack) || e) };
    }
  })();
}

async function evalScripting(tab, src) {
  const [res] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "MAIN",
    func: pageEval,
    args: [src],
  });
  return res && res.result;
}

async function evalDebugger(tab, src) {
  const target = { tabId: tab.id };
  let attached = false;
  try {
    await chrome.debugger.attach(target, "1.3");
    attached = true;
  } catch (e) {
    if (!/Another debugger/i.test(String(e))) throw e;
  }
  try {
    const expr =
      "(async()=>{try{const v=await(" +
      src +
      ");let o;try{o=JSON.parse(JSON.stringify(v===undefined?null:v))}catch{o=String(v)}return{ok:true,value:o}}catch(e){return{ok:false,error:String((e&&e.stack)||e)}}})()";
    const r = await chrome.debugger.sendCommand(target, "Runtime.evaluate", {
      expression: expr,
      awaitPromise: true,
      returnByValue: true,
      allowUnsafeEvalBlockedByCSP: true,
    });
    if (r && r.exceptionDetails) {
      return { ok: false, error: (r.exceptionDetails.text || "") + " " + (r.exceptionDetails.exception?.description || "") };
    }
    return r && r.result && r.result.value;
  } finally {
    if (attached) {
      try {
        await chrome.debugger.detach(target);
      } catch {
        /* ignore */
      }
    }
  }
}

async function evalInTab(tabId, src, forceCdp) {
  const tab = await resolveTab(tabId);
  assertScriptable(tab);
  if (tab.discarded) await chrome.tabs.reload(tab.id);
  if (!forceCdp) {
    try {
      const r = await evalScripting(tab, src);
      if (r && r.ok) return { ok: true, value: r.value };
      const err = String((r && r.error) || "");
      // 页面 CSP 挡住 new Function 时改走 DevTools 协议
      if (!/unsafe-eval|Content Security Policy|EvalError|Refused to evaluate/i.test(err)) {
        return { ok: false, error: err };
      }
    } catch (e) {
      const m = String((e && e.message) || e);
      if (!/Cannot access|Cannot access contents|The extensions gallery/i.test(m)) throw e;
    }
  }
  return await evalDebugger(tab, src);
}

// ---------------------------------------------------------------- dispatch
async function dispatch(method, p) {
  switch (method) {
    case "ping":
      return { pong: Date.now(), version: chrome.runtime.getManifest().version };

    case "tabs.list": {
      const tabs = await chrome.tabs.query(p.windowId ? { windowId: p.windowId } : {});
      return tabs
        .filter((t) => p.all || t.url)
        .map((t) => ({ ...tabInfo(t), windowActive: undefined }));
    }

    case "tabs.active":
      return tabInfo(await activeTab());

    case "tabs.create": {
      if (!p.url) throw new Error("tabs.create 需要 url");
      const t = await chrome.tabs.create({ url: p.url, active: p.active !== false });
      return tabInfo(t);
    }

    case "tabs.update": {
      const tab = await resolveTab(p.tabId);
      const patch = {};
      if (p.url) patch.url = p.url;
      if (p.active !== undefined) patch.active = p.active;
      const t = await chrome.tabs.update(tab.id, patch);
      if (p.active !== false) await chrome.windows.update(t.windowId, { focused: true });
      return tabInfo(t);
    }

    case "tabs.activate": {
      const tab = await resolveTab(p.tabId);
      await chrome.tabs.update(tab.id, { active: true });
      await chrome.windows.update(tab.windowId, { focused: true });
      return tabInfo(await chrome.tabs.get(tab.id));
    }

    case "tabs.close": {
      const tab = await resolveTab(p.tabId);
      const id = tab.id;
      await chrome.tabs.remove(id);
      return { closed: id };
    }

    case "tabs.reload": {
      const tab = await resolveTab(p.tabId);
      await chrome.tabs.reload(tab.id);
      return { reloaded: tab.id };
    }

    case "eval": {
      if (typeof p.code !== "string" || !p.code.trim()) throw new Error("eval 需要 code");
      const r = await evalInTab(p.tabId, p.code, !!p.cdp);
      if (!r) throw new Error("执行没有返回结果");
      if (r.ok === false) throw new Error(r.error || "页面脚本报错");
      return { value: r.value };
    }

    case "screenshot": {
      const tab = await resolveTab(p.tabId);
      if (!tab.active) {
        await chrome.tabs.update(tab.id, { active: true });
        await chrome.windows.update(tab.windowId, { focused: true });
        await sleep(250);
      }
      const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
      return { dataUrl };
    }

    case "debug.attach": {
      const tab = await resolveTab(p.tabId);
      await chrome.debugger.attach({ tabId: tab.id }, "1.3");
      return { attached: tab.id };
    }

    case "debug.detach": {
      const tab = await resolveTab(p.tabId);
      await chrome.debugger.detach({ tabId: tab.id });
      return { detached: tab.id };
    }

    default:
      throw new Error(`不认识的方法：${method}`);
  }
}

// ---------------------------------------------------------------- lifecycle
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("keepalive", { periodInMinutes: 0.5 });
  connect();
});
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "keepalive") {
    if (!connected) connect();
    else send({ event: "ping", t: Date.now() });
  }
});
chrome.alarms.create("keepalive", { periodInMinutes: 0.5 }).catch(() => {});

setInterval(() => {
  if (!connected) connect();
  else send({ event: "ping", t: Date.now() });
}, 20000);

// 工具栏图标点一下：重连并提示状态
chrome.action.onClicked.addListener(() => {
  if (connected) send({ event: "ping" });
  else connect();
});

connect();
