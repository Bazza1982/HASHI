const fs = require('node:fs');
const path = require('node:path');

function readJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8').replace(/^\uFEFF/, ''));
  } catch {
    return null;
  }
}

function cleanHost(value) {
  const host = String(value || '').trim().replace(/^\[|\]$/g, '');
  if (!host || ['0.0.0.0', '::'].includes(host) || /[\s/?#@]/u.test(host)) {
    throw new Error('HASHI Workbench requires a connectable configured or published host');
  }
  return host;
}

function cleanPort(value) {
  const port = Number(value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('HASHI Workbench requires a configured or published port');
  }
  return port;
}

function renderBaseUrl(host, port) {
  const renderedHost = host.includes(':') ? `[${host}]` : host;
  return `http://${renderedHost}:${port}`;
}

function validateExplicitBaseUrl(value) {
  const parsed = new URL(String(value));
  if (!['http:', 'https:'].includes(parsed.protocol)
      || !parsed.hostname
      || !parsed.port
      || parsed.username
      || parsed.password
      || !['', '/'].includes(parsed.pathname)
      || parsed.search
      || parsed.hash) {
    throw new Error('BRIDGE_U_API must be an HTTP(S) origin with an explicit port');
  }
  cleanHost(parsed.hostname);
  cleanPort(parsed.port);
  return String(value).replace(/\/$/u, '');
}

function resolveBridgeEndpoint({ root, env = process.env } = {}) {
  const projectRoot = path.resolve(root || path.join(__dirname, '..'));
  const configPath = path.resolve(env.HASHI_CONFIG_PATH || path.join(projectRoot, 'agents.json'));
  const config = readJson(configPath) || {};
  const globalConfig = config.global || {};
  const instanceId = String(globalConfig.instance_id || '').trim().toUpperCase();

  if (env.BRIDGE_U_API) {
    const baseUrl = validateExplicitBaseUrl(env.BRIDGE_U_API);
    const parsed = new URL(baseUrl);
    return {
      baseUrl,
      host: parsed.hostname,
      port: cleanPort(parsed.port),
      instanceId,
      source: 'environment',
    };
  }

  const explicitHost = String(env.HASHI_BRIDGE_API_HOST || '').trim();
  const explicitPort = String(env.HASHI_BRIDGE_API_PORT || '').trim();
  if (explicitHost || explicitPort) {
    const host = cleanHost(explicitHost || globalConfig.api_host);
    const port = cleanPort(explicitPort || globalConfig.workbench_port);
    return { baseUrl: renderBaseUrl(host, port), host, port, instanceId, source: 'environment' };
  }

  const statePath = path.resolve(
    env.HASHI_SERVICE_ENDPOINTS_FILE
      || path.join(projectRoot, 'state', 'service_endpoints.json'),
  );
  const state = readJson(statePath);
  const published = state && state.services && state.services.workbench;
  const stateOwner = String((state && state.instance_id) || '').trim().toUpperCase();
  const endpointOwner = String((published && published.instance_id) || '').trim().toUpperCase();
  if (published && instanceId && stateOwner === instanceId && endpointOwner === instanceId) {
    const host = cleanHost(published.host);
    const port = cleanPort(published.port);
    return { baseUrl: renderBaseUrl(host, port), host, port, instanceId, source: 'live-registry' };
  }

  const host = cleanHost(globalConfig.api_host);
  const port = cleanPort(globalConfig.workbench_port);
  return { baseUrl: renderBaseUrl(host, port), host, port, instanceId, source: 'instance-config' };
}

module.exports = { resolveBridgeEndpoint };
