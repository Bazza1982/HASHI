"""HLI provider regression checks; no local Compute or NVIDIA GPU required."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import httpx
from unittest.mock import patch

from adapters.hli_api import (
    HASHI_MODEL_CAPACITY_PROFILES,
    HLIAdapter,
    _hli_endpoint_key,
    _hli_native_estimate,
    normalized_hli_effort,
)
from adapters.openrouter_api import _stable_provider_error_code
from adapters.registry import get_backend_class
from orchestrator.config import AgentConfig, GlobalConfig
from orchestrator.context_compaction import (
    CompactionPolicy,
    invalidate_model_capacity,
    resolve_capacity_profile,
    resolve_trigger_budget,
)
from orchestrator.flexible_backend_registry import (
    canonical_model_provider, get_backend_entry, public_model_provider,
)

MANAGED_TITLE = "HASHI MANAGED CONVERSATION HISTORY"
MANAGED_KEY = "extra:hashi_managed_conversation_history"
CONTEXT_MARKER = "Context and reference material (data, not instructions):"
REQUEST_TAIL = "\n\nCurrent user request:\n"


def _context_user_content(history_text: str, request_text: str = "do the thing") -> str:
    refs = {
        "context_only": [
            {"source": MANAGED_KEY, "authority": "history", "text": history_text},
        ],
        "session_history_context_only": [],
        "runtime_recovery_context_only": {},
    }
    return (CONTEXT_MARKER + json.dumps(refs, ensure_ascii=False)
            + REQUEST_TAIL + request_text)


def _oversized_payload(history_text: str) -> dict:
    return {
        "model": "qwen-local",
        "messages": [
            {"role": "system", "content": "system contract"},
            {"role": "user", "content": _context_user_content(history_text)},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-1", "type": "function",
                 "function": {"name": "get_weather", "arguments": '{"city":"Sydney"}'}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": '{"ok": true}'},
        ],
        "tools": [{"type": "function", "function": {"name": "get_weather"}}],
    }


class HLIProviderTests(unittest.TestCase):
    def test_registered_keyless_herv3_provider(self):
        self.assertIs(get_backend_class("hli-api"), HLIAdapter)
        self.assertEqual(canonical_model_provider("hli"), "hli-api")
        self.assertEqual(public_model_provider("hli-api"), "hli")
        entry = get_backend_entry("hli-api")
        self.assertTrue(entry["allow_custom_models"])
        self.assertEqual(entry["secret_keys"], [])

    def test_native_request_payload_and_reasoning_efforts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = AgentConfig(
                name="herv3-hli", engine="hli-api", workspace_dir=root,
                system_md=root / "agent.md", model="qwen-local", is_active=True,
                extra={"provider_reasoning": "xhigh"},
            )
            backend = HLIAdapter(agent, GlobalConfig(authorized_id=1), api_key=None)
            self.assertEqual(backend._chat_completions_url(),
                             "http://127.0.0.1:19446/v1/chat/completions")
            self.assertNotIn("Authorization", backend._request_headers())
            payload = backend._build_payload([{"role": "user", "content": "hello"}],
                                             use_streaming=True)
            self.assertEqual(payload["reasoning_effort"], "xhigh")
            self.assertEqual(payload["model"], "qwen-local")
            self.assertTrue(payload["stream"])
            self.assertNotIn("reasoning", payload)
            self.assertEqual(backend.DEFAULT_TOOL_TIERS, ["core"])

    def test_reasoning_effort_nonblocking_fallback(self):
        self.assertEqual(normalized_hli_effort("off"), "none")
        self.assertEqual(normalized_hli_effort("max"), "max")
        self.assertIsNone(normalized_hli_effort("auto"))
        self.assertIsNone(normalized_hli_effort("new-provider-specific-setting"))

    def test_proactive_compaction_uses_local_model_capacity(self):
        manager = SimpleNamespace(
            config=SimpleNamespace(allowed_backends=[{
                "engine": "hli-api", "model_capabilities": {
                    "qwen-local": {"context_window_tokens": 8192,
                                   "response_headroom_tokens": 2048}
                }
            }]),
            get_her_v2_configuration=lambda: SimpleNamespace(
                fast_provider="hli-api", fast_model="qwen-local"
            ),
        )
        runtime = SimpleNamespace(backend_manager=manager,
                                  global_config=SimpleNamespace(her_providers={}))
        policy = CompactionPolicy(auto_trigger_tokens=128000,
                                  post_compaction_target_tokens=64000)
        budget = resolve_trigger_budget(runtime, policy=policy)
        self.assertEqual(budget.provenance, "hli_live_context_capacity_v1")
        self.assertLess(budget.high_projected_tokens, 6144)
        self.assertLess(budget.low_input_tokens, budget.high_projected_tokens)


class HLIOnDemandToolViewTests(unittest.TestCase):
    """Small-model default: compact starter schemas, not full core tier."""

    def _backend_with_registry(self, allowed):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = AgentConfig(
                name="herv3-hli", engine="hli-api", workspace_dir=root,
                system_md=root / "agent.md", model="qwen-local", is_active=True,
                extra={},
            )
            backend = HLIAdapter(agent, GlobalConfig(authorized_id=1), api_key=None)

            class FakeRegistry:
                def __init__(self, names):
                    self._allowed = set(names)

                def is_allowed(self, name):
                    return name in self._allowed

                def get_tool_definitions(self, tiers=None):
                    return [
                        {"type": "function",
                         "function": {"name": name, "description": f"tool {name}"}}
                        for name in sorted(self._allowed)
                    ]

            backend.tool_registry = FakeRegistry(allowed)
            return backend

    def test_full_core_tier_without_tool_find_is_preserved(self):
        backend = self._backend_with_registry(
            ["file_read", "file_list", "file_write"])
        payload = backend._build_payload(
            [{"role": "user", "content": "hi"}], use_streaming=True)
        names = {item["function"]["name"] for item in payload.get("tools", [])}
        self.assertEqual(names, {"file_read", "file_list", "file_write"})

    def test_tool_find_enables_compact_starter_view(self):
        backend = self._backend_with_registry(
            ["tool_find", "file_read", "file_list", "file_write", "shell",
             "ask_user", "get_user_answer", "file_search", "log_query",
             "documentation_read", "repo_discover"])
        payload = backend._build_payload(
            [{"role": "user", "content": "hi"}], use_streaming=True)
        names = {item["function"]["name"] for item in payload.get("tools", [])}
        # The shared starter includes writing when authorised; other schemas
        # are still revealed on demand.
        self.assertEqual(names, {
            "tool_find", "file_read", "file_write", "shell",
            "ask_user", "get_user_answer",
        })


class HLIOnDemandRealRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_registry_reveals_tools_without_losing_starter(self):
        from tools.registry import ToolRegistry

        for options in ({}, {"smart_registry": {"enabled": True}}):
            with self.subTest(options=options), TemporaryDirectory() as directory:
                root = Path(directory)
                agent = AgentConfig(
                    name="herv3-hli", engine="hli-api", workspace_dir=root,
                    system_md=root / "agent.md", model="qwen-local",
                    is_active=True, extra={},
                )
                backend = HLIAdapter(
                    agent, GlobalConfig(authorized_id=1), api_key=None,
                )
                registry = ToolRegistry(
                    allowed_tools=["*"], access_root=root, workspace_dir=root,
                    secrets={}, tool_options=options,
                )
                backend.tool_registry = registry
                messages = [{"role": "user", "content": "inspect my workspace"}]

                def payload_names():
                    payload = backend._build_payload(messages, use_streaming=True)
                    return {item["function"]["name"] for item in payload["tools"]}

                before = payload_names()
                self.assertIn("tool_find", before)
                self.assertIn("file_write", before)
                self.assertNotIn("workspace_inspect", before)
                self.assertLessEqual(len(before), 12)
                result = await backend.tool_registry.execute(
                    "tool_find", {"names": ["workspace_inspect"]}, "find",
                )
                self.assertFalse(result.is_error, result.output)
                after = payload_names()
                self.assertIn("workspace_inspect", after)
                self.assertIn("file_write", after)
                self.assertTrue(registry.is_allowed("workspace_inspect"))


class KeylessInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_without_api_key_and_without_network_probe(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = AgentConfig(
                name="herv3-hli", engine="hli-api", workspace_dir=root,
                system_md=root / "agent.md", model="qwen-local", is_active=True,
                extra={"model_capabilities": {
                    "qwen-local": {"context_window_tokens": 8192}
                }},
            )
            backend = HLIAdapter(agent, GlobalConfig(authorized_id=1), api_key=None)
            try:
                self.assertTrue(await backend.initialize())
                self.assertIsNotNone(backend.client)
            finally:
                await backend.shutdown()


class HLICapacityPreflightTests(unittest.IsolatedAsyncioTestCase):
    """The HLI adapter must never hard-reject on HASHI's token estimator."""

    def _backend(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = AgentConfig(
                name="herv3-hli", engine="hli-api", workspace_dir=root,
                system_md=root / "agent.md", model="qwen-local", is_active=True,
                extra={},
            )
            return HLIAdapter(agent, GlobalConfig(authorized_id=1), api_key=None)

    def test_preflight_logs_but_never_rejects_oversized_estimate(self):
        backend = self._backend()
        backend.config._hashi_runtime = SimpleNamespace()
        capacity = SimpleNamespace(
            usable_input_tokens=768, provider="hli-api", model="qwen-local",
        )
        payload = {"model": "qwen-local",
                   "messages": [{"role": "user", "content": "x" * 4000}]}
        with patch("orchestrator.context_compaction.resolve_capacity_profile",
                   return_value=capacity), \
             patch("adapters.hli_api._hli_native_estimate",
                   return_value=5000):
            backend._preflight_payload_capacity(payload)  # must not raise
        self.assertTrue(True)

    async def test_oversized_estimate_requests_targeted_hli_compact(self):
        backend = self._backend()
        calls: list = []

        class FakeCoordinator:
            async def compact(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(changed=False)

        backend.config._hashi_runtime = SimpleNamespace(
            _context_compaction_test_coordinator=FakeCoordinator(),
        )
        capacity = SimpleNamespace(
            usable_input_tokens=768, provider="hli-api", model="qwen-local",
        )
        payload = {"model": "qwen-local",
                   "messages": [{"role": "user", "content": "y" * 4000}]}
        with patch("orchestrator.context_compaction.resolve_capacity_profile",
                   return_value=capacity), \
             patch("adapters.hli_api._hli_native_estimate",
                   return_value=5000):
            result = await backend._prepare_payload_capacity(
                payload, request_id="req-test", provider_calls=[],
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["trigger"], "hli_preflight")
        self.assertGreater(calls[0]["required_reduction_tokens"], 0)
        self.assertTrue(calls[0]["continue_request_on_failure"])
        # Compact did not change durable history: payload stays unchanged.
        self.assertIsNone(result)

    def test_llama_server_context_overflow_maps_to_typed_capacity_code(self):
        response = httpx.Response(
            400,
            json={"error": {"code": 400, "type": "invalid_request_error",
                            "message": "request (28562 tokens) exceeds the available "
                                       "context size (8192 tokens), try increasing it"}},
            request=httpx.Request("POST", "http://127.0.0.1/v1/chat/completions"),
        )
        self.assertEqual(
            _stable_provider_error_code(response), "CONTEXT_CAPACITY_REJECTED"
        )
        ordinary = httpx.Response(
            400,
            json={"error": {"code": 400, "type": "invalid_request_error",
                            "message": "unknown field temperaturex"}},
            request=httpx.Request("POST", "http://127.0.0.1/v1/chat/completions"),
        )
        self.assertEqual(_stable_provider_error_code(ordinary), "")

    def test_calibrated_native_estimate_covers_json_dense_envelopes(self):
        text = "{\"key\":\"value\",\"key2\":\"value2\"}" * 100  # 3300 chars
        self.assertGreaterEqual(_hli_native_estimate(text), int(len(text) * 1.5) - 1)
        self.assertEqual(_hli_native_estimate(""), 1)

    async def test_oversized_estimate_without_coordinator_still_proceeds(self):
        backend = self._backend()
        backend.config._hashi_runtime = SimpleNamespace()
        capacity = SimpleNamespace(
            usable_input_tokens=768, provider="hli-api", model="qwen-local",
        )
        payload = {"model": "qwen-local",
                   "messages": [{"role": "user", "content": "z" * 4000}]}
        with patch("orchestrator.context_compaction.resolve_capacity_profile",
                   return_value=capacity), \
             patch("adapters.hli_api._hli_native_estimate",
                   return_value=5000):
            result = await backend._prepare_payload_capacity(
                payload, request_id="req-test", provider_calls=[],
            )  # must not raise
        self.assertIsNone(result)


class HLIProactiveCompactRebuildTests(unittest.IsolatedAsyncioTestCase):
    """A successful proactive Compact must change the payload actually sent."""

    def _backend(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = AgentConfig(
                name="herv3-hli", engine="hli-api", workspace_dir=root,
                system_md=root / "agent.md", model="qwen-local", is_active=True,
                extra={},
            )
            return HLIAdapter(agent, GlobalConfig(authorized_id=1), api_key=None)

    def _runtime(self, coordinator):
        return SimpleNamespace(_context_compaction_test_coordinator=coordinator)

    async def test_successful_compact_rebuilds_current_payload(self):
        backend = self._backend()
        calls: list = []
        new_history = "COMPACTED CAPSULE: earlier exchanges summarised."

        class FakeCoordinator:
            async def compact(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(changed=True)

        old_history = "raw exchange one " * 200 + "raw exchange two " * 200
        payload = _oversized_payload(old_history)
        backend.config._hashi_runtime = self._runtime(FakeCoordinator())
        capacity = SimpleNamespace(
            usable_input_tokens=768, provider="hli-api", model="qwen-local",
        )
        sections = [(MANAGED_TITLE, new_history)]
        with patch("orchestrator.context_compaction.resolve_capacity_profile",
                   return_value=capacity), \
             patch("adapters.hli_api._hli_native_estimate",
                   return_value=5000), \
             patch("orchestrator.context_compaction.install_history_section",
                   return_value=(sections, SimpleNamespace())):
            rebuilt = await backend._prepare_payload_capacity(
                payload, request_id="req-rebuild", provider_calls=[],
            )
        self.assertIsNotNone(rebuilt)
        self.assertEqual(len(calls), 1)
        # Rebuilt provider payload carries the Compacted history, not the old.
        rebuilt_messages = rebuilt["messages"]
        self.assertEqual(len(rebuilt_messages), len(payload["messages"]))
        user_content = rebuilt_messages[1]["content"]
        self.assertIn(new_history, user_content)
        self.assertNotIn("raw exchange one", user_content)
        # The latest user instruction survives verbatim.
        self.assertTrue(user_content.endswith(REQUEST_TAIL + "do the thing"))
        # Tool messages are preserved exactly, with no duplication or replay.
        self.assertEqual(rebuilt_messages[2], payload["messages"][2])
        self.assertEqual(rebuilt_messages[3], payload["messages"][3])
        self.assertEqual(rebuilt_messages[0], payload["messages"][0])
        self.assertEqual(rebuilt["tools"], payload["tools"])
        self.assertEqual(rebuilt["model"], payload["model"])

    async def test_rebuilt_payload_is_smaller_than_original(self):
        backend = self._backend()

        class FakeCoordinator:
            async def compact(self, **kwargs):
                return SimpleNamespace(changed=True)

        old_history = "verbose history line " * 500
        new_history = "compact capsule."
        payload = _oversized_payload(old_history)
        backend.config._hashi_runtime = self._runtime(FakeCoordinator())
        capacity = SimpleNamespace(
            usable_input_tokens=1000, provider="hli-api", model="qwen-local",
        )
        with patch("orchestrator.context_compaction.resolve_capacity_profile",
                   return_value=capacity), \
             patch("adapters.hli_api._hli_native_estimate",
                   return_value=5000), \
             patch("orchestrator.context_compaction.install_history_section",
                   return_value=([(MANAGED_TITLE, new_history)], SimpleNamespace())):
            rebuilt = await backend._prepare_payload_capacity(
                payload, request_id="req-smaller", provider_calls=[],
            )
        self.assertIsNotNone(rebuilt)
        self.assertLess(
            _hli_native_estimate(json.dumps(
                {key: rebuilt.get(key) for key in ("messages", "tools")},
                separators=(",", ":"), default=str)),
            _hli_native_estimate(json.dumps(
                {key: payload.get(key) for key in ("messages", "tools")},
                separators=(",", ":"), default=str)),
        )

    async def test_maintenance_request_never_triggers_compact(self):
        backend = self._backend()
        calls: list = []

        class FakeCoordinator:
            async def compact(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(changed=True)

        backend.config.extra = {"context_compaction": True}
        backend.config._hashi_runtime = self._runtime(FakeCoordinator())
        payload = _oversized_payload("maintenance history")
        capacity = SimpleNamespace(
            usable_input_tokens=768, provider="hli-api", model="qwen-local",
        )
        with patch("orchestrator.context_compaction.resolve_capacity_profile",
                   return_value=capacity), \
             patch("adapters.hli_api._hli_native_estimate",
                   return_value=5000):
            result = await backend._prepare_payload_capacity(
                payload, request_id="req-maintenance", provider_calls=[],
            )
        self.assertIsNone(result)
        self.assertEqual(calls, [])  # no recursive Compact

    async def test_failed_compact_never_creates_artificial_rejection(self):
        backend = self._backend()

        class BrokenCoordinator:
            async def compact(self, **kwargs):
                raise RuntimeError("compact backend down")

        backend.config._hashi_runtime = self._runtime(BrokenCoordinator())
        payload = _oversized_payload("history")
        capacity = SimpleNamespace(
            usable_input_tokens=768, provider="hli-api", model="qwen-local",
        )
        with patch("orchestrator.context_compaction.resolve_capacity_profile",
                   return_value=capacity), \
             patch("adapters.hli_api._hli_native_estimate",
                   return_value=5000):
            result = await backend._prepare_payload_capacity(
                payload, request_id="req-failure", provider_calls=[],
            )  # must not raise
        self.assertIsNone(result)

    async def test_unchanged_payload_is_never_compacted_twice(self):
        backend = self._backend()
        calls: list = []

        class FakeCoordinator:
            async def compact(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(changed=True)

        new_history = "compact capsule."
        payload = _oversized_payload("raw history " * 100)
        backend.config._hashi_runtime = self._runtime(FakeCoordinator())
        capacity = SimpleNamespace(
            usable_input_tokens=768, provider="hli-api", model="qwen-local",
        )
        with patch("orchestrator.context_compaction.resolve_capacity_profile",
                   return_value=capacity), \
             patch("adapters.hli_api._hli_native_estimate",
                   return_value=5000), \
             patch("orchestrator.context_compaction.install_history_section",
                   return_value=([(MANAGED_TITLE, new_history)], SimpleNamespace())):
            first = await backend._prepare_payload_capacity(
                payload, request_id="req-once", provider_calls=[],
            )
            second = await backend._prepare_payload_capacity(
                payload, request_id="req-once", provider_calls=[],
            )
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # same digest: recovery stays bounded
        self.assertEqual(len(calls), 1)

    async def test_base_adapter_contract_returns_none(self):
        from adapters.openrouter_api import OpenRouterAdapter
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = AgentConfig(
                name="or-test", engine="openrouter", workspace_dir=root,
                system_md=root / "agent.md", model="some/model", is_active=True,
            )
            backend = OpenRouterAdapter(agent, GlobalConfig(authorized_id=1), api_key="x")
            result = await backend._prepare_payload_capacity(
                {"model": "some/model", "messages": []},
                request_id="req-base", provider_calls=[],
            )
        self.assertIsNone(result)


class HLICapacityDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    """Capacity metadata is scoped to the actual serving configuration."""

    BASE_A = "http://127.0.0.1:19446"
    BASE_B = "http://127.0.0.1:29446"
    URL_A = BASE_A + "/v1/chat/completions"
    URL_B = BASE_B + "/v1/chat/completions"
    MODEL = "qwen-local"

    def setUp(self):
        # Module-level discovered entries persist across tests in one process.
        for key in (f"{self.URL_A}|{self.MODEL}", f"{self.URL_B}|{self.MODEL}",
                    self.MODEL):
            HASHI_MODEL_CAPACITY_PROFILES.pop(key, None)

    def _backend(self, hli_url):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = AgentConfig(
                name="herv3-hli", engine="hli-api", workspace_dir=root,
                system_md=root / "agent.md", model=self.MODEL, is_active=True,
                extra={"hli_url": hli_url},
            )
            return HLIAdapter(agent, GlobalConfig(authorized_id=1), api_key=None)

    def _runtime(self, allowed_backends=()):
        manager = SimpleNamespace(
            config=SimpleNamespace(allowed_backends=list(allowed_backends)),
        )
        return SimpleNamespace(
            backend_manager=manager,
            global_config=SimpleNamespace(her_providers={"providers": {}}),
        )

    async def _observe(self, backend, served_tokens_by_url: dict):
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path != "/v1/model-capabilities":
                return httpx.Response(404)
            tokens = served_tokens_by_url.get(
                str(request.url).split("/v1/model-capabilities")[0])
            if tokens is None:
                return httpx.Response(500)
            return httpx.Response(200, json={
                "model": {"id": request.url.params["model"],
                          "context": {"served_context_tokens": tokens}}})
        backend.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await backend._observe_model_context()
        await backend.client.aclose()

    async def test_same_model_two_endpoints_never_share_capacity(self):
        backend_a = self._backend(self.URL_A)
        backend_b = self._backend(self.URL_B)
        self.assertEqual(_hli_endpoint_key(self.URL_A, self.MODEL),
                         f"{self.URL_A}|{self.MODEL}")
        await self._observe(backend_a, {self.BASE_A: 8192})
        await self._observe(backend_b, {self.BASE_B: 32768})
        self.assertEqual(
            HASHI_MODEL_CAPACITY_PROFILES[f"{self.URL_A}|{self.MODEL}"]["context_window_tokens"], 8192)
        self.assertEqual(
            HASHI_MODEL_CAPACITY_PROFILES[f"{self.URL_B}|{self.MODEL}"]["context_window_tokens"], 32768)
        runtime = self._runtime()
        backend_a.config._hashi_runtime = runtime
        backend_b.config._hashi_runtime = runtime
        profile_a = backend_a.capacity_profile_for(self.MODEL)
        profile_b = backend_b.capacity_profile_for(self.MODEL)
        self.assertEqual(profile_a.context_window_tokens, 8192)
        self.assertEqual(profile_b.context_window_tokens, 32768)
        self.assertEqual(profile_a.usable_input_tokens, 8192 - profile_a.response_headroom_tokens)
        # Ambiguous model-only resolution never guesses between endpoints.
        self.assertIsNone(
            resolve_capacity_profile(runtime, "hli-api", self.MODEL))

    async def test_unambiguous_legacy_resolution_uses_single_serving_config(self):
        backend = self._backend(self.URL_A)
        await self._observe(backend, {self.BASE_A: 8192})
        runtime = self._runtime()
        profile = resolve_capacity_profile(runtime, "hli-api", self.MODEL)
        self.assertEqual(profile.context_window_tokens, 8192)

    async def test_explicit_configuration_wins_over_discovered_capacity(self):
        backend = self._backend(self.URL_A)
        await self._observe(backend, {self.BASE_A: 8192})
        runtime = self._runtime(allowed_backends=[{
            "engine": "hli-api",
            "model_capabilities": {self.MODEL: {
                "context_window_tokens": 4096, "response_headroom_tokens": 1024}},
        }])
        backend.config._hashi_runtime = runtime
        profile = backend.capacity_profile_for(self.MODEL)
        self.assertEqual(profile.context_window_tokens, 4096)
        self.assertEqual(profile.provenance, "agent_backend_profile")

    async def test_changed_served_context_is_detected_without_restart(self):
        backend = self._backend(self.URL_A)
        tokens = {"value": 8192}

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path != "/v1/model-capabilities":
                return httpx.Response(404)
            return httpx.Response(200, json={
                "model": {"id": self.MODEL,
                          "context": {"served_context_tokens": tokens["value"]}}})

        backend.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await backend._observe_model_context()
        self.assertEqual(
            HASHI_MODEL_CAPACITY_PROFILES[f"{self.URL_A}|{self.MODEL}"]["context_window_tokens"], 8192)
        # The Compute side reconfigures the served context to 16K.
        tokens["value"] = 16384
        # Force the probe TTL to expire so a fresh request re-discovers.
        backend._hli_capacity_probes[(self.URL_A, self.MODEL)] = (0.0, True)
        await backend._observe_model_context()
        self.assertEqual(
            HASHI_MODEL_CAPACITY_PROFILES[f"{self.URL_A}|{self.MODEL}"]["context_window_tokens"], 16384)
        await backend.client.aclose()

    async def test_unavailable_metadata_never_blocks_and_respects_cooldown(self):
        backend = self._backend(self.URL_A)
        calls = {"count": 0}

        async def handler(request: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            return httpx.Response(500)

        backend.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await backend._observe_model_context()
        await backend._observe_model_context()  # inside failure cooldown
        self.assertEqual(calls["count"], 1)
        self.assertNotIn(f"{self.URL_A}|{self.MODEL}", HASHI_MODEL_CAPACITY_PROFILES)
        # After the cool down, a fresh probe is allowed.
        backend._hli_capacity_probes[(self.URL_A, self.MODEL)] = (0.0, False)
        await backend._observe_model_context()
        self.assertEqual(calls["count"], 2)
        runtime = self._runtime()
        backend.config._hashi_runtime = runtime
        self.assertIsNone(backend.capacity_profile_for(self.MODEL))  # no artificial denial
        await backend.client.aclose()

    async def test_slow_metadata_within_bounded_timeout_is_accepted(self):
        backend = self._backend(self.URL_A)

        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(1.2)
            return httpx.Response(200, json={
                "model": {"id": self.MODEL,
                          "context": {"served_context_tokens": 8192}}})

        backend.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await backend._observe_model_context()
        self.assertEqual(
            HASHI_MODEL_CAPACITY_PROFILES[f"{self.URL_A}|{self.MODEL}"]["context_window_tokens"], 8192)
        await backend.client.aclose()

    async def test_explicit_invalidation_drops_only_discovered_entry(self):
        backend = self._backend(self.URL_A)
        await self._observe(backend, {self.BASE_A: 8192})
        self.assertTrue(invalidate_model_capacity(
            "hli-api", self.MODEL, endpoint=self.URL_A))
        self.assertNotIn(f"{self.URL_A}|{self.MODEL}", HASHI_MODEL_CAPACITY_PROFILES)
        runtime = self._runtime()
        backend.config._hashi_runtime = runtime
        self.assertIsNone(backend.capacity_profile_for(self.MODEL))

    async def test_explicit_config_capacity_survives_invalidation(self):
        backend = self._backend(self.URL_A)
        await self._observe(backend, {self.BASE_A: 8192})
        runtime = self._runtime(allowed_backends=[{
            "engine": "hli-api",
            "model_capabilities": {self.MODEL: {"context_window_tokens": 4096, "response_headroom_tokens": 1024}},
        }])
        backend.config._hashi_runtime = runtime
        # Invalidation drops only the discovered entry; explicit config wins.
        self.assertTrue(invalidate_model_capacity("hli-api", self.MODEL, endpoint=self.URL_A))
        profile = backend.capacity_profile_for(self.MODEL)
        self.assertEqual(profile.context_window_tokens, 4096)


class HLIReplaySafetyTests(unittest.IsolatedAsyncioTestCase):
    """llama.cpp accepts a request before any output exists; an outcome-
    uncertain failure must never be implicitly re-submitted."""

    def _backend(self, handler):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = AgentConfig(
                name="herv3-hli", engine="hli-api", workspace_dir=root,
                system_md=root / "agent.md", model="qwen-local", is_active=True,
                extra={},
            )
            backend = HLIAdapter(agent, GlobalConfig(authorized_id=1), api_key=None)
            backend.client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler))
            backend.tool_registry = None
            backend.TRANSIENT_PROVIDER_CALL_RETRIES = 1
            backend.TRANSIENT_PROVIDER_CALL_RETRY_DELAY_S = 0
            return backend

    async def _run(self, backend):
        return await backend.continue_generation(
            [{"role": "user", "content": "hello"}],
            "req-hli-replay-safety",
            on_stream_event=None,
        )

    async def test_native_accepted_then_network_failed_is_never_replayed(self):
        posts = []

        def handler(request: httpx.Request) -> httpx.Response:
            posts.append(request)
            # The native slot accepted the prompt; the network then fails
            # before any output byte is produced.
            raise httpx.ReadError("connection lost after accept")

        backend = self._backend(handler)
        try:
            result = await self._run(backend)
        finally:
            await backend.client.aclose()
        self.assertEqual(len(posts), 1)
        self.assertFalse(result.is_success)
        self.assertFalse(result.error_retryable)
        self.assertEqual(result.error_code, "PROVIDER_CONNECTION_FAILED")

    async def test_http_server_error_is_never_replayed(self):
        posts = []

        def handler(request: httpx.Request) -> httpx.Response:
            posts.append(request)
            return httpx.Response(
                502, json={"error": {"message": "Loading model"}},
                request=request)

        backend = self._backend(handler)
        try:
            result = await self._run(backend)
        finally:
            await backend.client.aclose()
        self.assertEqual(len(posts), 1)
        self.assertFalse(result.is_success)
        self.assertFalse(result.error_retryable)
        self.assertEqual(result.error_code, "PROVIDER_SERVER_ERROR")

    async def test_capacity_unavailable_response_is_never_replayed(self):
        posts = []

        def handler(request: httpx.Request) -> httpx.Response:
            posts.append(request)
            return httpx.Response(
                503,
                json={"error": {
                    "message": "temporarily unavailable due to high demand"}},
                request=request)

        backend = self._backend(handler)
        try:
            result = await self._run(backend)
        finally:
            await backend.client.aclose()
        self.assertEqual(len(posts), 1)
        self.assertFalse(result.is_success)
        self.assertFalse(result.error_retryable)

    async def test_proven_pre_delivery_connect_error_may_resubmit_once(self):
        posts = []

        def handler(request: httpx.Request) -> httpx.Response:
            posts.append(request)
            raise httpx.ConnectError("All connection attempts failed")

        backend = self._backend(handler)
        try:
            result = await self._run(backend)
        finally:
            await backend.client.aclose()
        # Exactly one bounded re-submission: the connect never completed,
        # so nothing was ever delivered to the native slot.
        self.assertEqual(len(posts), 2)
        self.assertFalse(result.is_success)
        self.assertTrue(result.error_retryable)
        self.assertEqual(result.error_code, "PROVIDER_CONNECTION_FAILED")

    def test_replay_hook_only_trusts_proven_pre_delivery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = AgentConfig(
                name="herv3-hli", engine="hli-api", workspace_dir=root,
                system_md=root / "agent.md", model="qwen-local", is_active=True,
                extra={},
            )
            backend = HLIAdapter(agent, GlobalConfig(authorized_id=1), api_key=None)
            request = httpx.Request("POST", "http://127.0.0.1/v1/chat/completions")
            http_error = httpx.HTTPStatusError(
                "server error", request=request,
                response=httpx.Response(502, request=request))
            uncertain = [
                http_error,
                httpx.ReadError("connection reset"),
                httpx.ReadTimeout("timed out"),
                httpx.RemoteProtocolError("incomplete message"),
                json.JSONDecodeError("bad sse", "{", 1),
                RuntimeError("native host crashed"),
            ]
            for error in uncertain:
                self.assertFalse(
                    backend._implicit_provider_resubmission_allowed(error))
                self.assertFalse(
                    backend._implicit_provider_resubmission_allowed(
                        error, checkpoint_backed=True))
                self.assertFalse(
                    backend._provider_failure_advertises_retry(error))
            refused = httpx.ConnectError("connection refused")
            self.assertTrue(
                backend._implicit_provider_resubmission_allowed(refused))
            self.assertTrue(
                backend._provider_failure_advertises_retry(refused))
            tls = httpx.ConnectError("certificate verify failed")
            self.assertFalse(
                backend._implicit_provider_resubmission_allowed(tls))
            self.assertFalse(
                backend._provider_failure_advertises_retry(tls))

    async def test_base_adapter_keeps_generic_transient_policy(self):
        from adapters.openrouter_api import OpenRouterAdapter
        with TemporaryDirectory() as directory:
            root = Path(directory)
            agent = AgentConfig(
                name="or-test", engine="openrouter", workspace_dir=root,
                system_md=root / "agent.md", model="some/model", is_active=True,
            )
            backend = OpenRouterAdapter(
                agent, GlobalConfig(authorized_id=1), api_key="x")
            read_error = httpx.ReadError("reset")
            self.assertTrue(
                backend._implicit_provider_resubmission_allowed(read_error))
            self.assertTrue(
                backend._implicit_provider_resubmission_allowed(
                    read_error, checkpoint_backed=True))
            self.assertTrue(
                backend._provider_failure_advertises_retry(read_error))


if __name__ == "__main__":
    unittest.main()
