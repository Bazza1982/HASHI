from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


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
