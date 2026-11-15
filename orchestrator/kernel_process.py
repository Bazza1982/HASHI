"""Small process supervisor. No product modules execute in this process."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from orchestrator.function_worker_bootstrap import (
    QUALIFICATION_RESULT_PREFIX,
)
from orchestrator.function_worker_protocol import JsonConnectionPeer
from orchestrator.runtime_process import launch_function_process_async, run_protected_call_async
from orchestrator.kernel_artifact import (
    artifact_verification_stats,
    validate_artifact_identity,
    verify_artifact,
)
from orchestrator.runtime_contract import (
    CORE_HOST_ROLE,
    RuntimeContractError,
    RuntimeFingerprint,
    admitted_process_runtime_fingerprint,
    admitted_runtime_from_reference,
    compare_runtime_boundary,
    load_core_runtime_selection,
    current_process_runtime,
    runtime_verification_stats,
    runtime_installation_reference,
    compare_runtime_fingerprints,
    enforce_runtime_contract,
)

RECEIPT_PREFIX = QUALIFICATION_RESULT_PREFIX


def _observed_runtime(code_root: Path, runtime: RuntimeFingerprint):
    admitted = current_process_runtime()
    if admitted is not None and admitted.fingerprint.role == runtime.role:
        return admitted_process_runtime_fingerprint(admitted, code_root=code_root)
    return enforce_runtime_contract(code_root, role=runtime.role)


def canonical_instance_home(code_root: Path, override=None) -> Path:
    raw = (
        str(override).strip()
        if override is not None
        else os.environ.get("BRIDGE_HOME", "").strip()
    )
    if '"' in raw:
        raw = raw[: raw.index('"')]
    raw = raw.strip().rstrip("'")
    return (
        Path(os.path.expanduser(os.path.expandvars(raw))).resolve()
        if raw
        else Path(code_root).resolve()
    )


def instance_runtime_dir(bridge_home: Path) -> Path:
    return Path(bridge_home) / "state" / "instance"


def write_record(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


async def qualify(
    code_root: Path, bridge_home: Path, runtime: RuntimeFingerprint, *, functions_runtime=None
) -> dict:
    """Run product qualification out of process; verify its exact receipt here."""
    compare_runtime_fingerprints(runtime, _observed_runtime(code_root, runtime))
    if runtime.role == CORE_HOST_ROLE and functions_runtime is None:
        raise RuntimeContractError("Core host requires an independently qualified Functions runtime")
    expected = functions_runtime.fingerprint if functions_runtime is not None else runtime
    if runtime.role == CORE_HOST_ROLE:
        compare_runtime_boundary(runtime, expected, expected_functions=expected)
    profile = json.loads((code_root / "runtime-entry.json").read_text(encoding="utf-8"))
    payload = {
        "code_root": str(code_root),
        "bridge_home": str(bridge_home),
        "runtime": expected.to_dict(),
        **({"core_runtime": runtime.to_dict()} if runtime.role == CORE_HOST_ROLE else {}),
        "entrypoint": profile["entrypoint"],
        "qualifier": profile["qualifier"],
    }
    release = await run_protected_call_async(
        payload, purpose="qualification", timeout=240, runtime=functions_runtime)
    compare_runtime_fingerprints(
        expected, RuntimeFingerprint.from_mapping(release["runtime"])
    )
    compare_runtime_fingerprints(runtime, _observed_runtime(code_root, runtime))
    if functions_runtime is not None:
        release["functions_installation"] = runtime_installation_reference(functions_runtime)
    artifact = Path(release["generation_root"]).resolve()
    if not artifact.is_relative_to(bridge_home / "state" / "function_generations"):
        raise ValueError("qualified artifact is outside this instance")
    if release["entrypoint"] != profile["entrypoint"]:
        raise ValueError("qualification entrypoint mismatch")
    verify_artifact(artifact, release["manifest"])
    return release


class ProcessClient:
    def __init__(self, process, connection):
        self.process = process
        self.requested_exit = False
        self.owns_group = False
        self.job = None
        self.peer = JsonConnectionPeer(
            connection, label=f"functions:{process.pid}", event_handler=self._event
        )
        self.peer.start()

    async def _event(self, event, payload):
        if event == "application.stopped":
            self.requested_exit = True

    @classmethod
    async def prepare(cls, bootstrap: dict):
        # Qualification already verified bytes once. Routine launch validates
        # the generation identity and private admission contract only.
        selected = bootstrap.get("functions_installation")
        process, parent = await launch_function_process_async(
            bootstrap,
            runtime=selected,
            generation_already_verified=True,
        )
        client = cls(process, parent)
        try:
            ready = await client.peer.request("prepare", timeout=240)
            if (
                ready.get("pid") != process.pid
                or ready.get("generation_id") != bootstrap["manifest"]["generation_id"]
            ):
                raise RuntimeError("Function process READY identity mismatch")
            client.ready_metadata = ready
            client.owns_group = bool(bootstrap.get("process_group"))
            return client
        except BaseException as exc:
            # The private parent accepted this child before product imports
            # were released, so a later READY failure may have executed product
            # code even though no ProcessClient is returned to the caller.
            setattr(exc, "hashi_product_code_execution_began", True)
            setattr(exc, "hashi_candidate_pid", process.pid)
            await client.close()
            raise

    async def close(self):
        if self.process.is_alive() and not self.peer.is_closed:
            try:
                await self.peer.request("stop", timeout=45)
            except Exception:
                pass
        await self.peer.close()
        await asyncio.to_thread(self.process.join, 5)
        if self.process.is_alive():
            # Each child is an isolated process group. Never signal another
            # instance or the supervising Core. Windows uses an exact PID tree.
            if os.name == "nt":
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            else:
                try:
                    if os.getpgid(self.process.pid) == self.process.pid:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    else:
                        self.process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.to_thread(self.process.join, 5)
        if self.job is not None:
            self.job.close()
            self.job = None
        if os.name != "nt" and self.owns_group:
            deadline = asyncio.get_running_loop().time() + 30
            while True:
                try:
                    os.killpg(self.process.pid, 0)
                except ProcessLookupError:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    break
                await asyncio.sleep(0.1)
        self.process.close()


class KernelRuntime:
    """Retain the process/lock while independently replacing Functions.

    Product execution and service state live exclusively in the child. Agent
    replacements are handled there without involving this broad handoff.
    """

    def __init__(
        self,
        code_root: Path,
        bridge_home: Path,
        runtime: RuntimeFingerprint,
        *,
        arguments: dict | None = None,
        functions_runtime=None,
    ):
        self.code_root = code_root.resolve()
        self.bridge_home = bridge_home.resolve()
        self.runtime = runtime
        self.functions_runtime = functions_runtime
        self.arguments = arguments or {}
        self.active = None
        self.release = None
        self.stop_event = asyncio.Event()
        self._switch_lock = asyncio.Lock()
        self.state_dir = instance_runtime_dir(self.bridge_home)
        self.last_replacement_outcome = None

    def verify_supplied_release(self, release: dict) -> None:
        """Qualify an externally supplied generation once before execution.

        ``qualify()`` already performs this scan for generations it creates.
        A supplied mapping carries no unforgeable in-process qualification
        fact, so it must cross the same byte boundary here.  Recovery of the
        exact committed ``self.release`` is handled separately and remains an
        identity-only admission.
        """
        artifact = Path(release["generation_root"]).resolve()
        generation_root = (
            self.bridge_home / "state" / "function_generations"
        ).resolve()
        if not artifact.is_relative_to(generation_root):
            raise ValueError("supplied artifact is outside this instance")
        verify_artifact(artifact, release["manifest"])

    def selected_functions_runtime(self):
        if self.runtime.role == CORE_HOST_ROLE:
            if self.functions_runtime is not None:
                compare_runtime_boundary(
                    self.runtime,
                    self.functions_runtime.fingerprint,
                    expected_functions=self.functions_runtime.fingerprint,
                )
                return self.functions_runtime
            selected = load_core_runtime_selection(self.code_root)
            compare_runtime_fingerprints(self.runtime, selected.core.fingerprint)
            self.functions_runtime = selected.functions
            return self.functions_runtime
        return self.functions_runtime

    def release_functions_runtime(self, release):
        if self.runtime.role == CORE_HOST_ROLE:
            bound = current_process_runtime()
            if bound is None or bound.fingerprint.role != CORE_HOST_ROLE:
                raise RuntimeContractError("Core host has no admitted installation binding")
            compare_runtime_fingerprints(self.runtime, bound.fingerprint)
        reference = release.get("functions_installation")
        if reference is not None:
            selected = self.functions_runtime
            if (
                selected is None
                or runtime_installation_reference(selected) != reference
            ):
                selected = admitted_runtime_from_reference(
                    reference, code_root=self.code_root
                )
                self.functions_runtime = selected
        else:
            selected = self.selected_functions_runtime()
        if self.runtime.role == CORE_HOST_ROLE:
            if selected is None:
                raise RuntimeContractError("Missing qualified Functions installation")
            compare_runtime_boundary(self.runtime, selected.fingerprint,
                                     expected_functions=selected.fingerprint)
        expected = selected.fingerprint if selected is not None else self.runtime
        if release.get("runtime") is not None:
            compare_runtime_fingerprints(expected, RuntimeFingerprint.from_mapping(release["runtime"]))
        return selected, expected

    def bootstrap(
        self, release, handoff=None, *, recovery=False, restore=False, initial=False
    ):
        selected, functions_runtime = self.release_functions_runtime(release)
        terminal = None
        try:
            if sys.stdin.isatty():
                terminal = (
                    "CONIN$" if os.name == "nt" else os.ttyname(sys.stdin.fileno())
                )
        except (OSError, ValueError, AttributeError):
            pass
        return {
            **release,
            "code_root": str(self.code_root),
            "bridge_home": str(self.bridge_home),
            "runtime": functions_runtime.to_dict(),
            **({"core_runtime": self.runtime.to_dict()} if self.runtime.role == CORE_HOST_ROLE else {}),
            **({"functions_installation": runtime_installation_reference(selected)} if selected is not None else {}),
            "arguments": self.arguments,
            "handoff": handoff or {},
            "kernel_pid": os.getpid(),
            "process_group": True,
            "stdin_terminal": terminal,
            "recovery": recovery,
            "restore": restore,
            "initial": initial,
        }

    def publish(self, phase, **extra):
        try:
            write_record(
                self.state_dir / "kernel.json",
                {
                    "kernel_pid": os.getpid(),
                    "phase": phase,
                    "function_pid": self.active.process.pid if self.active else None,
                    "generation_id": self.release["manifest"]["generation_id"]
                    if self.release
                    else None,
                    "runtime": self.runtime.to_dict(),
                    "functions_runtime": self.release.get("runtime") if self.release else None,
                    "verification": {
                        "runtime": runtime_verification_stats(),
                        "generation": artifact_verification_stats(),
                    },
                    **extra,
                },
            )
        except OSError as exc:
            print(f"Kernel status write failed: {exc}", file=sys.stderr, flush=True)

    async def start(self, release=None, *, recovery=False):
        if release is None:
            release = await qualify(
                self.code_root, self.bridge_home, self.runtime,
                functions_runtime=self.selected_functions_runtime(),
            )
        elif release is self.release and self.release is not None:
            # Same Kernel, same committed in-memory receipt and
            # content-addressed root.  Internal recovery never rematerializes
            # it.  A privileged/local writer can still alter Windows bytes;
            # that residual risk is intentionally left to explicit audits.
            validate_artifact_identity(
                Path(release["generation_root"]), release["manifest"]
            )
        else:
            self.verify_supplied_release(release)
        client = await ProcessClient.prepare(
            self.bootstrap(release, recovery=recovery, initial=self.release is None)
        )
        try:
            await client.peer.request(
                "activate",
                timeout=86400 if client.ready_metadata.get("interactive") else 360,
            )
            await client.peer.request("commit", timeout=120)
        except BaseException:
            await client.close()
            raise
        self.active, self.release = client, release
        self.publish("active")

    async def replace(self, release=None, *, operation_id=None) -> bool:
        async with self._switch_lock:
            operation_id = str(operation_id or uuid4().hex)
            started = time.monotonic()
            stage_started = started
            stage = "generation_qualification"
            timings: dict[str, float] = {}
            old, previous = self.active, self.release
            candidate, handoff = None, None
            retired = False
            quiesced = False
            candidate_pid = None
            product_code_execution_began = False
            candidate_generation = None

            def advance(next_stage: str) -> None:
                nonlocal stage, stage_started
                timings[stage] = round((time.monotonic() - stage_started) * 1000, 3)
                stage = next_stage
                stage_started = time.monotonic()

            def safe_message(error: BaseException) -> str:
                from orchestrator.bootstrap_logging import redact_log_text

                return redact_log_text(
                    str(error).replace("\r", " ").replace("\n", " ")
                )[:1000]

            def outcome(**changes):
                value = {
                    "schema": 1,
                    "operation_id": operation_id,
                    "requested_scope": "shared_functions",
                    "candidate_generation": candidate_generation,
                    "candidate_pid": candidate_pid,
                    "product_code_execution_began": product_code_execution_began,
                    "stage_timings_ms": {
                        **timings,
                        "total": round((time.monotonic() - started) * 1000, 3),
                    },
                    **changes,
                }
                self.last_replacement_outcome = value
                return value

            try:
                if release is None:
                    release = await qualify(
                        self.code_root,
                        self.bridge_home,
                        self.runtime,
                        functions_runtime=self.selected_functions_runtime(),
                    )
                else:
                    candidate_generation = str(
                        release.get("manifest", {}).get("generation_id") or ""
                    ) or None
                    self.verify_supplied_release(release)
                candidate_generation = release["manifest"]["generation_id"]
                advance("candidate_prepare")
                candidate = await ProcessClient.prepare(self.bootstrap(release))
                product_code_execution_began = True
                candidate_pid = getattr(candidate.process, "pid", None)
                advance("handoff_quiesce")
                self.publish("preparing_handoff", operation_id=operation_id)
                # The child closes intake and drains. A rejected drain leaves
                # old authority intact, so it can resume with no publication.
                handoff = await old.peer.request("quiesce", timeout=180)
                quiesced = True
                advance("handoff_validation")
                validate_artifact_identity(
                    Path(release["generation_root"]), release["manifest"]
                )
                compare_runtime_fingerprints(
                    self.runtime, _observed_runtime(self.code_root, self.runtime)
                )
                advance("old_process_retirement")
                await old.close()
                retired = True
                advance("candidate_activation")
                await candidate.peer.request(
                    "activate", {"handoff": handoff}, timeout=360
                )
                timings[stage] = round((time.monotonic() - stage_started) * 1000, 3)
            except (Exception, asyncio.CancelledError) as exc:
                product_code_execution_began = bool(
                    getattr(
                        exc,
                        "hashi_product_code_execution_began",
                        product_code_execution_began,
                    )
                )
                candidate_pid = getattr(exc, "hashi_candidate_pid", candidate_pid)
                failure_stage = stage
                timings[stage] = round((time.monotonic() - stage_started) * 1000, 3)
                close_error = None
                if candidate is not None:
                    try:
                        await candidate.close()
                    except Exception as cleanup_error:
                        close_error = safe_message(cleanup_error)
                rollback = {"attempted": False, "succeeded": True, "mode": "not_required"}
                rollback_error = None
                if retired:
                    rollback = {"attempted": True, "succeeded": False, "mode": "restore_previous"}
                    rollback_started = time.monotonic()
                    restored = None
                    try:
                        restored = await ProcessClient.prepare(
                            self.bootstrap(previous, handoff, restore=True)
                        )
                        await restored.peer.request(
                            "activate", {"handoff": handoff or {}}, timeout=360
                        )
                        await restored.peer.request("commit", timeout=120)
                    except Exception as restore_error:
                        rollback_error = restore_error
                        if restored is not None:
                            try:
                                await restored.close()
                            except Exception:
                                pass
                    else:
                        self.active = restored
                        rollback["succeeded"] = True
                    timings["rollback"] = round(
                        (time.monotonic() - rollback_started) * 1000, 3
                    )
                elif quiesced and old is not None and old.process.is_alive():
                    rollback = {"attempted": True, "succeeded": False, "mode": "resume_previous"}
                    rollback_started = time.monotonic()
                    try:
                        await old.peer.request("resume", timeout=45)
                    except Exception as resume_error:
                        rollback_error = resume_error
                    else:
                        rollback["succeeded"] = True
                    timings["rollback"] = round(
                        (time.monotonic() - rollback_started) * 1000, 3
                    )
                self.release = previous
                result = outcome(
                    ok=False,
                    result="rolled_back" if rollback["succeeded"] else "rollback_failed",
                    failure_stage=failure_stage,
                    exception_type=type(exc).__name__,
                    reason_code=f"replacement_{failure_stage}_failed",
                    error=safe_message(exc),
                    candidate_cleanup_error=close_error,
                    rollback={
                        **rollback,
                        **(
                            {
                                "exception_type": type(rollback_error).__name__,
                                "error": safe_message(rollback_error),
                            }
                            if rollback_error is not None
                            else {}
                        ),
                    },
                )
                if rollback_error is not None:
                    self.active = None if retired else old
                    self.publish("failed", replacement_outcome=result)
                    raise RuntimeError(
                        "Function replacement and rollback failed"
                    ) from rollback_error
                self.publish("active", result="rolled_back", replacement_outcome=result)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return False
            # Publication is the commit point. A later notification/transport
            # fault cannot be reported as an all-or-none rollback.
            self.active, self.release = candidate, release
            post_commit_error = None
            commit_started = time.monotonic()
            try:
                await candidate.peer.request("commit", timeout=120)
            except Exception as exc:
                post_commit_error = {
                    "exception_type": type(exc).__name__,
                    "error": safe_message(exc),
                }
            timings["commit"] = round((time.monotonic() - commit_started) * 1000, 3)
            result = outcome(
                ok=True,
                result="committed",
                failure_stage=None,
                exception_type=None,
                reason_code=None,
                error=None,
                rollback={"attempted": False, "succeeded": True, "mode": "not_required"},
                post_commit_error=post_commit_error,
            )
            self.publish("active", result="committed", replacement_outcome=result)
            return True

    async def run(self):
        await self.start()
        recoveries = 0
        try:
            while not self.stop_event.is_set():
                for request_path in sorted(
                    (self.state_dir / "kernel-requests").glob("*.json")
                ):
                    try:
                        if request_path.stat().st_size > 4096:
                            raise ValueError("oversized process-control request")
                        request = json.loads(request_path.read_text(encoding="utf-8"))
                        request_id = request_path.stem
                        if (
                            request.get("id") != request_id
                            or len(request_id) != 32
                            or any(c not in "0123456789abcdef" for c in request_id)
                        ):
                            raise ValueError("invalid process-control request identity")
                    except (OSError, ValueError, AttributeError) as exc:
                        self.publish("active", request_error=str(exc))
                    else:
                        request_path.unlink()
                        try:
                            ok = await self.replace(operation_id=request_id)
                        except BaseException:
                            try:
                                write_record(
                                    self.state_dir
                                    / ("replacement-" + request_id + ".json"),
                                    {
                                        "ok": False,
                                        "generation_id": (
                                            self.release["manifest"]["generation_id"]
                                            if self.release
                                            else None
                                        ),
                                        "outcome": self.last_replacement_outcome,
                                    },
                                )
                            except OSError as receipt_error:
                                self.publish("failed", receipt_error=str(receipt_error))
                            raise
                        try:
                            write_record(
                                self.state_dir
                                / ("replacement-" + request_id + ".json"),
                                {
                                    "ok": ok,
                                    "generation_id": self.release["manifest"][
                                        "generation_id"
                                    ],
                                    "outcome": self.last_replacement_outcome,
                                },
                            )
                        except OSError as exc:
                            self.publish("active", receipt_error=str(exc))
                    finally:
                        request_path.unlink(missing_ok=True)
                if self.active and self.active.requested_exit:
                    break
                if not self.active or not self.active.process.is_alive():
                    if recoveries >= 3:
                        raise RuntimeError("Function process recovery exhausted")
                    recoveries += 1
                    if self.active:
                        await self.active.close()
                    # Recovery is pinned to the last committed artifact. It
                    # never adopts checkout edits or silently installs a release.
                    await self.start(self.release, recovery=True)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=0.5)
                except TimeoutError:
                    pass
        finally:
            if self.active:
                await self.active.close()
            self.publish("stopped")
# HASHI-Fingerprint: hfp_7470e3beed48486aa5bb508435d70826
