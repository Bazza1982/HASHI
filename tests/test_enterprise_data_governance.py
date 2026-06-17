from __future__ import annotations

from orchestrator.enterprise import (
    DataClassification,
    DataEgressDecision,
    DataGovernancePolicy,
    assess_data_egress,
    classify_text,
)


def test_classify_empty_text_as_public():
    classification, findings = classify_text("")

    assert classification == DataClassification.PUBLIC
    assert findings == ()


def test_classify_plain_business_text_as_internal():
    classification, findings = classify_text("Quarterly deployment plan")

    assert classification == DataClassification.INTERNAL
    assert findings == ()


def test_classify_email_as_confidential_without_exposing_full_local_part():
    classification, findings = classify_text("Contact alice@example.com for approval")

    assert classification == DataClassification.CONFIDENTIAL
    assert findings[0].kind == "email_address"
    assert findings[0].snippet == "a***@example.com"


def test_classify_secret_assignment_as_restricted_and_redacted():
    classification, findings = classify_text("api_key=super-secret-token")

    assert classification == DataClassification.RESTRICTED
    assert findings[0].kind == "secret_assignment"
    assert findings[0].snippet == "api_key=[REDACTED]"
    assert "super-secret-token" not in findings[0].snippet


def test_classify_private_key_as_restricted():
    classification, findings = classify_text("-----BEGIN RSA PRIVATE KEY-----\n...")

    assert classification == DataClassification.RESTRICTED
    assert findings[0].kind == "private_key"


def test_classify_luhn_valid_payment_card_as_restricted():
    classification, findings = classify_text("card 4111 1111 1111 1111")

    assert classification == DataClassification.RESTRICTED
    assert findings[0].kind == "payment_card"
    assert findings[0].snippet == "****1111"


def test_assess_internal_text_allows_default_egress():
    assessment = assess_data_egress("Internal deployment note")

    assert assessment.classification == DataClassification.INTERNAL
    assert assessment.decision == DataEgressDecision.ALLOW
    assert assessment.reason == "classification_within_auto_egress"


def test_assess_confidential_text_requires_default_approval():
    assessment = assess_data_egress("Contact alice@example.com")

    assert assessment.classification == DataClassification.CONFIDENTIAL
    assert assessment.decision == DataEgressDecision.APPROVAL_REQUIRED
    assert assessment.reason == "classification_requires_approval"


def test_assess_restricted_text_denies_default_egress():
    assessment = assess_data_egress("token=super-secret-token")

    assert assessment.classification == DataClassification.RESTRICTED
    assert assessment.decision == DataEgressDecision.DENY
    assert assessment.reason == "classification_exceeds_approval_threshold"


def test_assess_data_residency_denies_unapproved_region():
    assessment = assess_data_egress(
        "Quarterly deployment note",
        destination_region="eu-west-1",
        policy=DataGovernancePolicy(allowed_residency_regions=("au-southeast-2",)),
    )

    assert assessment.decision == DataEgressDecision.DENY
    assert assessment.reason == "destination_region_not_allowed"
    assert assessment.destination_region == "eu-west-1"


def test_policy_can_allow_confidential_auto_egress():
    assessment = assess_data_egress(
        "Contact alice@example.com",
        policy=DataGovernancePolicy(max_auto_egress=DataClassification.CONFIDENTIAL),
    )

    assert assessment.decision == DataEgressDecision.ALLOW


def test_assessment_payload_is_json_safe_and_redacted():
    assessment = assess_data_egress("password=super-secret-password")
    payload = assessment.to_dict()

    assert payload["classification"] == "restricted"
    assert payload["decision"] == "deny"
    assert payload["findings"][0]["snippet"] == "password=[REDACTED]"
    assert "super-secret-password" not in str(payload)
