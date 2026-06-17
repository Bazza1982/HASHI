from __future__ import annotations

from datetime import datetime, timezone

import pytest

from orchestrator.enterprise import build_saml_authn_start, parse_saml_idp_metadata, validate_saml_assertion
from orchestrator.enterprise.auth_providers import load_auth_providers


IDP_METADATA = """\
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
    xmlns:ds="http://www.w3.org/2000/09/xmldsig#"
    entityID="https://idp.example.com/metadata">
  <md:IDPSSODescriptor>
    <md:KeyDescriptor use="signing">
      <ds:KeyInfo>
        <ds:X509Data>
          <ds:X509Certificate>
            MIIC FAKE CERT
          </ds:X509Certificate>
        </ds:X509Data>
      </ds:KeyInfo>
    </md:KeyDescriptor>
    <md:SingleSignOnService
      Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
      Location="https://idp.example.com/sso/post"/>
    <md:SingleSignOnService
      Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
      Location="https://idp.example.com/sso/redirect"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>
"""


ASSERTION = """\
<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">
  <saml:Issuer>https://idp.example.com/metadata</saml:Issuer>
  <saml:Subject>
    <saml:NameID>user@example.com</saml:NameID>
  </saml:Subject>
  <saml:Conditions NotBefore="2026-06-17T00:00:00Z" NotOnOrAfter="2026-06-17T01:00:00Z">
    <saml:AudienceRestriction>
      <saml:Audience>hashi-enterprise</saml:Audience>
    </saml:AudienceRestriction>
  </saml:Conditions>
  <saml:AttributeStatement>
    <saml:Attribute Name="email">
      <saml:AttributeValue>user@example.com</saml:AttributeValue>
    </saml:Attribute>
    <saml:Attribute Name="displayName">
      <saml:AttributeValue>Example User</saml:AttributeValue>
    </saml:Attribute>
  </saml:AttributeStatement>
</saml:Assertion>
"""


def test_parse_saml_idp_metadata_prefers_redirect_binding_and_redacts_cert_payload():
    metadata = parse_saml_idp_metadata(IDP_METADATA)

    assert metadata.entity_id == "https://idp.example.com/metadata"
    assert metadata.sso_url == "https://idp.example.com/sso/redirect"
    assert metadata.binding == "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
    assert metadata.x509_certificates == ("MIICFAKECERT",)
    assert metadata.to_dict()["x509_certificate_count"] == 1
    assert "MIIC" not in str(metadata.to_dict())


def test_build_saml_authn_start_builds_redirect_payload_without_metadata_leak():
    provider = load_auth_providers(
        [
            {
                "type": "saml",
                "id": "okta-saml",
                "enabled": True,
                "metadata_xml": IDP_METADATA,
                "sp_entity_id": "hashi-enterprise",
                "acs_url": "https://hashi.example.com/api/auth/saml/okta-saml/callback",
            }
        ]
    )[1]

    start = build_saml_authn_start(provider)
    payload = start.public_payload()

    assert start.provider_id == "okta-saml"
    assert start.state.startswith("saml_")
    assert start.request_id.startswith("_")
    assert start.redirect_url.startswith("https://idp.example.com/sso/redirect?")
    assert "SAMLRequest=" in start.redirect_url
    assert "RelayState=" in start.redirect_url
    assert payload["sp_entity_id"] == "hashi-enterprise"
    assert "MIIC" not in repr(payload)


def test_validate_saml_assertion_requires_preverified_signature():
    with pytest.raises(ValueError, match="signature must be verified"):
        validate_saml_assertion(
            ASSERTION,
            expected_issuer="https://idp.example.com/metadata",
            expected_audience="hashi-enterprise",
            signature_verified=False,
        )


def test_validate_saml_assertion_extracts_claims_after_signature_verified():
    claims = validate_saml_assertion(
        ASSERTION,
        expected_issuer="https://idp.example.com/metadata",
        expected_audience="hashi-enterprise",
        signature_verified=True,
        now=datetime(2026, 6, 17, 0, 30, tzinfo=timezone.utc),
    )

    assert claims.issuer == "https://idp.example.com/metadata"
    assert claims.subject == "user@example.com"
    assert claims.email == "user@example.com"
    assert claims.display_name == "Example User"
    assert claims.attributes["email"] == ("user@example.com",)


def test_validate_saml_assertion_rejects_issuer_audience_and_expiry_mismatch():
    with pytest.raises(ValueError, match="issuer mismatch"):
        validate_saml_assertion(
            ASSERTION,
            expected_issuer="https://other-idp.example.com",
            expected_audience="hashi-enterprise",
            signature_verified=True,
            now=datetime(2026, 6, 17, 0, 30, tzinfo=timezone.utc),
        )

    with pytest.raises(ValueError, match="audience mismatch"):
        validate_saml_assertion(
            ASSERTION,
            expected_issuer="https://idp.example.com/metadata",
            expected_audience="other-sp",
            signature_verified=True,
            now=datetime(2026, 6, 17, 0, 30, tzinfo=timezone.utc),
        )

    with pytest.raises(ValueError, match="expired"):
        validate_saml_assertion(
            ASSERTION,
            expected_issuer="https://idp.example.com/metadata",
            expected_audience="hashi-enterprise",
            signature_verified=True,
            now=datetime(2026, 6, 17, 1, 5, tzinfo=timezone.utc),
            clock_skew_seconds=0,
        )


def test_saml_xml_rejects_dtd_and_entity_declarations():
    with pytest.raises(ValueError, match="DTD or entity"):
        parse_saml_idp_metadata("<!DOCTYPE foo><foo/>")

    with pytest.raises(ValueError, match="DTD or entity"):
        validate_saml_assertion(
            "<!ENTITY xxe SYSTEM 'file:///etc/passwd'><foo/>",
            expected_issuer="issuer",
            expected_audience="audience",
            signature_verified=True,
        )
