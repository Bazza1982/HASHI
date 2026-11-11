"""Real governed login and approval HTTP forms; no instance/L1 impersonation."""

import time
from dataclasses import replace
from html.parser import HTMLParser
from types import SimpleNamespace
from urllib.parse import urlsplit

from aiohttp import CookieJar, web
from aiohttp.test_utils import TestClient, TestServer
import pytest
import pytest_asyncio

from orchestrator.permissions.authority import OperationConflict, PermissionDenied
from orchestrator.permissions.human_approval import approval_origin, human_approval, register_human_approval
from orchestrator.permissions.management import PermissionManagement
from orchestrator.permissions.enterprise_decisions import SessionDecisionDenied
from orchestrator.permissions.models import PermissionLevel, PermissionSettings, normalize_scope
from tests.test_permission_service_application import environment as environment


class Forms(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.rows = []
        self.feed(body)

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == "form":
            self.rows.append({})
        if tag == "input" and "name" in values:
            self.rows[-1][values["name"]] = values.get("value", "")


@pytest_asyncio.fixture
async def browser(environment, tmp_path):
    service, authority, identity, arguments, subject = environment
    ledger_offset = [0.0]
    authority._clock = lambda: time.time() + ledger_offset[0]
    authority._monotonic = lambda: time.monotonic() + ledger_offset[0]
    policy = service._policy_resolver(subject.agent_id, subject.owner_id, subject.session_id)
    policy = replace(policy, settings=PermissionSettings(PermissionLevel.L2))
    service._policy_resolver = service.runs._policy = lambda *args: policy
    service.levels = ("L2",)
    service._management = PermissionManagement(authority, identity, owner_bindings=service.owners,
                                               config_admin=None, run_service=service.runs, level_transition=None)
    binding = await service.open_execution(**arguments)
    target = tmp_path / "original-effect.txt"
    scope = normalize_scope("file", str(target), ("write",), cwd=tmp_path)
    request = authority.request_access(binding.context, [scope], purpose="<original exact operation>",
                                       operation_id="original-op", arguments_digest="a" * 64)
    api = SimpleNamespace(app=web.Application(), permission_control=service, session_store=service.store,
                          global_config=SimpleNamespace(permission_service={}))
    register_human_approval(api)
    client = TestClient(TestServer(api.app), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    origin = str(client.make_url("/")).rstrip("/")
    api.global_config.permission_service = {"approval_origin": origin}
    manager = human_approval(api)
    url = manager.invite(subject, request.request_id, request.revision, locale="en", connection_binding="original-workbench")
    path = urlsplit(url).path
    try:
        yield SimpleNamespace(service=service, authority=authority, identity=identity, subject=subject,
                              binding=binding, target=target, scope=scope, request=request,
                              manager=manager, client=client, api=api, origin=origin, path=path, ledger_offset=ledger_offset)
    finally:
        await client.close()
        await service.close()


async def get_forms(browser):
    response = await browser.client.get(browser.path)
    body = await response.text()
    return response, body, Forms(body).rows


async def login(browser, *, email="owner@example.test", password="only-a-fixture-password"):
    _, _, forms = await get_forms(browser)
    form = next(form for form in forms if form.get("operation") == "login")
    form.update(org_id="people", email=email, password=password)
    response = await browser.client.post(browser.path, data=form, headers={"Origin": browser.origin})
    return response, await response.text()


def choice(browser, forms, lifetime):
    for form in forms:
        for actions in browser.manager._invitations[browser.path.rsplit("/", 1)[1]].actions.values():
            action = actions.get(form.get("action_id"))
            if action and action.choice == "approve" and action.lifetime == lifetime:
                return form
    raise AssertionError("the policy choice was not displayed")


@pytest.mark.asyncio
async def test_recorded_denial_remains_explicit_after_refresh_without_grant_or_effect(browser):
    class StatusText(HTMLParser):
        def __init__(self, body):
            super().__init__()
            self.in_status = False
            self.text = []
            self.feed(body)

        def handle_starttag(self, tag, attrs):
            if tag == "p":
                self.in_status = dict(attrs).get("role") == "status"

        def handle_endtag(self, tag):
            if tag == "p":
                self.in_status = False

        def handle_data(self, data):
            if self.in_status:
                self.text.append(data)

    b = browser
    _, body = await login(b)
    invitation = b.manager._invitations[b.path.rsplit("/", 1)[1]]
    actions = {key: action for group in invitation.actions.values() for key, action in group.items()}
    deny = next(form for form in Forms(body).rows
                if form.get("action_id") in actions and actions[form["action_id"]].choice == "deny")
    response = await b.client.post(b.path, data=deny, headers={"Origin": b.origin})
    assert response.status == 200
    for body in (await response.text(), (await get_forms(b))[1]):
        assert "Denied" in StatusText(body).text
        assert not any(form.get("operation") in {"decide", "revoke"} for form in Forms(body).rows)
    assert b.authority.decision_count(b.request.request_id) == 1
    assert not b.authority.evaluate(b.binding.context, [b.scope], operation_id="original-op")
    assert not b.target.exists()
    assert all(not task["leases"] for task in b.service.session_summary(subject=b.subject)["tasks"])


@pytest.mark.asyncio
async def test_themed_review_keeps_exact_once_and_deny_primary_and_broad_grants_explicit(browser):
    import base64
    import hashlib
    import re
    from orchestrator.permissions.approval_page import THEMES, SIMPLE_THEMES

    class Layout(Forms):
        def __init__(self, body):
            self.more = False
            super().__init__(body)

        def handle_starttag(self, tag, attributes):
            super().handle_starttag(tag, attributes)
            attrs = dict(attributes)
            if tag == "details" and attrs.get("class") == "more":
                self.more = True
            if tag == "form":
                self.rows[-1]["_more"] = self.more

        def handle_endtag(self, tag):
            if tag == "details":
                self.more = False

    import os
    import json
    from pathlib import Path
    export = os.environ.get("HASHI_APPROVAL_PREVIEW_DIR")
    previews = []
    b = browser
    await login(b)
    for mode, themes in (("chat", THEMES), ("simple", SIMPLE_THEMES)):
        for theme in sorted(themes):
            url = b.path + "?theme=" + theme + "&mode=" + mode
            if export:
                b.client.session.cookie_jar.clear()
                login_response = await b.client.get(url)
                login_body = await login_response.text()
                await login(b)
            response = await b.client.get(url)
            body = await response.text()
            assert response.status == 200
            assert not re.search(r"safety\.human_", body), "visible translation keys must never replace UI copy"
            if export:
                previews.append({"mode": mode, "theme": theme, "login": login_body,
                                 "review": body, "csp": response.headers["Content-Security-Policy"]})
            style = re.search(r"<style>(.*?)</style>", body, re.S).group(1)
            digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
            assert "style-src 'sha256-" + digest + "'" in response.headers["Content-Security-Policy"]
            assert "default-src 'none'" in response.headers["Content-Security-Policy"]
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert "<script" not in body and "unsafe-inline" not in response.headers["Content-Security-Policy"]
            assert 'class="app theme-' + theme in body
            forms = [form for form in Layout(body).rows if form.get("operation") == "decide"]
            invitation = b.manager._invitations[b.path.rsplit("/", 1)[1]]
            actions = {key: action for group in invitation.actions.values() for key, action in group.items()}
            primary = [actions[form["action_id"]] for form in forms if not form["_more"]]
            assert [action.choice for action in primary] == ["approve", "deny"]
            assert primary[0].lifetime == "once"
            assert all(form["_more"] for form in forms
                       if actions[form["action_id"]].choice == "approve"
                       and actions[form["action_id"]].lifetime != "once")
    if export:
        folder = Path(export)
        folder.mkdir(parents=True, exist_ok=True)
        folder.joinpath("trusted-pages.json").write_text(json.dumps({"pages": previews}), encoding="utf-8")
    response = await b.client.get(b.path + '?theme=%22%3E%3Cscript%3E&mode=forged')
    body = await response.text()
    assert 'class="app theme-bright"' in body
    assert "<script>" not in body
    assert b.authority.decision_count(b.request.request_id) == 0


@pytest.mark.asyncio
async def test_pending_discovery_projects_original_resource_facts_and_never_actions(browser):
    import json
    from orchestrator.permissions.frontend_requests import read

    b = browser
    b.api._strict_authenticated_owner = lambda _: b.subject.owner_id
    request = SimpleNamespace(match_info={"session_id": b.subject.session_id},
                              query={"agent_id": b.subject.agent_id,
                                     "context_generation": b.subject.context_generation})
    response = await read(b.api, request)
    assert response.status == 200
    row = json.loads(response.text)["requests"][0]
    assert row["request_id"] == b.request.request_id
    assert row["summary"]["purpose"] == b.request.purpose
    assert row["summary"]["resources"] == [{"name": b.target.name, "kind": "file",
                                            "resource": b.scope.resource, "actions": ["write"]}]
    assert "actions" not in row and "token" not in response.text
    assert b.authority.decision_count(b.request.request_id) == 0


@pytest.mark.asyncio
async def test_governed_form_login_once_and_exact_duplicate_decision(browser):
    b = browser
    with pytest.raises(PermissionDenied):
        b.service.personal_l1_login(subject=b.subject)
    response, body, forms = await get_forms(b)
    assert response.status == 200 and "original-effect" not in body and "original exact operation" not in body
    assert response.headers["Referrer-Policy"] == "same-origin"
    forged = await b.client.post(b.path, data={"operation": "decide", "action_id": "forged", "csrf": "yes"},
                                 headers={"Origin": b.origin, "Authorization": "Bearer instance-hmac"})
    assert forged.status == 403 and b.authority.decision_count(b.request.request_id) == 0
    no_human = await b.client.post(b.path, data={"operation": "decide", "action_id": "forged", "csrf": forms[0]["csrf"]},
                                   headers={"Origin": b.origin, "Authorization": "Bearer instance-hmac"})
    assert no_human.status == 401 and b.authority.decision_count(b.request.request_id) == 0
    response, body = await login(b)
    assert response.status == 200 and "&lt;original exact operation&gt;" in body
    forms = Forms(body).rows
    form = choice(b, forms, "once")
    for _ in range(2):
        response = await b.client.post(b.path, data=form, headers={"Origin": b.origin})
        assert response.status == 200, await response.text()
    assert b.authority.decision_count(b.request.request_id) == 1
    claim = b.authority.prepare(b.binding.context, "original-op", "a" * 64, [b.scope])
    b.authority.start(b.binding.context, claim.claim_id)
    b.target.write_text("effect once", encoding="utf-8")
    b.authority.finish(b.binding.context, claim.claim_id, outcome="succeeded", result_digest="b" * 64)
    with pytest.raises(OperationConflict):
        b.authority.prepare(b.binding.context, "original-op", "a" * 64, [b.scope])
    assert b.authority.operation_status_for_task(b.binding.context, "original-op").state == "succeeded"
    assert b.target.read_text(encoding="utf-8") == "effect once"
    refreshed = await b.client.get(b.path)
    current_body = await refreshed.text()
    assert refreshed.status == 200
    assert not any(form.get("operation") in {"decide", "revoke"} for form in Forms(current_body).rows)
    assert "cannot be reused" in current_body
    assert not b.authority.evaluate(b.binding.context, [b.scope], operation_id="another-op")
    cookie = b.client.session.cookie_jar.filter_cookies(b.client.make_url(b.path))["hashi_approval_session"]
    assert cookie.value not in body and cookie.value not in repr(b.manager._invitations)
    assert cookie.value.encode() not in b.authority.path.read_bytes()


@pytest.mark.asyncio
async def test_cross_owner_bad_password_origin_and_csrf_never_create_a_decision(browser):
    b = browser
    response, _ = await login(b, email="other@example.test")
    assert response.status == 401
    response, _ = await login(b, password="wrong-password")
    assert response.status == 401
    _, body = await login(b)
    form = choice(b, Forms(body).rows, "task")
    for origin, csrf in (("https://other.test", form["csrf"]), ("null", form["csrf"]), (b.origin, "tampered")):
        rejected = await b.client.post(b.path, data={**form, "csrf": csrf}, headers={"Origin": origin})
        assert rejected.status == 403
    assert b.authority.decision_count(b.request.request_id) == 0


@pytest.mark.asyncio
async def test_governed_revocation_and_logout_fence_further_access(browser):
    b = browser
    _, body = await login(b)
    approve = choice(b, Forms(body).rows, "task")
    approved = await b.client.post(b.path, data=approve, headers={"Origin": b.origin})
    forms = Forms(await approved.text()).rows
    assert b.authority.evaluate(b.binding.context, [b.scope], operation_id="future")
    revoke = next(form for form in forms if form["operation"] == "revoke")
    response = await b.client.post(b.path, data=revoke, headers={"Origin": b.origin})
    assert response.status == 200
    assert not b.authority.evaluate(b.binding.context, [b.scope], operation_id="future")
    forms = Forms(await response.text()).rows
    logout = next(form for form in forms if form["operation"] == "logout")
    response = await b.client.post(b.path, data=logout, headers={"Origin": b.origin})
    assert response.status == 200
    stale = await b.client.post(b.path, data=approve, headers={"Origin": b.origin})
    assert stale.status in {401, 403}
    assert b.authority.decision_count(b.request.request_id) == 1


@pytest.mark.asyncio
async def test_expired_or_revised_original_request_cannot_be_approved(browser):
    b = browser
    _, body = await login(b)
    form = choice(b, Forms(body).rows, "once")
    invitation = b.manager._invitations[b.path.rsplit("/", 1)[1]]
    invitation.expires_at = 0
    rejected = await b.client.post(b.path, data=form, headers={"Origin": b.origin})
    assert rejected.status == 410
    assert b.authority.decision_count(b.request.request_id) == 0


@pytest.mark.asyncio
async def test_commit_then_lost_response_is_unknown_and_not_blindly_retried(browser, monkeypatch):
    b = browser
    _, body = await login(b)
    form = choice(b, Forms(body).rows, "once")
    submit = b.service.submit_session_action
    calls = []

    def lost(*args, **kwargs):
        calls.append("submit")
        submit(*args, **kwargs)
        raise OSError("private-transport-canary")
    monkeypatch.setattr(b.service, "submit_session_action", lost)
    for _ in range(2):
        response = await b.client.post(b.path, data=form, headers={"Origin": b.origin})
        body = await response.text()
        assert response.status == 503 and "not confirmed" in body and "private-transport-canary" not in body
    assert calls == ["submit"] and b.authority.decision_count(b.request.request_id) == 1


@pytest.mark.asyncio
async def test_personal_l1_token_is_not_governed_l2_approval_even_after_a_policy_switch(browser):
    b = browser
    resolver = b.service._policy_resolver
    b.service.levels = ("L1", "L2")
    b.service._policy_resolver = lambda *args: replace(resolver(*args), settings=PermissionSettings(PermissionLevel.L1))
    personal = b.service.personal_l1_login(subject=b.subject)["session_token"]
    b.service._policy_resolver = resolver
    assert b.identity.get_session_user(personal) is not None
    with pytest.raises(SessionDecisionDenied):
        b.service.issue_session_actions(b.subject, b.request.request_id, b.request.revision,
                                       session_token=personal, presentation_id="cooperative-token")
    governed = b.service.login(org_id="people", email="owner@example.test", password="only-a-fixture-password")["session"]["token"]
    actions = b.service.issue_session_actions(b.subject, b.request.request_id, b.request.revision,
                                            session_token=governed, presentation_id="governed-review")
    with pytest.raises(SessionDecisionDenied):
        b.service.submit_session_action(b.subject, actions[0].action_id, session_token=personal)
    with pytest.raises(SessionDecisionDenied):
        b.service.submit_session_decision(b.subject, ["approve", b.request.request_id, "task", "--revision", "1"],
                                         session_token=personal, invocation_id="personal-escalation", connection_binding="fc")
    with pytest.raises(SessionDecisionDenied):
        await b.service.manage_session(subject=b.subject, arguments=["revoke-task", b.binding.context.task_id],
                                       session_token=personal, invocation_id="personal-revocation")
    assert b.authority.decision_count(b.request.request_id) == 0
    assert b.authority.execution_status(b.binding.context).task_state == "active"
    b.service.logout(session_token=governed)
    _, body = await login(b)
    assert choice(b, Forms(body).rows, "once")


@pytest.mark.asyncio
async def test_lease_expiry_and_revoked_governed_session_are_enforced(browser):
    b = browser
    policy = b.service._policy_resolver(b.subject.agent_id, b.subject.owner_id, b.subject.session_id)
    b.service.control._decisions._settings = replace(policy.settings, task_default_seconds=120)
    _, body = await login(b)
    form = choice(b, Forms(body).rows, "ttl")
    response = await b.client.post(b.path, data=form, headers={"Origin": b.origin})
    assert response.status == 200
    assert b.authority.evaluate(b.binding.context, [b.scope], operation_id="future")
    # Advance both trusted time sources, independently of CI disk speed.
    b.ledger_offset[0] += 121
    assert not b.authority.evaluate(b.binding.context, [b.scope], operation_id="future")
    token = b.client.session.cookie_jar.filter_cookies(b.client.make_url(b.path))["hashi_approval_session"].value
    b.identity.revoke_session(token)
    response = await b.client.get(b.path)
    assert response.status == 401
    assert b.authority.decision_count(b.request.request_id) == 1


@pytest.mark.asyncio
async def test_configured_original_native_human_keeps_l2_approval_support(browser):
    b = browser
    with pytest.raises(PermissionDenied):
        b.service.native_login(subject=b.subject, connector_id="backend_api", actor_id="731", invocation_id="synthetic-api")
    native = b.service.native_login(subject=b.subject, connector_id="telegram", actor_id="731", invocation_id="original-native-update")
    token = native["session_token"]
    actions = b.service.issue_session_actions(b.subject, b.request.request_id, b.request.revision,
                                            session_token=token, presentation_id="original-native-update")
    once = next(action for action in actions if action.lifetime == "once")
    outcome = b.service.submit_session_action(b.subject, once.action_id, session_token=token)
    assert outcome.choice == "approve" and b.authority.decision_count(b.request.request_id) == 1
    assert b.authority.evaluate(b.binding.context, [b.scope], operation_id="original-op")
    assert not b.authority.evaluate(b.binding.context, [b.scope], operation_id="different-op")
    assert b.service.logout(session_token=token)["revoked"]
    with pytest.raises(SessionDecisionDenied):
        b.service.submit_session_action(b.subject, once.action_id, session_token=token)


@pytest.mark.parametrize("peers, origin", [
    ("127.0.0.1", "https://approval.test"),
    (["localhost"], "https://approval.test"),
    (["127.0.0.1/8"], "https://approval.test"),
    (["0.0.0.0"], "https://approval.test"),
    (["::"], "https://approval.test"),
    (["239.0.0.1"], "https://approval.test"),
    (["127.0.0.1"], "http://localhost"),
])
def test_approval_proxy_configuration_requires_explicit_https_peers(peers, origin):
    from orchestrator.permissions.human_approval import approval_proxy_peers

    with pytest.raises(ValueError):
        approval_proxy_peers(peers, origin)



@pytest.mark.asyncio
async def test_https_proxy_requires_explicit_socket_peer_and_preserves_governed_login(browser, tmp_path):
    """Exercise a real TLS frontend -> HTTP backend, including fail-closed proxy headers."""
    from datetime import datetime, timedelta, timezone
    import ipaddress
    import ssl

    from aiohttp import ClientSession, TCPConnector
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from multidict import CIMultiDict

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "temporary test proxy")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                       critical=False).sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "proxy.pem", tmp_path / "proxy.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption()))
    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(cert_path, key_path)
    client_tls = ssl.create_default_context(cafile=str(cert_path))
    original_client = browser.client
    forwarded = {"proto": "https", "host": None}
    hop_headers = {"connection", "transfer-encoding", "content-length", "host"}

    async with ClientSession(auto_decompress=False) as upstream:
        async def proxy(request):
            headers = CIMultiDict((k, v) for k, v in request.headers.items()
                                  if k.lower() not in hop_headers
                                  and not k.lower().startswith("x-forwarded-"))
            headers["Host"] = forwarded.get("request_host", request.host)
            if forwarded["proto"] is not None:
                headers["X-Forwarded-Proto"] = forwarded["proto"]
            if forwarded.get("duplicate_proto"):
                headers.add("X-Forwarded-Proto", "http")
            headers["X-Forwarded-Host"] = forwarded["host"] or request.host
            headers["X-Forwarded-For"] = "127.0.0.2"
            async with upstream.request(request.method, original_client.make_url(request.rel_url),
                                        data=await request.read(), headers=headers,
                                        allow_redirects=False) as response:
                return web.Response(status=response.status, body=await response.read(),
                                    headers=CIMultiDict((k, v) for k, v in response.headers.items()
                                                       if k.lower() not in hop_headers))

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", proxy)
        tls_server = TestServer(app, scheme="https")
        await tls_server.start_server(ssl=server_tls)
        tls_client = TestClient(tls_server,
                                connector=TCPConnector(ssl=client_tls),
                                cookie_jar=CookieJar(unsafe=True))
        await tls_client.start_server()
        try:
            origin = str(tls_client.make_url("/")).rstrip("/")
            browser.client, browser.origin = tls_client, origin

            def invite(peers=None):
                descriptor = {"approval_origin": origin}
                if peers is not None:
                    descriptor["approval_proxy_peers"] = peers
                browser.api.global_config.permission_service = descriptor
                browser.manager = human_approval(browser.api)
                browser.path = urlsplit(browser.manager.invite(
                    browser.subject, browser.request.request_id, browser.request.revision,
                    locale="en", connection_binding="original-workbench",
                )).path

            invite()
            response = await tls_client.get(browser.path)
            assert response.status == 403  # HTTPS proxy use is an explicit operator opt-in.
            invite(["127.0.0.2"])  # A forwarded client address is never the actual proxy peer.
            response = await tls_client.get(browser.path)
            assert response.status == 403

            invite(["127.0.0.1"])
            for bad_proto in (None, "http", "https,http"):
                forwarded["proto"] = bad_proto
                response = await tls_client.get(browser.path)
                assert response.status == 403
            forwarded["proto"] = "https"
            forwarded["duplicate_proto"] = True
            response = await tls_client.get(browser.path)
            assert response.status == 403
            forwarded["duplicate_proto"] = False
            forwarded["host"] = "foreign.example"
            response = await tls_client.get(browser.path)
            assert response.status == 403
            forwarded["host"] = None
            forwarded["request_host"] = "foreign.example"
            response = await tls_client.get(browser.path)
            assert response.status == 403
            forwarded.pop("request_host")
            response, _, forms = await get_forms(browser)
            assert response.status == 200
            cookie = response.headers.getall("Set-Cookie")[0]
            assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
            forged = await tls_client.post(
                browser.path, data={"operation": "decide", "csrf": forms[0]["csrf"], "action_id": "forged"},
                headers={"Origin": origin, "Authorization": "Bearer instance-hmac"},
            )
            assert forged.status == 401 and browser.authority.decision_count(browser.request.request_id) == 0
            response, body = await login(browser)
            assert response.status == 200
            assert next(cookie for cookie in tls_client.session.cookie_jar
                        if cookie.key == "hashi_approval_session")["secure"]
            approve = choice(browser, Forms(body).rows, "task")
            bad_origin = await tls_client.post(browser.path, data=approve, headers={"Origin": "https://foreign.example"})
            assert bad_origin.status == 403
            for _ in range(2):
                response = await tls_client.post(browser.path, data=approve, headers={"Origin": origin})
                assert response.status == 200, await response.text()
            assert browser.authority.decision_count(browser.request.request_id) == 1
            assert browser.authority.evaluate(browser.binding.context, [browser.scope], operation_id="original-op")
            forms = Forms(await response.text()).rows
            revoke = next(form for form in forms if form["operation"] == "revoke")
            response = await tls_client.post(browser.path, data=revoke, headers={"Origin": origin})
            assert response.status == 200
            assert not browser.authority.evaluate(browser.binding.context, [browser.scope], operation_id="original-op")
        finally:
            browser.client = original_client
            await tls_client.close()

@pytest.mark.parametrize("origin", ["http://192.168.0.2:80", "https://good.test/path",
                                   "https://user:pass@good.test", "https://good.test/?secret=x",
                                   "https://good.test/#fragment", "https://good.test\\bad"])
def test_approval_origin_is_configured_and_never_guessed_from_request_headers(origin):
    with pytest.raises(ValueError):
        approval_origin(origin)
# HASHI-Fingerprint: hfp_ed926dddcdf345479e266750d9cd90a0


@pytest.mark.asyncio
async def test_pending_discovery_keeps_two_original_requests_in_one_session(browser):
    import json
    import os
    from pathlib import Path
    from orchestrator.permissions.frontend_requests import read

    b = browser
    second_scope = normalize_scope("file", str(b.target.parent / "second-report.txt"), ("read",), cwd=b.target.parent)
    second = b.authority.request_access(b.binding.context, [second_scope], purpose="Read second report",
                                       operation_id="second-op", arguments_digest="c" * 64)
    b.api._strict_authenticated_owner = lambda _: b.subject.owner_id
    request = SimpleNamespace(match_info={"session_id": b.subject.session_id},
                              query={"agent_id": b.subject.agent_id,
                                     "context_generation": b.subject.context_generation})
    response = await read(b.api, request)
    assert response.status == 200
    payload = json.loads(response.text)
    rows = {row["request_id"]: row for row in payload["requests"]}
    assert set(rows) == {b.request.request_id, second.request_id}
    assert payload["session_id"] == b.subject.session_id
    assert payload["context_generation"] == int(b.subject.context_generation)
    assert rows[second.request_id]["summary"]["resources"][0]["actions"] == ["read"]
    assert b.authority.decision_count(second.request_id) == 0
    folder = os.environ.get("HASHI_APPROVAL_PREVIEW_DIR")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        Path(folder, "pending-requests.json").write_text(response.text, encoding="utf-8")
