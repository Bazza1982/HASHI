from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

import httpx

from orchestrator.enterprise.connectors.base import ConnectorAction, ConnectorHealth, ConnectorResult


GitHubTransport = Callable[[str, str, Mapping[str, str], Mapping[str, Any] | None], Mapping[str, Any]]


class GitHubConnector:
    connector_type = "github"

    def __init__(
        self,
        *,
        token: str | None = None,
        api_base_url: str = "https://api.github.com",
        timeout_seconds: float = 10.0,
        transport: GitHubTransport | None = None,
    ):
        self.token = (token or "").strip() or None
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    def health_check(self) -> ConnectorHealth:
        try:
            data = self._request_json("GET", "/rate_limit")
        except Exception as exc:
            return ConnectorHealth(ok=False, status="unhealthy", message=str(exc), data={})
        return ConnectorHealth(
            ok=True,
            status="healthy",
            message="GitHub API reachable",
            data={"rate": _json_safe_mapping(data.get("rate", {}))},
        )

    def execute(self, action: ConnectorAction) -> ConnectorResult:
        action_name = action.action.lower()
        if action_name in {"repo.get", "repo.read"}:
            return self._get_repository(action)
        if action_name == "issue.create":
            return self._create_issue(action)
        return ConnectorResult(
            ok=False,
            status="unsupported_action",
            message=f"unsupported GitHub connector action: {action.action}",
            data={"connector_type": self.connector_type, "action": action.action},
        )

    def _get_repository(self, action: ConnectorAction) -> ConnectorResult:
        repo_ref = _repo_ref_from_action(action)
        if repo_ref is None:
            return ConnectorResult(
                ok=False,
                status="invalid_parameters",
                message="repo.get requires owner/repo in resource or parameters",
            )
        owner, repo = repo_ref
        if action.dry_run:
            return ConnectorResult(
                ok=True,
                status="dry_run",
                message="repository lookup dry run",
                data={"owner": owner, "repo": repo},
            )
        data = self._request_json("GET", f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}")
        return ConnectorResult(
            ok=True,
            status="success",
            message="repository fetched",
            data={
                "id": data.get("id"),
                "full_name": data.get("full_name"),
                "private": data.get("private"),
                "default_branch": data.get("default_branch"),
                "html_url": data.get("html_url"),
            },
        )

    def _create_issue(self, action: ConnectorAction) -> ConnectorResult:
        repo_ref = _repo_ref_from_action(action)
        parameters = dict(action.parameters or {})
        title = str(parameters.get("title") or "").strip()
        if repo_ref is None:
            return ConnectorResult(
                ok=False,
                status="invalid_parameters",
                message="issue.create requires owner/repo in resource or parameters",
            )
        if not title:
            return ConnectorResult(ok=False, status="invalid_parameters", message="issue.create requires title")
        owner, repo = repo_ref
        body = str(parameters.get("body") or "")
        labels = parameters.get("labels") if isinstance(parameters.get("labels"), list) else []
        payload = {"title": title, "body": body, "labels": [str(label) for label in labels]}
        if action.dry_run:
            return ConnectorResult(
                ok=True,
                status="dry_run",
                message="issue creation dry run",
                data={"owner": owner, "repo": repo, "payload": payload},
            )
        data = self._request_json("POST", f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/issues", payload)
        return ConnectorResult(
            ok=True,
            status="success",
            message="issue created",
            data={
                "id": data.get("id"),
                "number": data.get("number"),
                "title": data.get("title"),
                "html_url": data.get("html_url"),
                "state": data.get("state"),
            },
        )

    def _request_json(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "HASHI-Enterprise-Connector",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self._transport is not None:
            return dict(self._transport(method, path, headers, body))
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.request(method, f"{self.api_base_url}{path}", headers=headers, json=body)
            response.raise_for_status()
            return response.json()


def _repo_ref_from_action(action: ConnectorAction) -> tuple[str, str] | None:
    parameters = dict(action.parameters or {})
    owner = str(parameters.get("owner") or "").strip()
    repo = str(parameters.get("repo") or "").strip()
    if owner and repo:
        return owner, repo

    resource = str(action.resource or "").strip()
    if resource.startswith("repo:"):
        resource = resource.removeprefix("repo:")
    parts = [part for part in resource.split("/") if part]
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    return None


def _json_safe_mapping(value: Any) -> dict:
    if not isinstance(value, Mapping):
        return {}
    result = {}
    for key, item in value.items():
        if item is None or isinstance(item, (str, int, float, bool)):
            result[str(key)] = item
        else:
            result[str(key)] = repr(item)
    return result
