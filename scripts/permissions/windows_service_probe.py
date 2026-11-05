"""Opt-in native SCM component qualification using independent test identities.

The payload tests lifecycle and actual SCM/identity boundaries, not a complete
installed Safety/Runner or an L2 approval/execution chain. It uses the complete
pinned runtime capsule. No production registration is read or changed.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

from orchestrator.config_json import new_config_json, write_config_json
from orchestrator.permissions.capsule import qualify_capsule
from orchestrator.permissions.windows_ipc import current_process_user_sid
from orchestrator.permissions.windows_security import require_nonadmin_identity
from orchestrator.permissions.windows_service import RUNNING, STOPPED, ServiceHost
from scripts.permissions.windows_test_identity import (
    DisposableTestIdentity, WindowsAccounts, CleanupUnverified, quarantine_identity,
)
from scripts.permissions.windows_test_profile import DisposableWindowsProfile, WindowsProfiles
from scripts.permissions.windows_test_service import (
    DisposableTestService, NativeServiceLogonRights, NativeTestServices,
    TestServiceSpec, disposable_service_logon,
)


def _acl(path, grants):
    command = ["icacls.exe", str(path), "/inheritance:r", "/grant:r",
               "*S-1-5-18:(OI)(CI)(F)", "*S-1-5-32-544:(OI)(CI)(F)", *grants]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise OSError("disposable SCM probe directory ACL setup failed")


def _service_control_denials(name):
    services = NativeTestServices(WindowsAccounts())
    denied = {}
    for label, access in (("change_config", 2), ("start", 16), ("stop", 32), ("delete", 0x10000)):
        try:
            with services.handle(name, access):
                denied[label] = False
        except OSError as error:
            if error.winerror != 5:
                raise
            denied[label] = True
    if not all(denied.values()):
        raise PermissionError("ordinary service account could manage its SCM registration")
    return denied


def _service_sid_present(name):
    accounts = WindowsAccounts()
    service_sid = accounts._sid("NT SERVICE\\" + name)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertStringSidToSidW.restype = wintypes.BOOL
    adv.CheckTokenMembership.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]
    adv.CheckTokenMembership.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    native, present = ctypes.c_void_p(), wintypes.BOOL()
    if not adv.ConvertStringSidToSidW(service_sid, ctypes.byref(native)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not adv.CheckTokenMembership(None, native, ctypes.byref(present)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not present.value:
            raise PermissionError("SCM service SID is missing from actual token")
        return service_sid
    finally:
        kernel.LocalFree(native)


def service_entry(arguments):
    async def payload(_path, *, stopped, on_ready):
        sid = current_process_user_sid()
        require_nonadmin_identity(arguments.sid)
        record = new_config_json(arguments.record, {
            "version": 1, "component_only": True, "sid": sid,
            "service_sid": _service_sid_present(arguments.name),
            "control_denied": _service_control_denials(arguments.name),
            "stage": "starting", "pid": os.getpid(),
        })
        write_config_json(arguments.record, record)
        if arguments.scenario == "startup_failure":
            raise OSError("qualification startup failure")
        on_ready()
        record["stage"] = "ready"
        write_config_json(arguments.record, record)
        await stopped.wait()
        record["stage"] = "cleanup_failed" if arguments.scenario == "cleanup_failure" else "cleanup_complete"
        write_config_json(arguments.record, record)
        if arguments.scenario == "cleanup_failure":
            raise OSError("qualification cleanup failure")

    ServiceHost(arguments.name, payload, arguments.record).run()


def run_probe(arguments):
    output = arguments.output
    if not output.is_absolute() or output.exists():
        raise ValueError("a new absolute qualification output directory is required")
    output.mkdir()
    _acl(output, [])
    capsule = qualify_capsule(arguments.capsule_manifest, arguments.capsule_sha256, trusted_sids=())
    accounts, profiles, rights = WindowsAccounts(), WindowsProfiles(), NativeServiceLogonRights()
    services = NativeTestServices(accounts)
    owners, registrations, results, cleanup_errors = [], [], [], []
    failure = None
    try:
        for role in ("safety", "runner"):
            identity = DisposableTestIdentity(accounts, output / (role + "-account.json"))
            identity.__enter__()
            owner = {"role": role, "identity": identity, "profile": None, "logon": None}
            owners.append(owner)
            profile = DisposableWindowsProfile(accounts, identity, profiles, output / (role + "-profile.json"))
            profile.__enter__()
            owner["profile"] = profile
            # Durable account ownership predates the only new LSA right.
            logon = disposable_service_logon(rights, sid=identity.sid)
            logon.__enter__()
            owner["logon"] = logon
        assert owners[0]["identity"].sid != owners[1]["identity"].sid
        # New root only: allow these two fresh identities to traverse it.
        _acl(output, ["*" + item["identity"].sid + ":(RX)" for item in owners])
        for role, scenario in (("safety", "normal"), ("runner", "normal"),
                               ("runner", "startup_failure"), ("runner", "cleanup_failure")):
            owner = next(item for item in owners if item["role"] == role)
            identity = owner["identity"]
            identifier = uuid4().hex
            name = "Hashi" + role.title() + "Test_" + identifier[:12]
            private = output / identifier
            private.mkdir()
            _acl(private, ["*" + identity.sid + ":(OI)(CI)(F)"])
            record = private / "runtime.json"
            command = (str(capsule.executable), "-I", "-B", "-m",
                       "scripts.permissions.windows_service_probe", "service",
                       "--name", name, "--sid", identity.sid, "--record", str(record),
                       "--scenario", scenario)
            spec = TestServiceSpec(name, command, ".\\" + identity.name, identity.sid,
                                   "HASHI disposable SCM probe " + identifier)
            registration = DisposableTestService(services, spec, identity.password, output / (identifier + "-service.json"))
            registrations.append(registration)
            registration.__enter__()
            try:
                owner["profile"].before_logon(identity.sid)
                try:
                    started = services.start(name)
                except OSError as error:
                    if scenario != "startup_failure" or error.winerror not in (1066, 1067):
                        raise
                    started = services.status(name)
                if scenario == "startup_failure":
                    assert started["state"] == STOPPED and started["win32"] != 0
                    final = started
                else:
                    assert started["state"] == RUNNING and started["pid"] > 0
                    final = services.stop(name)
                    assert final["state"] == STOPPED
                    assert (final["win32"] != 0) == (scenario == "cleanup_failure")
                observed = json.loads(record.read_text(encoding="utf-8"))
                assert observed["sid"] == identity.sid and all(observed["control_denied"].values())
                assert observed["stage"] == {
                    "normal": "cleanup_complete", "startup_failure": "starting",
                    "cleanup_failure": "cleanup_failed",
                }[scenario]
                results.append({"role": role, "scenario": scenario, "service": name,
                                "startup": started, "final": final, "runtime": observed})
            finally:
                registration.__exit__(None, None, None)
    except BaseException as error:
        failure = type(error).__name__ + ": " + str(error)
    finally:
        for owner in reversed(owners):
            identity = owner["identity"]
            try:
                # Do not delete an account/profile still referenced by an
                # unverified registration, even if a previous cleanup failed.
                relevant = [item for item in registrations if item.spec.sid == identity.sid]
                if any(services.query(item.spec.name) is not None for item in relevant):
                    raise CleanupUnverified("owned service is still present; account retained")
                if owner["logon"] is not None:
                    owner["logon"].__exit__(None, None, None)
                if owner["profile"] is not None:
                    owner["profile"].__exit__(None, None, None)
                identity.__exit__(None, None, None)
                assert accounts.info(identity.name) is None
                assert "SeServiceLogonRight" not in rights.list(identity.sid)
            except BaseException as error:
                retained = {"name": identity.name, "sid": identity.sid,
                            "error": type(error).__name__ + ": " + str(error)}
                try:
                    retained["quarantine"] = quarantine_identity(
                        accounts, output / (owner["role"] + "-account.json"),
                    )
                except BaseException as quarantine_error:
                    retained["quarantine_error"] = type(quarantine_error).__name__ + ": " + str(quarantine_error)
                cleanup_errors.append(retained)
        report = {
            "version": 1, "component_only": True, "source_commit": capsule.manifest["source_commit"],
            "capsule_sha256": capsule.digest,
            "host_sha256": capsule.manifest["files"]["app/orchestrator/permissions/windows_service.py"]["sha256"],
            "results": results,
            "failure": failure, "cleanup_errors": cleanup_errors,
            "pass": failure is None and not cleanup_errors and len(results) == 4,
        }
        write_config_json(output / "report.json", new_config_json(output / "report.json", report))
        print(json.dumps({"pass": report["pass"], "cases": len(results),
                          "cleanup_errors": len(cleanup_errors), "failure": failure}, ensure_ascii=False))
    if not report["pass"]:
        raise RuntimeError("native SCM component qualification did not pass")


def recover_probe(output, *, accounts=None, profiles=None, rights=None, services=None):
    """Finish recorded cleanup only after an operator's Windows maintenance.

    The original qualification receipt remains immutable. This command never
    reboots, unloads a hive, creates an account or stops an unexpected service.
    A retained loaded profile remains a failure, not an automatic retry.
    """
    from orchestrator.config_json import read_config_json
    from scripts.permissions.windows_test_identity import _validate_manifest, recover_identity
    from scripts.permissions.windows_test_profile import recover_profile

    output = Path(output)
    if (not output.is_absolute() or not output.is_dir()
            or any(part.is_symlink() or getattr(part, "is_junction", lambda: False)()
                   for part in (output, *output.parents))):
        raise CleanupUnverified("an existing ordinary qualification directory is required")
    original_path = output / "report.json"
    original = read_config_json(original_path)
    digest = original.revision
    if (original.get("version") != 1 or original.get("component_only") is not True
            or type(original.get("pass")) is not bool or not isinstance(original.get("results"), list)):
        raise CleanupUnverified("invalid original SCM qualification receipt")
    accounts = accounts if accounts is not None else WindowsAccounts()
    profiles = profiles if profiles is not None else WindowsProfiles()
    rights = rights if rights is not None else NativeServiceLogonRights()
    services = services if services is not None else NativeTestServices(accounts)
    owners = {}
    for role in ("safety", "runner"):
        value = _validate_manifest(dict(read_config_json(output / (role + "-account.json"))))
        if value["sid"] is None or value["state"] not in {"created", "cleaned"}:
            raise CleanupUnverified("recorded test account ownership is incomplete")
        info = accounts.info(value["name"])
        if info is not None and (value["state"] != "created" or info.get("sid") != value["sid"]
                                or info.get("comment") != value["marker"] or not accounts.disabled(value["name"])):
            raise CleanupUnverified("retained account ownership or disabled state changed")
        if info is None and value["state"] != "cleaned":
            raise CleanupUnverified("retained account is missing; reconcile first")
        if profiles.loaded(value["sid"]):
            raise CleanupUnverified("the owned profile is still loaded")
        if rights.list(value["sid"]):
            raise CleanupUnverified("retained account still has logon rights; reconcile first")
        owners[role] = value
    if owners["safety"]["sid"] == owners["runner"]["sid"]:
        raise CleanupUnverified("independent test account identities are required")

    journals = {}
    for path in output.glob("*-service.json"):
        journal = read_config_json(path)
        if set(journal) != {"version", "id", "name", "binary", "account", "sid", "display", "state"}:
            raise CleanupUnverified("invalid recorded service ownership")
        # Reuse the namespace/identity validator; no command is executed.
        TestServiceSpec(journal["name"], (journal["binary"],), journal["account"], journal["sid"], journal["display"])
        if journal["name"] in journals:
            raise CleanupUnverified("duplicate recorded service identity")
        journals[journal["name"]] = journal
    if not original["results"]:
        raise CleanupUnverified("no recorded SCM result can be reconciled")
    for case in original["results"]:
        role, name = case.get("role"), case.get("service")
        journal = journals.get(name)
        if (role not in owners or journal is None or journal["sid"] != owners[role]["sid"]
                or journal["account"] != ".\\" + owners[role]["name"]):
            raise CleanupUnverified("SCM result does not match recorded account ownership")
        if services.query(name) is not None:
            raise CleanupUnverified("recorded service is still present; reconcile first")
    # Unknown journals cannot be silently ignored even when absent from results.
    if set(journals) != {case["service"] for case in original["results"]}:
        raise CleanupUnverified("unreconciled SCM registration journal remains")

    receipt_path = output / "recovery.json"
    if receipt_path.exists():
        receipt = read_config_json(receipt_path)
        if receipt.get("original_report_sha256") != digest:
            raise CleanupUnverified("the original SCM receipt changed")
    else:
        receipt = new_config_json(receipt_path, {
            "version": 1, "component_only": True, "source_commit": original.get("source_commit"),
            "original_report_sha256": digest, "original_pass": original["pass"],
            "cleanup_confirmed": False, "roles": [],
        })
    for role in ("runner", "safety"):
        recover_profile(accounts, profiles, output / (role + "-account.json"), output / (role + "-profile.json"))
        recover_identity(accounts, output / (role + "-account.json"))
        if role not in receipt["roles"]:
            receipt["roles"].append(role)
            write_config_json(receipt_path, receipt)
    receipt["cleanup_confirmed"] = True
    write_config_json(receipt_path, receipt)
    return dict(receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--capsule-manifest", type=Path, required=True)
    probe.add_argument("--capsule-sha256", required=True)
    probe.add_argument("--output", type=Path, required=True)
    recover = commands.add_parser("recover", help="finish owned cleanup after Windows has released profiles")
    recover.add_argument("--output", type=Path, required=True)
    service = commands.add_parser("service")
    service.add_argument("--name", required=True)
    service.add_argument("--sid", required=True)
    service.add_argument("--record", type=Path, required=True)
    service.add_argument("--scenario", choices=("normal", "startup_failure", "cleanup_failure"), required=True)
    arguments = parser.parse_args(argv)
    if arguments.command == "service":
        service_entry(arguments)
    elif arguments.command == "recover":
        print(json.dumps(recover_probe(arguments.output), ensure_ascii=False))
    else:
        run_probe(arguments)


if __name__ == "__main__":
    main()
# HASHI-Fingerprint: hfp_7864e5e5f75344a79a3c47c4c6a401c5
