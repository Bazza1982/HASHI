import test from 'node:test';
import assert from 'node:assert/strict';
import express from 'express';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const WORKBENCH_DIR = path.resolve(__dirname, '..');
const REPO_ROOT = path.resolve(WORKBENCH_DIR, '..');
const PROJECTS_DIR = path.join(WORKBENCH_DIR, 'data', 'projects');
const ARTEFACTS_FILE = path.join(WORKBENCH_DIR, 'data', 'minato_artefacts.json');
const TODAY = new Date().toISOString().slice(0, 10);

function projectSlug(name) {
  return String(name || '')
    .toLowerCase()
    .replace(/['"]/g, '')
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    || 'default';
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function startExpress(app) {
  const server = await new Promise((resolve) => {
    const instance = app.listen(0, '127.0.0.1', () => resolve(instance));
  });
  const address = server.address();
  return {
    server,
    url: `http://127.0.0.1:${address.port}`,
    async close() {
      await new Promise((resolve, reject) => server.close((error) => (error ? reject(error) : resolve())));
    },
  };
}

async function withBridgeStub(fn) {
  const app = express();
  app.use(express.json());

  const sentMessages = [];

  app.get('/api/health', (_req, res) => {
    res.json({ ok: true, agents: ['mcp_echo'] });
  });

  app.get('/api/agents', (_req, res) => {
    res.json({ agents: [{ id: 'mcp_echo', name: 'mcp_echo' }] });
  });

  app.post('/api/chat', (req, res) => {
    sentMessages.push(req.body || {});
    res.json({
      ok: true,
      request_id: `req_${sentMessages.length}`,
      echoed_text: req.body?.text || '',
    });
  });

  app.get('/api/project-chat/:agent/:project', (req, res) => {
    res.json({
      entries: [
        {
          agent: req.params.agent,
          project: decodeURIComponent(req.params.project),
          text: 'stub project chat',
        },
      ],
      count: 1,
    });
  });

  const bridge = await startExpress(app);
  try {
    await fn({
      baseUrl: bridge.url,
      sentMessages,
    });
  } finally {
    await bridge.close();
  }
}

async function withKasumiStub(fn) {
  const app = express();
  app.use(express.json());

  const toolCalls = [];
  const resourceReads = [];

  app.post('/api/kasumi/mcp/v1/resources/read', (req, res) => {
    resourceReads.push(req.body || {});
    res.json({
      uri: req.body?.uri || '',
      mime_type: 'application/json',
      data: {
        source: 'kasumi-stub',
        uri: req.body?.uri || '',
      },
    });
  });

  app.post('/api/kasumi/mcp/v1/tools/call', (req, res) => {
    toolCalls.push(req.body || {});
    res.json({
      jsonrpc: '2.0',
      id: req.body?.id || null,
      result: {
        ok: true,
        delegated: true,
        tool: req.body?.params?.name || null,
        arguments: req.body?.params?.arguments || {},
      },
    });
  });

  const kasumi = await startExpress(app);
  try {
    await fn({
      baseUrl: `${kasumi.url}/api/kasumi/mcp/v1`,
      toolCalls,
      resourceReads,
    });
  } finally {
    await kasumi.close();
  }
}

async function withTempAgentsFixture(fn) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'minato-mcp-agents-'));
  const workspaceDir = path.join(root, 'workspaces', 'mcp_echo');
  fs.mkdirSync(workspaceDir, { recursive: true });

  const transcriptPath = path.join(workspaceDir, 'transcript.jsonl');
  fs.writeFileSync(transcriptPath, [
    JSON.stringify({ role: 'user', text: 'hello from user', source: 'text' }),
    JSON.stringify({ role: 'assistant', text: 'hello from assistant', source: 'text' }),
    '',
  ].join('\n'), 'utf8');

  const agentsJsonPath = path.join(root, 'agents.json');
  fs.writeFileSync(agentsJsonPath, JSON.stringify({
    global: { authorized_id: 0, workbench_port: 18802 },
    agents: [
      {
        name: 'mcp_echo',
        display_name: 'MCP Echo',
        emoji: '🧪',
        type: 'flex',
        engine: 'codex-cli',
        workspace_dir: 'workspaces/mcp_echo',
        is_active: true,
        model: 'gpt-5.4',
        allowed_backends: [{ engine: 'codex-cli', model: 'gpt-5.4' }],
        active_backend: 'codex-cli',
      },
    ],
  }, null, 2), 'utf8');

  try {
    await fn({ root, agentsJsonPath, transcriptPath });
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

async function withProjectFixture(project, fn) {
  const slug = projectSlug(project);
  const projectDir = path.join(PROJECTS_DIR, slug);
  const conversationsDir = path.join(projectDir, 'conversations');
  const logDir = path.join(projectDir, 'log');
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'minato-mcp-project-'));
  const artefactPath = path.join(tempDir, 'draft.md');

  fs.mkdirSync(conversationsDir, { recursive: true });
  fs.mkdirSync(logDir, { recursive: true });
  fs.writeFileSync(path.join(conversationsDir, `${TODAY}.jsonl`), [
    JSON.stringify({
      ts: `${TODAY}T10:00:00.000Z`,
      session_id: 'sess-seed',
      direction: 'outbound',
      agent: 'mcp_echo',
      user: 'user',
      text: 'seed conversation',
      project,
      shimanto_phases: ['planning'],
      nagare_workflows: ['smoke-test'],
      scope: 'E2E verification',
    }),
    '',
  ].join('\n'), 'utf8');
  fs.writeFileSync(path.join(logDir, `${TODAY}.md`), `# ${project}\n\nSeed markdown log.\n`, 'utf8');
  fs.writeFileSync(artefactPath, '# Draft\n\nHello from Minato MCP E2E.\n', 'utf8');

  const artefactsBackup = fs.existsSync(ARTEFACTS_FILE) ? fs.readFileSync(ARTEFACTS_FILE, 'utf8') : null;
  fs.mkdirSync(path.dirname(ARTEFACTS_FILE), { recursive: true });
  fs.writeFileSync(ARTEFACTS_FILE, JSON.stringify({ last_id: 0, artefacts: [] }, null, 2) + '\n', 'utf8');

  const runId = 'run-smoke-test-20260326-072341';
  const runPath = path.join(REPO_ROOT, 'flow', 'runs', runId, 'state.json');
  const runBackup = fs.readFileSync(runPath, 'utf8');

  try {
    await fn({
      project,
      slug,
      artefactPath,
      runId,
      runPath,
    });
  } finally {
    fs.rmSync(projectDir, { recursive: true, force: true });
    fs.rmSync(tempDir, { recursive: true, force: true });
    fs.writeFileSync(runPath, runBackup, 'utf8');
    if (artefactsBackup === null) {
      fs.rmSync(ARTEFACTS_FILE, { force: true });
    } else {
      fs.writeFileSync(ARTEFACTS_FILE, artefactsBackup, 'utf8');
    }
  }
}

async function withWorkbenchServer(env, fn) {
  const port = 3311 + Math.floor(Math.random() * 2000);
  const child = spawn(process.execPath, ['server/index.js'], {
    cwd: WORKBENCH_DIR,
    env: {
      ...process.env,
      ...env,
      PORT: String(port),
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  let stdout = '';
  let stderr = '';
  child.stdout.on('data', (chunk) => {
    stdout += chunk.toString();
  });
  child.stderr.on('data', (chunk) => {
    stderr += chunk.toString();
  });

  const baseUrl = `http://127.0.0.1:${port}`;
  const started = Date.now();
  let ready = false;

  while (Date.now() - started < 10000) {
    if (child.exitCode !== null) break;
    try {
      const response = await fetch(`${baseUrl}/api/config`);
      if (response.ok) {
        ready = true;
        break;
      }
    } catch {}
    await sleep(150);
  }

  if (!ready) {
    child.kill('SIGTERM');
    throw new Error(`Workbench server did not start.\nstdout:\n${stdout}\nstderr:\n${stderr}`);
  }

  try {
    await fn(baseUrl);
  } finally {
    child.kill('SIGTERM');
    await new Promise((resolve) => child.once('exit', () => resolve()));
  }
}

async function callTool(baseUrl, name, args = {}, headers = {}) {
  const response = await fetch(`${baseUrl}/api/minato/mcp/v1/tools/call`, {
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

test('Minato MCP end-to-end surface works through the real server', async () => {
  const project = 'Minato MCP E2E Project';

  await withBridgeStub(async ({ baseUrl: bridgeUrl, sentMessages }) => {
    await withKasumiStub(async ({ baseUrl: kasumiUrl, toolCalls, resourceReads }) => {
      await withTempAgentsFixture(async ({ agentsJsonPath, transcriptPath }) => {
        await withProjectFixture(project, async ({ slug, artefactPath, runId }) => {
          await withWorkbenchServer({
            BRIDGE_U_API: bridgeUrl,
            BRIDGE_U_AGENTS_JSON: agentsJsonPath,
            KASUMI_MCP_API: kasumiUrl,
          }, async (baseUrl) => {
            const sessionHeaders = { 'x-minato-session': 'sess-minato-mcp-e2e' };

            const toolsListResponse = await fetch(`${baseUrl}/api/minato/mcp/v1/tools/list`);
            assert.equal(toolsListResponse.status, 200);
            const toolsList = await toolsListResponse.json();
            assert.equal(toolsList.tools.length, 23);

            const resourcesListResponse = await fetch(`${baseUrl}/api/minato/mcp/v1/resources/list`);
            assert.equal(resourcesListResponse.status, 200);
            const resourcesList = await resourcesListResponse.json();
            assert.ok(resourcesList.resources.some((item) => item.uri === `minato://project/${slug}/state`));
            assert.ok(resourcesList.resources.some((item) => item.uri === 'minato://prompts/list'));
            assert.ok(resourcesList.resources.some((item) => item.uri === 'minato://chat/mcp_echo/recent'));

            const promptsListResponse = await fetch(`${baseUrl}/api/minato/mcp/v1/prompts/list`);
            assert.equal(promptsListResponse.status, 200);
            const promptsList = await promptsListResponse.json();
            assert.ok(promptsList.prompts.length >= 4);
            assert.ok(promptsList.prompts.some((item) => item.name === 'minato_operator_handoff'));

            const promptReadResponse = await fetch(`${baseUrl}/api/minato/mcp/v1/prompts/read`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name: 'minato_operator_handoff' }),
            });
            assert.equal(promptReadResponse.status, 200);
            const promptRead = await promptReadResponse.json();
            assert.equal(promptRead.name, 'minato_operator_handoff');

            const promptRenderResponse = await fetch(`${baseUrl}/api/minato/mcp/v1/prompts/render`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                name: 'minato_operator_handoff',
                arguments: {
                  project,
                  current_state: 'E2E validation in progress',
                  next_actions: 'Review the final validation report',
                },
              }),
            });
            assert.equal(promptRenderResponse.status, 200);
            const promptRender = await promptRenderResponse.json();
            assert.match(promptRender.rendered, /E2E validation in progress/);

            const projectList = await callTool(baseUrl, 'project_list');
            assert.equal(projectList.response.status, 200);
            assert.ok(projectList.body.result.projects.some((item) => item.slug === slug));

            const stateResult = await callTool(baseUrl, 'project_get_state', { project });
            assert.equal(stateResult.response.status, 200);
            assert.equal(stateResult.body.result.project, project);
            assert.deepEqual(stateResult.body.result.shimanto_phases, ['planning']);

            const switchResult = await callTool(baseUrl, 'project_switch', { project }, sessionHeaders);
            assert.equal(switchResult.response.status, 200);
            assert.equal(switchResult.body.result.active_project, project);

            const currentPhase = await callTool(baseUrl, 'shimanto_get_current_phase', {}, sessionHeaders);
            assert.equal(currentPhase.response.status, 200);
            assert.deepEqual(currentPhase.body.result.phases, ['planning']);

            const phaseList = await callTool(baseUrl, 'shimanto_list_phases', {}, sessionHeaders);
            assert.equal(phaseList.response.status, 200);
            assert.ok(phaseList.body.result.all_phases_seen.includes('planning'));

            const phaseTransition = await callTool(baseUrl, 'shimanto_transition_phase', {
              to_phases: ['execution'],
              note: 'E2E transition',
            }, sessionHeaders);
            assert.equal(phaseTransition.response.status, 200);
            assert.deepEqual(phaseTransition.body.result.phases, ['execution']);

            const workflows = await callTool(baseUrl, 'nagare_list_workflows', {}, sessionHeaders);
            assert.equal(workflows.response.status, 200);
            assert.ok(workflows.body.result.workflows.includes('smoke-test'));

            const workflowDag = await callTool(baseUrl, 'nagare_get_workflow_dag', {
              workflow_id: 'smoke-test',
            });
            assert.equal(workflowDag.response.status, 200);
            assert.equal(workflowDag.body.result.workflow_id, 'smoke-test');
            assert.ok(Array.isArray(workflowDag.body.result.steps));

            const runStatus = await callTool(baseUrl, 'nagare_get_run_status', {
              workflow_id: 'smoke-test',
              run_id: runId,
            });
            assert.equal(runStatus.response.status, 200);
            const stepId = runStatus.body.result.steps[0].step_id;
            assert.ok(stepId);

            const updateStep = await callTool(baseUrl, 'nagare_update_step_status', {
              run_id: runId,
              step_id: stepId,
              status: 'completed',
              note: 'E2E updated this step',
            }, sessionHeaders);
            assert.equal(updateStep.response.status, 200);
            assert.equal(updateStep.body.result.ok, true);

            const fileArtefact = await callTool(baseUrl, 'artefacts_create', {
              name: 'Draft Report',
              type: 'file',
              path: artefactPath,
              nagare_step: stepId,
              shimanto_phase: 'execution',
            }, sessionHeaders);
            assert.equal(fileArtefact.response.status, 200);
            assert.equal(fileArtefact.body.result.artefact_id, 'art_001');

            const kasumiArtefact = await callTool(baseUrl, 'artefacts_create', {
              name: 'Workbook Mirror',
              type: 'kasumi',
              kasumi_id: 'wb_001',
              kasumi_module: 'nexcel',
            }, sessionHeaders);
            assert.equal(kasumiArtefact.response.status, 200);
            assert.equal(kasumiArtefact.body.result.artefact_id, 'art_002');

            const listedArtefacts = await callTool(baseUrl, 'artefacts_list', {}, sessionHeaders);
            assert.equal(listedArtefacts.response.status, 200);
            assert.equal(listedArtefacts.body.result.artefacts.length, 2);

            const readFileArtefact = await callTool(baseUrl, 'artefacts_read', { artefact_id: 'art_001' });
            assert.equal(readFileArtefact.response.status, 200);
            assert.match(readFileArtefact.body.result.content, /Hello from Minato MCP E2E/);

            const readKasumiArtefact = await callTool(baseUrl, 'artefacts_read', { artefact_id: 'art_002' });
            assert.equal(readKasumiArtefact.response.status, 200);
            assert.equal(readKasumiArtefact.body.result.data.source, 'kasumi-stub');
            assert.equal(resourceReads.length, 1);

            const linkedArtefact = await callTool(baseUrl, 'artefacts_link', {
              artefact_id: 'art_001',
              shimanto_phase: 'review',
            }, sessionHeaders);
            assert.equal(linkedArtefact.response.status, 200);
            assert.ok(linkedArtefact.body.result.artefact.linked_shimanto_phases.includes('review'));

            const kasumiCall = await callTool(baseUrl, 'artefacts_kasumi_call', {
              artefact_id: 'art_002',
              tool_name: 'nexcel_get_data',
              arguments: { workbookId: 'wb_001' },
            }, sessionHeaders);
            assert.equal(kasumiCall.response.status, 200);
            assert.equal(kasumiCall.body.result.result.delegated, true);
            assert.equal(toolCalls.length, 1);

            const appendLog = await callTool(baseUrl, 'log_append', {
              type: 'decision',
              summary: 'E2E log append',
              details: ['validated from end-to-end test'],
            }, sessionHeaders);
            assert.equal(appendLog.response.status, 200);
            assert.equal(appendLog.body.result.ok, true);

            const queriedLog = await callTool(baseUrl, 'log_query', { limit: 20 }, sessionHeaders);
            assert.equal(queriedLog.response.status, 200);
            assert.ok(queriedLog.body.result.count >= 1);

            const projectChat = await callTool(baseUrl, 'log_project_chat', {
              agent: 'mcp_echo',
              limit: 5,
            }, sessionHeaders);
            assert.equal(projectChat.response.status, 200);
            assert.equal(projectChat.body.result.entries[0].text, 'stub project chat');

            const sentChat = await callTool(baseUrl, 'chat_send', {
              agent_id: 'mcp_echo',
              text: 'Please review this output.',
              inject_context: true,
            }, sessionHeaders);
            assert.equal(sentChat.response.status, 200);
            assert.equal(sentChat.body.result.ok, true);
            assert.equal(sentMessages.length, 1);
            assert.match(sentMessages[0].text, /\[MINATO CONTEXT/);
            assert.match(sentMessages[0].text, /Please review this output\./);

            const history = await callTool(baseUrl, 'chat_get_history', {
              agent_id: 'mcp_echo',
              limit: 5,
            });
            assert.equal(history.response.status, 200);
            assert.ok(history.body.result.messages.length >= 1);

            const offset = fs.statSync(transcriptPath).size;
            fs.appendFileSync(transcriptPath, `${JSON.stringify({ role: 'assistant', text: 'new reply from agent', source: 'text' })}\n`, 'utf8');
            const poll = await callTool(baseUrl, 'chat_poll', {
              agent_id: 'mcp_echo',
              offset,
            });
            assert.equal(poll.response.status, 200);
            assert.equal(poll.body.result.messages[0].content, 'new reply from agent');

            const docsList = await callTool(baseUrl, 'docs_list');
            assert.equal(docsList.response.status, 200);
            assert.ok(docsList.body.result.docs.some((item) => item.name === 'MINATO_README'));

            const docsRead = await callTool(baseUrl, 'docs_read', { doc: 'MINATO_README' });
            assert.equal(docsRead.response.status, 200);
            assert.match(docsRead.body.result.content, /Minato MCP/i);

            const readProjectState = await fetch(`${baseUrl}/api/minato/mcp/v1/resources/read`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ uri: `minato://project/${slug}/state` }),
            });
            assert.equal(readProjectState.status, 200);
            const projectStateResource = await readProjectState.json();
            assert.equal(projectStateResource.data.project, project);

            const readArtefacts = await fetch(`${baseUrl}/api/minato/mcp/v1/resources/read`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ uri: `minato://artefacts/${slug}` }),
            });
            assert.equal(readArtefacts.status, 200);
            const artefactsResource = await readArtefacts.json();
            assert.equal(artefactsResource.data.artefacts.length, 2);

            const readLogMarkdown = await fetch(`${baseUrl}/api/minato/mcp/v1/resources/read`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ uri: `minato://log/${slug}/markdown/today` }),
            });
            assert.equal(readLogMarkdown.status, 200);
            const markdownResource = await readLogMarkdown.json();
            assert.equal(markdownResource.mime_type, 'text/markdown');

            const readChatResource = await fetch(`${baseUrl}/api/minato/mcp/v1/resources/read`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ uri: 'minato://chat/mcp_echo/recent' }),
            });
            assert.equal(readChatResource.status, 200);
            const chatResource = await readChatResource.json();
            assert.equal(chatResource.data.agent_id, 'mcp_echo');

            const readPromptResource = await fetch(`${baseUrl}/api/minato/mcp/v1/resources/read`, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ uri: 'minato://prompt/minato_operator_handoff' }),
            });
            assert.equal(readPromptResource.status, 200);
            const promptResource = await readPromptResource.json();
            assert.equal(promptResource.data.name, 'minato_operator_handoff');
          });
        });
      });
    });
  });
});
