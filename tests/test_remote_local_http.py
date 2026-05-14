from remote import local_http


def test_local_http_hosts_include_interface_ipv4_fallback_on_non_wsl(monkeypatch):
    monkeypatch.setattr(local_http, "_is_wsl", lambda: False)
    monkeypatch.setattr(local_http, "_interface_ipv4_hosts", lambda: ("192.168.0.6", "10.0.0.2"))
    local_http.local_http_hosts.cache_clear()
    try:
        assert local_http.local_http_hosts() == ("127.0.0.1", "192.168.0.6", "10.0.0.2")
    finally:
        local_http.local_http_hosts.cache_clear()


def test_local_http_hosts_preserve_wsl_loopback_alias_priority(monkeypatch):
    monkeypatch.setattr(local_http, "_is_wsl", lambda: True)
    monkeypatch.setattr(local_http, "_interface_ipv4_hosts", lambda: ("10.255.255.254", "192.168.0.211"))
    monkeypatch.setattr(
        local_http.subprocess,
        "check_output",
        lambda *args, **kwargs: "lo               UNKNOWN        127.0.0.1/8 10.255.255.254/32\n",
    )
    local_http.local_http_hosts.cache_clear()
    try:
        assert local_http.local_http_hosts() == ("10.255.255.254", "192.168.0.211", "127.0.0.1")
    finally:
        local_http.local_http_hosts.cache_clear()


def test_is_local_http_host_accepts_interface_fallback(monkeypatch):
    monkeypatch.setattr(local_http, "_is_wsl", lambda: False)
    monkeypatch.setattr(local_http, "_interface_ipv4_hosts", lambda: ("192.168.0.6",))
    local_http.local_http_hosts.cache_clear()
    try:
        assert local_http.is_local_http_host("192.168.0.6") is True
        assert local_http.is_local_http_host("127.0.0.1") is True
    finally:
        local_http.local_http_hosts.cache_clear()
