from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from orchestrator.function_generation import FunctionGenerationError
from orchestrator.hot_reload import HotReloadError
from orchestrator.reboot_manager import RebootManager, _resolve_restart_targets


class _ConsoleHandler:
    def addFilter(self, _filter):
        return None

    def removeFilter(self, _filter):
        return None


class _Services:
    def __init__(self, events: list[str], *, fail_once: bool = False):
        self.events = events
        self.fail_once = fail_once
        self.refresh_count = 0

    async def refresh_hot_services(self, *, expected_whatsapp=False):
        self.refresh_count += 1
        self.events.append(
            f"refresh:{self.refresh_count}:whatsapp={expected_whatsapp}"
        )
        if self.fail_once and self.refresh_count == 1:
            raise RuntimeError("service cutover failed")


class _Candidate:
    def __init__(
        self,
        events: list[str],
        *,
        generation: str = "a" * 64,
        activation_error: Exception | None = None,
    ):
        self.events = events
        self.activation_error = activation_error
        self.active = False
        self.manifest = SimpleNamespace(
            generation_id=f"sha256:{generation}",
            entries=(object(), object()),
        )
        self.receipt = SimpleNamespace(
            probe_pid=321,
            runtime=SimpleNamespace(runtime_id="cpython-3.12/core-1/function-1"),
        )

    def activate(self, kernel):
        self.events.append("activate")
        if self.activation_error is not None:
            raise self.activation_error
        self.active = True
        kernel.function_generation = {"generation_id": self.manifest.generation_id}

    def rollback(self, kernel):
        self.events.append("rollback")
        self.active = False
        kernel.function_generation = {"generation_id": "previous"}


class _Kernel:
    def __init__(
        self,
        events: list[str],
        *,
        names: tuple[str, ...] = ("zelda", "sunny"),
        start_results: list[tuple[bool, str]] | None = None,
        service_fail_once: bool = False,
        whatsapp_running: bool = False,
    ):
        self.events = events
        self.runtimes = [SimpleNamespace(name=name) for name in names]
        self.original_runtimes = {runtime.name: runtime for runtime in self.runtimes}
        self.whatsapp = object() if whatsapp_running else None
        self.global_cfg = SimpleNamespace(workbench_port=18800)
        self.api_gateway = None
        self.service_manager = _Services(events, fail_once=service_fail_once)
        self.stop_calls: list[tuple[str, str]] = []
        self.start_calls: list[str] = []
        self.start_results = list(start_results or [])
        self.function_generation = {"generation_id": "previous"}

    def configured_agent_names(self):
        return ["zelda", "sunny", "offline"]

    async def stop_agent(self, name, reason):
        self.events.append(f"stop:{name}:{reason}")
        self.stop_calls.append((name, reason))
        runtime = next((item for item in self.runtimes if item.name == name), None)
        if runtime is None:
            return False, f"Agent '{name}' is not running."
        self.runtimes.remove(runtime)
        return True, "stopped"

    async def start_agent(self, name):
        generation = self.function_generation["generation_id"]
        self.events.append(f"start:{name}:{generation}")
        self.start_calls.append(name)
        result = self.start_results.pop(0) if self.start_results else (True, "started")
        if result[0]:
            runtime = self.original_runtimes.get(name, SimpleNamespace(name=name))
            self.runtimes.append(runtime)
        return result

    def _load_config_bundle(self):
        return (
            None,
            [SimpleNamespace(name=name) for name in self.configured_agent_names()],
            None,
        )


def _install_candidate(monkeypatch, manager, candidate, events):
    def prepare():
        events.append("prepare")
        return candidate

    monkeypatch.setattr(manager, "prepare_candidate_generation", prepare)
    monkeypatch.setattr("orchestrator.banner.show_startup_banner", lambda **_kwargs: None)


def test_in_place_reload_is_retired():
    manager = RebootManager(kernel=object(), console_handler=None)

    with pytest.raises(HotReloadError, match="In-place module reload is retired"):
        manager.reload_project_modules(["orchestrator.runtime_pipeline"])


@pytest.mark.parametrize(
    ("restart", "expected"),
    [
        ({"mode": "min", "agent_name": "zelda"}, ("zelda",)),
        ({"mode": "number", "agent_number": 3}, ("offline",)),
        ({"mode": "same"}, ("zelda", "sunny")),
        ({"mode": "max"}, ("zelda", "sunny")),
    ],
)
def test_restart_scope_requires_an_explicit_broad_mode(restart, expected):
    kernel = SimpleNamespace(
        runtimes=[SimpleNamespace(name="zelda"), SimpleNamespace(name="sunny")],
        configured_agent_names=lambda: ["zelda", "sunny", "offline"],
    )

    assert _resolve_restart_targets(kernel, restart) == expected


@pytest.mark.parametrize(
    "restart",
    [
        {"mode": "min"},
        {"mode": "number", "agent_number": 0},
        {"mode": "number", "agent_number": 4},
        {"mode": "number", "agent_number": "2"},
        {"mode": "unexpected", "agent_name": "zelda"},
    ],
)
def test_invalid_restart_scope_is_rejected_instead_of_falling_back(restart):
    kernel = SimpleNamespace(
        runtimes=[SimpleNamespace(name="zelda"), SimpleNamespace(name="sunny")],
        configured_agent_names=lambda: ["zelda", "sunny", "offline"],
    )

    with pytest.raises(ValueError):
        _resolve_restart_targets(kernel, restart)


@pytest.mark.asyncio
async def test_candidate_rejection_never_touches_running_agents(monkeypatch, capsys):
    events: list[str] = []
    kernel = _Kernel(events, names=("zelda",))
    originals = list(kernel.runtimes)
    manager = RebootManager(kernel, _ConsoleHandler())
    monkeypatch.setattr(
        manager,
        "prepare_candidate_generation",
        lambda: (_ for _ in ()).throw(
            FunctionGenerationError("isolated import failed")
        ),
    )

    result = await manager.hot_restart({"mode": "min", "agent_name": "zelda"})

    assert result is False
    assert kernel.runtimes == originals
    assert kernel.stop_calls == []
    assert kernel.start_calls == []
    assert kernel.service_manager.refresh_count == 0
    output = capsys.readouterr().out.casefold()
    assert "running agents were not touched" in output
    assert "cold" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("restart", "target"),
    [
        ({"mode": "min", "agent_name": "zelda"}, "zelda"),
        ({"mode": "number", "agent_number": 2}, "sunny"),
    ],
)
async def test_verified_generation_cutover_works_for_single_live_target(
    monkeypatch, restart, target
):
    events: list[str] = []
    kernel = _Kernel(events, names=(target,))
    candidate = _Candidate(events)
    manager = RebootManager(kernel, _ConsoleHandler())
    _install_candidate(monkeypatch, manager, candidate, events)

    result = await manager.hot_restart(restart)

    stop_event = f"stop:{target}:hot-restart:{restart['mode']}"
    assert result is True
    assert events.index("prepare") < events.index(stop_event)
    assert events.index(stop_event) < events.index("activate")
    assert events.index("activate") < next(
        index
        for index, event in enumerate(events)
        if event.startswith(f"start:{target}:")
    )
    assert kernel.stop_calls == [(target, f"hot-restart:{restart['mode']}")]
    assert kernel.start_calls == [target]
    assert candidate.active is True
    assert kernel.service_manager.refresh_count == 1


@pytest.mark.asyncio
async def test_targeted_process_scope_cutover_rejects_unselected_live_agent(
    monkeypatch,
    capsys,
):
    events: list[str] = []
    kernel = _Kernel(events)
    candidate = _Candidate(events)
    manager = RebootManager(kernel, _ConsoleHandler())
    _install_candidate(monkeypatch, manager, candidate, events)

    result = await manager.hot_restart({"mode": "min", "agent_name": "zelda"})

    assert result is False
    assert events == []
    assert kernel.stop_calls == []
    assert kernel.start_calls == []
    assert candidate.active is False
    output = capsys.readouterr().out.casefold()
    assert "unsafe target scope" in output
    assert "no agents were stopped" in output


@pytest.mark.asyncio
async def test_commit_failure_restores_stopped_agent_on_previous_generation(
    monkeypatch, capsys
):
    events: list[str] = []
    kernel = _Kernel(events, names=("zelda",))
    candidate = _Candidate(
        events,
        activation_error=FunctionGenerationError("source changed"),
    )
    manager = RebootManager(kernel, _ConsoleHandler())
    _install_candidate(monkeypatch, manager, candidate, events)

    result = await manager.hot_restart({"mode": "min", "agent_name": "zelda"})

    assert result is False
    assert kernel.start_calls == ["zelda"]
    assert [runtime.name for runtime in kernel.runtimes] == ["zelda"]
    assert kernel.function_generation == {"generation_id": "previous"}
    assert kernel.service_manager.refresh_count == 0
    assert "previous generation restored" in capsys.readouterr().out.casefold()


@pytest.mark.asyncio
async def test_new_agent_start_failure_rolls_back_before_old_agent_restore(
    monkeypatch, capsys
):
    events: list[str] = []
    kernel = _Kernel(
        events,
        names=("zelda",),
        start_results=[(False, "new generation failed"), (True, "old restored")],
    )
    candidate = _Candidate(events)
    manager = RebootManager(kernel, _ConsoleHandler())
    _install_candidate(monkeypatch, manager, candidate, events)

    result = await manager.hot_restart({"mode": "min", "agent_name": "zelda"})

    assert result is False
    assert kernel.start_calls == ["zelda", "zelda"]
    assert events.index("rollback") < events.index("start:zelda:previous")
    assert candidate.active is False
    assert [runtime.name for runtime in kernel.runtimes] == ["zelda"]
    assert kernel.service_manager.refresh_count == 0
    assert "previous generation restored" in capsys.readouterr().out.casefold()


@pytest.mark.asyncio
async def test_service_cutover_failure_rolls_back_code_agents_and_services(
    monkeypatch, capsys
):
    events: list[str] = []
    kernel = _Kernel(
        events,
        names=("zelda",),
        service_fail_once=True,
        whatsapp_running=True,
    )
    candidate = _Candidate(events)
    manager = RebootManager(kernel, _ConsoleHandler())
    _install_candidate(monkeypatch, manager, candidate, events)

    result = await manager.hot_restart({"mode": "min", "agent_name": "zelda"})

    assert result is False
    assert candidate.active is False
    assert kernel.function_generation == {"generation_id": "previous"}
    assert kernel.start_calls == ["zelda", "zelda"]
    assert kernel.service_manager.refresh_count == 2
    assert "refresh:1:whatsapp=True" in events
    assert "refresh:2:whatsapp=True" in events
    assert [runtime.name for runtime in kernel.runtimes] == ["zelda"]
    assert "rolled back" in capsys.readouterr().out.casefold()


@pytest.mark.asyncio
async def test_agent_stop_timeout_does_not_activate_candidate(monkeypatch, capsys):
    release_stop = asyncio.Event()
    stop_cancelled = asyncio.Event()
    events: list[str] = []
    kernel = _Kernel(events, names=("zelda",))

    async def hanging_stop(name, reason):
        events.append(f"stop:{name}:{reason}")
        while not release_stop.is_set():
            try:
                await release_stop.wait()
            except asyncio.CancelledError:
                stop_cancelled.set()
        return True, "stopped"

    kernel.stop_agent = hanging_stop
    candidate = _Candidate(events)
    manager = RebootManager(kernel, _ConsoleHandler())
    _install_candidate(monkeypatch, manager, candidate, events)
    monkeypatch.setattr(
        "orchestrator.reboot_manager.AGENT_STOP_TIMEOUT_SECONDS", 0.01
    )

    result = await asyncio.wait_for(
        manager.hot_restart({"mode": "min", "agent_name": "zelda"}),
        timeout=0.5,
    )

    assert result is False
    assert candidate.active is False
    assert "activate" not in events
    assert kernel.start_calls == []
    assert "retry /reboot" in capsys.readouterr().out.casefold()
    await asyncio.sleep(0)
    assert stop_cancelled.is_set()
    release_stop.set()


@pytest.mark.asyncio
async def test_broad_stop_failure_restores_only_already_stopped_agents(monkeypatch):
    events: list[str] = []
    kernel = _Kernel(events)
    original_stop = kernel.stop_agent

    async def stop_with_second_failure(name, reason):
        if name == "sunny":
            kernel.stop_calls.append((name, reason))
            events.append(f"stop:{name}:{reason}")
            return False, "still active"
        return await original_stop(name, reason)

    kernel.stop_agent = stop_with_second_failure
    candidate = _Candidate(events)
    manager = RebootManager(kernel, _ConsoleHandler())
    _install_candidate(monkeypatch, manager, candidate, events)

    result = await manager.hot_restart({"mode": "max"})

    assert result is False
    assert candidate.active is False
    assert "activate" not in events
    assert kernel.start_calls == ["zelda"]
    assert sorted(runtime.name for runtime in kernel.runtimes) == ["sunny", "zelda"]


@pytest.mark.asyncio
async def test_repeated_reboot_commits_each_complete_generation(monkeypatch):
    events: list[str] = []
    kernel = _Kernel(events, names=("zelda",))
    candidates = [_Candidate(events, generation=char * 64) for char in ("a", "b")]
    manager = RebootManager(kernel, _ConsoleHandler())
    monkeypatch.setattr(
        manager,
        "prepare_candidate_generation",
        lambda: candidates.pop(0),
    )
    monkeypatch.setattr("orchestrator.banner.show_startup_banner", lambda **_kwargs: None)

    assert await manager.hot_restart({"mode": "min", "agent_name": "zelda"})
    first = kernel.function_generation["generation_id"]
    assert await manager.hot_restart({"mode": "min", "agent_name": "zelda"})
    second = kernel.function_generation["generation_id"]

    assert first == "sha256:" + "a" * 64
    assert second == "sha256:" + "b" * 64
    assert kernel.start_calls == ["zelda", "zelda"]
