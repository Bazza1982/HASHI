"""Local connection service: explicit selection, real adapter probe and preserving save."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
from uuid import uuid4

from adapters.registry import packaged_backend_engines
from orchestrator.config import (
    FlexibleAgentConfig,
    GlobalConfig,
    default_global_tools_config,
)
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.flexible_backend_registry import (
    get_available_models, get_default_model, get_backend_entry, allows_custom_models, PROVIDER_ONLY_ENGINE_IDS,
)
from orchestrator.pcm import atomic_write_pcm, render_pcm_document
from orchestrator.config_json import (
    ConfigConflictError, ConfigDurabilityError, delete_config_json,
    new_config_json, read_config_json, write_config_json,
)


class ConnectionError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _read(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ConnectionError('CONFIG_INVALID')
    return value


def choices(home):
    """Discovery never starts a CLI, probes a provider, or reads login tokens."""
    config = _read(Path(home) / 'agents.json')
    overrides = [b for a in config.get('agents', []) for b in a.get('allowed_backends', [])]
    rows = []
    for engine in sorted(packaged_backend_engines()):
        if not (engine.endswith('-cli') or engine in PROVIDER_ONLY_ENGINE_IDS):
            continue
        models = list(dict.fromkeys([m for b in overrides if b.get('engine') == engine
                                     for m in ([b.get('model')] + list(b.get('models', []))) if m]
                                    + get_available_models(engine)))
        if not models:
            continue
        program = ''
        if engine.endswith('-cli'):
            program = shutil.which(engine.removesuffix('-cli')) or ''
            if not program or (os.name != 'nt' and program.lower().endswith(('.exe','.cmd','.bat'))):
                continue
            if os.name == 'nt' and program.lower().startswith(('\\\\wsl$', '\\\\wsl.localhost')):
                continue
        # API routes requiring additional endpoint/OAuth fields need their own local setup.
        if engine in PROVIDER_ONLY_ENGINE_IDS and not get_backend_entry(engine).get('secret_keys'):
            continue
        rows.append({'engine':engine, 'models':models, 'default':get_default_model(engine) or models[0],
                     'kind':'cli' if program else 'api', 'program':program,
                     'custom_models': allows_custom_models(engine) or engine == 'openrouter-api',
                     'authentication':'unverified'})
    return rows


def backend_configuration(engine, model):
    if engine not in packaged_backend_engines():
        raise ConnectionError('ADAPTER_UNSUPPORTED')
    if engine in PROVIDER_ONLY_ENGINE_IDS:
        # Use the canonical HER parser to derive all required profile roles.
        from orchestrator.her_v2.config import HERv2Config, DEFAULT_STAGE_ROLES
        roles = set(DEFAULT_STAGE_ROLES.values())
        profiles = {role:{'engine':engine,'model':model} for role in roles}
        her = {'profiles':profiles}
        HERv2Config.from_mapping(her)
        return 'her-v2', [
            {'engine':'her-v2','model':'role-configured','effort':'zero','her_v2':her},
            {'engine':engine,'model':model,'api_key_secret':'hashiko.connection.' + engine},
        ]
    return engine, [{'engine':engine,'model':model}]


async def validate(home, engine, model, key, *, confirmed=False):
    if not confirmed:
        raise ConnectionError('CONFIRMATION_REQUIRED')
    available = {row['engine']:row for row in choices(home)}
    row = available.get(engine)
    if (row is None or not isinstance(model, str) or not model.strip()
            or any(ord(char) < 32 for char in model)
            or (model not in row['models'] and not row.get('custom_models'))):
        raise ConnectionError('MODEL_UNAVAILABLE')
    if row['kind'] == 'api' and not key:
        raise ConnectionError('CREDENTIAL_REQUIRED')
    active, backends = backend_configuration(engine, model)
    probe_backends = copy.deepcopy(backends)
    for backend in probe_backends:
        backend['tools'] = {'enabled': False}
        if backend.get('engine') in PROVIDER_ONLY_ENGINE_IDS:
            # This transient value applies only to the locally confirmed probe.
            backend['_connection_probe_call_limit'] = 1
        if backend.get('engine') == 'her-v2':
            # Consent covers one check, not automatic paid retries.
            backend['her_v2']['provider_recovery'] = {
                'max_provider_retries': 0, 'capacity_delays_s': [],
            }
    # Probe in a disposable workspace, through the same manager/adapter chain.
    # The selected credential stays in memory and is never copied into prompt/state.
    with tempfile.TemporaryDirectory(prefix='hashi-connect-') as directory:
        root = Path(directory)
        cfg = FlexibleAgentConfig(name='hashiko',workspace_dir=root,system_md=root/'agent.md',
            telegram_token_key='',allowed_backends=probe_backends,active_backend=active,
            access_scope='workspace',extra={
                'ephemeral_session': True,
                'habit_learning_eligible': False,
                'habit_meditation_enabled': False,
            },project_root=root)
        atomic_write_pcm(cfg.agent_md, render_pcm_document(persona='Connection check.',system='Reply with HASHI_CONNECTED only. Do not use tools.'))
        global_cfg = GlobalConfig(authorized_id=0,project_root=root,bridge_home=root,base_logs_dir=root/'logs')
        if row['kind'] == 'cli':
            setattr(global_cfg, engine.removesuffix('-cli') + '_cmd', row['program'])
        manager = FlexibleBackendManager(cfg,global_cfg,{'hashiko.connection.'+engine:key} if key else {})
        manager.runtime = SimpleNamespace(backend_manager=manager)
        try:
            if not await asyncio.wait_for(manager.initialize_active_backend(),timeout=30):
                raise ConnectionError('ADAPTER_NOT_READY')
            response = await asyncio.wait_for(manager.generate_response(
                'Reply with HASHI_CONNECTED only. Do not use tools.', 'connection-'+uuid4().hex,
                silent=True),timeout=90)
            if not response.is_success or not response.text.strip():
                raise ConnectionError(response.error_code or ('AUTHENTICATION_FAILED' if response.http_status in (401,403) else 'PROVIDER_FAILED'))
            return {'engine':engine,'model':model,'verified':True}
        except asyncio.TimeoutError:
            raise ConnectionError('NETWORK_TIMEOUT') from None
        finally:
            await manager.shutdown()


def _snapshot(path):
    try:
        return read_config_json(path)
    except FileNotFoundError:
        return new_config_json(path)
    except (ValueError, json.JSONDecodeError):
        raise ConnectionError('CONFIG_INVALID') from None


def connection_revisions(home):
    """Opaque revisions only; never return credential contents."""
    home = Path(home)
    return {
        'config': _snapshot(home / 'agents.json').revision,
        'secrets': _snapshot(home / 'secrets.json').revision,
    }


def _publish_connection_pair(config_path, cfg, secrets_path, secrets, original_secrets):
    """Revision-checked publication; undo only our own unchanged secret write."""
    secret_revision = None
    try:
        if dict(secrets) != dict(original_secrets):
            secret_revision = write_config_json(secrets_path, secrets)
        write_config_json(config_path, cfg)
    except ConfigDurabilityError:
        # The primitive reports a committed publication with uncertain
        # durability. Never blindly roll it back or retry.
        raise ConnectionError('SAVE_DURABILITY_UNKNOWN') from None
    except (ConfigConflictError, OSError) as exc:
        if secret_revision is not None:
            try:
                if original_secrets.revision is None:
                    delete_config_json(secrets_path, expected_revision=secret_revision)
                else:
                    write_config_json(secrets_path, dict(original_secrets),
                                      expected_revision=secret_revision)
            except (ConfigConflictError, OSError):
                raise ConnectionError('SAVE_PARTIAL') from None
        raise ConnectionError('CONFIG_CHANGED' if isinstance(exc, ConfigConflictError)
                              else 'SAVE_FAILED') from None


def save(home, engine, model, key, *, verified=None, allow_unverified=False,
         replace_confirmed=False, language='en', expected_revisions=None):
    valid_proof = (isinstance(verified, dict) and verified.get('verified') is True
                   and verified.get('engine') == engine and verified.get('model') == model)
    if verified is not None and not valid_proof:
        raise ConnectionError('VALIDATION_MISMATCH')
    if not valid_proof and allow_unverified is not True:
        raise ConnectionError('VALIDATION_REQUIRED')
    if (not isinstance(model, str) or not model.strip()
            or any(ord(char) < 32 for char in model)):
        raise ConnectionError('MODEL_UNAVAILABLE')
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    config_path, secrets_path = home/'agents.json', home/'secrets.json'
    cfg, secrets = _snapshot(config_path), _snapshot(secrets_path)
    if expected_revisions is not None:
        if not isinstance(expected_revisions, dict) or expected_revisions != {
            'config': cfg.revision, 'secrets': secrets.revision,
        }:
            raise ConnectionError('CONFIG_CHANGED')
    original_secrets = secrets.copy()
    if cfg.revision is None:
        cfg.setdefault('global', {})['default_tools'] = default_global_tools_config()
    reference = 'hashiko.connection.' + engine
    if key and secrets.get(reference) and secrets[reference] != key and not replace_confirmed:
        raise ConnectionError('REPLACE_CONFIRMATION_REQUIRED')
    active, backends = backend_configuration(engine,model)
    agents = cfg.setdefault('agents',[])
    agent = next((a for a in agents if a.get('name') == 'hashiko'),None)
    if agent is None:
        agent = {'name':'hashiko','display_name':'小乔' if language == 'zh' else 'Hashiko',
                 'type':'flex','workspace_dir':'workspaces/hashiko','is_active':True,
                 'telegram_token_key':'','access_scope':'workspace'}
        agents.append(agent)
    else:
        agent = copy.deepcopy(agent)
        agents[:] = [agent if a.get('name') == 'hashiko' else a for a in agents]
    old = {b['engine']:b for b in agent.get('allowed_backends',[])}
    for backend in backends:
        if 'tools' in old.get(backend['engine'],{}):
            backend['tools'] = old[backend['engine']]['tools']
        old[backend['engine']] = {**old.get(backend['engine'],{}),**backend}
    agent['allowed_backends'] = list(old.values())
    agent['active_backend'] = active
    agent['connection_revision'] = uuid4().hex
    agent['connection_verification'] = {
        'engine': engine, 'model': model, 'verified': valid_proof,
    }
    if key:
        secrets[reference] = key
    _publish_connection_pair(config_path, cfg, secrets_path, secrets, original_secrets)
    workspace = Path(agent['workspace_dir'])
    if not workspace.is_absolute():
        workspace = home/workspace
    try:
        workspace.mkdir(parents=True,exist_ok=True)
        if not (workspace/'agent.md').exists():
            atomic_write_pcm(workspace/'agent.md',render_pcm_document(
                persona='You are Hashiko (小乔), a concise, helpful personal assistant.',
                system='The user can start work immediately. Telegram and all further setup are optional. Ask only what is needed for their task. Never request credentials in chat. Use the local connection page for secrets. Stay inside your authorized workspace and request access as needed.'))
    except OSError:
        raise ConnectionError('SAVED_WORKSPACE_PENDING') from None
    return {'agent':'hashiko','engine':active,'saved':True,
            'verified':valid_proof,'runtime_ready':False}


async def adopt_if_running(home):
    """Request only Hashiko's existing controlled reload; never restart an instance."""
    import aiohttp
    home=Path(home)
    config=_read(home/'agents.json')
    hashiko=next((a for a in config.get('agents',[]) if a.get('name')=='hashiko'),{})
    endpoints=_read(home/'state'/'service_endpoints.json')
    url=endpoints.get('services',{}).get('workbench',{}).get('base_url','')
    if not url:
        return {'running':False,'adopted':False}
    from urllib.parse import urlparse
    if urlparse(url).hostname not in {'127.0.0.1','localhost','::1'}:
        raise ConnectionError('LOCAL_ENDPOINT_REQUIRED')
    secret=_read(home/'secrets.json').get('workbench_admin_token','')
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        try:
            async with session.get(url+'/api/health') as response:
                if response.status != 200:
                    return {'running':False,'adopted':False}
                health=await response.json()
                expected=str(config.get('global',{}).get('instance_id') or '')
                if expected and health.get('instance_id') != expected:
                    raise ConnectionError('LOCAL_ENDPOINT_MISMATCH')
        except (aiohttp.ClientError,TimeoutError):
            return {'running':False,'adopted':False}
        deadline=asyncio.get_running_loop().time()+90
        request_key='connection-'+str(hashiko.get('connection_revision') or uuid4().hex)
        operation_id=None
        try:
            while asyncio.get_running_loop().time() < deadline:
                if operation_id is None:
                    async with session.post(url+'/api/admin/reboot-agent',
                            headers={'X-Workbench-Token':secret},
                            json={'agent':'hashiko','request_key':request_key}) as response:
                        result=await response.json()
                        if response.status==409 and result.get('error')=='AGENT_BUSY':
                            pass
                        elif response.status==200 and result.get('accepted') is True and result.get('operation_id'):
                            operation_id=str(result['operation_id'])
                        else:
                            raise ConnectionError('SAVED_ADOPTION_PENDING')
                else:
                    async with session.get(url+'/api/admin/reboot-agent/'+operation_id,
                            headers={'X-Workbench-Token':secret}) as response:
                        result=await response.json()
                        if response.status != 200 or result.get('targets') != ['hashiko']:
                            raise ConnectionError('SAVED_ADOPTION_PENDING')
                        if result.get('status')=='succeeded':
                            return {'running':True,'adopted':True,'reload_requested':True,
                                    'operation_id':operation_id}
                        if result.get('status') not in {'accepted','running'}:
                            raise ConnectionError('SAVED_ADOPTION_PENDING')
                await asyncio.sleep(.5)
        except (aiohttp.ClientError,TimeoutError,ValueError):
            raise ConnectionError('SAVED_ADOPTION_PENDING') from None
        raise ConnectionError('SAVED_ADOPTION_PENDING')


async def connect_telegram(home, token, user_id, *, confirmed=False, replace_confirmed=False):
    """Optional local-only Bot setup; never infers ownership from an incoming message."""
    import aiohttp
    if not confirmed:
        raise ConnectionError('CONFIRMATION_REQUIRED')
    if not str(user_id).isdigit() or int(user_id) <= 0:
        raise ConnectionError('AUTHORIZED_USER_ID_REQUIRED')
    if not token or any(c.isspace() for c in token) or '/' in token:
        raise ConnectionError('BOT_TOKEN_INVALID')
    home=Path(home)
    cfg,secrets=_snapshot(home/'agents.json'),_snapshot(home/'secrets.json')
    original_secrets=secrets.copy()
    agent=next((a for a in cfg.get('agents',[]) if a.get('name')=='hashiko'),None)
    if agent is None:
        raise ConnectionError('BACKEND_CONNECTION_REQUIRED')
    existing_id=int(secrets.get('authorized_telegram_id') or cfg.get('global',{}).get('authorized_id') or 0)
    if existing_id and existing_id != int(user_id):
        raise ConnectionError('AUTHORIZED_USER_CONFLICT')
    reference='hashiko.telegram'
    if secrets.get(reference) and secrets[reference] != token and not replace_confirmed:
        raise ConnectionError('REPLACE_CONFIRMATION_REQUIRED')
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get('https://api.telegram.org/bot'+token+'/getMe') as response:
                result=await response.json()
                if response.status != 200 or not result.get('ok') or not result.get('result',{}).get('is_bot'):
                    raise ConnectionError('BOT_AUTHENTICATION_FAILED')
    except (aiohttp.ClientError,TimeoutError,ValueError):
        raise ConnectionError('BOT_CONNECTION_FAILED') from None
    secrets[reference]=token
    secrets['authorized_telegram_id']=int(user_id)
    agent['telegram_token_key']=reference
    # Mirror preferences are a different owner and are never written here.
    _publish_connection_pair(home/'agents.json', cfg, home/'secrets.json', secrets, original_secrets)
    return {'agent':'hashiko','saved':True,'bot_verified':True,'runtime_ready':False}
