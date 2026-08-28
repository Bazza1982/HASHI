import crypto from 'node:crypto';

function encoded(value) {
  return encodeURIComponent(String(value || ''));
}

async function responseJson(response, purpose) {
  const raw = await response.text();
  let body = {};
  if (raw) {
    try {
      body = JSON.parse(raw);
    } catch {
      body = { error: raw };
    }
  }
  if (!response.ok || body.ok === false) {
    const message = body.error || body.message || `${purpose} failed`;
    const error = new Error(`${message} (HTTP ${response.status})`);
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

export class NativeAudioBridge {
  constructor(baseUrl, fetchImpl = fetch) {
    this.baseUrl = String(baseUrl || '').replace(/\/$/, '');
    this.fetch = fetchImpl;
  }

  async json(path, options = {}, purpose = 'HASHI native audio request') {
    const headers = { ...(options.headers || {}) };
    if (options.body !== undefined && !headers['Content-Type']) {
      headers['Content-Type'] = 'application/json';
    }
    const response = await this.fetch(`${this.baseUrl}${path}`, {
      ...options,
      headers,
      body:
        options.body === undefined || typeof options.body === 'string'
          ? options.body
          : JSON.stringify(options.body),
    });
    return responseJson(response, purpose);
  }

  async requireCapability(agentId) {
    const capabilities = await this.json('/api/v1/capabilities');
    if (!capabilities.audio?.input || !capabilities.audio?.output) {
      const error = new Error('Native audio chat is not enabled by HASHI.');
      error.status = 503;
      throw error;
    }
    const catalogue = await this.json('/api/v1/agents');
    const agent = (catalogue.agents || []).find((item) => item.agent_id === agentId);
    if (!agent) {
      const error = new Error(`Unknown HASHI agent: ${agentId}`);
      error.status = 404;
      throw error;
    }
    if (!agent.native_audio_chat) {
      const error = new Error(`Native audio chat is not configured for ${agentId}.`);
      error.status = 409;
      throw error;
    }
  }

  async ensureSession(agentId, sessionId = '') {
    if (sessionId) {
      try {
        const existing = await this.json(
          `/api/v1/sessions/${encoded(sessionId)}`,
          {},
          'restore Workbench audio Session',
        );
        if (existing.session?.agent_id === agentId) return existing.session;
      } catch (error) {
        if (![404, 409].includes(Number(error.status))) throw error;
      }
    }

    const listing = await this.json(
      `/api/v1/sessions?agent_id=${encoded(agentId)}&limit=100`,
      {},
      'list Workbench audio Sessions',
    );
    const reusable = (listing.sessions || []).find(
      (session) => session.agent_id === agentId && session.title === 'Workbench voice',
    );
    if (reusable) return reusable;

    const created = await this.json(
      '/api/v1/sessions',
      {
        method: 'POST',
        body: {
          agent_id: agentId,
          title: 'Workbench voice',
          surface: 'workbench',
          channel_key: `workbench:${agentId}`,
        },
      },
      'create Workbench audio Session',
    );
    return created.session;
  }

  async ensureConsumer(sessionId, agentId, consumerId = '') {
    const stableId = consumerId || `workbench-${agentId}-${sessionId}`;
    const created = await this.json(
      `/api/v1/sessions/${encoded(sessionId)}/event-consumers`,
      { method: 'POST', body: { consumer_id: stableId } },
      'create Workbench Event consumer',
    );
    return created.consumer;
  }

  async submitTurn({
    agentId,
    caption = '',
    files = [],
    sessionId = '',
    consumerId = '',
    idempotencyKey = '',
  }) {
    if (!agentId) throw new Error('agentId is required');
    if (!files.length || files.some((file) => !String(file.mimetype || '').startsWith('audio/'))) {
      throw new Error('Native audio Turn requires one or more audio files.');
    }
    await this.requireCapability(agentId);
    const session = await this.ensureSession(agentId, sessionId);
    const consumer = await this.ensureConsumer(session.session_id, agentId, consumerId);
    const content = [];

    for (const file of files) {
      const bytes = Buffer.from(file.buffer);
      const digest = crypto.createHash('sha256').update(bytes).digest('hex');
      const staged = await this.json(
        `/api/v1/sessions/${encoded(session.session_id)}/attachments`,
        {
          method: 'POST',
          body: {
            filename: file.originalname || 'voice-message',
            media_type: file.mimetype,
            size_bytes: bytes.length,
            sha256: digest,
            semantic_role: 'voice_message',
          },
        },
        'stage Workbench audio attachment',
      );
      const attachmentId = staged.attachment.attachment_id;
      await responseJson(
        await this.fetch(
          `${this.baseUrl}/api/v1/sessions/${encoded(session.session_id)}`
            + `/attachments/${encoded(attachmentId)}/content`,
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/octet-stream' },
            body: bytes,
          },
        ),
        'upload Workbench audio attachment',
      );
      await this.json(
        `/api/v1/sessions/${encoded(session.session_id)}`
          + `/attachments/${encoded(attachmentId)}/commit`,
        { method: 'POST', body: {} },
        'commit Workbench audio attachment',
      );
      content.push({
        type: 'audio',
        attachment_id: attachmentId,
        semantic_role: 'voice_message',
        mime_type: file.mimetype,
      });
    }
    if (String(caption || '').trim()) {
      content.push({ type: 'text', text: String(caption).trim() });
    }

    const stableKey = idempotencyKey || crypto.randomUUID();
    const accepted = await this.json(
      `/api/v1/sessions/${encoded(session.session_id)}/runs`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': stableKey },
        body: {
          idempotency_key: stableKey,
          surface: 'workbench',
          client_id: consumer.consumer_id,
          message: { content },
          response_preferences: {
            audio_for_voice_input: true,
            assistant_text: true,
          },
        },
      },
      'start Workbench native audio Turn',
    );
    return {
      ...accepted,
      session_id: session.session_id,
      consumer_id: consumer.consumer_id,
      idempotency_key: stableKey,
    };
  }

  pollEvents(sessionId, consumerId) {
    return this.json(
      `/api/v1/sessions/${encoded(sessionId)}/events?consumer_id=${encoded(consumerId)}&limit=200`,
      {},
      'poll Workbench native audio Events',
    );
  }

  acknowledge(sessionId, consumerId, sequence) {
    return this.json(
      `/api/v1/sessions/${encoded(sessionId)}`
        + `/event-consumers/${encoded(consumerId)}/ack`,
      { method: 'POST', body: { sequence } },
      'acknowledge Workbench native audio Events',
    );
  }

  decideTranscript(sessionId, transcriptId, decision) {
    return this.json(
      `/api/v1/sessions/${encoded(sessionId)}`
        + `/voice-transcripts/${encoded(transcriptId)}`,
      { method: 'POST', body: { decision } },
      'decide Workbench Safe Voice transcript',
    );
  }

  async fetchAsset(sessionId, assetId) {
    return this.fetch(
      `${this.baseUrl}/api/v1/sessions/${encoded(sessionId)}`
        + `/audio-assets/${encoded(assetId)}`,
    );
  }
}
