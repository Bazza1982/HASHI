from __future__ import annotations

import json

from orchestrator.post_turn_registry import build_post_turn_observers


def test_missing_observer_config_returns_empty_list(tmp_path):
    observers = build_post_turn_observers(
        workspace_dir=tmp_path,
        bridge_memory_store=object(),
        backend_invoker=None,
        backend_context_getter=None,
    )

    assert observers == []


def test_invalid_observer_config_logs_warning_and_returns_empty_list(tmp_path, caplog):
    (tmp_path / "post_turn_observers.json").write_text("{not-json", encoding="utf-8")

    observers = build_post_turn_observers(
        workspace_dir=tmp_path,
        bridge_memory_store=object(),
        backend_invoker=None,
        backend_context_getter=None,
    )

    assert observers == []
    assert "Failed to read" in caplog.text


def test_disabled_observer_is_ignored(tmp_path):
    (tmp_path / "post_turn_observers.json").write_text(
        json.dumps(
            {
                "observers": [
                    {
                        "factory": "missing.module:factory",
                        "enabled": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    observers = build_post_turn_observers(
        workspace_dir=tmp_path,
        bridge_memory_store=object(),
        backend_invoker=None,
        backend_context_getter=None,
    )

    assert observers == []


def test_valid_observer_factory_loads_with_runtime_dependencies(tmp_path, monkeypatch):
    module_dir = tmp_path / "modules"
    module_dir.mkdir()
    (module_dir / "observer_plugin.py").write_text(
        """
class DummyObserver:
    def __init__(self, workspace_dir, bridge_memory_store, backend_invoker, backend_context_getter, options):
        self.workspace_dir = workspace_dir
        self.bridge_memory_store = bridge_memory_store
        self.backend_invoker = backend_invoker
        self.backend_context_getter = backend_context_getter
        self.options = options

    def should_observe(self, source, *, is_bridge_request):
        return True

    def schedule_observation(self, request, background_tasks):
        return None

    def workspace_files_to_preserve(self):
        return frozenset({"dummy.json"})


def build_observer(*, workspace_dir, bridge_memory_store, backend_invoker=None, backend_context_getter=None, options=None):
    return DummyObserver(workspace_dir, bridge_memory_store, backend_invoker, backend_context_getter, options or {})
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(module_dir))
    bridge_memory_store = object()

    (tmp_path / "post_turn_observers.json").write_text(
        json.dumps(
            {
                "observers": [
                    {
                        "factory": "observer_plugin:build_observer",
                        "options": {"mode": "shadow"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    observers = build_post_turn_observers(
        workspace_dir=tmp_path,
        bridge_memory_store=bridge_memory_store,
        backend_invoker="invoker",
        backend_context_getter="context",
    )

    assert len(observers) == 1
    observer = observers[0]
    assert observer.workspace_dir == tmp_path
    assert observer.bridge_memory_store is bridge_memory_store
    assert observer.backend_invoker == "invoker"
    assert observer.backend_context_getter == "context"
    assert observer.options == {"mode": "shadow"}
