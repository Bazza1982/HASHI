from __future__ import annotations
import json
import os
import socket
import threading
from pathlib import Path
import pytest
from tools.embedded_browser_bridge import EmbeddedBrowserBridge, EmbeddedBrowserError, SUPPORTED_ACTIONS
from tools.browser_extension_bridge import project_browser_audit_metadata

def setup(tmp_path):
    key=tmp_path/'agent-bridge.key';key.write_text('a'*64)
    descriptor=tmp_path/'workbench-agent-bridge.json'
    # AF_UNIX paths must be short even under pytest's nested temp directories.
    endpoint=tmp_path/'b.sock'
    value=dict(schema_version=1,enabled=True,transport='unix_jsonl',endpoint=str(endpoint),auth_file=str(key),instance_id='generation',hashi_instance_id='HASHI3')
    descriptor.write_text(json.dumps(value))
    return descriptor, endpoint, value

def test_descriptor_binding_and_scope(tmp_path):
    descriptor,endpoint,value=setup(tmp_path)
    bridge=EmbeddedBrowserBridge(descriptor,'HASHI3')
    assert bridge.descriptor()['instance_id']=='generation'
    with pytest.raises(EmbeddedBrowserError):EmbeddedBrowserBridge(descriptor,'HASHI2').descriptor()
    value['auth_file']=str(tmp_path.parent/'secret.key');descriptor.write_text(json.dumps(value))
    with pytest.raises(EmbeddedBrowserError):bridge.descriptor()

@pytest.mark.skipif(os.name=='nt',reason='AF_UNIX stub; native named pipe has a separate local acceptance gate')
def test_raw_jsonl_auth_generation_and_result(tmp_path):
    descriptor,endpoint,value=setup(tmp_path)
    server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);server.bind(str(endpoint));server.listen(1)
    received=[]
    def serve():
        with server:
            client,_=server.accept()
            with client:
                body=bytearray()
                while b'\n' not in body:body.extend(client.recv(4096))
                request=json.loads(body);received.append(request)
                client.sendall((json.dumps({'ok':True,'request_id':request['request_id'],'output':'page text'})+'\n').encode())
    thread=threading.Thread(target=serve,daemon=True);thread.start()
    assert EmbeddedBrowserBridge(descriptor,'HASHI3').execute('get_text',{'handoff_id':'h','tab_id':'t','_audit':{'session_id':'s'}})=='page text'
    thread.join(timeout=2)
    assert received[0]['auth_token']=='a'*64
    assert received[0]['args']['_browser_instance_id']=='generation'
    assert received[0]['args']['_audit']['instance_id']=='HASHI3'
    assert received[0]['args']['handoff_id']=='h'

def test_canonical_session_projects_without_runtime_objects():
    result=project_browser_audit_metadata({'hashi_session_id':'canonical','session_id':'old-engine','request_id':'r','_runtime':object(),'secret':'NO'})
    assert result=={'session_id':'canonical','request_id':'r'}

def test_unimplemented_and_privileged_actions_not_advertised():
    assert not {'evaluate','get_html','session','password_fill','extension_load','upload'} & SUPPORTED_ACTIONS


@pytest.mark.parametrize('action', ['get_text', 'screenshot'])
def test_live_read_checks_current_url_without_navigating(tmp_path, monkeypatch, action):
    bridge = EmbeddedBrowserBridge(tmp_path / 'descriptor.json', 'HASHI3')
    calls = []

    def call(name, args):
        calls.append((name, args))
        if name == 'active_tab':
            return json.dumps({'url': 'https://example.test/form', 'title': 'Form'})
        assert 'url' not in args, 'A read grant must not reload a live form'
        return 'owned visible page'

    monkeypatch.setattr(bridge, 'call', call)
    arguments = {'handoff_id': 'h', 'tab_id': 't', 'url': 'https://example.test/form',
                 '_audit': {'session_id': 's', 'request_id': 'r'}, 'maximize': True}
    assert bridge.execute(action, arguments) == 'owned visible page'
    assert [name for name, _ in calls] == ['active_tab', action]
    assert calls[0][1]['maximize'] is False
    assert calls[1][1]['handoff_id'] == 'h'
    assert calls[1][1]['_audit']['request_id'] == 'r'
    assert arguments['url'] == 'https://example.test/form'


def test_live_read_refuses_different_page_without_content_read(tmp_path, monkeypatch):
    bridge = EmbeddedBrowserBridge(tmp_path / 'descriptor.json', 'HASHI3')
    calls = []

    def call(name, args):
        calls.append(name)
        return {'url': 'https://example.test/other'}

    monkeypatch.setattr(bridge, 'call', call)
    with pytest.raises(EmbeddedBrowserError, match='browser_handoff_url_mismatch'):
        bridge.execute('get_text', {'handoff_id': 'h', 'tab_id': 't',
                                   'url': 'https://example.test/form'})
    assert calls == ['active_tab']


def test_url_normalisation_does_not_relax_mutating_or_unbound_calls(tmp_path, monkeypatch):
    bridge = EmbeddedBrowserBridge(tmp_path / 'descriptor.json', 'HASHI3')
    calls = []
    monkeypatch.setattr(bridge, 'call', lambda name, args: calls.append((name, args)) or 'ok')
    bridge.execute('fill', {'handoff_id': 'h', 'tab_id': 't', 'url': 'https://example.test/form'})
    bridge.execute('get_text', {'url': 'https://example.test/form'})
    assert len(calls) == 2
    assert all(args['url'] == 'https://example.test/form' for _, args in calls)
