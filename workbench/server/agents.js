import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const DEFAULT_AGENTS_JSON = path.resolve(__dirname, '..', '..', 'agents.json');

export const AGENTS_JSON = process.env.BRIDGE_U_AGENTS_JSON || DEFAULT_AGENTS_JSON;
const BRIDGE_U_ROOT = path.dirname(AGENTS_JSON);

function loadConfig() {
  return JSON.parse(fs.readFileSync(AGENTS_JSON, 'utf-8'));
}

function escapeJsonUnicode(value) {
  return value.replace(/[^\u0020-\u007e]/g, (char) => {
    let escaped = '';
    for (let i = 0; i < char.length; i += 1) {
      escaped += `\\u${char.charCodeAt(i).toString(16).padStart(4, '0')}`;
    }
    return escaped;
  });
}

function saveConfig(data) {
  const json = JSON.stringify(data, null, 2);
  const escaped = json.replace(/"(?:\\.|[^"\\])*"/g, (match) => escapeJsonUnicode(match));
  fs.writeFileSync(AGENTS_JSON, `${escaped}\n`, 'utf-8');
}

function normalizeEmojiInput(value) {
  const raw = String(value || '').trim();
  if (!raw) return '🤖';

  const directCodePoint = raw.match(/^U\+([0-9A-F]{4,6})$/i);
  if (directCodePoint) {
    const parsed = Number.parseInt(directCodePoint[1], 16);
    if (Number.isFinite(parsed)) return String.fromCodePoint(parsed);
  }

  const slashUCodePoint = raw.match(/^\\u\{([0-9A-F]{4,6})\}$/i);
  if (slashUCodePoint) {
    const parsed = Number.parseInt(slashUCodePoint[1], 16);
    if (Number.isFinite(parsed)) return String.fromCodePoint(parsed);
  }

  return raw;
}

function modelForAgent(agent) {
  if (agent.model) return agent.model;
  if (agent.type === 'flex') {
    const active = agent.active_backend;
    const match = (agent.allowed_backends || []).find((backend) => backend.engine === active);
    return match?.model || 'unknown';
  }
  return 'unknown';
}

function transcriptPathForAgent(agent) {
  const transcriptName = agent.type === 'flex' ? 'transcript.jsonl' : 'conversation_log.jsonl';
  return path.join(BRIDGE_U_ROOT, agent.workspace_dir, transcriptName);
}

export function getAgents() {
  const data = loadConfig();
  return (data.agents || [])
    .filter((agent) => agent.is_active !== false)
    .map((agent) => ({
      id: agent.name,
      name: agent.name,
      displayName: agent.display_name || agent.name,
      emoji: agent.emoji || '🤖',
      engine: agent.engine || agent.active_backend || 'unknown',
      model: modelForAgent(agent),
      type: agent.type || 'fixed',
      workspaceDir: path.join(BRIDGE_U_ROOT, agent.workspace_dir),
      transcriptPath: transcriptPathForAgent(agent),
    }));
}

export function getAgentMap() {
  return new Map(getAgents().map((agent) => [agent.id, agent]));
}

export function updateAgentMetadata(agentId, updates = {}) {
  const data = loadConfig();
  const agents = data.agents || [];
  const agent = agents.find((item) => item.name === agentId);
  if (!agent) return null;

  if (Object.hasOwn(updates, 'display_name')) {
    const value = String(updates.display_name || '').trim();
    agent.display_name = value || agent.name;
  }

  if (Object.hasOwn(updates, 'emoji')) {
    agent.emoji = normalizeEmojiInput(updates.emoji);
  }

  saveConfig(data);

  return {
    id: agent.name,
    name: agent.name,
    displayName: agent.display_name || agent.name,
    emoji: agent.emoji || '🤖',
    engine: agent.engine || agent.active_backend || 'unknown',
    model: modelForAgent(agent),
    type: agent.type || 'fixed',
    workspaceDir: path.join(BRIDGE_U_ROOT, agent.workspace_dir),
    transcriptPath: transcriptPathForAgent(agent),
  };
}
