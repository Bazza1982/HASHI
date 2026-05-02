const els = {
  status: document.getElementById("status"),
  gatewayUrl: document.getElementById("gatewayUrl"),
  deviceLabel: document.getElementById("deviceLabel"),
  pairInfo: document.getElementById("pairInfo"),
  recoveryInfo: document.getElementById("recoveryInfo"),
  agentSelect: document.getElementById("agentSelect"),
  threadSelect: document.getElementById("threadSelect"),
  messages: document.getElementById("messages"),
  messageInput: document.getElementById("messageInput"),
  fileInput: document.getElementById("fileInput"),
  fileNote: document.getElementById("fileNote"),
  btnPair: document.getElementById("btnPair"),
  btnHealth: document.getElementById("btnHealth"),
  btnLoadAgents: document.getElementById("btnLoadAgents"),
  btnNewThread: document.getElementById("btnNewThread"),
  btnSend: document.getElementById("btnSend"),
  btnUpload: document.getElementById("btnUpload"),
  btnBackupKey: document.getElementById("btnBackupKey"),
  btnRestoreKey: document.getElementById("btnRestoreKey"),
};

const state = {
  gatewayUrl: "",
  deviceId: "",
  accessToken: "",
  fileKeyB64: "",
  recoveryUpdatedAt: "",
  streamController: null,
};

function addMessage(text, kind = "system") {
  const div = document.createElement("div");
  div.className = `bubble ${kind}`;
  div.textContent = text;
  els.messages.appendChild(div);
  els.messages.scrollTop = els.messages.scrollHeight;
}

function setStatus(text) {
  els.status.textContent = text;
}

function setRecoveryInfo(text) {
  els.recoveryInfo.textContent = text;
}

function bytesToB64(bytes) {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

function b64ToBytes(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

function randomBytes(length) {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return bytes;
}

function formatRecoveryCode(bytes) {
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return hex.match(/.{1,4}/g).join("-");
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function importFileKey(rawKey) {
  return crypto.subtle.importKey("raw", rawKey, "AES-GCM", false, ["encrypt", "decrypt"]);
}

async function deriveRecoveryKey(recoveryCode, saltBytes) {
  const baseKey = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(recoveryCode),
    "PBKDF2",
    false,
    ["deriveKey"],
  );
  return crypto.subtle.deriveKey(
    {
      name: "PBKDF2",
      salt: saltBytes,
      iterations: 150000,
      hash: "SHA-256",
    },
    baseKey,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

async function ensureFileKey() {
  if (state.fileKeyB64) {
    return b64ToBytes(state.fileKeyB64);
  }
  const rawKey = randomBytes(32);
  state.fileKeyB64 = bytesToB64(rawKey);
  await saveState();
  return rawKey;
}

async function saveState() {
  await chrome.storage.local.set({
    oll_gateway_url: state.gatewayUrl,
    oll_device_id: state.deviceId,
    oll_access_token: state.accessToken,
    oll_file_key_b64: state.fileKeyB64,
    oll_recovery_updated_at: state.recoveryUpdatedAt,
  });
}

async function loadState() {
  const data = await chrome.storage.local.get([
    "oll_gateway_url",
    "oll_device_id",
    "oll_access_token",
    "oll_file_key_b64",
    "oll_recovery_updated_at",
  ]);
  state.gatewayUrl = data.oll_gateway_url || "http://127.0.0.1:8876";
  state.deviceId = data.oll_device_id || "";
  state.accessToken = data.oll_access_token || "";
  state.fileKeyB64 = data.oll_file_key_b64 || "";
  state.recoveryUpdatedAt = data.oll_recovery_updated_at || "";
  els.gatewayUrl.value = state.gatewayUrl;
  els.deviceLabel.value = "Office Chrome";
}

function applyGatewayCompatHeaders(headers) {
  try {
    const gatewayHost = new URL(state.gatewayUrl).hostname.toLowerCase();
    if (gatewayHost.endsWith(".loca.lt")) {
      headers["bypass-tunnel-reminder"] = "true";
    }
  } catch {
    // Ignore invalid URL parsing here; fetch below will surface the real error.
  }
  return headers;
}

async function api(path, options = {}) {
  state.gatewayUrl = els.gatewayUrl.value.trim();
  await saveState();
  const headers = applyGatewayCompatHeaders({ "Content-Type": "application/json", ...(options.headers || {}) });
  if (state.accessToken) {
    headers.Authorization = `Bearer ${state.accessToken}`;
  }
  const res = await fetch(`${state.gatewayUrl}${path}`, { ...options, headers });
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

function inlineReplyEnabled() {
  return !state.streamController;
}

async function health() {
  try {
    const data = await api("/browser/health", { method: "GET", headers: {} });
    setStatus(`已连接 ${data.service}`);
  } catch (error) {
    setStatus(`离线: ${error.message}`);
  }
}

async function backupRecoveryKey() {
  if (!state.deviceId || !state.accessToken) {
    addMessage("请先完成配对", "system");
    return;
  }
  try {
    const rawKey = await ensureFileKey();
    const recoveryCode = formatRecoveryCode(randomBytes(12));
    const recoveryCodeHash = await sha256Hex(recoveryCode);
    const salt = randomBytes(16);
    const iv = randomBytes(12);
    const recoveryKey = await deriveRecoveryKey(recoveryCode, salt);
    const wrapped = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, recoveryKey, rawKey);
    await api("/browser/device/recovery/set", {
      method: "POST",
      body: JSON.stringify({
        recovery_code_hash: recoveryCodeHash,
        recovery_payload: {
          scheme: "PBKDF2-AES-GCM",
          salt_b64: bytesToB64(salt),
          iv_b64: bytesToB64(iv),
          wrapped_key_b64: bytesToB64(new Uint8Array(wrapped)),
        },
      }),
    });
    state.recoveryUpdatedAt = new Date().toISOString();
    await saveState();
    setRecoveryInfo(`已备份 ${new Date(state.recoveryUpdatedAt).toLocaleString()}`);
    window.prompt("请保存恢复码。浏览器丢失本地密钥时用它恢复。", recoveryCode);
    addMessage("恢复码备份成功", "system");
  } catch (error) {
    addMessage(`恢复码备份失败: ${error.message}`, "system");
  }
}

async function restoreRecoveryKey() {
  const deviceId = state.deviceId || window.prompt("请输入 device_id");
  if (!deviceId) {
    return;
  }
  const recoveryCode = window.prompt("请输入恢复码");
  if (!recoveryCode) {
    return;
  }
  try {
    const recoveryCodeHash = await sha256Hex(recoveryCode.trim());
    const data = await api("/browser/device/recovery/restore", {
      method: "POST",
      headers: {},
      body: JSON.stringify({
        device_id: deviceId.trim(),
        recovery_code_hash: recoveryCodeHash,
      }),
    });
    const payload = data.recovery_payload || {};
    const salt = b64ToBytes(payload.salt_b64 || "");
    const iv = b64ToBytes(payload.iv_b64 || "");
    const wrapped = b64ToBytes(payload.wrapped_key_b64 || "");
    const recoveryKey = await deriveRecoveryKey(recoveryCode.trim(), salt);
    const rawKey = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, recoveryKey, wrapped);
    state.fileKeyB64 = bytesToB64(new Uint8Array(rawKey));
    state.recoveryUpdatedAt = data.recovery_updated_at || new Date().toISOString();
    await saveState();
    setRecoveryInfo(`已恢复 ${new Date(state.recoveryUpdatedAt).toLocaleString()}`);
    addMessage(`密钥恢复成功: ${data.device_label || deviceId}`, "system");
  } catch (error) {
    addMessage(`恢复失败: ${error.message}`, "system");
  }
}

async function quickPair() {
  try {
    const label = els.deviceLabel.value.trim() || "Office Chrome";
    const req = await api("/browser/pair/request", {
      method: "POST",
      body: JSON.stringify({ device_label: label }),
    });
    const done = await api("/browser/pair/complete", {
      method: "POST",
      body: JSON.stringify({ device_id: req.device_id, pairing_code: req.pairing_code }),
    });
    state.deviceId = done.device_id;
    state.accessToken = done.access_token;
    await ensureFileKey();
    await saveState();
    els.pairInfo.textContent = `已配对: ${state.deviceId}`;
    addMessage(`配对成功: ${state.deviceId}`, "system");
    await loadAgents();
    await loadThreads();
  } catch (error) {
    addMessage(`配对失败: ${error.message}`, "system");
  }
}

async function loadAgents() {
  try {
    const data = await api("/browser/agents", { method: "GET", headers: {} });
    els.agentSelect.innerHTML = "";
    for (const agent of data.agents || []) {
      const opt = document.createElement("option");
      opt.value = agent.id;
      opt.textContent = agent.display_name || agent.name || agent.id;
      els.agentSelect.appendChild(opt);
    }
  } catch (error) {
    addMessage(`加载 agents 失败: ${error.message}`, "system");
  }
}

async function loadThreads() {
  try {
    const data = await api("/browser/threads", { method: "GET", headers: {} });
    els.threadSelect.innerHTML = "";
    for (const thread of data.threads || []) {
      const opt = document.createElement("option");
      opt.value = thread.thread_id;
      opt.textContent = thread.title || thread.thread_id;
      els.threadSelect.appendChild(opt);
    }
    if (els.threadSelect.value) {
      connectStream(els.threadSelect.value);
    }
  } catch (error) {
    addMessage(`加载 threads 失败: ${error.message}`, "system");
  }
}

async function createThread() {
  try {
    const agentId = els.agentSelect.value;
    if (!agentId) {
      throw new Error("请先选择 agent");
    }
    const data = await api("/browser/thread/create", {
      method: "POST",
      body: JSON.stringify({ agent_id: agentId }),
    });
    await loadThreads();
    els.threadSelect.value = data.thread.thread_id;
    connectStream(data.thread.thread_id);
    addMessage(`已创建线程 ${data.thread.title}`, "system");
  } catch (error) {
    addMessage(`创建线程失败: ${error.message}`, "system");
  }
}

function connectStream(threadId) {
  if (!threadId || !state.accessToken) {
    return;
  }
  if (state.streamController) {
    state.streamController.abort();
  }
  state.streamController = new AbortController();
  fetch(`${state.gatewayUrl}/browser/chat/stream/${threadId}`, {
    headers: applyGatewayCompatHeaders({ Authorization: `Bearer ${state.accessToken}` }),
    signal: state.streamController.signal,
  }).then(async (res) => {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop();
      for (const part of parts) {
        const eventLine = part.split("\n").find((line) => line.startsWith("event:"));
        const dataLine = part.split("\n").find((line) => line.startsWith("data:"));
        if (!dataLine) continue;
        try {
          const event = JSON.parse(dataLine.slice(5).trim());
          const eventType = eventLine ? eventLine.slice(6).trim() : "";
          if (eventType === "attachment_uploaded" && event.attachment) {
            addMessage(`附件已上传: ${event.attachment.filename}`, "system");
          } else if (event.text) {
            addMessage(event.text, "assistant");
          } else if (event.error) {
            addMessage(`错误: ${event.error}`, "system");
          }
        } catch (_error) {
          // ignore malformed event fragments
        }
      }
    }
  }).catch(() => {
    state.streamController = null;
  });
}

async function sendMessage() {
  const threadId = els.threadSelect.value;
  const text = els.messageInput.value.trim();
  if (!threadId || !text) return;
  els.messageInput.value = "";
  addMessage(text, "user");
  try {
    const data = await api("/browser/chat/send", {
      method: "POST",
      body: JSON.stringify({ thread_id: threadId, text }),
    });
    if (data.text && inlineReplyEnabled()) {
      addMessage(data.text, "assistant");
    }
  } catch (error) {
    addMessage(`发送失败: ${error.message}`, "system");
  }
}

async function uploadFile() {
  const threadId = els.threadSelect.value;
  const file = els.fileInput.files?.[0];
  if (!threadId || !file) {
    addMessage("请先选择线程和文件", "system");
    return;
  }
  try {
    const rawKey = await ensureFileKey();
    const cryptoKey = await importFileKey(rawKey);
    const iv = randomBytes(12);
    const plaintext = await file.arrayBuffer();
    const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, cryptoKey, plaintext);
    addMessage(`正在上传附件: ${file.name}`, "system");
    const data = await api("/browser/file/upload", {
      method: "POST",
      body: JSON.stringify({
        thread_id: threadId,
        filename: file.name,
        mime_type: file.type || "application/octet-stream",
        plaintext_bytes: file.size,
        ciphertext_b64: bytesToB64(new Uint8Array(ciphertext)),
        encryption: {
          scheme: "AES-GCM",
          key_id: `${state.deviceId || "device"}-filekey-v1`,
          iv_b64: bytesToB64(iv),
        },
        note: els.fileNote.value.trim(),
        notify_agent: true,
      }),
    });
    addMessage(`附件已加密上传: ${data.attachment.filename}`, "system");
    if (data.agent_result?.text && inlineReplyEnabled()) {
      addMessage(data.agent_result.text, "assistant");
    }
    els.fileInput.value = "";
    els.fileNote.value = "";
  } catch (error) {
    addMessage(`上传失败: ${error.message}`, "system");
  }
}

els.btnPair.addEventListener("click", quickPair);
els.btnHealth.addEventListener("click", health);
els.btnLoadAgents.addEventListener("click", loadAgents);
els.btnNewThread.addEventListener("click", createThread);
els.btnSend.addEventListener("click", sendMessage);
els.btnUpload.addEventListener("click", uploadFile);
els.btnBackupKey.addEventListener("click", backupRecoveryKey);
els.btnRestoreKey.addEventListener("click", restoreRecoveryKey);
els.threadSelect.addEventListener("change", () => connectStream(els.threadSelect.value));

loadState().then(async () => {
  els.pairInfo.textContent = state.deviceId ? `已配对: ${state.deviceId}` : "未配对";
  setRecoveryInfo(state.recoveryUpdatedAt ? `已备份 ${new Date(state.recoveryUpdatedAt).toLocaleString()}` : "未备份");
  await health();
  if (state.accessToken) {
    await loadAgents();
    await loadThreads();
  }
});
