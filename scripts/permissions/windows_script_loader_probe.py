"""Fixed diagnostic entry; successful debug runs are not release qualification."""
from __future__ import annotations

import json

import script_fixture as fixture
from loader_diagnostic import OwnedLoaderEvents
from orchestrator.permissions import windows_process


def probe(expected_sid):
    events = OwnedLoaderEvents()
    original_launch = fixture.launch_suspended

    class DiagnosedProcess:
        def __init__(self, owned):
            self.owned = owned
            self.observation = owned.observation

        def start(self):
            self.owned.start()

        def poll(self):
            if not events.exited:
                events.pump(self.owned)
            return events.exited

        def exit_code(self):
            # EXIT_PROCESS_DEBUG_EVENT precedes the signalled process object.
            if self.owned._kernel.WaitForSingleObject(self.owned.process_handle, 1000) != 0:
                raise OSError("the diagnostic child exit is not signalled")
            return self.owned.exit_code()

        def stop(self, **kwargs):
            events.terminate_and_drain(self.owned)
            return self.owned.stop(**kwargs)

    def launch(**kwargs):
        original_api = OwnedLoaderEvents.intercept(windows_process)
        try:
            return DiagnosedProcess(original_launch(**kwargs))
        finally:
            windows_process._api = original_api

    fixture.launch_suspended = launch
    # runtime_access needs the original handle; expose only that exact handle.
    DiagnosedProcess.process_handle = property(lambda self: self.owned.process_handle)
    try:
        result = fixture._probe(expected_sid)
        result["loader_diagnostic_only"] = events.facts()
        return result
    except Exception as error:
        raise RuntimeError(json.dumps({"type": type(error).__name__, "error": str(error)[:200],
                                       "loader": events.facts()})) from error
    finally:
        fixture.launch_suspended = original_launch
