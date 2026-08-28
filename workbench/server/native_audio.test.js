import assert from 'node:assert/strict';
import test from 'node:test';

import { NativeAudioBridge } from './native_audio.js';

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

test('Workbench submits provider-neutral audio through the Session contract', async () => {
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    const parsed = new URL(url);
    const method = options.method || 'GET';
    calls.push({ path: parsed.pathname + parsed.search, method, options });
    if (parsed.pathname === '/api/v1/capabilities') {
      return jsonResponse({ ok: true, audio: { input: true, output: true } });
    }
    if (parsed.pathname === '/api/v1/agents') {
      return jsonResponse({
        ok: true,
        agents: [{ agent_id: 'arale', native_audio_chat: true }],
      });
    }
    if (parsed.pathname === '/api/v1/sessions' && method === 'GET') {
      return jsonResponse({ ok: true, sessions: [] });
    }
    if (parsed.pathname === '/api/v1/sessions' && method === 'POST') {
      return jsonResponse({
        ok: true,
        session: { session_id: 'ses-1', agent_id: 'arale', title: 'Workbench voice' },
      }, 201);
    }
    if (parsed.pathname.endsWith('/event-consumers')) {
      return jsonResponse({
        ok: true,
        consumer: { consumer_id: 'consumer-1' },
      }, 201);
    }
    if (parsed.pathname.endsWith('/attachments') && method === 'POST') {
      return jsonResponse({
        ok: true,
        attachment: { attachment_id: 'att-1' },
      }, 201);
    }
    if (parsed.pathname.endsWith('/content') && method === 'PUT') {
      assert.deepEqual(Buffer.from(options.body), Buffer.from('OggSvoice'));
      return jsonResponse({ ok: true, attachment: { attachment_id: 'att-1' } });
    }
    if (parsed.pathname.endsWith('/commit')) {
      return jsonResponse({ ok: true, attachment: { attachment_id: 'att-1' } });
    }
    if (parsed.pathname.endsWith('/runs')) {
      return jsonResponse({
        ok: true,
        run_id: 'run-1',
        request_id: 'req-1',
      }, 202);
    }
    throw new Error(`unexpected request: ${method} ${parsed.pathname}`);
  };
  const client = new NativeAudioBridge('http://hashi.test', fetchImpl);

  const result = await client.submitTurn({
    agentId: 'arale',
    caption: 'Optional caption',
    idempotencyKey: 'stable-turn-key',
    files: [
      {
        originalname: 'voice.ogg',
        mimetype: 'audio/ogg',
        buffer: Buffer.from('OggSvoice'),
      },
    ],
  });

  assert.equal(result.session_id, 'ses-1');
  assert.equal(result.consumer_id, 'consumer-1');
  const runCall = calls.find((call) => call.path.endsWith('/runs'));
  const runBody = JSON.parse(runCall.options.body);
  assert.deepEqual(runBody.message.content, [
    {
      type: 'audio',
      attachment_id: 'att-1',
      semantic_role: 'voice_message',
      mime_type: 'audio/ogg',
    },
    { type: 'text', text: 'Optional caption' },
  ]);
  assert.deepEqual(runBody.response_preferences, {
    audio_for_voice_input: true,
    assistant_text: true,
  });
  assert.equal(runBody.idempotency_key, 'stable-turn-key');
  assert.equal(JSON.stringify(runBody).includes('OggSvoice'), false);
});

test('Workbench fails closed when the selected Agent lacks native audio', async () => {
  const fetchImpl = async (url) => {
    const path = new URL(url).pathname;
    if (path === '/api/v1/capabilities') {
      return jsonResponse({ ok: true, audio: { input: true, output: true } });
    }
    return jsonResponse({
      ok: true,
      agents: [{ agent_id: 'arale', native_audio_chat: false }],
    });
  };
  const client = new NativeAudioBridge('http://hashi.test', fetchImpl);

  await assert.rejects(
    client.submitTurn({
      agentId: 'arale',
      files: [{ mimetype: 'audio/ogg', buffer: Buffer.from('OggSvoice') }],
    }),
    /not configured/,
  );
});
