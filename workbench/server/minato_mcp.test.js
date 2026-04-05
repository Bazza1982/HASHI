import test from 'node:test';
import assert from 'node:assert/strict';
import express from 'express';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createMinatoMcpRouter } from './minato_mcp.js';
import { projectSlug } from './project_log.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const WORKBENCH_DIR = path.resolve(__dirname, '..');
const PROJECT_DATA_DIR = path.join(WORKBENCH_DIR, 'data', 'projects');
const ARTEFACTS_FILE = path.join(WORKBENCH_DIR, 'data', 'minato_artefacts.json');

async function withServer(router, fn) {
  const app = express();
  app.use(express.json());
  app.use('/api/minato/mcp/v1', router);

  const server = await new Promise((resolve) => {
    const instance = app.listen(0, '127.0.0.1', () => resolve(instance));
  });

  const address = server.address();
  const baseUrl = `http://127.0.0.1:${address.port}/api/minato/mcp/v1`;

  try {
    await fn(baseUrl);
  } finally {
    await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
  }
}

function makeRouter(overrides = {}) {
  const sentMessages = [];
  const projectChats = [];

  const router = createMinatoMcpRouter({
    projectList: async () => ({ projects: [{ slug: 'alpha', name: 'Alpha' }] }),
    logQuery: async ({ project, limit, since }) => ({ entries: [{ project, limit, since }], count: 1 }),
    logAppend: async (payload) => ({ ok: true, entry: payload }),
    logProjectChat: async ({ agent, project, limit }) => {
      projectChats.push({ agent, project, limit });
      return { entries: [{ agent, project }], count: 1 };
    },
    chatSend: async (payload) => {
      sentMessages.push(payload);
      return { ok: true, request_id: 'req_123' };
    },
    chatGetHistory: async ({ agentId, limit }) => ({ messages: [{ role: 'assistant', content: `hello ${agentId}` }], offset: limit || 50 }),
    chatPoll: async ({ agentId, offset }) => ({ messages: [{ role: 'assistant', content: `delta ${agentId}` }], offset: offset + 20 }),
    auditWriter: () => {},
    ...overrides,
  });

  return { router, sentMessages, projectChats };
}

async function callTool(baseUrl, name, args = {}, headers = {}) {
  const response = await fetch(`${baseUrl}/tools/call`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...headers,
    },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: `req-${name}`,
      method: 'tools/call',
      params: {
        name,
        arguments: args,
      },
    }),
  });

  return {
    response,
    body: await response.json(),
  };
}

async function withProjectLog(projectName, entries, fn) {
  const slug = projectSlug(projectName);
  const dir = path.join(PROJECT_DATA_DIR, slug);
  const conversationsDir = path.join(dir, 'conversations');
  fs.mkdirSync(conversationsDir, { recursive: true });
  const filePath = path.join(conversationsDir, '2026-04-05.jsonl');
  fs.writeFileSync(filePath, `${entries.map((entry) => JSON.stringify(entry)).join('\n')}\n`, 'utf8');

  try {
    await fn({ slug, dir, filePath });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

async function withArtefactStore(fn) {
  const backup = fs.existsSync(ARTEFACTS_FILE) ? fs.readFileSync(ARTEFACTS_FILE, 'utf8') : null;
  fs.mkdirSync(path.dirname(ARTEFACTS_FILE), { recursive: true });
  fs.writeFileSync(ARTEFACTS_FILE, JSON.stringify({ last_id: 0, artefacts: [] }, null, 2) + '\n', 'utf8');

  try {
    await fn();
  } finally {
    if (backup === null) {
      fs.rmSync(ARTEFACTS_FILE, { force: true });
    } else {
      fs.writeFileSync(ARTEFACTS_FILE, backup, 'utf8');
    }
  }
}

async function withTempFile(name, content, fn) {
  const tempDir = fs.mkdtempSync(path.join(WORKBENCH_DIR, 'tmp-minato-'));
  const filePath = path.join(tempDir, name);
  fs.writeFileSync(filePath, content, 'utf8');
  try {
    await fn(filePath);
  } finally {
    fs.rmSync(tempDir, { recursive: true, force: true });
  }
}

test('tools/list exposes Tier 1 and Tier 2 tools', async () => {
  const { router } = makeRouter();
  await withServer(router, async (baseUrl) => {
    const response = await fetch(`${baseUrl}/tools/list`);
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.deepEqual(
      body.tools.map((tool) => tool.name),
      [
        'project_list',
        'project_get_state',
        'project_switch',
        'shimanto_get_current_phase',
        'shimanto_list_phases',
        'shimanto_transition_phase',
        'nagare_list_workflows',
        'nagare_get_workflow_dag',
        'nagare_get_run_status',
        'nagare_update_step_status',
        'artefacts_list',
        'artefacts_create',
        'artefacts_read',
        'artefacts_link',
        'log_query',
        'log_append',
        'log_project_chat',
        'chat_send',
        'chat_get_history',
        'chat_poll',
        'docs_list',
        'docs_read',
      ],
    );
  });
});

test('artefacts tools create, read, link, and expose project-scoped resources', async () => {
  const project = 'Tier3 Artefact Project';
  await withArtefactStore(async () => {
    await withProjectLog(project, [
      {
        ts: '2026-04-05T12:00:00Z',
        project,
        shimanto_phases: ['planning'],
        nagare_workflows: ['smoke-test'],
      },
    ], async ({ slug }) => {
      await withTempFile('draft.md', '# Draft\n\nHello artefact.\n', async (filePath) => {
        const { router } = makeRouter({
          projectList: () => ({ projects: [{ slug, name: project }] }),
        });

        await withServer(router, async (baseUrl) => {
          const headers = { 'x-minato-session': 'sess-artefacts' };
          await callTool(baseUrl, 'project_switch', { project }, headers);

          const created = await callTool(baseUrl, 'artefacts_create', {
            name: 'Draft Report',
            type: 'file',
            path: filePath,
            nagare_step: 'draft',
            shimanto_phase: 'planning',
            note: 'Initial draft',
          }, headers);
          assert.equal(created.response.status, 200);
          assert.equal(created.body.result.ok, true);
          assert.equal(created.body.result.artefact_id, 'art_001');

          const listed = await callTool(baseUrl, 'artefacts_list', {}, headers);
          assert.equal(listed.response.status, 200);
          assert.equal(listed.body.result.artefacts.length, 1);
          assert.equal(listed.body.result.artefacts[0].path, filePath);

          const read = await callTool(baseUrl, 'artefacts_read', { artefact_id: 'art_001' }, headers);
          assert.equal(read.response.status, 200);
          assert.equal(read.body.result.artefact_id, 'art_001');
          assert.match(read.body.result.content, /Hello artefact/);

          const linked = await callTool(baseUrl, 'artefacts_link', {
            artefact_id: 'art_001',
            nagare_step: 'review',
            shimanto_phase: 'fieldwork',
          }, headers);
          assert.equal(linked.response.status, 200);
          assert.deepEqual(linked.body.result.artefact.linked_nagare_steps, ['draft', 'review']);
          assert.deepEqual(linked.body.result.artefact.linked_shimanto_phases, ['planning', 'fieldwork']);

          const resourceListResponse = await fetch(`${baseUrl}/resources/list`);
          assert.equal(resourceListResponse.status, 200);
          const resourceList = await resourceListResponse.json();
          assert.ok(resourceList.resources.some((item) => item.uri === `minato://artefacts/${slug}`));

          const resourceResponse = await fetch(`${baseUrl}/resources/read`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ uri: `minato://artefacts/${slug}` }),
          });
          assert.equal(resourceResponse.status, 200);
          const resourceBody = await resourceResponse.json();
          assert.equal(resourceBody.mime_type, 'application/json');
          assert.equal(resourceBody.data.artefacts.length, 1);
          assert.equal(resourceBody.data.artefacts[0].artefact_id, 'art_001');
        });
      });
    });
  });
});

test('nagare_update_step_status mutates run state and returns updated workflow status', async () => {
  const runId = 'run-meta-workflow-creation-20260326-075403';
  const runPath = path.join(WORKBENCH_DIR, '..', 'flow', 'runs', runId, 'state.json');
  const backup = fs.readFileSync(runPath, 'utf8');

  try {
    const { router } = makeRouter();
    await withServer(router, async (baseUrl) => {
      const updated = await callTool(baseUrl, 'nagare_update_step_status', {
        run_id: runId,
        step_id: 'create_workflow_files',
        status: 'completed',
        note: 'Approved manually',
      });
      assert.equal(updated.response.status, 200);
      assert.equal(updated.body.result.ok, true);
      assert.equal(updated.body.result.status, 'completed');

      const state = JSON.parse(fs.readFileSync(runPath, 'utf8'));
      assert.equal(state.steps.create_workflow_files.status, 'completed');
      assert.equal(state.human_interventions.at(-1).note, 'Approved manually');
    });
  } finally {
    fs.writeFileSync(runPath, backup, 'utf8');
  }
});

test('project_switch establishes session context used by project_get_state and chat_send', async () => {
  const project = 'Tier2 Session Project';
  await withProjectLog(project, [
    {
      ts: '2026-04-05T09:00:00Z',
      project,
      shimanto_phases: ['planning'],
      nagare_workflows: ['smoke-test'],
      scope: 'Draft summary',
    },
    {
      ts: '2026-04-05T10:00:00Z',
      project,
      shimanto_phases: ['fieldwork'],
      nagare_workflows: ['meta-workflow-creation'],
      scope: 'Field review',
    },
  ], async ({ slug }) => {
    const { router, sentMessages } = makeRouter({
      projectList: async () => ({ projects: [{ slug, name: project }] }),
    });

    await withServer(router, async (baseUrl) => {
      const headers = { 'x-minato-session': 'sess-tier2' };
      const switched = await callTool(baseUrl, 'project_switch', { project }, headers);
      assert.equal(switched.response.status, 200);
      assert.equal(switched.body.result.active_project, project);
      assert.equal(switched.body.result.slug, slug);

      const state = await callTool(baseUrl, 'project_get_state', {}, headers);
      assert.equal(state.response.status, 200);
      assert.equal(state.body.result.project, project);
      assert.deepEqual(state.body.result.shimanto_phases, ['fieldwork']);
      assert.deepEqual(state.body.result.nagare_workflows, ['meta-workflow-creation']);
      assert.deepEqual(state.body.result.all_shimanto_phases_seen, ['planning', 'fieldwork']);

      const send = await callTool(baseUrl, 'chat_send', {
        agent_id: 'kasumi',
        text: 'Need your review.',
        inject_context: true,
      }, headers);
      assert.equal(send.response.status, 200);
      assert.equal(sentMessages.length, 1);
      assert.match(sentMessages[0].text, /minato active project: Tier2 Session Project/);
      assert.match(sentMessages[0].text, /shimanto phases: fieldwork/);
      assert.match(sentMessages[0].text, /nagare workflows: meta-workflow-creation/);
    });
  });
});

test('shimanto tools aggregate phase history and transition writes milestone', async () => {
  const project = 'Tier2 Shimanto Project';
  await withProjectLog(project, [
    { ts: '2026-04-05T08:00:00Z', project, shimanto_phases: ['scoping'], nagare_workflows: [] },
    { ts: '2026-04-05T09:00:00Z', project, shimanto_phases: ['planning'], nagare_workflows: [] },
  ], async ({ slug }) => {
    let appended = null;
    const { router } = makeRouter({
      projectList: async () => ({ projects: [{ slug, name: project }] }),
      logAppend: async (payload) => {
        appended = payload;
        return { ok: true };
      },
    });

    await withServer(router, async (baseUrl) => {
      const headers = { 'x-minato-session': 'sess-shimanto' };
      await callTool(baseUrl, 'project_switch', { project }, headers);

      const phases = await callTool(baseUrl, 'shimanto_list_phases', {}, headers);
      assert.equal(phases.response.status, 200);
      assert.deepEqual(phases.body.result.all_phases_seen, ['scoping', 'planning']);
      assert.deepEqual(phases.body.result.current_phases, ['planning']);

      const current = await callTool(baseUrl, 'shimanto_get_current_phase', {}, headers);
      assert.deepEqual(current.body.result.phases, ['planning']);

      const transition = await callTool(baseUrl, 'shimanto_transition_phase', {
        to_phases: ['fieldwork'],
        note: 'Approved to move on',
      }, headers);
      assert.equal(transition.response.status, 200);
      assert.equal(transition.body.result.ok, true);
      assert.equal(appended.type, 'milestone');
      assert.equal(appended.project, project);
      assert.deepEqual(appended.shimanto_phases, ['fieldwork']);
      assert.match(appended.summary, /fieldwork/);
    });
  });
});

test('nagare tools expose workflow DAG and run status from real files', async () => {
  const project = 'Tier2 Nagare Project';
  await withProjectLog(project, [
    {
      ts: '2026-04-05T11:00:00Z',
      project,
      shimanto_phases: ['planning'],
      nagare_workflows: ['smoke-test', 'meta-workflow-creation'],
    },
  ], async ({ slug }) => {
    const { router } = makeRouter({
      projectList: async () => ({ projects: [{ slug, name: project }] }),
    });

    await withServer(router, async (baseUrl) => {
      const headers = { 'x-minato-session': 'sess-nagare' };
      await callTool(baseUrl, 'project_switch', { project }, headers);

      const listed = await callTool(baseUrl, 'nagare_list_workflows', {}, headers);
      assert.equal(listed.response.status, 200);
      assert.deepEqual(listed.body.result.workflows, ['smoke-test', 'meta-workflow-creation']);

      const dag = await callTool(baseUrl, 'nagare_get_workflow_dag', { workflow_id: 'smoke-test' });
      assert.equal(dag.response.status, 200);
      assert.equal(dag.body.result.workflow_id, 'smoke-test');
      assert.equal(dag.body.result.name, '冒烟测试');
      assert.ok(dag.body.result.steps.some((step) => step.id === 'step_write'));
      assert.ok(dag.body.result.path.endsWith('smoke_test.yaml'));

      const run = await callTool(baseUrl, 'nagare_get_run_status', { workflow_id: 'smoke-test' });
      assert.equal(run.response.status, 200);
      assert.equal(run.body.result.workflow_id, 'smoke-test');
      assert.equal(run.body.result.status, 'completed');
      assert.ok(run.body.result.steps.some((step) => step.id === 'step_check' && step.status === 'completed'));
    });
  });
});

test('chat_poll and log_project_chat wrap existing endpoints', async () => {
  const { router, projectChats } = makeRouter();
  await withServer(router, async (baseUrl) => {
    const poll = await callTool(baseUrl, 'chat_poll', { agent_id: 'kasumi', offset: 5 });
    assert.equal(poll.response.status, 200);
    assert.equal(poll.body.result.offset, 25);
    assert.equal(poll.body.result.messages[0].content, 'delta kasumi');

    const projectChat = await callTool(baseUrl, 'log_project_chat', {
      agent: 'kasumi',
      project: 'Alpha',
      limit: 10,
    });
    assert.equal(projectChat.response.status, 200);
    assert.equal(projectChats.length, 1);
    assert.deepEqual(projectChats[0], { agent: 'kasumi', project: 'Alpha', limit: 10 });
  });
});

test('docs tools and resources/read expose filesystem-backed reference docs', async () => {
  const { router } = makeRouter();
  await withServer(router, async (baseUrl) => {
    const docsList = await callTool(baseUrl, 'docs_list', {});
    assert.equal(docsList.response.status, 200);
    assert.ok(docsList.body.result.docs.some((doc) => doc.name === 'MINATO_MCP_SERVER_PLAN'));

    const doc = await callTool(baseUrl, 'docs_read', { doc: 'MINATO_MCP_SERVER_PLAN' });
    assert.equal(doc.response.status, 200);
    assert.equal(doc.body.result.doc, 'MINATO_MCP_SERVER_PLAN');
    assert.match(doc.body.result.content, /Minato MCP Server Plan/);

    const resourceListResponse = await fetch(`${baseUrl}/resources/list`);
    assert.equal(resourceListResponse.status, 200);
    const resourceList = await resourceListResponse.json();
    assert.ok(resourceList.resources.some((item) => item.uri === 'minato://docs/list'));

    const resourceResponse = await fetch(`${baseUrl}/resources/read`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ uri: 'minato://docs/MINATO_MCP_SERVER_PLAN' }),
    });
    assert.equal(resourceResponse.status, 200);
    const resourceBody = await resourceResponse.json();
    assert.equal(resourceBody.mime_type, 'text/markdown');
    assert.equal(resourceBody.data.doc, 'MINATO_MCP_SERVER_PLAN');
  });
});

test('invalid envelope returns INVALID_REQUEST', async () => {
  const { router } = makeRouter();
  await withServer(router, async (baseUrl) => {
    const response = await fetch(`${baseUrl}/tools/call`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: 'bad-1',
        method: 'wrong/method',
      }),
    });

    assert.equal(response.status, 400);
    const body = await response.json();
    assert.equal(body.error.code, -32600);
    assert.equal(body.error.message, 'INVALID_REQUEST');
  });
});

test('upstream 404 normalizes to NOT_FOUND', async () => {
  const { router } = makeRouter({
    chatGetHistory: async () => {
      const error = new Error('agent not found');
      error.status = 404;
      throw error;
    },
  });

  await withServer(router, async (baseUrl) => {
    const response = await fetch(`${baseUrl}/tools/call`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 'req-404',
        method: 'tools/call',
        params: {
          name: 'chat_get_history',
          arguments: {
            agent_id: 'missing-agent',
          },
        },
      }),
    });

    assert.equal(response.status, 404);
    const body = await response.json();
    assert.equal(body.error.code, -32010);
    assert.equal(body.error.message, 'NOT_FOUND');
  });
});
