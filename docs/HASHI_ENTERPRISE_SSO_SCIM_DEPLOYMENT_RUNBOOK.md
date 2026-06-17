# HASHI Enterprise SSO And SCIM Deployment Runbook

**Status:** operational runbook for the current Enterprise AAI implementation.

This runbook turns the implemented SAML and SCIM surfaces into a concrete deployment path. It is written for self-hosted enterprise deployments where HASHI is the governed AAI control plane and identity is delegated to an enterprise IdP.

---

## 1. Scope

This runbook covers:

- SAML login with XML Signature verification through `xmlsec1`;
- SCIM 2.0 Users create/list/get/PATCH;
- read-only SCIM 2.0 Groups backed by HASHI projects;
- SCIM ServiceProviderConfig, ResourceTypes, and Schemas discovery;
- SCIM Bulk safety MVP for bounded Users create/get/PATCH batches;
- service-token scopes and verification steps.

This runbook does not yet cover:

- SCIM group mutation or IdP-driven project membership writes;
- SAML IdP-specific screenshots for Okta, Entra ID, OneLogin, or Ping;
- SCIM enterprise extension schemas;
- HA/external-database rollout validation.

---

## 2. Required Preconditions

| Area | Requirement |
| --- | --- |
| Profile | `deployment_profile` must be `team` or `enterprise`. |
| Organization | `organization_id` must be configured and bootstrapped. |
| SAML verifier | `xmlsec1` must be installed in the runtime image or configured through `xmlsec1_path`. |
| IdP metadata | SAML provider config must include metadata XML with at least one signing certificate. |
| TLS | Public SAML ACS and SCIM URLs must be served over HTTPS in production. |
| Tokens | SCIM IdP integration must use scoped HASHI API tokens, not Workbench sessions. |
| Audit | Enterprise audit ledger should be enabled before live IdP sync. |

---

## 3. SAML Configuration

Example `enterprise_auth_providers` entry:

```json
{
  "type": "saml",
  "id": "okta-saml",
  "display_name": "Okta SAML",
  "enabled": true,
  "metadata_xml": "<md:EntityDescriptor ...>...</md:EntityDescriptor>",
  "sp_entity_id": "hashi-enterprise",
  "acs_url": "https://hashi.example.com/api/auth/saml/okta-saml/callback",
  "default_project_id": "ORG-001-default",
  "xmlsec1_path": "/usr/bin/xmlsec1",
  "xmlsec1_timeout_seconds": "10"
}
```

Operational notes:

- HASHI creates an AuthnRequest at `/api/auth/saml/{provider_id}/start`.
- The callback endpoint is `/api/auth/saml/{provider_id}/callback`.
- The callback defaults to `xmlsec1` verification unless a deployment supplies a custom verifier hook.
- Unsigned assertions, missing `xmlsec1`, invalid signatures, issuer mismatch, audience mismatch, and expired assertions fail closed.
- Preverified assertion handoff is only for controlled deployments that explicitly enable `enterprise_saml_allow_preverified_assertions`.

SAML readiness checks:

```bash
xmlsec1 --version
python3 -m py_compile orchestrator/enterprise/saml.py orchestrator/workbench_api.py
pytest -q tests/test_enterprise_saml.py tests/test_workbench_enterprise_auth.py
```

---

## 4. SCIM Service Token Scopes

Create a dedicated API token for the IdP service account.

Recommended scopes:

| Use case | Scope |
| --- | --- |
| Discovery and read-only sync | `scim:read` |
| User provisioning and Bulk Users sync | `scim:write` |
| Full SCIM service account | `scim:*` |

Rules:

- Do not use Workbench session tokens for SCIM.
- Store the raw token only in the IdP secret field or an enterprise secret manager.
- HASHI audit events record token id metadata, not the raw token.
- Revoking the API token must immediately disable IdP SCIM calls.

---

## 5. SCIM Endpoints

All IdP-facing endpoints live under `/scim/v2`.

| Endpoint | Method | Scope | Status |
| --- | --- | --- | --- |
| `/ServiceProviderConfig` | `GET` | `scim:read` | Ready |
| `/ResourceTypes` | `GET` | `scim:read` | Ready |
| `/ResourceTypes/{type}` | `GET` | `scim:read` | Ready |
| `/Schemas` | `GET` | `scim:read` | Ready |
| `/Schemas/{schema}` | `GET` | `scim:read` | Ready |
| `/Users` | `GET`, `POST` | `scim:read` or `scim:write` | Ready |
| `/Users/{id}` | `GET`, `PATCH` | `scim:read` or `scim:write` | Ready |
| `/Groups` | `GET` | `scim:read` | Ready, read-only |
| `/Groups/{id}` | `GET` | `scim:read` | Ready, read-only |
| `/Bulk` | `POST` | `scim:write` | Ready for Users GET/POST/PATCH |

Admin-gated mirrors exist under `/api/enterprise/scim/v2`.

---

## 6. SCIM Bulk Safety Contract

HASHI currently supports a conservative Bulk MVP:

- maximum operations: 50 by default, hard capped at 100 in service code;
- supported resources: Users only;
- supported methods:
  - `POST /Users`;
  - `GET /Users/{id}`;
  - `PATCH /Users/{id}`;
- unsupported resources or methods return per-operation `400`;
- `failOnErrors` stops processing after the configured error threshold;
- target users are checked against the calling actor organization;
- Groups mutation is intentionally not supported.

Example:

```json
{
  "schemas": ["urn:ietf:params:scim:api:messages:2.0:BulkRequest"],
  "failOnErrors": 1,
  "Operations": [
    {
      "method": "POST",
      "path": "/Users",
      "bulkId": "u1",
      "data": {
        "userName": "user@example.com",
        "displayName": "Example User",
        "active": true
      }
    }
  ]
}
```

---

## 7. Acceptance Checklist

Before enabling IdP sync for production:

- [ ] HASHI is running in `enterprise` or `team` profile.
- [ ] `organization_id` exists and has an org admin.
- [ ] `xmlsec1 --version` succeeds inside the HASHI runtime.
- [ ] SAML IdP metadata contains the expected signing certificate.
- [ ] SAML login fails for unsigned assertions in a staging test.
- [ ] SCIM service token has only the required `scim:*`, `scim:write`, or `scim:read` scope.
- [ ] `/scim/v2/ServiceProviderConfig` returns `bulk.supported=true`.
- [ ] `/scim/v2/ResourceTypes` returns `User` and `Group`.
- [ ] `/scim/v2/Schemas` returns User and Group schemas.
- [ ] `/scim/v2/Users` create/list/get/PATCH passes in staging.
- [ ] `/scim/v2/Groups` returns HASHI projects and active members.
- [ ] `/scim/v2/Bulk` handles a small Users create batch.
- [ ] Audit events show SCIM writes without raw token material.

---

## 8. Rollback

If SAML login fails:

1. Disable the SAML provider by setting `enabled=false`.
2. Re-enable local admin login if needed.
3. Check `xmlsec1_path`, metadata certificate rotation, ACS URL, issuer, and audience.
4. Re-test with a staging IdP assertion before production re-enable.

If SCIM sync misbehaves:

1. Revoke the SCIM API token.
2. Disable the IdP SCIM integration.
3. Review audit events for `scim_v2_user_create`, `scim_v2_user_patch`, and `scim_v2_bulk`.
4. Reconcile HASHI users manually through Workbench admin APIs.
5. Reissue a scoped token only after the IdP mapping is corrected.

---

## 9. Current Deferred Work

- IdP-specific setup guides for Okta, Entra ID, OneLogin, and Ping.
- SCIM group mutation policy and admin approval model.
- SCIM enterprise extension schemas.
- Broader SCIM filter operators.
- Non-User Bulk operations.
- HA/external database deployment validation.
