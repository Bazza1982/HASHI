from adapters.gemini_cli import _gemini_failure_response


def test_retired_individual_client_is_typed_and_actionable():
    response = _gemini_failure_response(
        "IneligibleTierError: Gemini Code Assist for individuals "
        "reasonCode=UNSUPPORTED_CLIENT",
        duration_ms=12,
    )

    assert response.is_success is False
    assert response.error_code == "PROVIDER_CLIENT_UNSUPPORTED"
    assert response.error_retryable is False
    assert "Antigravity" in response.error


def test_authentication_failure_is_not_misreported_as_retired_client():
    response = _gemini_failure_response(
        "Authentication failed. Please sign in.",
        duration_ms=12,
    )

    assert response.error_code == "PROVIDER_AUTHENTICATION_FAILED"
    assert response.error_retryable is False
