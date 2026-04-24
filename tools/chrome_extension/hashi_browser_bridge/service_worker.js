const HOST_NAME = "com.hashi.browser_bridge";
const BRIDGE_VERSION = "0.1.0";
const RECONNECT_DELAY_MS = 5000;
const HEARTBEAT_INTERVAL_MS = 10000;

let nativePort = null;
let reconnectTimer = null;
let heartbeatTimer = null;

function log(level, message, extra = {}) {
  const payload = { type: "log", level, message, details: extra };
  if (nativePort) {
    try {
      nativePort.postMessage(payload);
    } catch (_error) {
      // ignore broken port writes
    }
  }
  const method = level === "error" ? "error" : level === "warn" ? "warn" : "log";
  console[method]("[HASHI Bridge]", message, extra);
}

function clearHeartbeat() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

function scheduleReconnect() {
  if (reconnectTimer) {
    return;
  }
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    ensureNativeConnection("reconnect");
  }, RECONNECT_DELAY_MS);
}

function startHeartbeat() {
  clearHeartbeat();
  heartbeatTimer = setInterval(() => {
    try {
      nativePort?.postMessage({ type: "heartbeat", ts: Date.now() });
    } catch (_error) {
      // ignore broken port writes
    }
  }, HEARTBEAT_INTERVAL_MS);
}

function ensureNativeConnection(reason = "unknown") {
  if (nativePort) {
    return;
  }
  try {
    nativePort = chrome.runtime.connectNative(HOST_NAME);
    nativePort.onMessage.addListener(handleNativeMessage);
    nativePort.onDisconnect.addListener(() => {
      const errorMessage = chrome.runtime.lastError?.message || "native port disconnected";
      log("warn", errorMessage, { reason });
      nativePort = null;
      clearHeartbeat();
      scheduleReconnect();
    });
    nativePort.postMessage({
      type: "hello",
      extension_version: BRIDGE_VERSION,
      user_agent: navigator.userAgent,
      reason
    });
    startHeartbeat();
    log("info", "native host connected", { reason });
  } catch (error) {
    nativePort = null;
    clearHeartbeat();
    log("error", "connectNative failed", { reason, error: String(error) });
    scheduleReconnect();
  }
}

async function handleNativeMessage(message) {
  const type = String(message?.type || "");
  if (type === "hello_ack" || type === "pong") {
    return;
  }
  if (type !== "request") {
    log("warn", "unknown host message type", { type });
    return;
  }
  const requestId = String(message.request_id || "");
  try {
    const result = await executeAction(String(message.action || ""), message.args || {});
    nativePort?.postMessage({
      type: "response",
      request_id: requestId,
      ok: true,
      output: result.output,
      meta: result.meta || null
    });
  } catch (error) {
    nativePort?.postMessage({
      type: "response",
      request_id: requestId,
      ok: false,
      error: String(error?.message || error)
    });
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function queryActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!tabs.length) {
    throw new Error("No active tab found");
  }
  return tabs[0];
}

async function waitForTabComplete(tabId, timeoutMs = 30000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === "complete") {
      return tab;
    }
    await sleep(200);
  }
  throw new Error(`Timed out waiting for tab ${tabId} to finish loading`);
}

function assertScriptableTab(tab) {
  if (!tab?.id) {
    throw new Error("No tab selected");
  }
  const url = String(tab.url || "");
  if (!url || url.startsWith("chrome://") || url.startsWith("edge://") || url.startsWith("about:")) {
    throw new Error(`Tab URL is not scriptable: ${url || "unknown"}`);
  }
}

async function resolveTab(args = {}) {
  let tab = await queryActiveTab();
  const url = String(args.url || "").trim();
  if (url && url !== String(tab.url || "")) {
    await chrome.tabs.update(tab.id, { url });
    tab = await waitForTabComplete(tab.id, Number(args.timeout_ms || 30000));
  } else if (Number(args.wait_ms || 0) > 0) {
    await sleep(Number(args.wait_ms));
    tab = await chrome.tabs.get(tab.id);
  }
  return tab;
}

function tabMeta(tab) {
  return {
    tabId: tab.id,
    windowId: tab.windowId,
    url: String(tab.url || ""),
    title: String(tab.title || "")
  };
}

async function actionActiveTab(args) {
  const tab = await resolveTab(args);
  return {
    output: JSON.stringify(tabMeta(tab)),
    meta: tabMeta(tab)
  };
}

async function actionGetText(args) {
  const tab = await resolveTab(args);
  assertScriptableTab(tab);
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => document.body?.innerText || document.documentElement?.innerText || ""
  });
  return {
    output: String(results?.[0]?.result || ""),
    meta: tabMeta(tab)
  };
}

async function actionGetHtml(args) {
  const tab = await resolveTab(args);
  assertScriptableTab(tab);
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => document.documentElement?.outerHTML || ""
  });
  return {
    output: String(results?.[0]?.result || ""),
    meta: tabMeta(tab)
  };
}

async function actionScreenshot(args) {
  const tab = await resolveTab(args);
  const dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  return {
    output: String(dataUrl || ""),
    meta: tabMeta(tab)
  };
}

async function executeAction(action, args) {
  if (action === "active_tab" || action === "session_create" || action === "session") {
    return actionActiveTab(args);
  }
  if (action === "get_text") {
    return actionGetText(args);
  }
  if (action === "get_html") {
    return actionGetHtml(args);
  }
  if (action === "screenshot") {
    return actionScreenshot(args);
  }
  throw new Error(`unsupported action: ${action}`);
}

chrome.runtime.onInstalled.addListener(() => {
  ensureNativeConnection("onInstalled");
});

chrome.runtime.onStartup.addListener(() => {
  ensureNativeConnection("onStartup");
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "bridge-keepalive") {
    ensureNativeConnection("alarm");
  }
});

chrome.tabs.onUpdated.addListener(() => {
  ensureNativeConnection("tabs.onUpdated");
});

chrome.tabs.onActivated.addListener(() => {
  ensureNativeConnection("tabs.onActivated");
});

chrome.action.onClicked.addListener(() => {
  ensureNativeConnection("action.onClicked");
});

chrome.runtime.onSuspend.addListener(() => {
  log("info", "service worker suspending");
});

chrome.alarms.create("bridge-keepalive", { periodInMinutes: 1 });
setTimeout(() => {
  ensureNativeConnection("boot");
}, 0);
