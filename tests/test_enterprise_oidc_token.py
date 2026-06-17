from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone

import pytest

from orchestrator.enterprise.auth_providers import load_auth_providers
from orchestrator.enterprise.oidc_flow import build_oidc_authorization_start
from orchestrator.enterprise.oidc_token import validate_oidc_id_token_claims, verify_oidc_id_token

_RSA_N = int(
    "99cbd839e06fe8272a970416d22a97bc6228b2e1f689f6de80fb86395cc7c2b2347f1e3e263d257f052a2609801a2e4b38392b85fe12d2de8f6f75bac3a1a64976d9b0f598c4250458ba4dacf4ac7412f0585c41c0d1f8e8c0b8a16b05ce916b93280ecf69bc14a1d32045b39c17c36547c78edd4a124b7f0721cb414741a73a2665e2445d505a014c25e2ca7cd45d0c611bba3dcdf1e240c76b63dfbcd37e041d6ea1d5f664cc8c56b63bf3d217c1ba54105d10b69873cb9d07369dcea3036b9d85f78e4799693ac7cec526e0a4176522c3c3102163ef3bed7b61e05b44c8d8f2d4596db67ed0996159b2855215cfa10c7f067f82fcf79dde8845dfbd3312ad",
    16,
)
_RSA_E = 65537
_RSA_D = int(
    "3595189b697f73a199ac0da7a9c75f202a0ec5ec060a21317a3ca791faaa3a41fe1a3fbe25726e4ae7d0bc79d8e0c63a3cb7665b839ea94b132c211755ab4f150f4c5ee3e23a8c2f0c7eb42b4aff7e5d4ed16a2b1a73cd45c247512dc95323c517faffd5f19fae8c86d31a95ae0f756b26ebf6f1502a39956335b9ebdf58975b17ab0fac1ba10804845a6d569263c93d3d02e9fce742b87a9ab22b3eecd4d0092dba8c64afbf428fd72f875432b1cf1f90d2ee0ea44a388423ce43b584ff15b437a1ff730ed8b6a3345e84cee9e25af9ebba8a6a6cdfa5fdabd3a23b62696ad5207bafe52096c120cd49e5647a27822d67dd2df0746aba5801f09860510aca5d",
    16,
)


def _provider():
    return load_auth_providers(
        [
            {
                "type": "oidc",
                "id": "entra",
                "enabled": True,
                "issuer": "https://login.microsoftonline.com/tenant/v2.0",
                "client_id": "hashi-client",
                "authorization_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/authorize",
                "token_endpoint": "https://login.microsoftonline.com/tenant/oauth2/v2.0/token",
                "jwks_uri": "https://login.microsoftonline.com/tenant/discovery/v2.0/keys",
            }
        ]
    )[1]


def _flow(provider):
    return build_oidc_authorization_start(
        provider,
        redirect_uri="https://hashi.example.com/api/auth/oidc/entra/callback",
    )


def _claims(flow, **overrides):
    claims = {
        "iss": "https://login.microsoftonline.com/tenant/v2.0",
        "sub": "subject-123",
        "aud": "hashi-client",
        "exp": 1_800_000_000,
        "iat": 1_700_000_000,
        "nonce": flow.nonce,
        "email": "Admin@Example.com",
    }
    claims.update(overrides)
    return claims


def _rsa_key_and_jwks(kid: str = "key-1"):
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "alg": "RS256",
                "use": "sig",
                "n": _b64url_int(_RSA_N),
                "e": _b64url_int(_RSA_E),
            }
        ]
    }
    return {"n": _RSA_N, "d": _RSA_D}, jwks


def _jwt(private_key, claims, *, kid: str = "key-1", alg: str = "RS256"):
    header = {"typ": "JWT", "alg": alg, "kid": kid}
    signing_input = f"{_b64url_json(header)}.{_b64url_json(claims)}".encode("ascii")
    if alg == "RS256":
        signature = _sign_rs256(private_key, signing_input)
    elif alg == "none":
        signature = b""
    else:
        signature = _sign_rs256(private_key, signing_input)
    return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


def test_validate_oidc_id_token_claims_accepts_core_claims():
    provider = _provider()
    flow = _flow(provider)

    validated = validate_oidc_id_token_claims(
        provider,
        flow,
        _claims(flow),
        now=datetime.fromtimestamp(1_700_000_100, tz=timezone.utc),
    )

    assert validated.provider_id == "entra"
    assert validated.subject == "subject-123"
    assert validated.email == "admin@example.com"
    assert validated.audience == ("hashi-client",)
    assert "nonce" not in validated.public_payload()


def test_verify_oidc_id_token_accepts_rs256_jwks_signature():
    provider = _provider()
    flow = _flow(provider)
    private_key, jwks = _rsa_key_and_jwks()
    token = _jwt(private_key, _claims(flow))

    validated = verify_oidc_id_token(
        provider,
        flow,
        token,
        jwks,
        now=datetime.fromtimestamp(1_700_000_100, tz=timezone.utc),
    )

    assert validated.subject == "subject-123"
    assert validated.email == "admin@example.com"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"iss": "https://evil.example.com"}, "issuer mismatch"),
        ({"aud": "other-client"}, "audience mismatch"),
        ({"exp": 1_600_000_000}, "ID token is expired"),
        ({"nbf": 1_900_000_000}, "ID token is not yet valid"),
        ({"iat": 1_900_000_000}, "ID token issued_at is in the future"),
        ({"nonce": "wrong"}, "nonce mismatch"),
        ({"sub": ""}, "sub claim is required"),
    ],
)
def test_validate_oidc_id_token_claims_rejects_unsafe_claims(override, message):
    provider = _provider()
    flow = _flow(provider)

    with pytest.raises(ValueError, match=message):
        validate_oidc_id_token_claims(
            provider,
            flow,
            _claims(flow, **override),
            now=datetime.fromtimestamp(1_700_000_100, tz=timezone.utc),
        )


@pytest.mark.parametrize(
    ("token_factory", "jwks_factory", "message"),
    [
        (lambda key, flow: _jwt(key, _claims(flow), alg="none"), lambda jwks: jwks, "unsupported"),
        (lambda key, flow: _jwt(key, _claims(flow), kid="missing"), lambda jwks: jwks, "key not found"),
        (
            lambda key, flow: _jwt(key, _claims(flow))[:-2] + "xx",
            lambda jwks: jwks,
            "signature verification failed",
        ),
        (
            lambda key, flow: _jwt(key, _claims(flow)),
            lambda jwks: {"keys": [{**jwks["keys"][0], "alg": "RS512"}]},
            "algorithm mismatch",
        ),
    ],
)
def test_verify_oidc_id_token_rejects_unsafe_jwt_inputs(token_factory, jwks_factory, message):
    provider = _provider()
    flow = _flow(provider)
    private_key, jwks = _rsa_key_and_jwks()

    with pytest.raises(ValueError, match=message):
        verify_oidc_id_token(
            provider,
            flow,
            token_factory(private_key, flow),
            jwks_factory(jwks),
            now=datetime.fromtimestamp(1_700_000_100, tz=timezone.utc),
        )


def _b64url_json(value: dict) -> str:
    return _b64url(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def _b64url_int(value: int) -> str:
    return _b64url(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _sign_rs256(private_key: dict[str, int], signing_input: bytes) -> bytes:
    modulus = private_key["n"]
    private_exponent = private_key["d"]
    modulus_len = (modulus.bit_length() + 7) // 8
    encoded = _pkcs1_v1_5_sha256_encoded(signing_input, modulus_len)
    signature = pow(int.from_bytes(encoded, "big"), private_exponent, modulus)
    return signature.to_bytes(modulus_len, "big")


def _pkcs1_v1_5_sha256_encoded(message: bytes, length: int) -> bytes:
    digest_info_prefix = bytes.fromhex("3031300d060960864801650304020105000420")
    digest_info = digest_info_prefix + hashlib.sha256(message).digest()
    padding_len = length - len(digest_info) - 3
    return b"\x00\x01" + (b"\xff" * padding_len) + b"\x00" + digest_info
