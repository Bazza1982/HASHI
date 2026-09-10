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
from orchestrator.config import FlexibleAgentConfig, GlobalConfig
from orchestrator.flexible_backend_manager import FlexibleBackendManager
from orchestrator.flexible_backend_registry import (
    get_available_models, get_default_model, get_backend_entry, PROVIDER_ONLY_ENGINE_IDS,
)
from orchestrator.pcm import atomic_write_pcm, render_pcm_document
from onboarding.onboarding_main import _atomic_write_json


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
            {'engine':'her-v2','model':'role-configured','effort':'zero','her_v2':her,'tools':{'enabled':False}},
            {'engine':engine,'model':model,'api_key_secret':'hashiko.connection.' + engine,'tools':{'enabled':False}},
        ]
    return engine, [{'engine':engine,'model':model,'tools':{'enabled':False}}]


async def validate(home, engine, model, key, *, confirmed=False):
    if not confirmed:
        raise ConnectionError('CONFIRMATION_REQUIRED')
    available = {row['engine']:row for row in choices(home)}
    row = available.get(engine)
    if row is None or model not in row['models']:
        raise ConnectionError('MODEL_UNAVAILABLE')
    if row['kind'] == 'api' and not key:
        raise ConnectionError('CREDENTIAL_REQUIRED')
    active, backends = backend_configuration(engine, model)
    # Probe in a disposable workspace, through the same manager/adapter chain.
    # The selected credential stays in memory and is never copied into prompt/state.
    with tempfile.TemporaryDirectory(prefix='hashi-connect-') as directory:
        root = Path(directory)
        cfg = FlexibleAgentConfig(name='hashiko',workspace_dir=root,system_md=root/'agent.md',
            telegram_token_key='',allowed_backends=backends,active_backend=active,
            access_scope='workspace',extra={},project_root=root)
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


def save(home, engine, model, key, *, verified, replace_confirmed=False, language='en'):
    if not verified or verified.get('verified') is not True or verified.get('engine') != engine or verified.get('model') != model:
        raise ConnectionError('VALIDATION_REQUIRED')
    home = Path(home)
    config_path, secrets_path = home/'agents.json', home/'secrets.json'
    before = {p:p.read_bytes() if p.exists() else None for p in (config_path,secrets_path)}
    cfg, secrets = _read(config_path), _read(secrets_path)
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
    if key:
        secrets[reference] = key
    workspace = Path(agent['workspace_dir'])
    if not workspace.is_absolute():
        workspace = home/workspace
    workspace.mkdir(parents=True,exist_ok=True)
    if not (workspace/'agent.md').exists():
        atomic_write_pcm(workspace/'agent.md',render_pcm_document(
            persona='You are Hashiko (小乔), a concise, helpful personal assistant.',
            system='The user can start work immediately. Telegram and all further setup are optional. Ask only what is needed for their task. Never request credentials in chat. Use the local connection page for secrets. Stay inside your authorized workspace and request access as needed.'))
    # Refuse concurrent changes; never reconstruct the instance or its history.
    for path, original in before.items():
        if (path.read_bytes() if path.exists() else None) != original:
            raise ConnectionError('CONFIG_CHANGED')
    try:
        _atomic_write_json(secrets_path,secrets,private=True)
        _atomic_write_json(config_path,cfg,private=True)
    except OSError:
        # Restore only the credential file written by this transaction.
        if before[secrets_path] is None:
            secrets_path.unlink(missing_ok=True)
        else:
            _atomic_write_json(secrets_path,json.loads(before[secrets_path].decode('utf-8-sig')),private=True)
        raise ConnectionError('SAVE_FAILED') from None
    return {'agent':'hashiko','engine':active,'saved':True,'runtime_ready':False}


async def adopt_if_running(home):
    """Request only Hashiko's existing controlled reload; never restart an instance."""
    import aiohttp
    home=Path(home)
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
        except (aiohttp.ClientError,TimeoutError):
            return {'running':False,'adopted':False}
        async with session.post(url+'/api/admin/command',
                headers={'X-Workbench-Token':secret},json={'agent':'hashiko','command':'/reboot min'}) as response:
            result=await response.json()
            if response.status != 200 or not result.get('ok'):
                raise ConnectionError('SAVED_ADOPTION_PENDING')
    return {'running':True,'adopted':False,'reload_requested':True}


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
    cfg,secrets=_read(home/'agents.json'),_read(home/'secrets.json')
    agent=next((a for a in cfg.get('agents',[]) if a.get('name')=='hashiko'),None)
    if agent is None:
        raise ConnectionError('BACKEND_CONNECTION_REQUIRED')
    existing_id=int(secrets.get('authorized_telegram_id') or cfg.get('global',{}).get('authorized_id') or 0)
    if existing_id and existing_id != int(user_id):
        raise ConnectionError('AUTHORIZED_USER_CONFLICT')
    reference='hashiko.telegram'
    if secrets.get(reference) and secrets[reference] != token and not replace_confirmed:
        raise ConnectionError('REPLACE_CONFIRMATION_REQUIRED')
    before={p:p.read_bytes() if p.exists() else None for p in (home/'agents.json',home/'secrets.json')}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get('https://api.telegram.org/bot'+token+'/getMe') as response:
                result=await response.json()
                if response.status != 200 or not result.get('ok') or not result.get('result',{}).get('is_bot'):
                    raise ConnectionError('BOT_AUTHENTICATION_FAILED')
    except (aiohttp.ClientError,TimeoutError,ValueError):
        raise ConnectionError('BOT_CONNECTION_FAILED') from None
    for path,original in before.items():
        if (path.read_bytes() if path.exists() else None) != original:
            raise ConnectionError('CONFIG_CHANGED')
    secrets[reference]=token
    secrets['authorized_telegram_id']=int(user_id)
    agent['telegram_token_key']=reference
    # Mirror preferences are a different owner and are never written here.
    try:
        _atomic_write_json(home/'secrets.json',secrets,private=True)
        _atomic_write_json(home/'agents.json',cfg,private=True)
    except OSError:
        original=before[home/'secrets.json']
        if original is None:
            (home/'secrets.json').unlink(missing_ok=True)
        else:
            _atomic_write_json(home/'secrets.json',json.loads(original.decode('utf-8-sig')),private=True)
        raise ConnectionError('SAVE_FAILED') from None
    return {'agent':'hashiko','saved':True,'bot_verified':True,'runtime_ready':False}
