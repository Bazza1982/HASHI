from tests.test_remote_workbench_gateway import _client, _signed_headers
from remote.api import server as remote_server


def test_gateway_rejection_reports_target_and_layer_without_claiming_expiration(tmp_path, monkeypatch):
    client, token = _client(tmp_path)
    monkeypatch.setattr(remote_server, '_fetch_workbench_health', lambda timeout=1: {'ok': True})
    for path in ('/workbench/v1/status', '/workbench/v1/proxy/api/system'):
        headers = _signed_headers('wrong-fixture-secret', method='GET', path=path)
        result = client.get(path, headers=headers)
        assert result.status_code == 401
        body = result.json()
        assert body['error_code'] == 'auth_failed'
        assert body['failure_layer'] == 'remote_authentication'
        assert body['instance_id'] == 'HASHI1'
        assert 'expired' not in str(body).lower()
        assert 'wrong-fixture-secret' not in str(body) and token not in str(body)
    headers = _signed_headers(token, method='GET', path='/workbench/v1/status')
    assert client.get('/workbench/v1/status', headers=headers).status_code == 200
    assert client.get('/workbench/v1/status', headers=headers).status_code == 401
