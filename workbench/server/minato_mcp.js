import express from 'express';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { listProjects as defaultListProjects, projectSlug, readEntries } from './project_log.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const WORKBENCH_DIR = path.resolve(__dirname, '..');
const REPO_ROOT = path.resolve(WORKBENCH_DIR, '..');
const DOCS_DIR = path.join(REPO_ROOT, 'docs');
const FLOW_DIR = path.join(REPO_ROOT, 'flow');
const FLOW_WORKFLOWS_DIR = path.join(FLOW_DIR, 'workflows');
const FLOW_RUNS_DIR = path.join(FLOW_DIR, 'runs');
const AUDIT_DIR = path.join(WORKBENCH_DIR, 'data');
const PROJECTS_DIR = path.join(AUDIT_DIR, 'projects');
const AUDIT_FILE = path.join(AUDIT_DIR, 'minato_mcp_audit.jsonl');
const ARTEFACTS_FILE = path.join(AUDIT_DIR, 'minato_artefacts.json');

const ERROR_CODES = {
  INVALID_REQUEST: -32600,
  METHOD_NOT_FOUND: -32601,
  INVALID_PARAMS: -32602,
  NOT_FOUND: -32010,
  PERMISSION_DENIED: -32011,
  UPSTREAM_ERROR: -32012,
  CONFLICT: -32013,
  INTERNAL_ERROR: -32099,
};

function ok(id, result) {
  return { jsonrpc: '2.0', id: id ?? null, result };
}

function err(id, message, data = null) {
  const code = ERROR_CODES[message] ?? ERROR_CODES.INTERNAL_ERROR;
  return {
    jsonrpc: '2.0',
    id: id ?? null,
    error: {
      code,
      message,
      ...(data ? { data } : {}),
    },
  };
}

function createRpcError(message, data = null) {
  const error = new Error(message);
  error.rpcMessage = message;
  error.rpcData = data || null;
  return error;
}

function appendAuditRecord(record) {
  fs.mkdirSync(AUDIT_DIR, { recursive: true });
  fs.appendFileSync(AUDIT_FILE, JSON.stringify(record) + '\n', 'utf8');
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function expectObject(value, field = 'arguments') {
  if (!isPlainObject(value)) {
    throw createRpcError('INVALID_PARAMS', {
      field,
      reason: `${field} must be an object`,
    });
  }
  return value;
}

function requireString(value, field) {
  if (typeof value !== 'string' || !value.trim()) {
    throw createRpcError('INVALID_PARAMS', {
      field,
      reason: `${field} is required`,
    });
  }
  return value.trim();
}

function optionalString(value, field) {
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value !== 'string') {
    throw createRpcError('INVALID_PARAMS', {
      field,
      reason: `${field} must be a string`,
    });
  }
  return value.trim();
}

function optionalNumber(value, field, { min = null, max = null, integer = false } = {}) {
  if (value === undefined || value === null || value === '') return undefined;
  if (typeof value !== 'number' || Number.isNaN(value)) {
    throw createRpcError('INVALID_PARAMS', {
      field,
      reason: `${field} must be a number`,
    });
  }
  if (integer && !Number.isInteger(value)) {
    throw createRpcError('INVALID_PARAMS', {
      field,
      reason: `${field} must be an integer`,
    });
  }
  if (min !== null && value < min) {
    throw createRpcError('INVALID_PARAMS', {
      field,
      reason: `${field} must be >= ${min}`,
    });
  }
  if (max !== null && value > max) {
    throw createRpcError('INVALID_PARAMS', {
      field,
      reason: `${field} must be <= ${max}`,
    });
  }
  return value;
}

function optionalBoolean(value, field) {
  if (value === undefined || value === null) return undefined;
  if (typeof value !== 'boolean') {
    throw createRpcError('INVALID_PARAMS', {
      field,
      reason: `${field} must be a boolean`,
    });
  }
  return value;
}

function optionalStringArray(value, field) {
  if (value === undefined || value === null) return undefined;
  if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
    throw createRpcError('INVALID_PARAMS', {
      field,
      reason: `${field} must be an array of strings`,
    });
  }
  return value.map((item) => item.trim()).filter(Boolean);
}

function optionalExcerpt(value) {
  if (value === undefined || value === null) return undefined;
  if (!isPlainObject(value)) {
    throw createRpcError('INVALID_PARAMS', {
      field: 'excerpt',
      reason: 'excerpt must be an object',
    });
  }

  const excerpt = {};
  if (value.from !== undefined) excerpt.from = requireString(value.from, 'excerpt.from');
  if (value.to !== undefined) excerpt.to = requireString(value.to, 'excerpt.to');
  if (!excerpt.from && !excerpt.to) {
    throw createRpcError('INVALID_PARAMS', {
      field: 'excerpt',
      reason: 'excerpt must contain from or to',
    });
  }
  return excerpt;
}

function optionalDetails(value) {
  if (value === undefined || value === null) return undefined;
  if (typeof value === 'string') return value;
  if (Array.isArray(value) && value.every((item) => typeof item === 'string')) return value;
  throw createRpcError('INVALID_PARAMS', {
    field: 'details',
    reason: 'details must be a string or string[]',
  });
}

function makeMinatoContextHeader(args) {
  const lines = ['[MINATO CONTEXT]'];
  lines.push(`minato active project: ${args.project}`);

  if (args.shimanto_phases?.length) {
    lines.push(`shimanto phases: ${args.shimanto_phases.join(', ')}`);
  }
  if (args.nagare_workflows?.length) {
    lines.push(`nagare workflows: ${args.nagare_workflows.join(', ')}`);
  }
  if (args.scope) {
    lines.push(`scope: ${args.scope}`);
  }

  lines.push('[END CONTEXT]', '', args.text);
  return lines.join('\n');
}

function normalizeUpstreamError(error, fallbackField = null) {
  if (error?.rpcMessage) return error;

  const upstreamStatus = Number(error?.status);
  if (upstreamStatus === 404) {
    return createRpcError('NOT_FOUND', {
      field: fallbackField,
      reason: String(error?.message || error?.error || 'Upstream resource not found'),
    });
  }
  if (upstreamStatus === 409) {
    return createRpcError('CONFLICT', {
      field: fallbackField,
      reason: String(error?.message || error?.error || 'Upstream conflict'),
    });
  }

  return createRpcError('UPSTREAM_ERROR', {
    field: fallbackField,
    reason: String(error?.message || error?.error || error || 'Upstream request failed'),
    ...(upstreamStatus ? { status: upstreamStatus } : {}),
  });
}

function uniqStrings(values) {
  return [...new Set((values || []).filter(Boolean))];
}

function deepClone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function nowIso() {
  return new Date().toISOString();
}

function todayDateStr() {
  return new Date().toISOString().slice(0, 10);
}

function collectFiles(dir, predicate) {
  if (!fs.existsSync(dir)) return [];
  const results = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      results.push(...collectFiles(fullPath, predicate));
    } else if (predicate(fullPath, entry.name)) {
      results.push(fullPath);
    }
  }
  return results.sort();
}

function ensureArtefactStore() {
  fs.mkdirSync(AUDIT_DIR, { recursive: true });
  if (!fs.existsSync(ARTEFACTS_FILE)) {
    fs.writeFileSync(ARTEFACTS_FILE, JSON.stringify({ last_id: 0, artefacts: [] }, null, 2) + '\n', 'utf8');
  }
}

function readArtefactStore() {
  ensureArtefactStore();
  try {
    const parsed = JSON.parse(fs.readFileSync(ARTEFACTS_FILE, 'utf8'));
    return {
      last_id: Number(parsed?.last_id) || 0,
      artefacts: Array.isArray(parsed?.artefacts) ? parsed.artefacts : [],
    };
  } catch {
    return { last_id: 0, artefacts: [] };
  }
}

function writeArtefactStore(store) {
  ensureArtefactStore();
  fs.writeFileSync(ARTEFACTS_FILE, JSON.stringify(store, null, 2) + '\n', 'utf8');
}

function nextArtefactId(lastId) {
  return `art_${String(lastId + 1).padStart(3, '0')}`;
}

function normalizeArtefactRecord(record) {
  return {
    artefact_id: record.artefact_id,
    name: record.name,
    type: record.type,
    project: record.project,
    linked_projects: uniqStrings(record.linked_projects || [record.project].filter(Boolean)),
    path: record.path || null,
    kasumi_id: record.kasumi_id || null,
    kasumi_module: record.kasumi_module || null,
    nagare_step: record.nagare_step || null,
    linked_nagare_steps: uniqStrings(record.linked_nagare_steps || [record.nagare_step].filter(Boolean)),
    shimanto_phase: record.shimanto_phase || null,
    linked_shimanto_phases: uniqStrings(record.linked_shimanto_phases || [record.shimanto_phase].filter(Boolean)),
    note: record.note || '',
    linked_at: record.linked_at || record.created_at || null,
    created_at: record.created_at || record.linked_at || null,
    updated_at: record.updated_at || record.created_at || null,
  };
}

function listArtefactsForProject(projectName, { type = 'any' } = {}) {
  const project = requireString(projectName, 'project');
  const normalizedType = optionalString(type, 'type') || 'any';
  if (!['any', 'file', 'kasumi'].includes(normalizedType)) {
    throw createRpcError('INVALID_PARAMS', {
      field: 'type',
      reason: 'type must be one of any, file, kasumi',
    });
  }

  const store = readArtefactStore();
  const artefacts = store.artefacts
    .map(normalizeArtefactRecord)
    .filter((item) => item.linked_projects.includes(project))
    .filter((item) => normalizedType === 'any' || item.type === normalizedType)
    .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));

  return { project, artefacts };
}

function createArtefactRecord(payload) {
  const type = requireString(payload.type, 'type');
  if (!['file', 'kasumi'].includes(type)) {
    throw createRpcError('INVALID_PARAMS', {
      field: 'type',
      reason: 'type must be file or kasumi',
    });
  }

  const project = requireString(payload.project, 'project');
  const name = requireString(payload.name, 'name');
  const nagareStep = optionalString(payload.nagare_step, 'nagare_step');
  const shimantoPhase = optionalString(payload.shimanto_phase, 'shimanto_phase');
  const note = optionalString(payload.note, 'note') || '';

  const record = {
    name,
    type,
    project,
    linked_projects: [project],
    nagare_step: nagareStep || null,
    linked_nagare_steps: nagareStep ? [nagareStep] : [],
    shimanto_phase: shimantoPhase || null,
    linked_shimanto_phases: shimantoPhase ? [shimantoPhase] : [],
    note,
    linked_at: nowIso(),
    created_at: nowIso(),
    updated_at: nowIso(),
  };

  if (type === 'file') {
    const filePath = requireString(payload.path, 'path');
    record.path = path.resolve(filePath);
  } else {
    record.kasumi_id = requireString(payload.kasumi_id, 'kasumi_id');
    const kasumiModule = requireString(payload.kasumi_module, 'kasumi_module');
    if (!['nexcel', 'wordo'].includes(kasumiModule)) {
      throw createRpcError('INVALID_PARAMS', {
        field: 'kasumi_module',
        reason: 'kasumi_module must be nexcel or wordo',
      });
    }
    record.kasumi_module = kasumiModule;
  }

  const store = readArtefactStore();
  const artefactId = nextArtefactId(store.last_id);
  const finalRecord = normalizeArtefactRecord({
    ...record,
    artefact_id: artefactId,
  });
  store.last_id += 1;
  store.artefacts.push(finalRecord);
  writeArtefactStore(store);
  return finalRecord;
}

function getArtefactRecord(artefactId) {
  const requested = requireString(artefactId, 'artefact_id');
  const store = readArtefactStore();
  const match = store.artefacts.find((item) => item.artefact_id === requested);
  if (!match) {
    throw createRpcError('NOT_FOUND', {
      field: 'artefact_id',
      reason: `Unknown artefact '${artefactId}'`,
    });
  }
  return normalizeArtefactRecord(match);
}

function updateArtefactRecord(artefactId, updater) {
  const requested = requireString(artefactId, 'artefact_id');
  const store = readArtefactStore();
  const index = store.artefacts.findIndex((item) => item.artefact_id === requested);
  if (index === -1) {
    throw createRpcError('NOT_FOUND', {
      field: 'artefact_id',
      reason: `Unknown artefact '${artefactId}'`,
    });
  }
  const current = normalizeArtefactRecord(store.artefacts[index]);
  const updated = normalizeArtefactRecord({
    ...current,
    ...updater(current),
    updated_at: nowIso(),
  });
  store.artefacts[index] = updated;
  writeArtefactStore(store);
  return updated;
}

function readFilesystemArtefact(record) {
  const resolvedPath = requireString(record.path, 'path');
  if (!fs.existsSync(resolvedPath)) {
    throw createRpcError('NOT_FOUND', {
      field: 'path',
      reason: `Artefact file does not exist: ${resolvedPath}`,
    });
  }

  const stat = fs.statSync(resolvedPath);
  if (!stat.isFile()) {
    throw createRpcError('CONFLICT', {
      field: 'path',
      reason: `Artefact path is not a file: ${resolvedPath}`,
    });
  }

  const ext = path.extname(resolvedPath).toLowerCase();
  const isTextLike = ['.txt', '.md', '.json', '.jsonl', '.yaml', '.yml', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.py', '.html', '.css', '.csv'].includes(ext);
  const content = isTextLike ? fs.readFileSync(resolvedPath, 'utf8') : null;

  return {
    artefact_id: record.artefact_id,
    name: record.name,
    type: record.type,
    path: resolvedPath,
    size_bytes: stat.size,
    mime_type: isTextLike ? 'text/plain' : 'application/octet-stream',
    content,
  };
}

function readKasumiArtefact(record, deps) {
  if (typeof deps.kasumiRead !== 'function') {
    throw createRpcError('UPSTREAM_ERROR', {
      field: 'artefact_id',
      reason: 'KASUMI artefact delegation is not configured',
      artefact_id: record.artefact_id,
      kasumi_id: record.kasumi_id,
      kasumi_module: record.kasumi_module,
    });
  }

  return deps.kasumiRead(record);
}

function updateRunStateStep({ runId, stepId, status, note }) {
  const allowed = ['completed', 'failed', 'pending', 'skipped'];
  if (!allowed.includes(status)) {
    throw createRpcError('INVALID_PARAMS', {
      field: 'status',
      reason: `status must be one of ${allowed.join(', ')}`,
    });
  }

  const runStateRecord = resolveRunState({ runId, workflowId: 'run' });
  const state = deepClone(runStateRecord.state);
  if (!isPlainObject(state.steps?.[stepId])) {
    throw createRpcError('NOT_FOUND', {
      field: 'step_id',
      reason: `Unknown step '${stepId}' in run '${runId}'`,
    });
  }

  const ts = nowIso();
  const step = state.steps[stepId];
  step.status = status;
  step.updated_at = ts;
  if ((status === 'completed' || status === 'failed' || status === 'skipped') && !step.ended_at) {
    step.ended_at = ts;
  }
  if (status === 'pending') {
    delete step.ended_at;
  }
  if (note) {
    step.note = note;
  }

  state.updated_at = ts;
  state.human_interventions = Array.isArray(state.human_interventions) ? state.human_interventions : [];
  state.human_interventions.push({
    ts,
    step_id: stepId,
    status,
    note: note || '',
    source: 'minato_mcp',
  });

  const stepStatuses = Object.values(state.steps || {}).map((item) => item.status);
  if (stepStatuses.length && stepStatuses.every((item) => item === 'completed' || item === 'skipped')) {
    state.workflow_status = 'completed';
    state.ended_at = ts;
  } else if (stepStatuses.some((item) => item === 'failed')) {
    state.workflow_status = 'failed';
    state.ended_at = ts;
  } else if (stepStatuses.some((item) => item === 'pending')) {
    state.workflow_status = 'running';
    delete state.ended_at;
  }

  fs.writeFileSync(runStateRecord.path, JSON.stringify(state, null, 2) + '\n', 'utf8');
  const workflowId = String(state.run_id || '').replace(/^run-/, '').replace(/-\d{8}-\d{6}$/, '') || 'unknown';
  return normalizeRunState(workflowId, {
    ...runStateRecord,
    state,
  });
}

function stripQuotes(value) {
  return String(value || '').trim().replace(/^['"]|['"]$/g, '');
}

function parseInlineYamlArray(value) {
  const raw = stripQuotes(value);
  if (!raw.startsWith('[') || !raw.endsWith(']')) return [];
  return raw
    .slice(1, -1)
    .split(',')
    .map((item) => stripQuotes(item))
    .filter(Boolean);
}

function parseWorkflowYamlSummary(content) {
  const lines = String(content || '').split(/\r?\n/);
  const summary = {
    workflow_id: null,
    name: null,
    version: null,
    description: null,
    steps: [],
    pre_flight_questions: [],
  };

  let section = null;
  let currentStep = null;
  let currentQuestion = null;
  let inCollect = false;

  for (const rawLine of lines) {
    const line = rawLine.replace(/\t/g, '    ');
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;

    if (!line.startsWith(' ')) {
      section = trimmed.replace(/:$/, '');
      currentStep = null;
      currentQuestion = null;
      inCollect = false;
      continue;
    }

    if (section === 'workflow') {
      const match = line.match(/^\s{2}([a-z_]+):\s*(.+)\s*$/);
      if (match) {
        const [, key, value] = match;
        if (key === 'id') summary.workflow_id = stripQuotes(value);
        if (key === 'name') summary.name = stripQuotes(value);
        if (key === 'version') summary.version = stripQuotes(value);
        if (key === 'description') summary.description = stripQuotes(value);
      }
      continue;
    }

    if (section === 'pre_flight') {
      if (/^\s{2}collect_from_human:\s*$/.test(line)) {
        inCollect = true;
        currentQuestion = null;
        continue;
      }
      if (!inCollect) continue;

      const startMatch = line.match(/^\s{4}-\s+key:\s*(.+)\s*$/);
      if (startMatch) {
        currentQuestion = { key: stripQuotes(startMatch[1]) };
        summary.pre_flight_questions.push(currentQuestion);
        continue;
      }

      if (!currentQuestion) continue;
      const questionMatch = line.match(/^\s{6}question:\s*(.+)\s*$/);
      if (questionMatch) {
        currentQuestion.question = stripQuotes(questionMatch[1]);
        continue;
      }
      const typeMatch = line.match(/^\s{6}type:\s*(.+)\s*$/);
      if (typeMatch) {
        currentQuestion.type = stripQuotes(typeMatch[1]);
        continue;
      }
      const requiredMatch = line.match(/^\s{6}required:\s*(.+)\s*$/);
      if (requiredMatch) {
        currentQuestion.required = stripQuotes(requiredMatch[1]) === 'true';
      }
      continue;
    }

    if (section === 'steps') {
      const startMatch = line.match(/^\s{2}-\s+id:\s*(.+)\s*$/);
      if (startMatch) {
        currentStep = {
          id: stripQuotes(startMatch[1]),
          name: null,
          agent: null,
          depends: [],
        };
        summary.steps.push(currentStep);
        continue;
      }
      if (!currentStep) continue;

      const kvMatch = line.match(/^\s{4}([a-z_]+):\s*(.+)\s*$/);
      if (!kvMatch) continue;
      const [, key, value] = kvMatch;
      if (key === 'name') currentStep.name = stripQuotes(value);
      if (key === 'agent') currentStep.agent = stripQuotes(value);
      if (key === 'depends') currentStep.depends = parseInlineYamlArray(value);
    }
  }

  return summary;
}

function listWorkflowDefinitions() {
  return collectFiles(FLOW_WORKFLOWS_DIR, (_fullPath, name) => name.endsWith('.yaml') || name.endsWith('.yml'))
    .map((filePath) => {
      const content = fs.readFileSync(filePath, 'utf8');
      const parsed = parseWorkflowYamlSummary(content);
      return {
        workflow_id: parsed.workflow_id || path.basename(filePath, path.extname(filePath)),
        name: parsed.name || path.basename(filePath, path.extname(filePath)),
        version: parsed.version || null,
        description: parsed.description || null,
        steps: parsed.steps,
        pre_flight_questions: parsed.pre_flight_questions,
        path: filePath,
        raw_yaml: content,
      };
    });
}

function findWorkflowDefinition(workflowId) {
  const requested = requireString(workflowId, 'workflow_id').toLowerCase();
  const workflows = listWorkflowDefinitions();
  const workflow = workflows.find((item) => item.workflow_id.toLowerCase() === requested);
  if (!workflow) {
    throw createRpcError('NOT_FOUND', {
      field: 'workflow_id',
      reason: `Unknown workflow '${workflowId}'`,
    });
  }
  return workflow;
}

function listRunStates() {
  return collectFiles(FLOW_RUNS_DIR, (fullPath, name) => name === 'state.json')
    .map((filePath) => {
      const parsed = JSON.parse(fs.readFileSync(filePath, 'utf8'));
      return {
        path: filePath,
        dir: path.dirname(filePath),
        state: parsed,
      };
    })
    .sort((a, b) => {
      const aTs = a.state.updated_at || a.state.started_at || a.state.created_at || '';
      const bTs = b.state.updated_at || b.state.started_at || b.state.created_at || '';
      return bTs.localeCompare(aTs);
    });
}

function resolveRunState({ workflowId, runId }) {
  const runs = listRunStates();
  if (runId) {
    const exact = runs.find((item) => item.state.run_id === runId);
    if (!exact) {
      throw createRpcError('NOT_FOUND', {
        field: 'run_id',
        reason: `Unknown run '${runId}'`,
      });
    }
    return exact;
  }

  const expected = requireString(workflowId, 'workflow_id').toLowerCase();
  const match = runs.find((item) => String(item.state.run_id || '').toLowerCase().includes(expected));
  if (!match) {
    throw createRpcError('NOT_FOUND', {
      field: 'workflow_id',
      reason: `No run state found for workflow '${workflowId}'`,
    });
  }
  return match;
}

function normalizeRunState(workflowId, runStateRecord) {
  const state = runStateRecord.state || {};
  const steps = Object.entries(state.steps || {}).map(([id, step]) => ({
    id,
    status: step.status || 'unknown',
    started_at: step.started_at || null,
    updated_at: step.updated_at || null,
    completed_at: step.ended_at || null,
    ended_at: step.ended_at || null,
    note: step.note || null,
    artifacts: step.artifacts || step.artifacts_produced || null,
  }));

  return {
    run_id: state.run_id || path.basename(runStateRecord.dir),
    workflow_id: workflowId,
    status: state.workflow_status || 'unknown',
    started_at: state.started_at || state.created_at || null,
    created_at: state.created_at || null,
    updated_at: state.updated_at || null,
    ended_at: state.ended_at || null,
    error_count: state.error_count ?? 0,
    human_interventions: state.human_interventions || [],
    steps,
    path: runStateRecord.path,
  };
}

function listDocs() {
  return collectFiles(DOCS_DIR, (_fullPath, name) => name.endsWith('.md')).map((filePath) => ({
    name: path.basename(filePath, '.md'),
    path: filePath,
  }));
}

function projectConversationPath(projectSlugValue, date = todayDateStr()) {
  return path.join(PROJECTS_DIR, projectSlugValue, 'conversations', `${date}.jsonl`);
}

function projectMarkdownLogPath(projectSlugValue, date = todayDateStr()) {
  return path.join(PROJECTS_DIR, projectSlugValue, 'log', `${date}.md`);
}

function readTodayConversationLog(projectName, projectListFn) {
  const project = resolveProjectRecord(projectName, projectListFn);
  const date = todayDateStr();
  const filePath = projectConversationPath(project.slug, date);
  if (!fs.existsSync(filePath)) {
    return {
      project: project.name,
      slug: project.slug,
      date,
      path: filePath,
      entries: [],
      count: 0,
    };
  }

  const entries = fs.readFileSync(filePath, 'utf8')
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return null;
      }
    })
    .filter(Boolean);

  return {
    project: project.name,
    slug: project.slug,
    date,
    path: filePath,
    entries,
    count: entries.length,
  };
}

function readTodayMarkdownLog(projectName, projectListFn) {
  const project = resolveProjectRecord(projectName, projectListFn);
  const date = todayDateStr();
  const filePath = projectMarkdownLogPath(project.slug, date);
  const content = fs.existsSync(filePath) ? fs.readFileSync(filePath, 'utf8') : '';
  return {
    project: project.name,
    slug: project.slug,
    date,
    path: filePath,
    size_bytes: Buffer.byteLength(content, 'utf8'),
    content,
  };
}

function buildPromptCatalog() {
  return [
    {
      name: 'minato_start_session',
      description: 'Start a Minato work session with explicit project context and catch-up checks.',
      arguments: ['project'],
      template: [
        '1. Call `project_switch` for project "{project}".',
        '2. Call `project_get_state` and `log_query` to catch up on current phase, workflow, and recent activity.',
        '3. Summarise what matters before taking action.',
      ].join('\n'),
    },
    {
      name: 'minato_log_decision',
      description: 'Record a project decision in a durable, agent-readable way.',
      arguments: ['project', 'summary', 'details'],
      template: [
        'Use `log_append` with:',
        '- `type: "decision"`',
        '- `project: "{project}"`',
        '- `summary: "{summary}"`',
        '- `details: "{details}"`',
      ].join('\n'),
    },
    {
      name: 'minato_delegate_review',
      description: 'Send a review request to another agent with Minato context attached.',
      arguments: ['agent_id', 'text'],
      template: [
        '1. Make sure the active project is set with `project_switch`.',
        '2. Call `chat_send` with `inject_context: true`.',
        '3. Poll with `chat_poll` until the review response arrives.',
      ].join('\n'),
    },
    {
      name: 'minato_review_workflow_run',
      description: 'Inspect a Nagare workflow and its latest run before intervening.',
      arguments: ['workflow_id'],
      template: [
        '1. Call `nagare_get_workflow_dag` for "{workflow_id}".',
        '2. Call `nagare_get_run_status` for "{workflow_id}".',
        '3. If a human gate is needed, update the step with `nagare_update_step_status` and log the decision.',
      ].join('\n'),
    },
  ];
}

function readDoc(docName) {
  const requested = requireString(docName, 'doc').replace(/\.md$/i, '').toLowerCase();
  const docs = listDocs();
  const doc = docs.find((item) => item.name.toLowerCase() === requested);
  if (!doc) {
    throw createRpcError('NOT_FOUND', {
      field: 'doc',
      reason: `Unknown doc '${docName}'`,
    });
  }

  const content = fs.readFileSync(doc.path, 'utf8');
  return {
    doc: doc.name,
    content,
    path: doc.path,
    size_bytes: Buffer.byteLength(content, 'utf8'),
  };
}

function resolveProjectRecord(projectName, projectListFn) {
  const requested = requireString(projectName, 'project');
  const projects = (projectListFn?.() || defaultListProjects()).projects || [];
  const requestedSlug = projectSlug(requested);
  const match = projects.find((item) =>
    item.slug.toLowerCase() === requested.toLowerCase()
    || item.name.toLowerCase() === requested.toLowerCase()
    || item.slug.toLowerCase() === requestedSlug,
  );

  if (match) return match;
  return { slug: requestedSlug, name: requested };
}

function aggregateProjectState(projectName, projectListFn) {
  const project = resolveProjectRecord(projectName, projectListFn);
  const entries = readEntries(project.name, { limit: 5000 });
  if (!entries.length) {
    return {
      project: project.name,
      slug: project.slug,
      shimanto_phases: [],
      nagare_workflows: [],
      scope: '',
      last_activity: null,
      source_entry_count: 0,
    };
  }

  const latestFirst = [...entries].reverse();
  const currentPhases = latestFirst.find((entry) => entry.shimanto_phases?.length)?.shimanto_phases || [];
  const currentWorkflows = latestFirst.find((entry) => entry.nagare_workflows?.length)?.nagare_workflows || [];
  const currentScope = latestFirst.find((entry) => entry.scope)?.scope || '';
  const allPhasesSeen = uniqStrings(entries.flatMap((entry) => entry.shimanto_phases || []));
  const allWorkflowsSeen = uniqStrings(entries.flatMap((entry) => entry.nagare_workflows || []));

  return {
    project: project.name,
    slug: project.slug,
    shimanto_phases: currentPhases,
    nagare_workflows: currentWorkflows,
    all_shimanto_phases_seen: allPhasesSeen,
    all_nagare_workflows_seen: allWorkflowsSeen,
    scope: currentScope,
    last_activity: latestFirst[0]?.ts || null,
    source_entry_count: entries.length,
  };
}

async function buildResourceList(deps) {
  const projectListFn = deps?.projectList;
  const listAgentsFn = deps?.listAgents;
  const projectListResult = projectListFn ? await projectListFn() : { projects: defaultListProjects() };
  const projectList = (projectListResult?.projects || []);
  const projects = projectList.map((item) => ({
    uri: `minato://project/${item.slug}/state`,
    name: `Project state: ${item.name}`,
  }));
  const logResources = projectList.flatMap((item) => ([
    {
      uri: `minato://log/${item.slug}/today`,
      name: `Project log today: ${item.name}`,
    },
    {
      uri: `minato://log/${item.slug}/markdown/today`,
      name: `Project markdown log today: ${item.name}`,
    },
  ]));
  const shimantoResources = projectList.map((item) => ({
    uri: `minato://shimanto/${item.slug}/phases`,
    name: `Shimanto phases: ${item.name}`,
  }));
  const workflowResources = listWorkflowDefinitions().map((item) => ({
    uri: `minato://nagare/workflow/${item.workflow_id}`,
    name: `Nagare workflow: ${item.workflow_id}`,
  }));
  const runResources = listRunStates().map((item) => ({
    uri: `minato://nagare/run/${item.state.run_id}/state`,
    name: `Nagare run state: ${item.state.run_id}`,
  }));
  const artefactResources = projectList.map((item) => ({
    uri: `minato://artefacts/${item.slug}`,
    name: `Artefacts: ${item.name}`,
  }));
  const docResources = listDocs().map((item) => ({
    uri: `minato://docs/${item.name}`,
    name: `Doc: ${item.name}`,
  }));
  const chatResources = (((await listAgentsFn?.()) || []).map((item) => ({
    uri: `minato://chat/${encodeURIComponent(item.id)}/recent`,
    name: `Recent chat: ${item.id}`,
  })));

  return [
    { uri: 'minato://project/list', name: 'Project list' },
    { uri: 'minato://nagare/workflows', name: 'Nagare workflow definitions' },
    { uri: 'minato://docs/list', name: 'Minato docs list' },
    ...projects,
    ...logResources,
    ...shimantoResources,
    ...workflowResources,
    ...runResources,
    ...artefactResources,
    ...chatResources,
    ...docResources,
  ];
}

function createSessionStore() {
  const store = new Map();
  return {
    get(sessionId) {
      return store.get(sessionId) || null;
    },
    set(sessionId, value) {
      store.set(sessionId, value);
      return value;
    },
  };
}

function getSessionId(req) {
  return String(
    req.headers['x-minato-session']
      || req.headers['x-session-id']
      || `${req.ip || 'unknown'}|${req.headers['user-agent'] || 'unknown'}`,
  );
}

function getActiveProject(args, context, field = 'project') {
  const explicitProject = optionalString(args.project, field);
  if (explicitProject) return explicitProject;
  if (context.session?.active_project) return context.session.active_project;
  throw createRpcError('INVALID_PARAMS', {
    field,
    reason: `${field} is required when no active project has been set with project_switch`,
  });
}

function buildProjectActionLogPayload(tool, args, context, result, deps) {
  if (typeof tool.autoLog !== 'function') return null;

  const payload = tool.autoLog({ args, context, result, deps });
  if (!payload) return null;

  const project = optionalString(payload.project, 'project')
    || optionalString(args?.project, 'project')
    || context.session?.active_project
    || optionalString(result?.project, 'project');
  if (!project) return null;

  let state = null;
  try {
    state = aggregateProjectState(project, deps.projectList);
  } catch {}

  return {
    type: 'action',
    project,
    agent: 'minato_mcp',
    shimanto_phases: payload.shimanto_phases || state?.shimanto_phases || context.session?.shimanto_phases || undefined,
    nagare_workflows: payload.nagare_workflows || state?.nagare_workflows || context.session?.nagare_workflows || undefined,
    summary: payload.summary,
    details: payload.details,
    excerpt: payload.excerpt,
  };
}

function createToolRegistry(deps) {
  return [
    {
      name: 'project_list',
      description: 'List known Minato projects.',
      inputSchema: { type: 'object', properties: {}, additionalProperties: false },
      handler: async (rawArgs) => {
        expectObject(rawArgs);
        return deps.projectList();
      },
    },
    {
      name: 'project_get_state',
      description: 'Get the current Minato state for a project from the project log.',
      inputSchema: {
        type: 'object',
        properties: {
          project: { type: 'string' },
        },
        additionalProperties: false,
      },
      handler: async (rawArgs, context) => {
        const args = expectObject(rawArgs);
        return aggregateProjectState(getActiveProject(args, context), deps.projectList);
      },
    },
    {
      name: 'project_switch',
      description: 'Set the active project context for the current MCP session.',
      inputSchema: {
        type: 'object',
        properties: {
          project: { type: 'string' },
        },
        required: ['project'],
        additionalProperties: false,
      },
      handler: async (rawArgs, context) => {
        const args = expectObject(rawArgs);
        const state = aggregateProjectState(requireString(args.project, 'project'), deps.projectList);
        context.sessionStore.set(context.sessionId, {
          active_project: state.project,
          slug: state.slug,
          shimanto_phases: state.shimanto_phases,
          nagare_workflows: state.nagare_workflows,
          scope: state.scope || '',
          switched_at: new Date().toISOString(),
        });
        return {
          ok: true,
          active_project: state.project,
          slug: state.slug,
          session_id: context.sessionId,
          shimanto_phases: state.shimanto_phases,
          nagare_workflows: state.nagare_workflows,
          scope: state.scope || '',
        };
      },
    },
    {
      name: 'shimanto_get_current_phase',
      description: 'Return the current Shimanto phases for a project.',
      inputSchema: {
        type: 'object',
        properties: {
          project: { type: 'string' },
        },
        additionalProperties: false,
      },
      handler: async (rawArgs, context) => {
        const args = expectObject(rawArgs);
        const state = aggregateProjectState(getActiveProject(args, context), deps.projectList);
        return {
          project: state.project,
          phases: state.shimanto_phases,
        };
      },
    },
    {
      name: 'shimanto_list_phases',
      description: 'Return the full Shimanto phase history for a project.',
      inputSchema: {
        type: 'object',
        properties: {
          project: { type: 'string' },
        },
        additionalProperties: false,
      },
      handler: async (rawArgs, context) => {
        const args = expectObject(rawArgs);
        const state = aggregateProjectState(getActiveProject(args, context), deps.projectList);
        return {
          project: state.project,
          all_phases_seen: state.all_shimanto_phases_seen,
          current_phases: state.shimanto_phases,
        };
      },
    },
    {
      name: 'shimanto_transition_phase',
      description: 'Append a phase transition milestone and refresh session context.',
      inputSchema: {
        type: 'object',
        properties: {
          project: { type: 'string' },
          from_phases: { type: 'array', items: { type: 'string' } },
          to_phases: { type: 'array', items: { type: 'string' } },
          note: { type: 'string' },
        },
        required: ['to_phases'],
        additionalProperties: false,
      },
      handler: async (rawArgs, context) => {
        const args = expectObject(rawArgs);
        const project = getActiveProject(args, context);
        const fromPhases = optionalStringArray(args.from_phases, 'from_phases') || [];
        const toPhases = optionalStringArray(args.to_phases, 'to_phases') || [];
        if (!toPhases.length) {
          throw createRpcError('INVALID_PARAMS', {
            field: 'to_phases',
            reason: 'to_phases must contain at least one phase',
          });
        }

        await deps.logAppend({
          type: 'milestone',
          project,
          shimanto_phases: toPhases,
          summary: `Shimanto phase transition: ${toPhases.join(', ')}`,
          details: [
            fromPhases.length ? `From: ${fromPhases.join(', ')}` : 'From: (unspecified)',
            `To: ${toPhases.join(', ')}`,
            ...(optionalString(args.note, 'note') ? [`Note: ${optionalString(args.note, 'note')}`] : []),
          ],
        });

        const state = aggregateProjectState(project, deps.projectList);
        context.sessionStore.set(context.sessionId, {
          ...(context.session || {}),
          active_project: state.project,
          slug: state.slug,
          shimanto_phases: state.shimanto_phases,
          nagare_workflows: state.nagare_workflows,
          scope: state.scope || '',
          switched_at: new Date().toISOString(),
        });

        return { ok: true, project: state.project, phases: state.shimanto_phases };
      },
    },
    {
      name: 'nagare_list_workflows',
      description: 'List Nagare workflows referenced by the project log.',
      inputSchema: {
        type: 'object',
        properties: {
          project: { type: 'string' },
        },
        additionalProperties: false,
      },
      handler: async (rawArgs, context) => {
        const args = expectObject(rawArgs);
        const state = aggregateProjectState(getActiveProject(args, context), deps.projectList);
        return {
          project: state.project,
          workflows: state.all_nagare_workflows_seen,
        };
      },
    },
    {
      name: 'nagare_get_workflow_dag',
      description: 'Read and summarize a Nagare workflow YAML definition.',
      inputSchema: {
        type: 'object',
        properties: {
          workflow_id: { type: 'string' },
        },
        required: ['workflow_id'],
        additionalProperties: false,
      },
      handler: async (rawArgs) => {
        const args = expectObject(rawArgs);
        const workflow = findWorkflowDefinition(args.workflow_id);
        let currentRunId = null;
        try {
          currentRunId = resolveRunState({ workflowId: workflow.workflow_id }).state.run_id;
        } catch {}
        return {
          workflow_id: workflow.workflow_id,
          name: workflow.name,
          version: workflow.version,
          description: workflow.description,
          steps: workflow.steps,
          pre_flight_questions: workflow.pre_flight_questions,
          current_run_id: currentRunId,
          path: workflow.path,
          raw_yaml: workflow.raw_yaml,
        };
      },
    },
    {
      name: 'nagare_get_run_status',
      description: 'Read the latest or specified Nagare workflow run state.',
      inputSchema: {
        type: 'object',
        properties: {
          workflow_id: { type: 'string' },
          run_id: { type: 'string' },
        },
        required: ['workflow_id'],
        additionalProperties: false,
      },
      handler: async (rawArgs) => {
        const args = expectObject(rawArgs);
        const workflowId = requireString(args.workflow_id, 'workflow_id');
        const runState = resolveRunState({
          workflowId,
          runId: optionalString(args.run_id, 'run_id'),
        });
        return normalizeRunState(workflowId, runState);
      },
    },
    {
      name: 'nagare_update_step_status',
      description: 'Update a Nagare run step status for human-in-the-loop intervention.',
      inputSchema: {
        type: 'object',
        properties: {
          run_id: { type: 'string' },
          step_id: { type: 'string' },
          status: { type: 'string' },
          note: { type: 'string' },
        },
        required: ['run_id', 'step_id', 'status'],
        additionalProperties: false,
      },
      handler: async (rawArgs) => {
        const args = expectObject(rawArgs);
        const runId = requireString(args.run_id, 'run_id');
        const stepId = requireString(args.step_id, 'step_id');
        const status = requireString(args.status, 'status');
        const note = optionalString(args.note, 'note');
        const runState = updateRunStateStep({ runId, stepId, status, note });
        return {
          ok: true,
          run_id: runId,
          step_id: stepId,
          status,
          note: note || '',
          workflow_status: runState.status,
        };
      },
      autoLog: ({ args, context }) => ({
        project: context.session?.active_project,
        summary: `Nagare step '${args.step_id}' updated to '${args.status}'`,
        details: [
          `Run ID: ${args.run_id}`,
          `Step ID: ${args.step_id}`,
          `Status: ${args.status}`,
          ...(optionalString(args.note, 'note') ? [`Note: ${optionalString(args.note, 'note')}`] : []),
        ],
      }),
    },
    {
      name: 'artefacts_list',
      description: 'List artefacts linked to a Minato project.',
      inputSchema: {
        type: 'object',
        properties: {
          project: { type: 'string' },
          type: { type: 'string' },
        },
        additionalProperties: false,
      },
      handler: async (rawArgs, context) => {
        const args = expectObject(rawArgs);
        const project = getActiveProject(args, context);
        return listArtefactsForProject(project, {
          type: optionalString(args.type, 'type') || 'any',
        });
      },
    },
    {
      name: 'artefacts_create',
      description: 'Create a new Minato artefact record.',
      inputSchema: {
        type: 'object',
        properties: {
          project: { type: 'string' },
          name: { type: 'string' },
          type: { type: 'string' },
          path: { type: 'string' },
          kasumi_id: { type: 'string' },
          kasumi_module: { type: 'string' },
          nagare_step: { type: 'string' },
          shimanto_phase: { type: 'string' },
          note: { type: 'string' },
        },
        required: ['name', 'type'],
        additionalProperties: false,
      },
      handler: async (rawArgs, context) => {
        const args = expectObject(rawArgs);
        const record = createArtefactRecord({
          ...args,
          project: getActiveProject(args, context),
        });
        return {
          ok: true,
          artefact_id: record.artefact_id,
          artefact: record,
        };
      },
      autoLog: ({ result }) => {
        const artefact = result?.artefact;
        if (!artefact) return null;
        return {
          project: artefact.project,
          summary: `Registered artefact '${artefact.name}'`,
          details: [
            `Artefact ID: ${artefact.artefact_id}`,
            `Type: ${artefact.type}`,
            ...(artefact.path ? [`Path: ${artefact.path}`] : []),
            ...(artefact.kasumi_module ? [`KASUMI module: ${artefact.kasumi_module}`] : []),
            ...(artefact.kasumi_id ? [`KASUMI ID: ${artefact.kasumi_id}`] : []),
            ...(artefact.nagare_step ? [`Nagare step: ${artefact.nagare_step}`] : []),
            ...(artefact.shimanto_phase ? [`Shimanto phase: ${artefact.shimanto_phase}`] : []),
            ...(artefact.note ? [`Note: ${artefact.note}`] : []),
          ],
        };
      },
    },
    {
      name: 'artefacts_read',
      description: 'Read a Minato artefact by id.',
      inputSchema: {
        type: 'object',
        properties: {
          artefact_id: { type: 'string' },
        },
        required: ['artefact_id'],
        additionalProperties: false,
      },
      handler: async (rawArgs) => {
        const args = expectObject(rawArgs);
        const record = getArtefactRecord(args.artefact_id);
        if (record.type === 'kasumi') {
          return {
            ...record,
            ...(await readKasumiArtefact(record, deps)),
          };
        }
        return {
          ...record,
          ...readFilesystemArtefact(record),
        };
      },
    },
    {
      name: 'artefacts_link',
      description: 'Link an existing artefact to additional Minato project metadata.',
      inputSchema: {
        type: 'object',
        properties: {
          artefact_id: { type: 'string' },
          project: { type: 'string' },
          nagare_step: { type: 'string' },
          shimanto_phase: { type: 'string' },
        },
        required: ['artefact_id'],
        additionalProperties: false,
      },
      handler: async (rawArgs, context) => {
        const args = expectObject(rawArgs);
        const project = optionalString(args.project, 'project') || context.session?.active_project;
        const nagareStep = optionalString(args.nagare_step, 'nagare_step');
        const shimantoPhase = optionalString(args.shimanto_phase, 'shimanto_phase');
        if (!project && !nagareStep && !shimantoPhase) {
          throw createRpcError('INVALID_PARAMS', {
            field: 'artefact_id',
            reason: 'at least one of project, nagare_step, or shimanto_phase is required',
          });
        }

        const updated = updateArtefactRecord(args.artefact_id, (current) => ({
          linked_projects: uniqStrings([...current.linked_projects, ...(project ? [project] : [])]),
          project: current.project || project || null,
          linked_nagare_steps: uniqStrings([...current.linked_nagare_steps, ...(nagareStep ? [nagareStep] : [])]),
          nagare_step: current.nagare_step || nagareStep || null,
          linked_shimanto_phases: uniqStrings([...current.linked_shimanto_phases, ...(shimantoPhase ? [shimantoPhase] : [])]),
          shimanto_phase: current.shimanto_phase || shimantoPhase || null,
        }));
        return { ok: true, artefact: updated };
      },
      autoLog: ({ args, result, context }) => {
        const artefact = result?.artefact;
        if (!artefact) return null;
        return {
          project: artefact.project || context.session?.active_project,
          summary: `Linked artefact '${artefact.name}' to additional Minato context`,
          details: [
            `Artefact ID: ${artefact.artefact_id}`,
            ...(optionalString(args.project, 'project') ? [`Project link: ${optionalString(args.project, 'project')}`] : []),
            ...(optionalString(args.nagare_step, 'nagare_step') ? [`Nagare step link: ${optionalString(args.nagare_step, 'nagare_step')}`] : []),
            ...(optionalString(args.shimanto_phase, 'shimanto_phase') ? [`Shimanto phase link: ${optionalString(args.shimanto_phase, 'shimanto_phase')}`] : []),
          ],
        };
      },
    },
    {
      name: 'log_query',
      description: 'Read project conversation log entries.',
      inputSchema: {
        type: 'object',
        properties: {
          project: { type: 'string' },
          limit: { type: 'integer', minimum: 1, maximum: 1000 },
          since: { type: 'string' },
        },
        additionalProperties: false,
      },
      handler: async (rawArgs, context) => {
        const args = expectObject(rawArgs);
        return deps.logQuery({
          project: getActiveProject(args, context),
          limit: optionalNumber(args.limit, 'limit', { integer: true, min: 1, max: 1000 }),
          since: optionalString(args.since, 'since'),
        });
      },
    },
    {
      name: 'log_append',
      description: 'Append a structured project activity entry.',
      inputSchema: {
        type: 'object',
        properties: {
          type: { type: 'string' },
          project: { type: 'string' },
          agent: { type: 'string' },
          user: { type: 'string' },
          ts: { type: 'string' },
          shimanto_phases: { type: 'array', items: { type: 'string' } },
          nagare_workflows: { type: 'array', items: { type: 'string' } },
          summary: { type: 'string' },
          details: { anyOf: [{ type: 'string' }, { type: 'array', items: { type: 'string' } }] },
          excerpt: {
            type: 'object',
            properties: {
              from: { type: 'string' },
              to: { type: 'string' },
            },
          },
        },
        additionalProperties: false,
      },
      handler: async (rawArgs, context) => {
        const args = expectObject(rawArgs);
        const payload = {
          type: optionalString(args.type, 'type') || 'note',
          project: getActiveProject(args, context),
          agent: optionalString(args.agent, 'agent'),
          user: optionalString(args.user, 'user'),
          ts: optionalString(args.ts, 'ts'),
          shimanto_phases: optionalStringArray(args.shimanto_phases, 'shimanto_phases'),
          nagare_workflows: optionalStringArray(args.nagare_workflows, 'nagare_workflows'),
          summary: optionalString(args.summary, 'summary'),
          details: optionalDetails(args.details),
          excerpt: optionalExcerpt(args.excerpt),
        };

        if (!payload.summary && !payload.details && !payload.excerpt) {
          throw createRpcError('INVALID_PARAMS', {
            field: 'summary',
            reason: 'at least one of summary, details, or excerpt is required',
          });
        }

        return deps.logAppend(payload);
      },
    },
    {
      name: 'log_project_chat',
      description: 'Read the per-agent project chat log from the bridge server.',
      inputSchema: {
        type: 'object',
        properties: {
          agent: { type: 'string' },
          project: { type: 'string' },
          limit: { type: 'integer', minimum: 1, maximum: 500 },
        },
        required: ['agent'],
        additionalProperties: false,
      },
      handler: async (rawArgs, context) => {
        const args = expectObject(rawArgs);
        return deps.logProjectChat({
          agent: requireString(args.agent, 'agent'),
          project: getActiveProject(args, context),
          limit: optionalNumber(args.limit, 'limit', { integer: true, min: 1, max: 500 }),
        });
      },
    },
    {
      name: 'chat_send',
      description: 'Send a text message to an agent through the HASHI bridge.',
      inputSchema: {
        type: 'object',
        properties: {
          agent_id: { type: 'string' },
          text: { type: 'string' },
          inject_context: { type: 'boolean' },
          project: { type: 'string' },
          shimanto_phases: { type: 'array', items: { type: 'string' } },
          nagare_workflows: { type: 'array', items: { type: 'string' } },
          scope: { type: 'string' },
        },
        required: ['agent_id', 'text'],
        additionalProperties: false,
      },
      handler: async (rawArgs, context) => {
        const args = expectObject(rawArgs);
        const injectContext = optionalBoolean(args.inject_context, 'inject_context') || false;
        const activeProject = optionalString(args.project, 'project') || context.session?.active_project;
        const payload = {
          agent_id: requireString(args.agent_id, 'agent_id'),
          text: requireString(args.text, 'text'),
          inject_context: injectContext,
          project: activeProject,
          shimanto_phases: optionalStringArray(args.shimanto_phases, 'shimanto_phases') || context.session?.shimanto_phases,
          nagare_workflows: optionalStringArray(args.nagare_workflows, 'nagare_workflows') || context.session?.nagare_workflows,
          scope: optionalString(args.scope, 'scope') ?? context.session?.scope,
        };

        if (injectContext && !payload.project) {
          throw createRpcError('INVALID_PARAMS', {
            field: 'project',
            reason: 'project is required when inject_context is true',
          });
        }

        const text = injectContext ? makeMinatoContextHeader(payload) : payload.text;
        return deps.chatSend({
          agentId: payload.agent_id,
          text,
          context: injectContext
            ? {
                project: payload.project,
                shimanto_phases: payload.shimanto_phases || [],
                nagare_workflows: payload.nagare_workflows || [],
                scope: payload.scope || '',
                original_text: payload.text,
              }
            : null,
        });
      },
    },
    {
      name: 'chat_get_history',
      description: 'Read recent transcript messages for an agent.',
      inputSchema: {
        type: 'object',
        properties: {
          agent_id: { type: 'string' },
          limit: { type: 'integer', minimum: 1, maximum: 200 },
        },
        required: ['agent_id'],
        additionalProperties: false,
      },
      handler: async (rawArgs) => {
        const args = expectObject(rawArgs);
        return deps.chatGetHistory({
          agentId: requireString(args.agent_id, 'agent_id'),
          limit: optionalNumber(args.limit, 'limit', { integer: true, min: 1, max: 200 }),
        });
      },
    },
    {
      name: 'chat_poll',
      description: 'Poll an agent transcript for incremental messages.',
      inputSchema: {
        type: 'object',
        properties: {
          agent_id: { type: 'string' },
          offset: { type: 'integer', minimum: 0 },
        },
        required: ['agent_id', 'offset'],
        additionalProperties: false,
      },
      handler: async (rawArgs) => {
        const args = expectObject(rawArgs);
        return deps.chatPoll({
          agentId: requireString(args.agent_id, 'agent_id'),
          offset: optionalNumber(args.offset, 'offset', { integer: true, min: 0 }),
        });
      },
    },
    {
      name: 'docs_list',
      description: 'List available Minato system reference documents.',
      inputSchema: { type: 'object', properties: {}, additionalProperties: false },
      handler: async (rawArgs) => {
        expectObject(rawArgs);
        return { docs: listDocs() };
      },
    },
    {
      name: 'docs_read',
      description: 'Read a Minato system reference document.',
      inputSchema: {
        type: 'object',
        properties: {
          doc: { type: 'string' },
        },
        required: ['doc'],
        additionalProperties: false,
      },
      handler: async (rawArgs) => {
        const args = expectObject(rawArgs);
        return readDoc(args.doc);
      },
    },
  ];
}

function createResourceReader(deps) {
  return async function readResource(uri) {
    const value = requireString(uri, 'uri');

    if (value === 'minato://project/list') {
      return { uri: value, mime_type: 'application/json', data: await deps.projectList() };
    }
    if (value === 'minato://nagare/workflows') {
      return {
        uri: value,
        mime_type: 'application/json',
        data: {
          workflows: listWorkflowDefinitions().map((item) => ({
            workflow_id: item.workflow_id,
            name: item.name,
            version: item.version,
            path: item.path,
          })),
        },
      };
    }
    if (value === 'minato://docs/list') {
      return { uri: value, mime_type: 'application/json', data: { docs: listDocs() } };
    }

    const projectStateMatch = value.match(/^minato:\/\/project\/([^/]+)\/state$/);
    if (projectStateMatch) {
      const project = resolveProjectRecord(projectStateMatch[1], deps.projectList).name;
      return {
        uri: value,
        mime_type: 'application/json',
        data: aggregateProjectState(project, deps.projectList),
      };
    }

    const shimantoMatch = value.match(/^minato:\/\/shimanto\/([^/]+)\/phases$/);
    if (shimantoMatch) {
      const project = resolveProjectRecord(shimantoMatch[1], deps.projectList).name;
      const state = aggregateProjectState(project, deps.projectList);
      return {
        uri: value,
        mime_type: 'application/json',
        data: {
          project: state.project,
          all_phases_seen: state.all_shimanto_phases_seen,
          current_phases: state.shimanto_phases,
        },
      };
    }

    const workflowMatch = value.match(/^minato:\/\/nagare\/workflow\/([^/]+)$/);
    if (workflowMatch) {
      const workflow = findWorkflowDefinition(decodeURIComponent(workflowMatch[1]));
      return {
        uri: value,
        mime_type: 'application/yaml',
        data: {
          workflow_id: workflow.workflow_id,
          name: workflow.name,
          version: workflow.version,
          path: workflow.path,
          raw_yaml: workflow.raw_yaml,
          steps: workflow.steps,
          pre_flight_questions: workflow.pre_flight_questions,
        },
      };
    }

    const runMatch = value.match(/^minato:\/\/nagare\/run\/([^/]+)\/state$/);
    if (runMatch) {
      const runState = resolveRunState({ runId: decodeURIComponent(runMatch[1]), workflowId: 'run' });
      const workflowId = String(runState.state.run_id || '').replace(/^run-/, '').replace(/-\d{8}-\d{6}$/, '');
      return {
        uri: value,
        mime_type: 'application/json',
        data: normalizeRunState(workflowId || 'unknown', runState),
      };
    }

    const artefactMatch = value.match(/^minato:\/\/artefacts\/([^/]+)$/);
    if (artefactMatch) {
      const project = resolveProjectRecord(decodeURIComponent(artefactMatch[1]), deps.projectList).name;
      return {
        uri: value,
        mime_type: 'application/json',
        data: listArtefactsForProject(project),
      };
    }

    const logJsonMatch = value.match(/^minato:\/\/log\/([^/]+)\/today$/);
    if (logJsonMatch) {
      const project = resolveProjectRecord(decodeURIComponent(logJsonMatch[1]), deps.projectList).name;
      return {
        uri: value,
        mime_type: 'application/json',
        data: readTodayConversationLog(project, deps.projectList),
      };
    }

    const logMarkdownMatch = value.match(/^minato:\/\/log\/([^/]+)\/markdown\/today$/);
    if (logMarkdownMatch) {
      const project = resolveProjectRecord(decodeURIComponent(logMarkdownMatch[1]), deps.projectList).name;
      return {
        uri: value,
        mime_type: 'text/markdown',
        data: readTodayMarkdownLog(project, deps.projectList),
      };
    }

    const chatRecentMatch = value.match(/^minato:\/\/chat\/([^/]+)\/recent$/);
    if (chatRecentMatch) {
      const agentId = decodeURIComponent(chatRecentMatch[1]);
      return {
        uri: value,
        mime_type: 'application/json',
        data: {
          agent_id: agentId,
          ...(await deps.chatGetHistory({ agentId, limit: 50 })),
        },
      };
    }

    const docMatch = value.match(/^minato:\/\/docs\/([^/]+)$/);
    if (docMatch) {
      return {
        uri: value,
        mime_type: 'text/markdown',
        data: readDoc(decodeURIComponent(docMatch[1])),
      };
    }

    throw createRpcError('NOT_FOUND', {
      field: 'uri',
      reason: `Unknown resource '${value}'`,
    });
  };
}

export function createMinatoMcpRouter(deps) {
  const {
    projectList,
    logQuery,
    logAppend,
    logProjectChat,
    chatSend,
    chatGetHistory,
    chatPoll,
    auditWriter = appendAuditRecord,
    sessionStore = createSessionStore(),
  } = deps || {};

  if (![projectList, logQuery, logAppend, logProjectChat, chatSend, chatGetHistory, chatPoll].every((fn) => typeof fn === 'function')) {
    throw new Error('createMinatoMcpRouter requires projectList, logQuery, logAppend, logProjectChat, chatSend, chatGetHistory, and chatPoll functions');
  }

  const router = express.Router();
  const tools = createToolRegistry(deps);
  const toolMap = new Map(tools.map((tool) => [tool.name, tool]));
  const readResource = createResourceReader(deps);

  router.get('/tools/list', (_req, res) => {
    res.json({
      server: 'minato-mcp',
      version: 'v1',
      tools: tools.map(({ name, description, inputSchema }) => ({
        name,
        description,
        inputSchema,
      })),
    });
  });

  router.get('/resources/list', async (_req, res) => {
    res.json({ resources: await buildResourceList(deps) });
  });

  router.post('/resources/read', async (req, res) => {
    const id = req.body?.id ?? null;
    try {
      const body = expectObject(req.body || {}, 'body');
      const resource = await readResource(body.uri || body.id);
      return res.json(resource);
    } catch (error) {
      const normalized = normalizeUpstreamError(error);
      const payload = err(id, normalized.rpcMessage || 'INTERNAL_ERROR', normalized.rpcData || null);
      const status = payload.error.code === ERROR_CODES.METHOD_NOT_FOUND || payload.error.code === ERROR_CODES.NOT_FOUND ? 404 : 400;
      return res.status(status).json(payload);
    }
  });

  router.get('/prompts/list', (_req, res) => {
    res.json({ prompts: buildPromptCatalog() });
  });

  router.post('/tools/call', async (req, res) => {
    const envelope = req.body;
    const id = envelope?.id ?? null;

    try {
      if (!isPlainObject(envelope) || envelope.jsonrpc !== '2.0' || envelope.method !== 'tools/call') {
        throw createRpcError('INVALID_REQUEST', {
          reason: 'Expected a JSON-RPC 2.0 envelope with method "tools/call"',
        });
      }

      const params = expectObject(envelope.params, 'params');
      const toolName = requireString(params.name, 'params.name');
      const args = params.arguments ?? {};
      const tool = toolMap.get(toolName);

      if (!tool) {
        throw createRpcError('METHOD_NOT_FOUND', {
          field: 'params.name',
          reason: `Unknown tool '${toolName}'`,
        });
      }

      const sessionId = getSessionId(req);
      const session = deepClone(sessionStore.get(sessionId));
      const context = { req, sessionId, session, sessionStore };
      const result = await tool.handler(args, context);
      const actionLog = buildProjectActionLogPayload(tool, args, context, result, deps);
      if (actionLog) {
        await deps.logAppend(actionLog);
      }

      auditWriter({
        ts: new Date().toISOString(),
        tool: toolName,
        ok: true,
        session_id: sessionId,
        arguments: args,
      });

      return res.json(ok(id, result));
    } catch (error) {
      const normalized = normalizeUpstreamError(error);
      auditWriter({
        ts: new Date().toISOString(),
        tool: envelope?.params?.name || null,
        ok: false,
        session_id: getSessionId(req),
        arguments: envelope?.params?.arguments ?? null,
        error: {
          message: normalized.rpcMessage || normalized.message || 'INTERNAL_ERROR',
          data: normalized.rpcData || null,
        },
      });
      const payload = err(id, normalized.rpcMessage || 'INTERNAL_ERROR', normalized.rpcData || null);
      const status = payload.error.code === ERROR_CODES.METHOD_NOT_FOUND || payload.error.code === ERROR_CODES.NOT_FOUND ? 404 : 400;
      return res.status(status).json(payload);
    }
  });

  return router;
}
