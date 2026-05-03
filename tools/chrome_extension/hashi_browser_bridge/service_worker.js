const HOST_NAME = "com.hashi.browser_bridge";
const BRIDGE_VERSION = "0.1.0";
const RECONNECT_DELAY_MS = 5000;
const HEARTBEAT_INTERVAL_MS = 10000;
const DEBUGGER_VERSION = "1.3";

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

function stringifyOutput(value) {
  if (value === undefined || value === null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  try {
    return JSON.stringify(value);
  } catch (_error) {
    return String(value);
  }
}

async function withDebugger(tabId, callback) {
  const target = { tabId };
  let attached = false;
  try {
    await chrome.debugger.attach(target, DEBUGGER_VERSION);
    attached = true;
  } catch (error) {
    const message = String(error?.message || error);
    if (!message.includes("Another debugger is already attached")) {
      throw error;
    }
  }

  try {
    return await callback(target);
  } finally {
    if (attached) {
      try {
        await chrome.debugger.detach(target);
      } catch (_error) {
        // ignore detach failures on cleanup
      }
    }
  }
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

async function actionClick(args) {
  const tab = await resolveTab(args);
  assertScriptableTab(tab);
  const selector = String(args.selector || "").trim();
  const timeoutMs = Number(args.timeout_ms || 10000);
  if (!selector) {
    throw new Error("selector is required");
  }
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    args: [selector, timeoutMs],
    func: async (selector, timeoutMs) => {
      const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      const started = Date.now();
      let element = null;
      while (Date.now() - started < timeoutMs) {
        element = document.querySelector(selector);
        if (element) {
          break;
        }
        await sleep(100);
      }
      if (!element) {
        throw new Error(`selector not found: ${selector}`);
      }
      element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
      if (typeof element.focus === "function") {
        element.focus({ preventScroll: true });
      }
      const rect = element.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) {
        throw new Error(`selector is not visible: ${selector}`);
      }
      element.click();
      return {
        selector,
        tagName: element.tagName,
        text: String(element.innerText || element.textContent || "").slice(0, 200)
      };
    }
  });
  if (Number(args.wait_ms || 0) > 0) {
    await sleep(Number(args.wait_ms));
  }
  const updatedTab = await chrome.tabs.get(tab.id);
  const details = results?.[0]?.result || {};
  return {
    output: `OK: clicked '${selector}'`,
    meta: {
      ...tabMeta(updatedTab),
      action: "click",
      selector,
      details
    }
  };
}

async function actionFill(args) {
  const tab = await resolveTab(args);
  assertScriptableTab(tab);
  const selector = String(args.selector || "").trim();
  const text = String(args.text || "");
  const submit = Boolean(args.submit);
  const timeoutMs = Number(args.timeout_ms || 10000);
  if (!selector) {
    throw new Error("selector is required");
  }
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    args: [selector, text, submit, timeoutMs],
    func: async (selector, text, submit, timeoutMs) => {
      const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      const started = Date.now();
      let element = null;
      while (Date.now() - started < timeoutMs) {
        element = document.querySelector(selector);
        if (element) {
          break;
        }
        await sleep(100);
      }
      if (!element) {
        throw new Error(`selector not found: ${selector}`);
      }
      element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
      if (typeof element.focus === "function") {
        element.focus({ preventScroll: true });
      }

      if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
        element.value = text;
      } else if (element instanceof HTMLSelectElement) {
        element.value = text;
      } else if (element instanceof HTMLElement && element.isContentEditable) {
        element.textContent = text;
      } else if ("value" in element) {
        element.value = text;
      } else {
        throw new Error(`selector is not fillable: ${selector}`);
      }

      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));

      let submitted = false;
      if (submit) {
        const form = element.form || element.closest("form");
        if (form && typeof form.requestSubmit === "function") {
          form.requestSubmit();
          submitted = true;
        }
      }

      return {
        selector,
        tagName: element.tagName,
        value: "value" in element ? String(element.value || "") : String(element.textContent || ""),
        submitted
      };
    }
  });
  if (Number(args.wait_ms || 0) > 0) {
    await sleep(Number(args.wait_ms));
  }
  const updatedTab = await chrome.tabs.get(tab.id);
  const details = results?.[0]?.result || {};
  return {
    output: `OK: filled '${selector}'`,
    meta: {
      ...tabMeta(updatedTab),
      action: "fill",
      selector,
      submitted: Boolean(details.submitted),
      details
    }
  };
}

async function actionTypeText(args) {
  const tab = await resolveTab(args);
  assertScriptableTab(tab);
  const selector = String(args.selector || "").trim();
  const text = String(args.text || "");
  const timeoutMs = Number(args.timeout_ms || 10000);
  if (!selector) {
    throw new Error("selector is required");
  }
  // Wait for element to appear (same timeout-poll pattern as actionFill)
  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    args: [selector, timeoutMs],
    func: async (sel, timeoutMs) => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const started = Date.now();
      let el = null;
      while (Date.now() - started < timeoutMs) {
        el = document.querySelector(sel);
        if (el) break;
        await sleep(100);
      }
      if (!el) throw new Error(`selector not found: ${sel}`);
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) {
        throw new Error(`element is not visible: ${sel}`);
      }
      el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
      if (typeof el.focus === "function") {
        el.focus({ preventScroll: true });
      }
      if (typeof el.click === "function") {
        el.click();
      }
    }
  });
  await sleep(150);
  // CDP Input.insertText — bypasses CSP, triggers real beforeinput events (React-compatible)
  // NOTE: withDebugger lacks per-tab serialisation; concurrent type_text + screenshot-fallback
  // calls on the same tab may race. Tracked as a known risk for future improvement.
  await withDebugger(tab.id, async (target) => {
    await chrome.debugger.sendCommand(target, "Input.insertText", { text });
  });
  const updatedTab = await chrome.tabs.get(tab.id);
  return {
    output: `OK: typed text into '${selector}'`,
    meta: { ...tabMeta(updatedTab), action: "type_text", selector }
  };
}

async function actionEvaluate(args) {
  const tab = await resolveTab(args);
  assertScriptableTab(tab);
  const script = String(args.script || "").trim();
  if (!script) {
    throw new Error("script is required");
  }
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    world: "MAIN",
    args: [script],
    func: async (script) => {
      try {
        let result = globalThis.eval(script);
        if (typeof result === "function") {
          result = result();
        }
        return await Promise.resolve(result);
      } catch (_evalError) {
        const statement = new Function(script);
        const result = statement();
        return await Promise.resolve(result);
      }
    }
  });
  return {
    output: stringifyOutput(results?.[0]?.result),
    meta: {
      ...tabMeta(tab),
      action: "evaluate"
    }
  };
}

async function actionScreenshot(args) {
  const tab = await resolveTab(args);
  await chrome.tabs.update(tab.id, { active: true });
  if (tab.windowId) {
    try {
      await chrome.windows.update(tab.windowId, { focused: true });
    } catch (error) {
      log("warn", "failed to focus Chrome window before screenshot", {
        tabId: tab.id,
        windowId: tab.windowId,
        error: String(error)
      });
    }
  }
  await sleep(Number(args.wait_ms || 300));
  let dataUrl = "";
  try {
    dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
  } catch (_error) {
    await sleep(500);
    try {
      dataUrl = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
    } catch (_retryError) {
      const base64 = await withDebugger(tab.id, async (target) => {
        await chrome.debugger.sendCommand(target, "Page.enable");
        const result = await chrome.debugger.sendCommand(target, "Page.captureScreenshot", {
          format: "png"
        });
        return String(result?.data || "");
      });
      dataUrl = base64 ? `data:image/png;base64,${base64}` : "";
    }
  }
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
  if (action === "click") {
    return actionClick(args);
  }
  if (action === "fill") {
    return actionFill(args);
  }
  if (action === "type_text") {
    return actionTypeText(args);
  }
  if (action === "evaluate") {
    return actionEvaluate(args);
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
