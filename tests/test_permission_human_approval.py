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
                              manager=manager, client=client, origin=origin, path=path, ledger_offset=ledger_offset)
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


@pytest.mark.parametrize("origin", ["http://192.168.0.2:80", "https://good.test/path",
                                   "https://user:pass@good.test", "https://good.test/?secret=x",
                                   "https://good.test/#fragment", "https://good.test\\bad"])
def test_approval_origin_is_configured_and_never_guessed_from_request_headers(origin):
    with pytest.raises(ValueError):
        approval_origin(origin)
# HASHI-Fingerprint: hfp_ed926dddcdf345479e266750d9cd90a0
