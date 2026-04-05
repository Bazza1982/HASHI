/**
 * project_log.js — Minato-owned project conversation log.
 *
 * Writes every user↔agent exchange to:
 *   workbench/data/projects/{slug}/conversations/{YYYY-MM-DD}.jsonl
 *
 * This storage lives on the local machine and is completely independent of
 * the HASHI Python backend or agent transcripts. It is the authoritative
 * record of project conversations — auditable, portable, and readable by
 * future agents as project context.
 *
 * Each JSONL entry:
 * {
 *   ts:               ISO timestamp,
 *   session_id:       string (groups messages in one logical exchange),
 *   direction:        "outbound" | "inbound",
 *   agent:            agent id,
 *   user:             user identifier (default "user"),
 *   text:             message text (MINATO CONTEXT header stripped),
 *   project:          project display name,
 *   shimanto_phases:  string[],
 *   nagare_workflows: string[],
 *   scope:            string,
 * }
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Store under workbench/data/ — on MINATO's local disk, independent of HASHI
const DATA_DIR = path.resolve(__dirname, '..', 'data', 'projects');

const CONTEXT_RE = /\[MINATO CONTEXT[^\]]*\]([\s\S]*?)\[END CONTEXT\]/;
const HCHAT_HEADER_RE = /^\[hchat(?:\s+reply)?\s+from\s+[^\]]+\]\s*/;

// ─────────────────────────────────────────────────────────
// Parsing
// ─────────────────────────────────────────────────────────

export function parseMinatoContext(text) {
  const m = CONTEXT_RE.exec(text || '');
  if (!m) return null;
  const ctx = {};
  for (const line of m[1].split('\n')) {
    const l = line.trim();
    if (l.startsWith('minato active project:')) {
      ctx.project = l.split(':', 2)[1].trim();
    } else if (l.startsWith('shimanto phases:')) {
      ctx.shimanto_phases = l.split(':', 2)[1].trim().split(',').map((s) => s.trim()).filter(Boolean);
    } else if (l.startsWith('nagare workflows:')) {
      const raw = l.split(':', 2)[1].trim().replace(/\s*\d+\s*workflow\(s\)/, '').trim().replace(/,$/, '');
      ctx.nagare_workflows = raw ? raw.split(',').map((s) => s.trim()).filter((s) => s && s !== '0') : [];
    } else if (l.startsWith('scope:')) {
      ctx.scope = l.split(':', 2)[1].trim();
    }
  }
  return ctx.project ? ctx : null;
}

export function stripHeaders(text) {
  // Remove MINATO CONTEXT block and hchat header from stored text
  return (text || '')
    .replace(CONTEXT_RE, '')
    .replace(HCHAT_HEADER_RE, '')
    .trim();
}

export function projectSlug(name) {
  return (name || '')
    .toLowerCase()
    .replace(/['"]/g, '')
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    || 'default';
}

// ─────────────────────────────────────────────────────────
// Storage
// ─────────────────────────────────────────────────────────

function todayStr() {
  return new Date().toISOString().slice(0, 10);
}

function conversationsDir(slug) {
  return path.join(DATA_DIR, slug, 'conversations');
}

function logDir(slug) {
  return path.join(DATA_DIR, slug, 'log');
}

// ─────────────────────────────────────────────────────────
// Markdown activity log
// ─────────────────────────────────────────────────────────

const LOG_ICONS = {
  chat:      '💬',
  action:    '⚙️',
  decision:  '✅',
  milestone: '📌',
  note:      '📝',
};

function timeStr(ts) {
  const d = ts ? new Date(ts) : new Date();
  return d.toLocaleTimeString('en-AU', { hour: '2-digit', minute: '2-digit', hour12: false });
}

/**
 * Ensure today's log file exists with YAML frontmatter.
 * Returns the file path.
 */
function ensureLogFile(slug, projectName, date) {
  const dir = logDir(slug);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, `${date}.md`);
  if (!fs.existsSync(file)) {
    const frontmatter = [
      '---',
      `project: "${projectName}"`,
      `date: ${date}`,
      `agents: []`,
      `participants: []`,
      `tags: ["#project/${slug}"]`,
      '---',
      '',
      `# ${projectName} — ${date}`,
      '',
    ].join('\n');
    fs.writeFileSync(file, frontmatter, 'utf8');
  }
  return file;
}

/**
 * Append a structured activity entry to the project's daily Markdown log.
 *
 * entry fields:
 *   type          — 'chat' | 'action' | 'decision' | 'milestone' | 'note'
 *   project       — display name
 *   agent         — agent id (optional)
 *   user          — human id (optional, default 'user')
 *   ts            — ISO timestamp (optional, defaults to now)
 *   shimanto_phases  — string[]
 *   nagare_workflows — string[]
 *   summary       — one-line summary (required)
 *   details       — freeform string or array of strings (optional)
 *   excerpt       — for 'chat': { from: text, to: text } (optional)
 */
export function appendLogEntry(entry) {
  const {
    type = 'note',
    project,
    agent,
    user = 'user',
    ts,
    shimanto_phases = [],
    nagare_workflows = [],
    summary = '',
    details,
    excerpt,
  } = entry;

  if (!project) return;

  const slug = projectSlug(project);
  const date = (ts || new Date().toISOString()).slice(0, 10);
  const time = timeStr(ts);
  const icon = LOG_ICONS[type] || '📝';
  const actor = agent || user;
  const file = ensureLogFile(slug, project, date);

  const lines = ['', `## ${time} · ${icon} ${type.charAt(0).toUpperCase() + type.slice(1)} · ${actor}`, ''];

  // Metadata line
  const meta = [];
  if (shimanto_phases.length) meta.push(`**Shimanto:** ${shimanto_phases.join(' · ')}`);
  if (nagare_workflows.length) meta.push(`**Nagare:** ${nagare_workflows.join(', ')}`);
  if (meta.length) lines.push(meta.join('  \n') + '  ');

  if (summary) lines.push(`**${summary}**`, '');

  // Chat excerpt block
  if (type === 'chat' && excerpt) {
    if (excerpt.from) lines.push(`> **${user} →** ${excerpt.from.slice(0, 200)}${excerpt.from.length > 200 ? '…' : ''}`);
    if (excerpt.to) lines.push(`> **${actor} →** ${excerpt.to.slice(0, 200)}${excerpt.to.length > 200 ? '…' : ''}`);
    lines.push('');
  }

  // Details block
  if (details) {
    const detailLines = Array.isArray(details) ? details : String(details).split('\n');
    for (const line of detailLines) lines.push(line);
    lines.push('');
  }

  lines.push('---');

  fs.appendFileSync(file, lines.join('\n') + '\n', 'utf8');
}

export function appendEntry(entry) {
  const slug = projectSlug(entry.project);
  const dir = conversationsDir(slug);
  fs.mkdirSync(dir, { recursive: true });
  const file = path.join(dir, `${todayStr()}.jsonl`);
  fs.appendFileSync(file, JSON.stringify(entry) + '\n', 'utf8');
}

export function readEntries(projectName, { limit = 100, since = null } = {}) {
  const slug = projectSlug(projectName);
  const dir = conversationsDir(slug);
  if (!fs.existsSync(dir)) return [];

  const files = fs.readdirSync(dir)
    .filter((f) => f.endsWith('.jsonl'))
    .sort(); // chronological

  const entries = [];
  for (const file of files) {
    // Skip files older than 'since' date string (YYYY-MM-DD prefix)
    if (since && file.slice(0, 10) < since) continue;
    const text = fs.readFileSync(path.join(dir, file), 'utf8');
    for (const line of text.split('\n').filter(Boolean)) {
      try { entries.push(JSON.parse(line)); } catch { /* skip malformed lines */ }
    }
  }
  return entries.slice(-limit);
}

export function listProjects() {
  if (!fs.existsSync(DATA_DIR)) return [];
  return fs.readdirSync(DATA_DIR)
    .filter((f) => fs.statSync(path.join(DATA_DIR, f)).isDirectory())
    .map((slug) => {
      // Try to find the display name from any entry
      const dir = conversationsDir(slug);
      if (!fs.existsSync(dir)) return { slug, name: slug };
      const files = fs.readdirSync(dir).filter((f) => f.endsWith('.jsonl')).sort().reverse();
      for (const file of files.slice(0, 3)) {
        const text = fs.readFileSync(path.join(dir, file), 'utf8');
        const lines = text.split('\n').filter(Boolean);
        for (const line of lines.slice(-10).reverse()) {
          try {
            const entry = JSON.parse(line);
            if (entry.project) return { slug, name: entry.project };
          } catch { /* skip */ }
        }
      }
      return { slug, name: slug };
    });
}
