"""Fixed independent-account qualification of the production LPAC launcher."""
from dataclasses import asdict
from hashlib import sha256
import json
import time

import appcontainer_fixture as fixture
from orchestrator.permissions.windows_process import launch_suspended


def probe(expected_user_sid):
    def restricted(sid, executable, arguments):
        child = launch_suspended(
            executable=executable, arguments=arguments, cwd=executable.parent,
            environment=fixture._environment(), expected_user_sid=expected_user_sid,
            executable_digest=sha256(executable.read_bytes()).hexdigest(),
            appcontainer_sid=fixture._sid_text(sid), require_lpac=True,
        )
        try:
            child.start()
            deadline = time.monotonic() + 15
            while not child.poll():
                if time.monotonic() >= deadline:
                    raise TimeoutError("fixed LPAC sentinel did not finish")
                time.sleep(0.02)
            code = child.exit_code()
            observation = asdict(child.observation)
        finally:
            child.stop()
        return {
            "token_is_appcontainer": observation["appcontainer_sid"] is not None,
            "token_sid_matches_profile": observation["appcontainer_sid"] == fixture._sid_text(sid),
            "atomic_job_binding_verified": observation["atomic_job"],
            "capability_count": observation["capability_count"],
            "process_exit_code": code, "lpac": observation["lpac"],
            "user_sid_matches": observation["user_sid"] == expected_user_sid,
            "not_elevated": not observation["elevated"],
            "job_stopped": True,
        }

    fixture._run_restricted = restricted
    try:
        return fixture.probe(expected_user_sid)
    except BaseException as error:
        causes, seen, pending = [], set(), [error]
        while pending and len(causes) < 8:
            current = pending.pop(0)
            if current is None or id(current) in seen:
                continue
            seen.add(id(current))
            causes.append({"type": type(current).__name__, "error": str(current)[:300],
                           "winerror": getattr(current, "winerror", None)})
            observation = getattr(current, "observation", None)
            if observation is not None:
                causes[-1]["observation"] = asdict(observation)
            pending.extend([current.__cause__, current.__context__])
        raise RuntimeError(json.dumps({"native_failure": causes})) from error
