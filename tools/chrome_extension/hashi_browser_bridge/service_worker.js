const HOST_NAME = "com.hashi.browser_bridge";
const BRIDGE_VERSION = "0.1.5";
const RECONNECT_DELAY_MS = 5000;
const HEARTBEAT_INTERVAL_MS = 10000;
const DEBUGGER_VERSION = "1.3";
const SUPPORTED_ACTIONS = Object.freeze([
  "active_tab", "session_create", "session", "get_text", "get_html",
  "click", "react", "hover", "fill", "type_text", "evaluate", "scroll", "screenshot"
]);

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
      actions: SUPPORTED_ACTIONS,
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
  if (value === undefined) {
    return "undefined";
  }
  if (value === null) {
    return "null";
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
  const maxLength = Number(args.max_length || 0);
  const output = String(results?.[0]?.result || "");
  return {
    output: maxLength > 0 ? output.slice(0, maxLength) : output,
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
  const maxLength = Number(args.max_length || 0);
  const output = String(results?.[0]?.result || "");
  return {
    output: maxLength > 0 ? output.slice(0, maxLength) : output,
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
  const waitMs = Number(args.wait_ms ?? 350);
  const details = await withDebugger(tab.id, async (target) => {
    const expression = `(async () => {
      const selector = ${JSON.stringify(selector)};
      const timeoutMs = ${JSON.stringify(timeoutMs)};
      const waitMs = ${JSON.stringify(waitMs)};
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
        throw new Error("selector not found: " + selector);
      }
      element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
      if (typeof element.focus === "function") {
        element.focus({ preventScroll: true });
      }
      const rect = element.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) {
        throw new Error("selector is not visible: " + selector);
      }
      const matched = document.querySelectorAll(selector).length;
      const before = {
        ariaLabel: element.getAttribute("aria-label"),
        ariaPressed: element.getAttribute("aria-pressed"),
        className: String(element.className || ""),
        text: String(element.innerText || element.textContent || "").trim().slice(0, 200),
        componentKey: element.getAttribute("componentkey"),
        href: element.getAttribute("href")
      };
      element.click();
      if (waitMs > 0) {
        await sleep(waitMs);
      }
      const after = {
        ariaLabel: element.getAttribute("aria-label"),
        ariaPressed: element.getAttribute("aria-pressed"),
        className: String(element.className || ""),
        text: String(element.innerText || element.textContent || "").trim().slice(0, 200),
        componentKey: element.getAttribute("componentkey"),
        href: element.getAttribute("href"),
        connected: element.isConnected
      };
      return {
        selector,
        matched,
        tagName: element.tagName,
        before,
        after,
        stateChanged: JSON.stringify(before) !== JSON.stringify({
          ariaLabel: after.ariaLabel,
          ariaPressed: after.ariaPressed,
          className: after.className,
          text: after.text,
          componentKey: after.componentKey,
          href: after.href
        })
      };
    })()`;
    const evaluated = await chrome.debugger.sendCommand(target, "Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true
    });
    if (evaluated?.exceptionDetails) {
      const message = evaluated.exceptionDetails.exception?.description
        || evaluated.exceptionDetails.text
        || `click failed for selector: ${selector}`;
      throw new Error(message);
    }
    return evaluated?.result?.value || null;
  });
  const updatedTab = await chrome.tabs.get(tab.id);
  if (!details) {
    throw new Error(`click produced no execution result for selector: ${selector}`);
  }
  return {
    output: JSON.stringify({
      ok: true,
      action: "click",
      selector,
      matched: details.matched,
      state_changed: Boolean(details.stateChanged),
      before: details.before,
      after: details.after
    }),
    meta: {
      ...tabMeta(updatedTab),
      action: "click",
      selector,
      details
    }
  };
}

async function actionReact(args) {
  const tab = await resolveTab({ ...args, wait_ms: 0 });
  assertScriptableTab(tab);
  const postText = String(args.post_text || "").trim();
  const author = String(args.author || "").trim();
  const reaction = String(args.reaction || "like").trim().toLowerCase();
  const waitMs = Number(args.wait_ms ?? 700);
  if (!postText) {
    throw new Error("post_text is required");
  }
  if (reaction !== "like") {
    throw new Error("only the 'like' reaction is currently supported");
  }
  const details = await withDebugger(tab.id, async (target) => {
    const expression = `(async () => {
      const postText = ${JSON.stringify(postText)};
      const author = ${JSON.stringify(author)};
      const waitMs = ${JSON.stringify(waitMs)};
      const normalize = (value) => String(value || "").replace(/\\s+/g, " ").trim().toLocaleLowerCase();
      const wantedPost = normalize(postText);
      const wantedAuthor = normalize(author);
      const buttons = Array.from(document.querySelectorAll('button[aria-label^="Reaction button state:"]'));
      const candidates = [];
      for (const button of buttons) {
        let node = button;
        let matchedContainer = null;
        for (let depth = 0; depth < 14 && node?.parentElement; depth += 1) {
          node = node.parentElement;
          const text = normalize(node.innerText || node.textContent);
          if (text.includes(wantedPost) && (!wantedAuthor || text.includes(wantedAuthor))) {
            matchedContainer = node;
            break;
          }
        }
        if (matchedContainer) {
          const rect = button.getBoundingClientRect();
          candidates.push({ button, rect, container: matchedContainer });
        }
      }
      const visible = candidates.filter(({ rect }) => rect.width > 0 && rect.height > 0);
      if (visible.length !== 1) {
        throw new Error(
          visible.length === 0
            ? 'no visible post reaction matched post_text/author'
            : 'post_text/author matched multiple visible reaction buttons; provide a more specific post_text'
        );
      }
      const { button, container } = visible[0];
      button.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
      const before = {
        ariaLabel: button.getAttribute("aria-label"),
        ariaPressed: button.getAttribute("aria-pressed")
      };
      const alreadyReacted = !normalize(before.ariaLabel).includes("no reaction");
      if (!alreadyReacted) {
        button.click();
        if (waitMs > 0) await new Promise((resolve) => setTimeout(resolve, waitMs));
      }
      const after = {
        ariaLabel: button.getAttribute("aria-label"),
        ariaPressed: button.getAttribute("aria-pressed")
      };
      const stateChanged = JSON.stringify(before) !== JSON.stringify(after);
      const verified = alreadyReacted || (stateChanged && !normalize(after.ariaLabel).includes("no reaction"));
      return {
        reaction: "like",
        postText,
        author: author || null,
        postPreview: String(container.innerText || container.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 240),
        alreadyReacted,
        stateChanged,
        verified,
        before,
        after
      };
    })()`;
    const evaluated = await chrome.debugger.sendCommand(target, "Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true
    });
    if (evaluated?.exceptionDetails) {
      const message = evaluated.exceptionDetails.exception?.description
        || evaluated.exceptionDetails.text
        || "reaction failed";
      throw new Error(message);
    }
    return evaluated?.result?.value || null;
  });
  if (!details?.verified) {
    throw new Error("reaction click did not produce a verified state change");
  }
  const updatedTab = await chrome.tabs.get(tab.id);
  return {
    output: JSON.stringify({ ok: true, action: "react", ...details }),
    meta: { ...tabMeta(updatedTab), action: "react", details }
  };
}

async function actionHover(args) {
  // wait_ms belongs after the mouse move for hover-triggered UI, so do not let
  // resolveTab consume it before the element is located.
  const tab = await resolveTab({ ...args, wait_ms: 0 });
  assertScriptableTab(tab);
  const selector = String(args.selector || "").trim();
  const timeoutMs = Number(args.timeout_ms ?? 10000);
  const waitMs = Number(args.wait_ms ?? 500);
  const xRatio = Number(args.x_ratio ?? 0.5);
  const yRatio = Number(args.y_ratio ?? 0.5);
  if (!selector) {
    throw new Error("selector is required");
  }
  if (!Number.isFinite(timeoutMs) || timeoutMs < 0) {
    throw new Error("timeout_ms must be a non-negative number");
  }
  if (!Number.isFinite(waitMs) || waitMs < 0) {
    throw new Error("wait_ms must be a non-negative number");
  }
  if (!Number.isFinite(xRatio) || xRatio < 0 || xRatio > 1) {
    throw new Error("x_ratio must be between 0 and 1");
  }
  if (!Number.isFinite(yRatio) || yRatio < 0 || yRatio > 1) {
    throw new Error("y_ratio must be between 0 and 1");
  }

  await chrome.tabs.update(tab.id, { active: true });
  if (tab.windowId) {
    try {
      await chrome.windows.update(tab.windowId, { focused: true });
    } catch (error) {
      log("warn", "failed to focus Chrome window before hover", {
        tabId: tab.id,
        windowId: tab.windowId,
        error: String(error)
      });
    }
  }

  let details = null;
  await withDebugger(tab.id, async (target) => {
    // Resolve coordinates through CDP as well. Some React-heavy pages execute
    // chrome.scripting callbacks but omit their return value, which made the
    // previous mixed scripting/CDP implementation unable to recover x/y.
    const expression = `(async () => {
      const selector = ${JSON.stringify(selector)};
      const timeoutMs = ${JSON.stringify(timeoutMs)};
      const xRatio = ${JSON.stringify(xRatio)};
      const yRatio = ${JSON.stringify(yRatio)};
      const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      const started = Date.now();
      let element = null;
      while (Date.now() - started <= timeoutMs) {
        element = document.querySelector(selector);
        if (element) break;
        await sleep(100);
      }
      if (!element) throw new Error(\`selector not found: \${selector}\`);
      element.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
      await new Promise((resolve) => requestAnimationFrame(() => resolve()));
      const rect = element.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) {
        throw new Error(\`selector is not visible: \${selector}\`);
      }
      const x = rect.left + rect.width * xRatio;
      const y = rect.top + rect.height * yRatio;
      if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) {
        throw new Error(\`selector is outside the viewport: \${selector}\`);
      }
      return {
        selector, x, y, xRatio, yRatio,
        tagName: element.tagName,
        text: String(element.innerText || element.textContent || "").slice(0, 200),
        rect: { left: rect.left, top: rect.top, width: rect.width, height: rect.height }
      };
    })()`;
    const evaluated = await chrome.debugger.sendCommand(target, "Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true
    });
    if (evaluated?.exceptionDetails) {
      const message = evaluated.exceptionDetails.exception?.description
        || evaluated.exceptionDetails.text
        || `failed to resolve hover coordinates: ${selector}`;
      throw new Error(message);
    }
    details = evaluated?.result?.value || null;
    if (!details || !Number.isFinite(details.x) || !Number.isFinite(details.y)) {
      throw new Error(`failed to resolve hover coordinates: ${selector}`);
    }
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseMoved",
      x: details.x,
      y: details.y,
      button: "none",
      buttons: 0,
      modifiers: 0,
      pointerType: "mouse"
    });
  });
  if (waitMs > 0) {
    await sleep(waitMs);
  }
  const updatedTab = await chrome.tabs.get(tab.id);
  return {
    output: `OK: hovered '${selector}'`,
    meta: {
      ...tabMeta(updatedTab),
      action: "hover",
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
  const expression = `(async () => {
    const candidate = (${script});
    return typeof candidate === "function" ? await candidate() : await candidate;
  })()`;
  const evaluated = await withDebugger(tab.id, async (target) => {
    return await chrome.debugger.sendCommand(target, "Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true
    });
  });
  if (evaluated?.exceptionDetails) {
    const message = evaluated.exceptionDetails.exception?.description
      || evaluated.exceptionDetails.text
      || "browser evaluate failed";
    throw new Error(message);
  }
  if (!evaluated?.result || !("value" in evaluated.result)) {
    throw new Error("browser evaluate produced no serializable result");
  }
  return {
    output: stringifyOutput(evaluated.result.value),
    meta: {
      ...tabMeta(tab),
      action: "evaluate"
    }
  };
}

async function actionScroll(args) {
  const tab = await resolveTab({ ...args, wait_ms: 0 });
  assertScriptableTab(tab);
  const selector = String(args.selector || "").trim();
  const x = Number(args.x ?? 0);
  const y = Number(args.y ?? 500);
  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    args: [selector, x, y],
    func: async (selector, x, y) => {
      const position = (target) => target === window
        ? { x: window.scrollX, y: window.scrollY }
        : { x: target.scrollLeft, y: target.scrollTop };
      const describe = (target) => {
        if (target === window) return "window";
        const id = target.id ? `#${target.id}` : "";
        const classes = String(target.className || "").trim().split(/\s+/).filter(Boolean).slice(0, 3).join(".");
        return `${target.tagName.toLowerCase()}${id}${classes ? `.${classes}` : ""}`;
      };
      let scrollTarget = window;
      const before = position(scrollTarget);
      let effectiveBefore = before;
      let target = null;
      if (selector) {
        target = document.querySelector(selector);
        if (!target) throw new Error(`selector not found: ${selector}`);
        let ancestor = target.parentElement;
        while (ancestor) {
          const style = getComputedStyle(ancestor);
          const canScrollY = ancestor.scrollHeight > ancestor.clientHeight + 1
            && ["auto", "scroll", "overlay"].includes(style.overflowY);
          const canScrollX = ancestor.scrollWidth > ancestor.clientWidth + 1
            && ["auto", "scroll", "overlay"].includes(style.overflowX);
          if (canScrollY || canScrollX) {
            scrollTarget = ancestor;
            effectiveBefore = position(scrollTarget);
            break;
          }
          ancestor = ancestor.parentElement;
        }
        target.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
      } else {
        window.scrollBy({ left: x, top: y, behavior: "instant" });
        await new Promise((resolve) => requestAnimationFrame(() => resolve()));
        const windowAfter = position(window);
        if (before.x === windowAfter.x && before.y === windowAfter.y && (x !== 0 || y !== 0)) {
          const scrollable = Array.from(document.querySelectorAll("*"))
            .filter((element) => {
              const style = getComputedStyle(element);
              const canScrollY = element.scrollHeight > element.clientHeight + 1
                && ["auto", "scroll", "overlay"].includes(style.overflowY);
              const canScrollX = element.scrollWidth > element.clientWidth + 1
                && ["auto", "scroll", "overlay"].includes(style.overflowX);
              const rect = element.getBoundingClientRect();
              return (canScrollY || canScrollX) && rect.width > 0 && rect.height > 0;
            })
            .sort((left, right) => {
              const leftScore = left.clientWidth * left.clientHeight + (left.scrollHeight - left.clientHeight);
              const rightScore = right.clientWidth * right.clientHeight + (right.scrollHeight - right.clientHeight);
              return rightScore - leftScore;
            });
          if (scrollable.length > 0) {
            scrollTarget = scrollable[0];
            effectiveBefore = position(scrollTarget);
            scrollTarget.scrollBy({ left: x, top: y, behavior: "instant" });
          }
        }
      }
      await new Promise((resolve) => requestAnimationFrame(() => resolve()));
      const after = position(scrollTarget);
      return {
        selector: selector || null,
        scrollTarget: describe(scrollTarget),
        before: effectiveBefore,
        after,
        stateChanged: effectiveBefore.x !== after.x || effectiveBefore.y !== after.y,
        targetText: target ? String(target.innerText || target.textContent || "").trim().slice(0, 200) : null
      };
    }
  });
  const details = results?.[0]?.result;
  if (!details) {
    throw new Error("scroll produced no execution result");
  }
  return {
    output: JSON.stringify({
      ok: true,
      action: "scroll",
      selector: details.selector,
      scroll_target: details.scrollTarget,
      before: details.before,
      after: details.after,
      state_changed: Boolean(details.stateChanged),
      target_text: details.targetText
    }),
    meta: { ...tabMeta(tab), action: "scroll", details }
  };
}

async function actionSession(args) {
  const steps = Array.isArray(args.steps) ? args.steps : [];
  if (!steps.length) {
    throw new Error("session steps must be a non-empty array");
  }
  const stopOnError = args.stop_on_error !== false;
  if (args.url) {
    await resolveTab({ url: args.url, timeout_ms: args.timeout_ms });
  }
  const results = [];
  for (let index = 0; index < steps.length; index += 1) {
    const step = steps[index] || {};
    const action = String(step.action || "").trim();
    try {
      let result;
      if (action === "wait") {
        const ms = Number(step.ms ?? 1000);
        await sleep(ms);
        result = { output: `waited ${ms}ms`, meta: { action: "wait", ms } };
      } else if (action === "goto") {
        result = await actionActiveTab({ ...args, ...step, url: step.url });
      } else if (action === "scroll_to") {
        result = await actionScroll({ ...args, ...step, selector: step.selector });
      } else if (["click", "react", "hover", "fill", "type_text", "evaluate", "scroll", "get_text", "get_html", "screenshot"].includes(action)) {
        result = await executeAction(action, { ...args, ...step, steps: undefined });
      } else {
        throw new Error(`unsupported session step: ${action || "<empty>"}`);
      }
      let output = result.output;
      if (typeof output === "string" && (output.startsWith("{") || output.startsWith("["))) {
        try {
          output = JSON.parse(output);
        } catch (_error) {
          // Keep non-JSON text exactly as returned by the primitive action.
        }
      }
      results.push({ index, action, ok: true, output, meta: result.meta || null });
    } catch (error) {
      const message = String(error?.message || error);
      results.push({ index, action, ok: false, error: message });
      if (stopOnError) {
        throw new Error(`session step ${index} (${action || "<empty>"}) failed: ${message}`);
      }
    }
  }
  const failed = results.filter((item) => !item.ok).length;
  const tab = await queryActiveTab();
  return {
    output: JSON.stringify({ ok: failed === 0, completed: results.length, failed, steps: results }),
    meta: { ...tabMeta(tab), action: "session", completed: results.length, failed }
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
  if (action === "active_tab" || action === "session_create") {
    return actionActiveTab(args);
  }
  if (action === "session") {
    return actionSession(args);
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
  if (action === "react") {
    return actionReact(args);
  }
  if (action === "hover") {
    return actionHover(args);
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
  if (action === "scroll") {
    return actionScroll(args);
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
