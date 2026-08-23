from __future__ import annotations

import hashlib

import pytest

from orchestrator.multimodal_contract import (
    MultimodalContractError,
    attachment_manifest,
    canonical_request_content,
    materialize_openai_user_content,
    resolve_input_capability,
    route_request_content,
)


def _image_part(path, *, index: int = 2, attachment_id: str = "attachment-1"):
    payload = path.read_bytes()
    return {
        "type": "media",
        "item_index": index,
        "attachment_id": attachment_id,
        "modality": "image",
        "kind": "photo",
        "mime_type": "image/png",
        "filename": path.name,
        "caption": "",
        "local_ref": str(path),
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "transport": {"message_id": index},
    }


def _write_png(path) -> None:
    # A minimal signature is enough for contract materialisation tests; image
    # decoding belongs to media_read/vision_inspect tests.
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"hashi-multimodal")


def test_capability_is_resolved_by_provider_model_and_modality():
    gemini = resolve_input_capability(
        "openrouter-api", "google/gemini-2.5-pro"
    )
    text_model = resolve_input_capability(
        "openrouter-api", "deepseek/deepseek-chat"
    )

    assert gemini.supports("image", "data_url") is True
    assert gemini.supports("audio", "data_url") is False
    assert text_model.supports("image", "data_url") is False


def test_unknown_model_fails_closed_to_local_fallback(tmp_path):
    image = tmp_path / "one.png"
    _write_png(image)
    content = canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Inspect it."},
            _image_part(image),
        ]
    )

    capability = resolve_input_capability("openrouter-api", "unknown/model")
    decisions = route_request_content(
        content,
        capability,
        fallback_modalities={"image"},
    )

    assert decisions[0].route == "local_fallback"
    assert decisions[0].reason == "native_capability_unavailable"


def test_local_fallback_materialisation_exposes_ordered_tool_references(tmp_path):
    image = tmp_path / "one.png"
    _write_png(image)
    content = canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Inspect it."},
            _image_part(image),
        ]
    )

    parts, decisions = materialize_openai_user_content(
        "Inspect it.",
        content,
        resolve_input_capability("openrouter-api", "unknown/model"),
        authorized_roots=[tmp_path],
        fallback_modalities={"image"},
    )

    assert [part["type"] for part in parts] == ["text", "text"]
    assert parts[0]["text"] == "Inspect it."
    assert "attachment-1" in parts[1]["text"]
    assert str(image) in parts[1]["text"]
    assert "media bytes were not sent natively" in parts[1]["text"]
    assert decisions[0].route == "local_fallback"


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("openrouter-api", "google/gemini-future-unverified"),
        ("hashi-api", "gpt-future-unverified"),
        ("codex-cli", "gpt-future-unverified"),
    ],
)
def test_model_family_prefix_does_not_grant_unverified_image_support(
    provider, model
):
    capability = resolve_input_capability(provider, model)

    assert capability.supports("image") is False
    assert capability.source == "unknown_fail_closed"


def test_native_image_support_does_not_imply_audio_video_or_pdf():
    capability = resolve_input_capability(
        "openrouter-api", "google/gemini-2.5-pro"
    )

    assert capability.input_modalities == frozenset({"text", "image"})
    assert capability.supports("audio") is False
    assert capability.supports("video") is False
    assert capability.supports("document") is False


def test_mixed_modalities_are_routed_per_attachment(tmp_path):
    image = tmp_path / "one.png"
    _write_png(image)
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS" + b"audio")
    audio_payload = audio.read_bytes()
    content = canonical_request_content(
        [
            _image_part(image, index=1),
            {
                "type": "media",
                "item_index": 2,
                "attachment_id": "attachment-2",
                "modality": "audio",
                "kind": "audio",
                "mime_type": "audio/ogg",
                "filename": audio.name,
                "caption": "",
                "local_ref": str(audio),
                "size_bytes": len(audio_payload),
                "sha256": hashlib.sha256(audio_payload).hexdigest(),
                "transport": {},
            },
        ]
    )

    decisions = route_request_content(
        content,
        resolve_input_capability("openrouter-api", "google/gemini-2.5-pro"),
        fallback_modalities={"audio"},
    )

    assert [(item.attachment_id, item.route) for item in decisions] == [
        ("attachment-1", "native"),
        ("attachment-2", "local_fallback"),
    ]


def test_privacy_policy_can_force_local_processing(tmp_path):
    image = tmp_path / "one.png"
    _write_png(image)
    content = canonical_request_content([_image_part(image, index=1)])
    capability = resolve_input_capability(
        "openrouter-api",
        "google/gemini-2.5-pro",
        config={"native_media_allowed": False},
    )

    decisions = route_request_content(
        content,
        capability,
        fallback_modalities={"image"},
    )

    assert decisions[0].route == "local_fallback"
    assert decisions[0].reason == "privacy_policy_requires_local"
    assert capability.supports("text") is True


def test_privacy_permission_alone_does_not_erase_verified_model_capability():
    capability = resolve_input_capability(
        "openrouter-api",
        "google/gemini-2.5-pro",
        config={"native_media_allowed": True},
    )

    assert capability.supports("image", "data_url") is True
    assert capability.source == "registry"


def test_canonical_envelope_preserves_order_identity_and_integrity(tmp_path):
    first = tmp_path / "same.png"
    second_dir = tmp_path / "other"
    second_dir.mkdir()
    second = second_dir / "same.png"
    _write_png(first)
    second.write_bytes(b"\x89PNG\r\n\x1a\nsecond")
    content = canonical_request_content(
        [
            _image_part(first, index=1, attachment_id="attachment-a"),
            _image_part(second, index=2, attachment_id="attachment-b"),
        ]
    )

    manifest = attachment_manifest(content)

    assert [item["item_index"] for item in manifest] == [1, 2]
    assert [item["attachment_id"] for item in manifest] == [
        "attachment-a",
        "attachment-b",
    ]
    assert manifest[0]["local_ref"] != manifest[1]["local_ref"]
    assert manifest[0]["sha256"] != manifest[1]["sha256"]


def test_canonical_envelope_rejects_duplicate_local_attachment_reference(tmp_path):
    image = tmp_path / "one.png"
    _write_png(image)

    with pytest.raises(MultimodalContractError, match="unique local_ref") as raised:
        canonical_request_content(
            [
                _image_part(image, index=1, attachment_id="attachment-a"),
                _image_part(image, index=2, attachment_id="attachment-b"),
            ]
        )

    assert raised.value.code == "DUPLICATE_MEDIA_REFERENCE"


@pytest.mark.parametrize(
    "forbidden",
    [
        b"raw",
        "data:image/png;base64,AAAA",
        "embedded payload: data:image/png;base64,AAAA",
    ],
)
def test_persistent_metadata_never_contains_inline_media_bytes(forbidden):
    part = {
        "type": "media",
        "item_index": 1,
        "attachment_id": "attachment-1",
        "modality": "image",
        "kind": "photo",
        "mime_type": "image/png",
        "filename": "one.png",
        "caption": "",
        "local_ref": forbidden,
        "size_bytes": 3,
        "sha256": "0" * 64,
        "transport": {},
    }

    with pytest.raises(MultimodalContractError):
        canonical_request_content([part])


def test_persistent_transport_rejects_unbounded_inline_blob_fields(tmp_path):
    image = tmp_path / "one.png"
    _write_png(image)
    part = _image_part(image, index=1)
    part["transport"] = {"provider_blob": "iVBORw0KGgoAAAANSUhEUg"}

    with pytest.raises(MultimodalContractError, match="transport field"):
        canonical_request_content([part])


def test_native_payload_preserves_order_detail_and_mime(tmp_path):
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    _write_png(first)
    second.write_bytes(b"\x89PNG\r\n\x1a\nsecond")
    first_part = _image_part(first, index=2, attachment_id="attachment-a")
    first_part["detail"] = "high"
    content = canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "Compare both."},
            first_part,
            _image_part(second, index=3, attachment_id="attachment-b"),
        ]
    )

    parts, decisions = materialize_openai_user_content(
        "Compare both.",
        content,
        resolve_input_capability("openrouter-api", "google/gemini-2.5-pro"),
        authorized_roots=[tmp_path],
        fallback_modalities={"image"},
    )

    assert parts[0] == {"type": "text", "text": "Compare both."}
    assert [item["attachment_id"] for item in decisions] == [
        "attachment-a",
        "attachment-b",
    ]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[1]["image_url"]["detail"] == "high"
    assert parts[2]["image_url"]["url"].startswith("data:image/png;base64,")


def test_capability_limits_are_checked_before_provider_materialisation(tmp_path):
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    _write_png(first)
    _write_png(second)
    content = canonical_request_content(
        [
            _image_part(first, index=1, attachment_id="attachment-a"),
            _image_part(second, index=2, attachment_id="attachment-b"),
        ]
    )
    capability = resolve_input_capability(
        "openrouter-api",
        "configured/model",
        config={
            "input_modalities": ["text", "image"],
            "input_transports": {"image": ["data_url"]},
            "input_limits": {"item_count": 1},
        },
    )

    decisions = route_request_content(content, capability)

    assert [(item.route, item.reason) for item in decisions] == [
        ("native", "native_capability_available"),
        ("unsupported", "native_item_count_limit_exceeded"),
    ]


def test_unverifiable_dimension_limit_fails_closed_before_native_submission(
    tmp_path,
):
    image = tmp_path / "one.png"
    _write_png(image)
    content = canonical_request_content([_image_part(image, index=1)])
    capability = resolve_input_capability(
        "openrouter-api",
        "configured/model",
        config={
            "input_modalities": ["text", "image"],
            "input_transports": {"image": ["data_url"]},
            "input_limits": {"dimensions": 4096},
        },
    )

    decisions = route_request_content(
        content,
        capability,
        fallback_modalities={"image"},
    )

    assert decisions[0].route == "local_fallback"
    assert decisions[0].reason == (
        "native_dimensions_limit_exceeded_unverified"
    )


def test_native_payload_preserves_interleaved_canonical_order(tmp_path):
    first = tmp_path / "one.png"
    second = tmp_path / "two.png"
    _write_png(first)
    _write_png(second)
    content = canonical_request_content(
        [
            {"type": "text", "item_index": 1, "text": "First."},
            _image_part(first, index=2, attachment_id="attachment-a"),
            {"type": "text", "item_index": 3, "text": "Then second."},
            _image_part(second, index=4, attachment_id="attachment-b"),
        ]
    )

    parts, _decisions = materialize_openai_user_content(
        "Provider instruction that is separate from the original items.",
        content,
        resolve_input_capability("openrouter-api", "google/gemini-2.5-pro"),
        authorized_roots=[tmp_path],
    )

    assert [part["type"] for part in parts] == [
        "text",
        "text",
        "image_url",
        "text",
        "image_url",
    ]
    assert parts[1]["text"] == "First."
    assert parts[3]["text"] == "Then second."


def test_native_materialisation_fails_closed_when_hash_changes(tmp_path):
    image = tmp_path / "one.png"
    _write_png(image)
    content = canonical_request_content([_image_part(image, index=1)])
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"changed-content")

    with pytest.raises(MultimodalContractError) as raised:
        materialize_openai_user_content(
            "Inspect it.",
            content,
            resolve_input_capability(
                "openrouter-api", "google/gemini-2.5-pro"
            ),
            authorized_roots=[tmp_path],
        )

    assert raised.value.code == "MEDIA_INTEGRITY_CHANGED"
    assert raised.value.attachment_id == "attachment-1"


def test_native_materialisation_rejects_path_outside_authorized_root(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    image = tmp_path / "outside.png"
    _write_png(image)
    content = canonical_request_content([_image_part(image, index=1)])

    with pytest.raises(MultimodalContractError) as raised:
        materialize_openai_user_content(
            "Inspect it.",
            content,
            resolve_input_capability(
                "openrouter-api", "google/gemini-2.5-pro"
            ),
            authorized_roots=[allowed],
        )

    assert raised.value.code == "MEDIA_PATH_NOT_AUTHORIZED"
    assert raised.value.attachment_id == "attachment-1"


def test_native_materialisation_rejects_declared_mime_signature_mismatch(tmp_path):
    image = tmp_path / "bad.png"
    image.write_bytes(b"not-a-png")
    content = canonical_request_content([_image_part(image, index=1)])

    with pytest.raises(MultimodalContractError) as raised:
        materialize_openai_user_content(
            "Inspect it.",
            content,
            resolve_input_capability(
                "openrouter-api", "google/gemini-2.5-pro"
            ),
            authorized_roots=[tmp_path],
        )

    assert raised.value.code == "MEDIA_SIGNATURE_MISMATCH"
    assert raised.value.attachment_id == "attachment-1"
