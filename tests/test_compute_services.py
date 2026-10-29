from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from orchestrator.config_json import new_config_json, write_config_json
from orchestrator.compute_services import (
    ComputeReference,
    ComputeRequirements,
    ComputeNode,
    ComputeServiceError,
    ComputeServiceClient,
    ComputeServicesConfig,
    ComputeServiceDirectory,
    parse_compute_manifest,
    validate_compute_node,
)


def _lan_node(**overrides):
    value = {
        "id": "studio-gpu",
        "label": "Studio GPU",
        "location": "lan",
        "base_url": "http://192.168.1.50:19446",
        "manifest_path": "/.well-known/hashi-compute",
        "health_path": "/health",
        "enabled": True,
    }
    value.update(overrides)
    return validate_compute_node(value)


def _formal_manifest(*, busy=False):
    return {
        "schema": "hashi.compute.capabilities",
        "schema_version": 1,
        "name": "Hashi Compute",
        "version": "0.2.0",
        "config_revision": 41,
        "privacy_location": "lan",
        "health": {"status": "ready", "path": "/health"},
        "capacity": {"max_concurrency": 4, "in_flight": 1, "busy": False},
        "services": [
            {
                "id": "vision",
                "kind": "vision",
                "enabled": True,
                "status": "busy" if busy else "ready",
                "capacity": {
                    "max_concurrency": 1,
                    "in_flight": 1 if busy else 0,
                    "busy": busy,
                },
                "models": [
                    {
                        "id": "minicpm-v-4.6",
                        "selected": True,
                        "capabilities": {
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                            "tools": False,
                            "thinking": False,
                            "context_length": 32768,
                            "image": {
                                "max_count": 16,
                                "max_bytes": 10485760,
                                "max_total_bytes": 67108864,
                                "formats": ["jpeg", "png", "webp"],
                            },
                            "audio": {
                                "input_formats": [],
                                "output_formats": [],
                            },
                        },
                        "interfaces": [
                            {
                                "id": "chat",
                                "protocol": "openai.chat.completions",
                                "method": "POST",
                                "path": "/v1/chat/completions",
                                "streaming": ["none", "sse"],
                            }
                        ],
                    }
                ],
            },
            {
                "id": "advanced-voice",
                "kind": "advanced_tts",
                "enabled": True,
                "status": "ready",
                "capacity": {
                    "max_concurrency": 2,
                    "in_flight": 0,
                    "busy": False,
                },
                "models": [
                    {
                        "id": "minicpm-o-4.5-zh-female",
                        "selected": True,
                        "capabilities": {
                            "input_modalities": ["text", "audio"],
                            "output_modalities": ["audio", "text"],
                            "tools": False,
                            "thinking": False,
                            "context_length": 32768,
                            "image": {
                                "max_count": None,
                                "max_bytes": None,
                                "max_total_bytes": None,
                                "formats": [],
                            },
                            "audio": {
                                "input_formats": ["wav", "pcm"],
                                "output_formats": ["wav", "mp3"],
                            },
                        },
                        "interfaces": [
                            {
                                "id": "speech",
                                "protocol": "openai.audio.speech",
                                "method": "POST",
                                "path": "/v1/audio/speech",
                                "streaming": ["none", "chunked"],
                            },
                            {
                                "id": "sample",
                                "protocol": "hashi.voice.sample",
                                "method": "GET",
                                "path": "/v1/voices/{voice_id}/sample",
                                "streaming": ["none"],
                            },
                        ],
                    }
                ],
            },
        ],
    }


def _hashi_compute_manifest():
    return {
        "format": "hashi-compute.manifest",
        "version": 1,
        "service": {
            "name": "Hashi Compute",
            "version": "0.5.0",
            "base_url": "http://untrusted.example/v1",
            "configuration_revision": 44,
        },
        "gateway": {
            "max_in_flight": 16,
            "in_flight": 0,
            "busy": False,
            "at_capacity": False,
        },
        "health": {"lightweight": "/health"},
        "privacy": {"client_access_location": "lan"},
        "models": [
            {
                "id": "minicpm-v-4.6",
                "service_id": "vision",
                "available": True,
                "availability": "available",
                "modalities": {
                    "input": {
                        "text": "supported",
                        "image": "supported",
                        "audio": "unsupported",
                    },
                    "output": {
                        "text": "supported",
                        "image": "unsupported",
                        "audio": "unsupported",
                    },
                },
                "interfaces": [
                    {
                        "path": "/v1/chat/completions",
                        "method": "POST",
                        "request_encoding": "application/json",
                        "streaming": {
                            "support": "unsupported",
                            "modes": [],
                        },
                    }
                ],
                "image": {
                    "support": "supported",
                    "max_count": 1,
                    "max_bytes_each": 2097152,
                    "max_pixels_each": 8000000,
                    "formats": ["jpeg", "png"],
                    "transports": ["data_uri_base64"],
                    "remote_url_fetch": False,
                },
                "audio": {"input_formats": [], "output_formats": []},
                "features": {
                    "tools": {"support": "unsupported"},
                    "thinking": {"support": "unsupported"},
                },
                "context": {
                    "model_max_tokens": None,
                    "served_context_tokens": None,
                    "max_input_chars": 8000,
                    "max_output_tokens": 512,
                },
                "runtime": {
                    "readiness": "ready",
                    "busy": False,
                    "at_capacity": False,
                    "in_flight": 0,
                    "pending": 0,
                    "max_pending_requests": 4,
                    "inference_parallelism": 1,
                },
            }
        ],
    }


def test_formal_manifest_projects_every_required_machine_capability():
    snapshot = parse_compute_manifest(_lan_node(), _formal_manifest())

    assert snapshot.schema_version == 1
    assert snapshot.source_format == "formal"
    assert snapshot.service_version == "0.2.0"
    assert snapshot.config_revision == "41"
    assert snapshot.privacy_location == "lan"
    assert snapshot.health.status == "ready"
    assert snapshot.health.path == "/health"
    assert snapshot.capacity.max_concurrency == 4
    assert snapshot.capacity.in_flight == 1
    assert snapshot.capacity.busy is False

    vision = snapshot.offering("vision", "minicpm-v-4.6")
    assert vision.input_modalities == frozenset({"text", "image"})
    assert vision.output_modalities == frozenset({"text"})
    assert vision.tools is False
    assert vision.thinking is False
    assert vision.context_length == 32768
    assert vision.image.max_count == 16
    assert vision.image.max_bytes == 10485760
    assert vision.image.max_total_bytes == 67108864
    assert vision.image.formats == ("jpeg", "png", "webp")
    assert vision.interface("openai.chat.completions").streaming == ("none", "sse")
    assert (
        vision.interface_url(snapshot.node, "openai.chat.completions")
        == "http://192.168.1.50:19446/v1/chat/completions"
    )

    voice = snapshot.offering("advanced-voice", "minicpm-o-4.5-zh-female")
    assert voice.audio.input_formats == ("wav", "pcm")
    assert voice.audio.output_formats == ("wav", "mp3")
    assert (
        voice.interface_url(
            snapshot.node,
            "hashi.voice.sample",
            voice_id="中文 女声",
        )
        == "http://192.168.1.50:19446/v1/voices/%E4%B8%AD%E6%96%87%20%E5%A5%B3%E5%A3%B0/sample"
    )


def test_hashi_compute_manifest_projects_vision_without_trusting_advertised_origin():
    snapshot = parse_compute_manifest(_lan_node(health_path="/health"), _hashi_compute_manifest())

    assert snapshot.schema_version == 1
    assert snapshot.source_format == "hashi_compute_manifest"
    assert snapshot.service_version == "0.5.0"
    assert snapshot.config_revision == "44"
    assert snapshot.privacy_location == "lan"
    assert snapshot.health.status == "ready"
    vision = snapshot.offering("vision", "minicpm-v-4.6")
    assert vision.input_modalities == frozenset({"text", "image"})
    assert vision.output_modalities == frozenset({"text"})
    assert vision.image.max_count == 1
    assert vision.image.max_bytes == 2097152
    assert vision.image.max_total_bytes is None
    assert vision.image.formats == ("jpeg", "png")
    assert vision.tools is False
    assert vision.thinking is False
    assert (
        vision.interface_url(snapshot.node, "openai.chat.completions")
        == "http://192.168.1.50:19446/v1/chat/completions"
    )


def test_hashi_compute_manifest_keeps_unavailable_unknown_model_non_authorizing():
    payload = _hashi_compute_manifest()
    payload["models"].append(
        {
            "id": "zimage",
            "service_id": "image",
            "available": False,
            "availability": "backend_or_model_unavailable",
            "modalities": {
                "input": {"text": "unknown", "image": "unknown", "audio": "unknown"},
                "output": {"text": "unknown", "image": "unknown", "audio": "unknown"},
            },
            "interfaces": [
                {
                    "path": "/v1/image/generations",
                    "method": "POST",
                    "request_encoding": "application/json",
                    "streaming": {"support": "unknown", "modes": []},
                }
            ],
            "image": {
                "support": "unknown",
                "max_count": None,
                "max_bytes_each": None,
                "formats": None,
            },
            "audio": {"input_formats": None, "output_formats": None},
            "features": {
                "tools": {"support": "unknown"},
                "thinking": {"support": "unknown"},
            },
            "context": {
                "model_max_tokens": None,
                "served_context_tokens": None,
            },
            "runtime": {
                "readiness": "unavailable",
                "busy": None,
                "at_capacity": None,
                "in_flight": None,
            },
        }
    )

    snapshot = parse_compute_manifest(_lan_node(), payload)
    unavailable = snapshot.offering("image", "zimage")

    assert unavailable.enabled is False
    assert unavailable.status == "unavailable"
    assert unavailable.input_modalities == frozenset()
    assert unavailable.output_modalities == frozenset()
    assert unavailable.interface("http.v1.image.generations").streaming == ()
    with pytest.raises(ComputeServiceError, match="service_unavailable"):
        snapshot.catalogue().resolve(
            ComputeReference("studio-gpu", "image", "zimage")
        )


def test_hashi_compute_manifest_rejects_unknown_streaming_for_available_model():
    payload = _hashi_compute_manifest()
    payload["models"][0]["interfaces"][0]["streaming"]["support"] = "unknown"

    with pytest.raises(ComputeServiceError, match="manifest_interface_invalid"):
        parse_compute_manifest(_lan_node(), payload)


@pytest.mark.parametrize(
    ("gateway", "blocked"),
    [
        ({"max_in_flight": 16, "in_flight": 1, "busy": True, "at_capacity": False}, False),
        ({"max_in_flight": 16, "in_flight": 1, "busy": True, "at_capacity": True}, True),
        ({"max_in_flight": 16, "in_flight": 16, "busy": True, "at_capacity": False}, True),
        ({"max_in_flight": 16, "in_flight": 1, "busy": True}, True),
    ],
)
def test_hashi_compute_gateway_activity_does_not_mean_capacity(gateway, blocked):
    # An active camera request must not block an idle offering on the same node.
    payload = _hashi_compute_manifest()
    payload["gateway"] = gateway
    catalogue = parse_compute_manifest(_lan_node(), payload).catalogue()
    reference = ComputeReference("studio-gpu", "vision", "minicpm-v-4.6")

    if blocked:
        with pytest.raises(ComputeServiceError, match="compute_node_busy"):
            catalogue.resolve(reference)
    else:
        assert catalogue.resolve(reference).reference == reference


def test_hashi_compute_manifest_cannot_change_configured_privacy_boundary():
    payload = _hashi_compute_manifest()
    payload["privacy"]["client_access_location"] = "internet"

    with pytest.raises(ComputeServiceError, match="manifest_location_mismatch"):
        parse_compute_manifest(_lan_node(), payload)


def test_formal_manifest_fixture_matches_published_json_schema():
    schema_path = (
        Path(__file__).parents[1] / "specs" / "compute" / "v1" / "manifest.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(_formal_manifest())
    invalid = _formal_manifest()
    invalid["services"][0]["models"][0]["capabilities"]["image"]["max_count"] = None
    assert list(validator.iter_errors(invalid))


def test_formal_manifest_rejects_missing_required_capability_details():
    payload = _formal_manifest()
    del payload["services"][0]["models"][0]["capabilities"]["image"]

    with pytest.raises(ComputeServiceError, match="manifest_capabilities_incomplete"):
        parse_compute_manifest(_lan_node(), payload)


def test_formal_manifest_cannot_change_configured_privacy_boundary():
    payload = _formal_manifest()
    payload["privacy_location"] = "internet"

    with pytest.raises(ComputeServiceError, match="manifest_location_mismatch"):
        parse_compute_manifest(_lan_node(), payload)


def test_formal_health_and_node_capacity_fail_closed():
    unhealthy = _formal_manifest()
    unhealthy["health"]["status"] = "unknown"
    ref = ComputeReference("studio-gpu", "vision", "minicpm-v-4.6")
    with pytest.raises(ComputeServiceError, match="compute_node_unhealthy"):
        parse_compute_manifest(_lan_node(), unhealthy).catalogue().resolve(ref)

    saturated = _formal_manifest()
    saturated["capacity"] = {
        "max_concurrency": 1,
        "in_flight": 1,
        "busy": False,
    }
    catalogue = parse_compute_manifest(_lan_node(), saturated).catalogue()
    with pytest.raises(ComputeServiceError, match="compute_node_busy"):
        catalogue.resolve(ref)
    assert catalogue.resolve(ref, allow_busy=True).reference == ref


def test_legacy_overview_is_compatible_without_inventing_media_limits():
    payload = {
        "name": "Hashi Compute",
        "version": "0.1.0",
        "revision": 38,
        "services": [
            {
                "id": "vision",
                "kind": "vision",
                "enabled": True,
                "status": "ready",
                "base_url": "http://127.0.0.1:48254",
                "models": [
                    {
                        "id": "minicpm-v-4.6",
                        "selected": True,
                        "capabilities": [],
                        "endpoints": ["/v1/chat/completions"],
                        "details": {},
                    }
                ],
            },
            {
                "id": "ollama",
                "kind": "ollama",
                "enabled": True,
                "status": "ready",
                "models": [
                    {
                        "id": "qwen35:9b",
                        "selected": True,
                        "capabilities": ["tools", "thinking", "completion"],
                        "endpoints": ["/v1/chat/completions", "/api/chat"],
                        "details": {"context_length": 262144},
                    }
                ],
            },
        ],
        "metrics": {"in_flight": 0, "completed": 10, "failed": 0},
    }

    snapshot = parse_compute_manifest(_lan_node(), payload)
    vision = snapshot.offering("vision", "minicpm-v-4.6")
    assert snapshot.source_format == "legacy_overview"
    assert vision.input_modalities == frozenset({"text", "image"})
    assert vision.output_modalities == frozenset({"text"})
    assert vision.image.max_count is None
    assert vision.image.max_bytes is None
    assert vision.image.max_total_bytes is None
    assert vision.image.formats == ()
    # Never route to the server's private loopback implementation address.
    assert (
        vision.interface_url(snapshot.node, "openai.chat.completions")
        == "http://192.168.1.50:19446/v1/chat/completions"
    )

    chat = snapshot.offering("ollama", "qwen35:9b")
    assert chat.tools is True
    assert chat.thinking is True
    assert chat.context_length == 262144


def test_legacy_overview_tolerates_absent_capability_list():
    payload = {
        "name": "Hashi Compute",
        "version": "0.1.0",
        "revision": 1,
        "services": [
            {
                "id": "stt",
                "kind": "stt",
                "enabled": True,
                "status": "ready",
                "models": [
                    {
                        "id": "sensevoice-small",
                        "selected": True,
                        "endpoints": ["/v1/audio/transcriptions"],
                    }
                ],
            }
        ],
    }

    offering = parse_compute_manifest(_lan_node(), payload).offering(
        "stt", "sensevoice-small"
    )
    assert offering.input_modalities == frozenset({"audio"})
    assert offering.tools is None


def test_legacy_overview_accepts_complete_enriched_capability_object():
    model = _formal_manifest()["services"][0]["models"][0]
    payload = {
        "name": "Hashi Compute",
        "version": "0.1.1",
        "revision": 39,
        "services": [
            {
                "id": "vision",
                "kind": "vision",
                "enabled": True,
                "status": "ready",
                "models": [
                    {
                        "id": model["id"],
                        "selected": True,
                        "capabilities": model["capabilities"],
                        "endpoints": ["/v1/chat/completions"],
                    }
                ],
            }
        ],
    }

    offering = parse_compute_manifest(_lan_node(), payload).offering(
        "vision", "minicpm-v-4.6"
    )
    assert offering.image.max_count == 16
    assert offering.image.formats == ("jpeg", "png", "webp")


@pytest.mark.parametrize(
    "value",
    [
        {"location": "lan", "base_url": "http://8.8.8.8:19446"},
        {"location": "lan", "base_url": "http://compute.local:19446"},
        {"location": "local", "base_url": "http://192.168.1.50:19446"},
        {"location": "internet", "base_url": "http://models.example.com"},
        {"location": "internet", "base_url": "https://192.168.1.50"},
        {"location": "lan", "base_url": "http://user:pass@192.168.1.50"},
        {"location": "lan", "base_url": "http://192.168.1.50/root"},
        {"location": "lan", "base_url": "http://192.168.1.50:99999"},
    ],
)
def test_node_configuration_fails_closed_at_privacy_boundary(value):
    row = {
        "id": "unsafe",
        "location": value["location"],
        "base_url": value["base_url"],
        "manifest_path": "/v1/capabilities",
    }
    with pytest.raises(ComputeServiceError, match="compute_node_invalid"):
        validate_compute_node(row)


def test_node_uses_a_non_conflicting_well_known_manifest_default():
    node = validate_compute_node(
        {
            "id": "studio-gpu",
            "location": "lan",
            "base_url": "http://192.168.1.50:19446",
        }
    )
    assert node.manifest_path == "/.well-known/hashi-compute"


def test_direct_dataclass_construction_cannot_bypass_node_validation():
    unsafe = ComputeNode(
        id="unsafe",
        label="Unsafe",
        location="lan",
        base_url="http://8.8.8.8",
        manifest_path="/v1/capabilities",
    )
    with pytest.raises(ComputeServiceError, match="compute_node_invalid"):
        parse_compute_manifest(unsafe, _formal_manifest())


def test_legacy_direct_voice_sample_is_a_get_interface():
    payload = {
        "name": "Hashi Compute",
        "version": "0.1.0",
        "revision": 40,
        "services": [
            {
                "id": "tts",
                "kind": "tts",
                "enabled": True,
                "status": "ready",
                "models": [
                    {
                        "id": "voice-model",
                        "selected": True,
                        "capabilities": [],
                        "endpoints": ["/v1/voices/{voice_id}/sample"],
                    }
                ],
            }
        ],
    }
    offering = parse_compute_manifest(_lan_node(), payload).offering(
        "tts", "voice-model"
    )
    interface = offering.interface("hashi.voice.sample")
    assert interface.method == "GET"
    assert (
        interface.render_url(_lan_node(), voice_id="sample-voice")
        == "http://192.168.1.50:19446/v1/voices/sample-voice/sample"
    )


def test_exact_resolution_checks_capabilities_and_never_falls_back():
    ready = parse_compute_manifest(_lan_node(), _formal_manifest())
    catalogue = ready.catalogue()
    ref = ComputeReference("studio-gpu", "vision", "minicpm-v-4.6")
    selected = catalogue.resolve(
        ref,
        ComputeRequirements(
            input_modalities=frozenset({"image"}),
            output_modalities=frozenset({"text"}),
            protocols=frozenset({"openai.chat.completions"}),
            locations=frozenset({"lan"}),
        ),
    )
    assert selected.reference == ref

    with pytest.raises(ComputeServiceError, match="offering_not_found"):
        catalogue.resolve(ComputeReference("studio-gpu", "vision", "other-model"))
    with pytest.raises(ComputeServiceError, match="capability_mismatch"):
        catalogue.resolve(
            ref,
            ComputeRequirements(output_modalities=frozenset({"audio"})),
        )


def test_busy_offering_is_visible_but_not_routable_by_default():
    snapshot = parse_compute_manifest(_lan_node(), _formal_manifest(busy=True))
    ref = ComputeReference("studio-gpu", "vision", "minicpm-v-4.6")

    with pytest.raises(ComputeServiceError, match="service_busy"):
        snapshot.catalogue().resolve(ref)
    assert snapshot.catalogue().resolve(ref, allow_busy=True).offering.busy is True


@pytest.mark.asyncio
async def test_directory_refreshes_configured_nodes_and_fences_failed_refresh(tmp_path):
    path = tmp_path / "compute_services.json"
    write_config_json(
        path,
        new_config_json(
            path,
            {
                "version": 1,
                "nodes": [
                    {
                        "id": "studio-gpu",
                        "location": "lan",
                        "base_url": "http://192.168.1.50:19446",
                        "manifest_path": "/v1/capabilities",
                        "health_path": "/health",
                        "enabled": True,
                    }
                ],
            },
        ),
    )

    class Client:
        error = None

        async def fetch_manifest(self, node):
            if self.error:
                raise ComputeServiceError(self.error)
            return parse_compute_manifest(node, _formal_manifest())

    client = Client()
    directory = ComputeServiceDirectory(ComputeServicesConfig(path), client=client)
    first = await directory.refresh()
    assert first.errors == {}
    assert (
        first.catalogue.resolve(
            ComputeReference("studio-gpu", "vision", "minicpm-v-4.6")
        ).offering.model_id
        == "minicpm-v-4.6"
    )

    client.error = "compute_node_unreachable"
    second = await directory.refresh()
    assert second.errors == {"studio-gpu": "compute_node_unreachable"}
    with pytest.raises(ComputeServiceError, match="compute_node_unreachable"):
        second.catalogue.resolve(
            ComputeReference("studio-gpu", "vision", "minicpm-v-4.6")
        )


def test_configuration_uses_revisioned_json_and_rejects_duplicate_nodes(tmp_path):
    path = tmp_path / "compute_services.json"
    config = ComputeServicesConfig(path)
    config.initialize()
    doc, nodes = config.read()
    assert doc["version"] == 1
    assert nodes == ()

    doc["nodes"] = [
        {
            "id": "studio-gpu",
            "location": "lan",
            "base_url": "http://192.168.1.50:19446",
            "manifest_path": "/v1/capabilities",
        },
        {
            "id": "studio-gpu",
            "location": "lan",
            "base_url": "http://192.168.1.51:19446",
            "manifest_path": "/v1/capabilities",
        },
    ]
    with pytest.raises(ComputeServiceError, match="compute_node_duplicate"):
        config.write(doc)

    # The invalid candidate never replaced the last durable configuration.
    assert json.loads(path.read_text(encoding="utf-8"))["nodes"] == []


class _FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, payload, *, status=200, content_type="application/json"):
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = _FakeContent(
            [payload if isinstance(payload, bytes) else json.dumps(payload).encode()]
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _FakeSession:
    def __init__(self, response, receipt):
        self.response = response
        self.receipt = receipt

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, url, **kwargs):
        self.receipt["method"] = "GET"
        self.receipt["url"] = url
        self.receipt.update(kwargs)
        return self.response

    def post(self, url, **kwargs):
        self.receipt["method"] = "POST"
        self.receipt["url"] = url
        self.receipt.update(kwargs)
        return self.response


class _FakeSessionFactory:
    def __init__(self, response):
        self.response = response
        self.receipt = {}

    def __call__(self, **kwargs):
        self.receipt["session"] = kwargs
        return _FakeSession(self.response, self.receipt)


@pytest.mark.asyncio
async def test_manifest_client_is_bounded_and_never_follows_redirects():
    factory = _FakeSessionFactory(_FakeResponse(_formal_manifest()))
    snapshot = await ComputeServiceClient(session_factory=factory).fetch_manifest(
        _lan_node()
    )

    assert snapshot.source_format == "formal"
    assert (
        factory.receipt["url"] == "http://192.168.1.50:19446/.well-known/hashi-compute"
    )
    assert factory.receipt["allow_redirects"] is False
    assert factory.receipt["session"]["trust_env"] is False

    redirect = _FakeSessionFactory(_FakeResponse({}, status=302))
    with pytest.raises(ComputeServiceError, match="compute_manifest_rejected"):
        await ComputeServiceClient(session_factory=redirect).fetch_manifest(_lan_node())

    oversized = _FakeSessionFactory(_FakeResponse(b"x" * (2 * 1024 * 1024 + 1)))
    with pytest.raises(ComputeServiceError, match="compute_manifest_too_large"):
        await ComputeServiceClient(session_factory=oversized).fetch_manifest(
            _lan_node()
        )

    malformed = _FakeSessionFactory(_FakeResponse(b"{not-json"))
    with pytest.raises(ComputeServiceError, match="manifest_invalid"):
        await ComputeServiceClient(session_factory=malformed).fetch_manifest(
            _lan_node()
        )


@pytest.mark.asyncio
async def test_invocation_client_posts_only_to_resolved_configured_origin():
    snapshot = parse_compute_manifest(_lan_node(), _hashi_compute_manifest())
    resolved = snapshot.catalogue().resolve(
        ComputeReference("studio-gpu", "vision", "minicpm-v-4.6")
    )
    response = {"choices": [{"message": {"content": "A red bird."}}]}
    factory = _FakeSessionFactory(_FakeResponse(response))

    result = await ComputeServiceClient(session_factory=factory).invoke_json(
        resolved,
        "openai.chat.completions",
        {"model": "minicpm-v-4.6", "messages": []},
    )

    assert result == response
    assert factory.receipt["method"] == "POST"
    assert factory.receipt["url"] == (
        "http://192.168.1.50:19446/v1/chat/completions"
    )
    assert factory.receipt["allow_redirects"] is False
    assert factory.receipt["session"]["trust_env"] is False
    assert factory.receipt["json"]["model"] == "minicpm-v-4.6"

    redirect = _FakeSessionFactory(_FakeResponse({}, status=302))
    with pytest.raises(ComputeServiceError, match="compute_invocation_rejected"):
        await ComputeServiceClient(session_factory=redirect).invoke_json(
            resolved,
            "openai.chat.completions",
            {"model": "minicpm-v-4.6", "messages": []},
        )

    oversized = _FakeSessionFactory(
        _FakeResponse(b"x" * (4 * 1024 * 1024 + 1))
    )
    with pytest.raises(
        ComputeServiceError,
        match="compute_invocation_response_too_large",
    ):
        await ComputeServiceClient(session_factory=oversized).invoke_json(
            resolved,
            "openai.chat.completions",
            {"model": "minicpm-v-4.6", "messages": []},
        )


@pytest.mark.asyncio
async def test_binary_invocation_keeps_audio_on_the_resolved_configured_origin(
    monkeypatch,
):
    snapshot = parse_compute_manifest(_lan_node(), _formal_manifest())
    resolved = snapshot.catalogue().resolve(
        ComputeReference("studio-gpu", "advanced-voice", "minicpm-o-4.5-zh-female")
    )
    wav = b"RIFF\x24\x00\x00\x00WAVEfmt "
    factory = _FakeSessionFactory(
        _FakeResponse(wav, content_type="audio/wav")
    )

    payload, content_type = await ComputeServiceClient(
        session_factory=factory
    ).invoke_bytes(
        resolved,
        "openai.audio.speech",
        {
            "model": "minicpm-o-4.5-zh-female",
            "input": "hello",
            "voice": "zf_001",
            "response_format": "wav",
        },
    )

    assert payload == wav
    assert content_type == "audio/wav"
    assert factory.receipt["url"] == "http://192.168.1.50:19446/v1/audio/speech"
    assert factory.receipt["allow_redirects"] is False
    assert factory.receipt["session"]["trust_env"] is False

    monkeypatch.setattr(
        "orchestrator.compute_services.MAX_AUDIO_RESPONSE_BYTES",
        len(wav) - 1,
    )
    oversized = _FakeSessionFactory(
        _FakeResponse(wav, content_type="audio/wav")
    )
    with pytest.raises(
        ComputeServiceError,
        match="compute_invocation_response_too_large",
    ):
        await ComputeServiceClient(session_factory=oversized).invoke_bytes(
            resolved,
            "openai.audio.speech",
            {"model": "minicpm-o-4.5-zh-female", "input": "hello"},
        )

    wrong_type = _FakeSessionFactory(_FakeResponse({}, content_type="application/json"))
    with pytest.raises(
        ComputeServiceError,
        match="compute_invocation_content_type_invalid",
    ):
        await ComputeServiceClient(session_factory=wrong_type).invoke_bytes(
            resolved,
            "openai.audio.speech",
            {"model": "minicpm-o-4.5-zh-female", "input": "hello"},
        )
