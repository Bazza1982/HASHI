from __future__ import annotations

import xml.etree.ElementTree as ET
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
from typing import Any
from urllib.parse import urlencode
import zlib

from orchestrator.enterprise.auth_providers import AuthProvider, AuthProviderType


MD_NS = "{urn:oasis:names:tc:SAML:2.0:metadata}"
DS_NS = "{http://www.w3.org/2000/09/xmldsig#}"
SAML_NS = "{urn:oasis:names:tc:SAML:2.0:assertion}"
HTTP_REDIRECT_BINDING = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
HTTP_POST_BINDING = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"


@dataclass(frozen=True)
class SamlIdentityProviderMetadata:
    entity_id: str
    sso_url: str
    binding: str
    x509_certificates: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "sso_url": self.sso_url,
            "binding": self.binding,
            "x509_certificate_count": len(self.x509_certificates),
        }


@dataclass(frozen=True)
class SamlAssertionClaims:
    issuer: str
    subject: str
    audience: str
    email: str
    display_name: str
    attributes: dict[str, tuple[str, ...]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "subject": self.subject,
            "audience": self.audience,
            "email": self.email,
            "display_name": self.display_name,
            "attributes": {key: list(value) for key, value in self.attributes.items()},
        }


@dataclass(frozen=True)
class SamlAuthnStart:
    provider_id: str
    state: str
    request_id: str
    acs_url: str
    sp_entity_id: str
    idp_entity_id: str
    binding: str
    sso_url: str
    redirect_url: str | None = None
    post_url: str | None = None
    saml_request: str | None = None

    def public_payload(self) -> dict[str, Any]:
        payload = {
            "provider_id": self.provider_id,
            "state": self.state,
            "request_id": self.request_id,
            "binding": self.binding,
            "sso_url": self.sso_url,
            "acs_url": self.acs_url,
            "sp_entity_id": self.sp_entity_id,
            "idp_entity_id": self.idp_entity_id,
        }
        if self.redirect_url:
            payload["redirect_url"] = self.redirect_url
        if self.post_url:
            payload["post_url"] = self.post_url
            payload["SAMLRequest"] = self.saml_request
        return payload


def parse_saml_idp_metadata(xml_text: str) -> SamlIdentityProviderMetadata:
    root = _parse_xml(xml_text)
    entity_id = _required_text(root.attrib.get("entityID"), "entityID")
    descriptor = _find_first(root, f".//{MD_NS}IDPSSODescriptor")
    if descriptor is None:
        raise ValueError("SAML metadata missing IDPSSODescriptor")
    services = descriptor.findall(f"{MD_NS}SingleSignOnService")
    service = _select_sso_service(services)
    certificates = tuple(
        _compact_certificate(cert.text)
        for cert in descriptor.findall(f".//{DS_NS}X509Certificate")
        if _compact_certificate(cert.text)
    )
    if not certificates:
        raise ValueError("SAML metadata missing signing certificate")
    return SamlIdentityProviderMetadata(
        entity_id=entity_id,
        sso_url=_required_text(service.attrib.get("Location"), "SingleSignOnService Location"),
        binding=_required_text(service.attrib.get("Binding"), "SingleSignOnService Binding"),
        x509_certificates=certificates,
    )


def build_saml_authn_start(provider: AuthProvider) -> SamlAuthnStart:
    if provider.type != AuthProviderType.SAML:
        raise ValueError("provider is not SAML")
    if not provider.ready:
        raise ValueError("SAML provider is not ready")
    metadata = parse_saml_idp_metadata(provider.config.get("metadata_xml") or "")
    sp_entity_id = _required_text(provider.config.get("sp_entity_id"), "SP entity ID")
    acs_url = _required_text(provider.config.get("acs_url"), "ACS URL")
    state = "saml_" + secrets.token_urlsafe(24)
    request_id = "_" + secrets.token_urlsafe(18)
    request_xml = _authn_request_xml(
        request_id=request_id,
        sp_entity_id=sp_entity_id,
        acs_url=acs_url,
        destination=metadata.sso_url,
    )
    binding = provider.config.get("sso_binding") or metadata.binding
    if binding == HTTP_REDIRECT_BINDING:
        encoded_request = _deflated_base64(request_xml)
        query = urlencode({"SAMLRequest": encoded_request, "RelayState": state})
        separator = "&" if "?" in metadata.sso_url else "?"
        return SamlAuthnStart(
            provider_id=provider.id,
            state=state,
            request_id=request_id,
            acs_url=acs_url,
            sp_entity_id=sp_entity_id,
            idp_entity_id=metadata.entity_id,
            binding=binding,
            sso_url=metadata.sso_url,
            redirect_url=f"{metadata.sso_url}{separator}{query}",
        )
    encoded_request = base64.b64encode(request_xml.encode("utf-8")).decode("ascii")
    return SamlAuthnStart(
        provider_id=provider.id,
        state=state,
        request_id=request_id,
        acs_url=acs_url,
        sp_entity_id=sp_entity_id,
        idp_entity_id=metadata.entity_id,
        binding=HTTP_POST_BINDING,
        sso_url=metadata.sso_url,
        post_url=metadata.sso_url,
        saml_request=encoded_request,
    )


def validate_saml_assertion(
    assertion_xml: str,
    *,
    expected_issuer: str,
    expected_audience: str,
    signature_verified: bool,
    now: datetime | None = None,
    clock_skew_seconds: int = 120,
) -> SamlAssertionClaims:
    if not signature_verified:
        raise ValueError("SAML assertion signature must be verified before claims validation")
    root = _parse_xml(assertion_xml)
    assertion = root if _local_name(root.tag) == "Assertion" else _find_first(root, f".//{SAML_NS}Assertion")
    if assertion is None:
        raise ValueError("SAML assertion not found")
    issuer = _required_text(_text_of(_find_first(assertion, f"{SAML_NS}Issuer")), "Issuer")
    if issuer != expected_issuer:
        raise ValueError("SAML issuer mismatch")
    audience = _required_text(_text_of(_find_first(assertion, f".//{SAML_NS}Audience")), "Audience")
    if audience != expected_audience:
        raise ValueError("SAML audience mismatch")
    _validate_conditions(assertion, now=now, clock_skew_seconds=clock_skew_seconds)
    subject = _required_text(_text_of(_find_first(assertion, f".//{SAML_NS}NameID")), "NameID")
    attributes = _attributes(assertion)
    email = _first_attribute(attributes, "email", "mail", "emailaddress") or subject
    display_name = _first_attribute(attributes, "displayname", "name") or email
    return SamlAssertionClaims(
        issuer=issuer,
        subject=subject,
        audience=audience,
        email=email,
        display_name=display_name,
        attributes=attributes,
    )


def _authn_request_xml(*, request_id: str, sp_entity_id: str, acs_url: str, destination: str) -> str:
    return (
        '<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="{request_id}" Version="2.0" '
        f'IssueInstant="{datetime.now(tz=timezone.utc).isoformat()}" '
        f'Destination="{_xml_escape(destination)}" '
        f'AssertionConsumerServiceURL="{_xml_escape(acs_url)}">'
        f"<saml:Issuer>{_xml_escape(sp_entity_id)}</saml:Issuer>"
        "</samlp:AuthnRequest>"
    )


def _deflated_base64(xml_text: str) -> str:
    compressor = zlib.compressobj(wbits=-15)
    compressed = compressor.compress(xml_text.encode("utf-8")) + compressor.flush()
    return base64.b64encode(compressed).decode("ascii")


def _xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _parse_xml(xml_text: str) -> ET.Element:
    text = str(xml_text or "").strip()
    if not text:
        raise ValueError("SAML XML is required")
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise ValueError("SAML XML must not contain DTD or entity declarations")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError("SAML XML is invalid") from exc


def _select_sso_service(services: list[ET.Element]) -> ET.Element:
    if not services:
        raise ValueError("SAML metadata missing SingleSignOnService")
    for binding in (HTTP_REDIRECT_BINDING, HTTP_POST_BINDING):
        for service in services:
            if service.attrib.get("Binding") == binding:
                return service
    return services[0]


def _validate_conditions(assertion: ET.Element, *, now: datetime | None, clock_skew_seconds: int) -> None:
    conditions = _find_first(assertion, f"{SAML_NS}Conditions")
    if conditions is None:
        raise ValueError("SAML Conditions are required")
    current = now or datetime.now(tz=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    skew = timedelta(seconds=max(0, int(clock_skew_seconds)))
    not_before = conditions.attrib.get("NotBefore")
    not_on_or_after = conditions.attrib.get("NotOnOrAfter")
    if not_before and current + skew < _parse_saml_time(not_before):
        raise ValueError("SAML assertion is not yet valid")
    if not_on_or_after and current - skew >= _parse_saml_time(not_on_or_after):
        raise ValueError("SAML assertion has expired")


def _attributes(assertion: ET.Element) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for attribute in assertion.findall(f".//{SAML_NS}Attribute"):
        name = _compact_text(attribute.attrib.get("Name") or attribute.attrib.get("FriendlyName"))
        if not name:
            continue
        values = tuple(
            _compact_text(value.text)
            for value in attribute.findall(f"{SAML_NS}AttributeValue")
            if _compact_text(value.text)
        )
        result[name.lower()] = values
    return result


def _parse_saml_time(value: str) -> datetime:
    text = _required_text(value, "SAML time")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _find_first(root: ET.Element, path: str) -> ET.Element | None:
    return root.find(path)


def _text_of(element: ET.Element | None) -> str:
    return _compact_text(element.text if element is not None else None)


def _first_attribute(attributes: dict[str, tuple[str, ...]], *names: str) -> str | None:
    for name in names:
        values = attributes.get(name.lower())
        if values:
            return values[0]
    return None


def _required_text(value: object, field_name: str) -> str:
    text = _compact_text(value)
    if not text:
        raise ValueError(f"SAML {field_name} is required")
    return text


def _compact_text(value: object) -> str:
    return str(value or "").strip()


def _compact_certificate(value: object) -> str:
    return "".join(str(value or "").split())


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
