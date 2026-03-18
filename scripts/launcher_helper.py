#!/usr/bin/env python3
"""Helper script for bridge-u.sh to parse agents.json and secrets.json"""
import json
import os
import sys

def main():
    bridge_home = os.environ.get('BRIDGE_HOME', os.path.dirname(os.path.abspath(__file__)))
    bridge_code = os.environ.get('BRIDGE_CODE_ROOT', bridge_home)
    
    # Find agents.json
    agents_path = os.path.join(bridge_home, 'agents.json')
    if not os.path.exists(agents_path):
        agents_path = os.path.join(bridge_code, 'agents.json')
    
    # Find secrets.json
    secrets_path = os.path.join(bridge_home, 'secrets.json')
    if not os.path.exists(secrets_path):
        secrets_path = os.path.join(bridge_code, 'secrets.json')
    
    if not os.path.exists(agents_path):
        print("LOAD_ERROR=1", file=sys.stderr)
        sys.exit(1)
    
    # Load configs
    with open(agents_path) as f:
        cfg = json.load(f)
    
    secrets = {}
    if os.path.exists(secrets_path):
        try:
            with open(secrets_path) as f:
                secrets = json.load(f)
        except:
            pass
    
    # Global settings
    global_cfg = cfg.get('global', {})
    wa = global_cfg.get('whatsapp', {})
    wa_enabled = wa.get('enabled', False)
    wa_default = wa.get('default_agent', '')
    wb_port = global_cfg.get('workbench_port', 18800)
    
    print(f'WHATSAPP_ENABLED="{("yes" if wa_enabled else "no")}"')
    print(f'WHATSAPP_DEFAULT_AGENT="{wa_default}"')
    print(f'WORKBENCH_PORT="{wb_port}"')
    
    # Process agents
    active_idx = 0
    inactive_idx = 0
    
    for a in cfg.get('agents', []):
        name = a['name']
        is_active = a.get('is_active', True)
        agent_type = a.get('type', 'fixed')
        backend = a.get('active_backend') or a.get('engine') or 'unknown'
        
        # Check token
        token_key = a.get('telegram_token_key', name)
        has_token = token_key in secrets and secrets[token_key]
        token_status = 'ok' if has_token else 'missing'
        
        if is_active:
            print(f'ACTIVE_AGENTS[{active_idx}]="{name}"')
            print(f'ACTIVE_BACKENDS[{active_idx}]="{backend}"')
            print(f'ACTIVE_TYPES[{active_idx}]="{agent_type}"')
            print(f'ACTIVE_TOKEN_STATUS[{active_idx}]="{token_status}"')
            active_idx += 1
        else:
            print(f'INACTIVE_AGENTS[{inactive_idx}]="{name}"')
            print(f'INACTIVE_BACKENDS[{inactive_idx}]="{backend}"')
            inactive_idx += 1

if __name__ == '__main__':
    main()
