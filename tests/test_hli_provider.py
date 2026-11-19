"""HLI provider regression checks; no local Compute or NVIDIA GPU required."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from adapters.hli_api import HLIAdapter, normalized_hli_effort
from adapters.registry import get_backend_class
from orchestrator.config import AgentConfig, GlobalConfig
from orchestrator.context_compaction import CompactionPolicy, resolve_trigger_budget
from orchestrator.flexible_backend_registry import (
    canonical_model_provider, get_backend_entry, public_model_provider,
)


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


if __name__ == "__main__":
    unittest.main()
