import express from 'express';
import multer from 'multer';
import os from 'node:os';
import fs from 'node:fs';
import { execSync } from 'node:child_process';
import { getAgents, getAgentMap, updateAgentMetadata } from './agents.js';
import { createMinatoMcpRouter } from './minato_mcp.js';
import { parseMinatoContext, stripHeaders, appendEntry, appendLogEntry, readEntries, listProjects } from './project_log.js';

// Per-agent active project context — updated when an outbound message carries MINATO CONTEXT.
// Used to tag inbound replies with the same project metadata.
const agentContextMap = {};

const PORT = Number(process.env.PORT || 3001);
const BRIDGE_U_API = process.env.BRIDGE_U_API || 'http://127.0.0.1:18800';
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 64 * 1024 * 1024 } });

const app = express();
app.use(express.json({ limit: '64mb' }));

let lastCpuInfo = null;
let lastCpuTime = 0;

function getCpuUsage() {
  const cpus = os.cpus();
  const now = Date.now();
  let totalIdle = 0;
  let totalTick = 0;

  for (const cpu of cpus) {
    for (const type in cpu.times) totalTick += cpu.times[type];
    totalIdle += cpu.times.idle;
  }

  if (!lastCpuInfo || now - lastCpuTime > 5000) {
    lastCpuInfo = { idle: totalIdle, total: totalTick };
    lastCpuTime = now;
    return null;
  }

  const idleDiff = totalIdle - lastCpuInfo.idle;
  const totalDiff = totalTick - lastCpuInfo.total;
  lastCpuInfo = { idle: totalIdle, total: totalTick };
  lastCpuTime = now;
  if (totalDiff === 0) return 0;
  return (1 - idleDiff / totalDiff) * 100;
}

let gpuCache = { data: null, time: 0 };

function getGpuInfo() {
  const now = Date.now();
  if (gpuCache.data && now - gpuCache.time < 5000) return gpuCache.data;

  let result = { available: false, name: null, vendor: null, status: 'Unavailable',
                 npu: { available: false, name: null, status: 'Unavailable' } };

  try {
    const platform = process.platform;

    if (platform === 'win32') {
      const output = execSync(
        `powershell -NoProfile -Command "$gpu = Get-CimInstance Win32_VideoController | Select-Object -First 1 Name,AdapterCompatibility,Status; ` +
        `$npu = Get-PnpDevice | Where-Object { $_.Class -eq 'ComputeAccelerator' } | Select-Object -First 1 FriendlyName,Class,Status; ` +
        `[pscustomobject]@{ gpu = $gpu; npu = $npu } | ConvertTo-Json -Depth 4 -Compress"`,
        { encoding: 'utf8', timeout: 5000, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] },
      ).trim();
      if (output) {
        const parsed = JSON.parse(output);
        result = {
          available: Boolean(parsed.gpu?.Name),
          name: parsed.gpu?.Name || null,
          vendor: parsed.gpu?.AdapterCompatibility || null,
          status: parsed.gpu?.Status || 'Unknown',
          npu: {
            available: Boolean(parsed.npu?.FriendlyName),
            name: parsed.npu?.FriendlyName || null,
            status: parsed.npu?.Status || 'Unknown',
          },
        };
      }
    } else if (platform === 'darwin') {
      const raw = execSync(
        `system_profiler SPDisplaysDataType 2>/dev/null`,
        { encoding: 'utf8', timeout: 8000, stdio: ['ignore','pipe','pipe'] }
      ).trim();

      const chipsetMatch = raw.match(/Chipset Model:\s*(.+)/);
      const name = chipsetMatch ? chipsetMatch[1].trim() : null;

      result = {
        available: !!name,
        name: name || 'Apple Integrated GPU',
        vendor: name?.includes('Apple') ? 'Apple' : detectVendor(name || ''),
        status: name ? 'OK' : 'Integrated',
        npu: {
          available: isAppleSilicon(),
          name: isAppleSilicon() ? 'Apple Neural Engine' : null,
          status: isAppleSilicon() ? 'Available' : 'Unavailable',
        },
      };

    } else {
      const name = linuxGpuName();
      result = {
        available: !!name,
        name: name || 'Unknown',
        vendor: detectVendor(name || ''),
        status: name ? 'OK' : 'Unavailable',
        npu: { available: false, name: null, status: 'Unavailable' },
      };
    }
  } catch (_) {
    // swallow
  }

  gpuCache.data = result;
  gpuCache.time = now;
  return result;
}

function isAppleSilicon() {
  try {
    const chip = execSync('sysctl -n machdep.cpu.brand_string 2>/dev/null', { encoding: 'utf8', timeout: 2000 }).trim();
    return chip.includes('Apple');
  } catch { return false; }
}

function linuxGpuName() {
  try {
    const raw = execSync('nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null', { encoding: 'utf8', timeout: 3000 }).trim();
    if (raw) return raw.split('\n')[0];
  } catch {}
  try {
    const raw = execSync('lspci 2>/dev/null | grep -i vga', { encoding: 'utf8', timeout: 3000 }).trim();
    if (raw) return raw.split(':').pop()?.trim() || null;
  } catch {}
  return null;
}

function detectVendor(name) {
  const n = name.toLowerCase();
  if (n.includes('nvidia'))  return 'NVIDIA';
  if (n.includes('amd') || n.includes('radeon')) return 'AMD';
  if (n.includes('intel'))   return 'Intel';
  if (n.includes('apple'))   return 'Apple';
  return 'Unknown';
}

function parseMessageRecord(line) {
  if (!line?.trim()) return null;
  let obj;
  try {
    obj = JSON.parse(line);
  } catch {
    return null;
  }

  if (!obj.role || !obj.text) return null;
  if (obj.role !== 'user' && obj.role !== 'assistant' && obj.role !== 'thinking') return null;
  return {
    role: obj.role,
    content: obj.text,
    source: obj.source || '',
    timestamp: obj.timestamp || null,
  };
}

function readTranscriptRecent(filePath, limit = 50) {
  if (!fs.existsSync(filePath)) return { messages: [], offset: 0 };
  const text = fs.readFileSync(filePath, 'utf8');
  const lines = text.split(/\r?\n/).filter(Boolean);
  const messages = [];
  for (const line of lines) {
    const rec = parseMessageRecord(line);
    if (rec) messages.push(rec);
  }
  return {
    messages: messages.slice(-limit),
    offset: Buffer.byteLength(text, 'utf8'),
  };
}

function readTranscriptIncrement(filePath, offset = 0) {
  if (!fs.existsSync(filePath)) return { messages: [], offset: 0 };
  const stat = fs.statSync(filePath);
  let safeOffset = Number(offset) || 0;
  if (safeOffset < 0 || safeOffset > stat.size) safeOffset = 0;

  const fd = fs.openSync(filePath, 'r');
  try {
    const len = stat.size - safeOffset;
    if (len <= 0) return { messages: [], offset: stat.size };
    const buffer = Buffer.alloc(len);
    fs.readSync(fd, buffer, 0, len, safeOffset);
    const slice = buffer.toString('utf8');
    const messages = [];
    for (const line of slice.split(/\r?\n/).filter(Boolean)) {
      const rec = parseMessageRecord(line);
      if (rec) messages.push(rec);
    }
    return { messages, offset: stat.size };
  } finally {
    fs.closeSync(fd);
  }
}

function getSessionInfo(agent) {
  return {
    model: agent.model || 'unknown',
    engine: agent.engine || 'unknown',
    updatedAt: null,
  };
}

async function fetchBridgeAgents() {
  const response = await fetch(`${BRIDGE_U_API}/api/agents`);
  if (!response.ok) throw new Error(`bridge-u-f /api/agents failed: ${response.status}`);
  return response.json();
}

async function fetchBridgeHealth() {
  const response = await fetch(`${BRIDGE_U_API}/api/health`);
  if (!response.ok) throw new Error(`bridge-u-f /api/health failed: ${response.status}`);
  return response.json();
}

function captureOutboundProjectMessage(agentId, text) {
  const ctx = parseMinatoContext(text);
  const cleanText = stripHeaders(text);
  if (!ctx) return { ctx: null, cleanText };

  const sessionId = `sess_${Date.now().toString(36)}`;
  agentContextMap[agentId] = { ...ctx, sessionId, pendingUserText: cleanText };
  appendEntry({
    ts: new Date().toISOString(),
    session_id: sessionId,
    direction: 'outbound',
    agent: agentId,
    user: 'user',
    text: cleanText,
    project: ctx.project,
    shimanto_phases: ctx.shimanto_phases || [],
    nagare_workflows: ctx.nagare_workflows || [],
    scope: ctx.scope || '',
  });

  return { ctx, cleanText };
}

async function sendBridgeText(agentId, text) {
  const response = await fetch(`${BRIDGE_U_API}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ agent: agentId, text }),
  });
  const body = await response.text();
  return {
    ok: response.ok,
    status: response.status,
    contentType: response.headers.get('content-type') || 'application/json',
    body,
  };
}

app.use('/api/minato/mcp/v1', createMinatoMcpRouter({
  projectList: async () => ({ projects: listProjects() }),
  logQuery: async ({ project, limit, since }) => {
    const entries = readEntries(project, {
      limit: Math.min(Number(limit || 200), 1000),
      since: since || null,
    });
    return { entries, count: entries.length };
  },
  logAppend: async (payload) => {
    appendLogEntry(payload);
    return { ok: true };
  },
  logProjectChat: async ({ agent, project, limit }) => {
    const response = await fetch(
      `${BRIDGE_U_API}/api/project-chat/${encodeURIComponent(agent)}/${encodeURIComponent(project)}?limit=${Math.max(1, Math.min(Number(limit || 100), 500))}`,
    );
    if (!response.ok) {
      const error = new Error(`Bridge project chat request failed with status ${response.status}`);
      error.status = response.status;
      error.error = await response.text();
      throw error;
    }
    return response.json();
  },
  chatSend: async ({ agentId, text }) => {
    captureOutboundProjectMessage(agentId, text);
    const response = await sendBridgeText(agentId, text);
    if (!response.ok) {
      const error = new Error(`Bridge chat request failed with status ${response.status}`);
      error.status = response.status;
      error.error = response.body;
      throw error;
    }
    try {
      return JSON.parse(response.body);
    } catch {
      return { ok: true, raw: response.body };
    }
  },
  chatGetHistory: async ({ agentId, limit }) => {
    const agent = getAgentMap().get(agentId);
    if (!agent) {
      const error = new Error(`agent '${agentId}' not found`);
      error.status = 404;
      throw error;
    }
    return readTranscriptRecent(agent.transcriptPath, Math.max(1, Math.min(Number(limit || 50), 200)));
  },
  chatPoll: async ({ agentId, offset }) => {
    const agent = getAgentMap().get(agentId);
    if (!agent) {
      const error = new Error(`agent '${agentId}' not found`);
      error.status = 404;
      throw error;
    }
    return readTranscriptIncrement(agent.transcriptPath, Math.max(0, Number(offset || 0)));
  },
}));

app.get('/api/config', async (_req, res) => {
  try {
    const bridge = await fetchBridgeAgents();
    res.json({
      bridgeUApi: BRIDGE_U_API,
      agents: bridge.agents || [],
    });
  } catch (error) {
    res.json({
      bridgeUApi: BRIDGE_U_API,
      agents: getAgents().map((agent) => ({
        ...agent,
        online: false,
        status: 'offline',
      })),
      warning: String(error.message || error),
    });
  }
});

app.get('/api/sessions', (_req, res) => {
  const agents = getAgents();
  const sessions = Object.fromEntries(agents.map((agent) => [agent.id, getSessionInfo(agent)]));
  res.json({ sessions });
});

app.get('/api/transcript/:agentId', (req, res) => {
  const agent = getAgentMap().get(req.params.agentId);
  if (!agent) return res.status(404).json({ error: 'agent not found' });
  const limit = Math.max(1, Math.min(Number(req.query.limit || 50), 200));
  return res.json(readTranscriptRecent(agent.transcriptPath, limit));
});

app.get('/api/transcript/:agentId/poll', (req, res) => {
  const agentId = req.params.agentId;
  const agent = getAgentMap().get(agentId);
  if (!agent) return res.status(404).json({ error: 'agent not found' });
  const offset = Number(req.query.offset || 0);
  const result = readTranscriptIncrement(agent.transcriptPath, offset);

  // Project log: capture inbound assistant replies if this agent has an active context
  const activeCtx = agentContextMap[agentId];
  if (activeCtx && result.messages?.length) {
    for (const msg of result.messages) {
      if (msg.role === 'assistant' && msg.content) {
        const replyText = stripHeaders(msg.content);
        const ts = msg.timestamp || new Date().toISOString();

        // JSONL machine-readable entry
        appendEntry({
          ts,
          session_id: activeCtx.sessionId,
          direction: 'inbound',
          agent: agentId,
          user: 'user',
          text: replyText,
          project: activeCtx.project,
          shimanto_phases: activeCtx.shimanto_phases || [],
          nagare_workflows: activeCtx.nagare_workflows || [],
          scope: activeCtx.scope || '',
        });

        // Markdown human-readable log entry (pairs user question with agent reply)
        appendLogEntry({
          type: 'chat',
          project: activeCtx.project,
          agent: agentId,
          user: 'user',
          ts,
          shimanto_phases: activeCtx.shimanto_phases || [],
          nagare_workflows: activeCtx.nagare_workflows || [],
          scope: activeCtx.scope || '',
          summary: `Conversation with ${agentId}`,
          excerpt: {
            from: activeCtx.pendingUserText || '',
            to: replyText,
          },
        });

        // Clear pending user text after pairing (keep context for further replies)
        activeCtx.pendingUserText = '';
      }
    }
  }

  return res.json(result);
});

app.post('/api/agents/:agentId/metadata', (req, res) => {
  try {
    const agentId = req.params.agentId;
    const displayName = req.body?.display_name;
    const emoji = req.body?.emoji;

    if (displayName === undefined && emoji === undefined) {
      return res.status(400).json({ error: 'display_name or emoji is required' });
    }

    const updated = updateAgentMetadata(agentId, {
      display_name: displayName,
      emoji,
    });

    if (!updated) {
      return res.status(404).json({ error: 'agent not found' });
    }

    return res.json({ ok: true, agent: updated });
  } catch (error) {
    return res.status(500).json({ error: String(error.message || error) });
  }
});

app.get('/api/system', async (_req, res) => {
  const cpuPercent = getCpuUsage() ?? 0;
  const totalMem = os.totalmem();
  const freeMem = os.freemem();
  const gpu = getGpuInfo();

  let bridge = { online: false, status: 'offline', agents: [] };
  try {
    const health = await fetchBridgeHealth();
    bridge = {
      online: Boolean(health.ok),
      status: health.ok ? 'online' : 'offline',
      agents: health.agents || [],
    };
  } catch (error) {
    bridge = {
      online: false,
      status: String(error.message || error),
      agents: [],
    };
  }

  res.json({
    cpuPercent: Number(cpuPercent.toFixed(1)),
    ramUsedGb: Number(((totalMem - freeMem) / 1024 ** 3).toFixed(2)),
    ramTotalGb: Number((totalMem / 1024 ** 3).toFixed(2)),
    gpu,
    bridge,
  });
});

// ─────────────────────────────────────────────────────────
// Project log endpoints (Minato-owned conversation history)
// ─────────────────────────────────────────────────────────

app.get('/api/project-log', (req, res) => {
  const { project, limit, since } = req.query;
  if (!project) return res.status(400).json({ error: 'project is required' });
  const entries = readEntries(project, {
    limit: Math.min(Number(limit || 200), 1000),
    since: since || null,
  });
  return res.json({ entries, count: entries.length });
});

app.get('/api/project-log/list', (_req, res) => {
  return res.json({ projects: listProjects() });
});

/**
 * POST /api/project-log/entry
 * Agents and humans can write structured activity entries to the project Markdown log.
 *
 * Body: {
 *   type:             'chat' | 'action' | 'decision' | 'milestone' | 'note'
 *   project:          string (display name)
 *   agent:            string (optional)
 *   user:             string (optional)
 *   ts:               ISO string (optional)
 *   shimanto_phases:  string[] (optional)
 *   nagare_workflows: string[] (optional)
 *   summary:          string
 *   details:          string | string[] (optional)
 *   excerpt:          { from?: string, to?: string } (optional, for 'chat' type)
 * }
 */
app.post('/api/project-log/entry', (req, res) => {
  try {
    const body = req.body || {};
    if (!body.project) return res.status(400).json({ error: 'project is required' });
    if (!body.summary && !body.details && !body.excerpt) {
      return res.status(400).json({ error: 'at least one of summary, details, or excerpt is required' });
    }
    appendLogEntry(body);
    return res.json({ ok: true });
  } catch (err) {
    return res.status(500).json({ error: String(err.message || err) });
  }
});

app.post('/api/agents/:agentId/command', async (req, res) => {
  try {
    const { agentId } = req.params;
    const response = await fetch(`${BRIDGE_U_API}/api/agents/${encodeURIComponent(agentId)}/command`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req.body),
    });
    const body = await response.text();
    return res.status(response.status).type(response.headers.get('content-type') || 'application/json').send(body);
  } catch (error) {
    return res.status(500).json({ error: String(error.message || error) });
  }
});

app.post('/api/chat', upload.any(), async (req, res) => {
  try {
    const contentType = req.headers['content-type'] || '';
    const isMultipart = contentType.startsWith('multipart/form-data');

    if (isMultipart) {
      const agentId = req.body.agentId || req.body.agent;
      const text = req.body.text || '';
      const caption = req.body.caption || '';
      const mediaType = req.body.media_type || '';
      const stickerEmoji = req.body.sticker_emoji || '';

      if (!agentId) return res.status(400).json({ error: 'agentId is required' });

      const form = new FormData();
      form.append('agent', agentId);
      if (text) form.append('text', text);
      if (caption) form.append('caption', caption);
      if (mediaType) form.append('media_type', mediaType);
      if (stickerEmoji) form.append('sticker_emoji', stickerEmoji);

      for (const file of req.files || []) {
        const blob = new Blob([file.buffer], { type: file.mimetype || 'application/octet-stream' });
        form.append('files', blob, file.originalname || 'upload.bin');
      }

      const response = await fetch(`${BRIDGE_U_API}/api/chat`, {
        method: 'POST',
        body: form,
      });
      const body = await response.text();
      return res.status(response.status).type(response.headers.get('content-type') || 'application/json').send(body);
    }

    const { agentId, text } = req.body || {};
    if (!agentId || !text) return res.status(400).json({ error: 'agentId and text are required' });

    captureOutboundProjectMessage(agentId, text);

    const response = await sendBridgeText(agentId, text);
    return res.status(response.status).type(response.contentType).send(response.body);
  } catch (error) {
    return res.status(500).json({ error: String(error.message || error) });
  }
});

const server = app.listen(PORT, () => {
  console.log(`Workbench API listening on http://localhost:${PORT}`);
  console.log(`bridge-u-f API: ${BRIDGE_U_API}`);
});

server.on('error', (err) => {
  console.error('Server error:', err);
});
