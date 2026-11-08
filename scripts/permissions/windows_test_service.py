"""Disposable SCM registration for native permission-service qualification only.

No service is created on import. Names are confined to the independent test
namespace. A revision-bearing journal records ownership; cleanup never adopts
an existing service or deletes a changed registration. Passwords stay in memory.
This is test infrastructure, not the production L2 installer.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import re
import subprocess
import time
from uuid import uuid4

from orchestrator.config_json import (
    ConfigConflictError, ConfigDurabilityError, new_config_json, write_config_json,
)
from scripts.permissions.windows_test_identity import CleanupUnverified


@dataclass(frozen=True)
class TestServiceSpec:
    __test__ = False
    name: str
    command: tuple[str, ...]
    account: str
    sid: str
    marker: str

    def __post_init__(self):
        if not re.fullmatch(r"Hashi(?:Safety|Runner)Test_[0-9a-f]{8,32}", self.name):
            raise ValueError("an independent permission test service name is required")
        if not re.fullmatch(r"\.\\HashiProbe_[0-9a-f]{8}", self.account):
            raise ValueError("a dedicated disposable local test account is required")
        if not re.fullmatch(r"S-1-5-21-(?:[0-9]+-){3}[0-9]+", self.sid):
            raise ValueError("a recorded local test account SID is required")
        if not self.command or not all(isinstance(item, str) and item and "\0" not in item for item in self.command):
            raise ValueError("an explicit test service command is required")
        if not self.marker.startswith("HASHI disposable SCM probe "):
            raise ValueError("a test ownership marker is required")

    @property
    def facts(self):
        return {"binary": subprocess.list2cmdline(self.command),
                "account": self.account, "sid": self.sid, "display": self.marker}


class DisposableTestService:
    def __init__(self, api, spec, password, journal):
        self.api, self.spec, self.password, self.journal = api, spec, password, journal
        self.record = None

    def _save(self, state):
        self.record["state"] = state
        write_config_json(self.journal, self.record)

    def _matches(self, observed):
        return observed == self.spec.facts

    def __enter__(self):
        self.record = new_config_json(self.journal, {
            "version": 1, "id": uuid4().hex, "name": self.spec.name,
            **self.spec.facts, "state": "intent",
        })
        write_config_json(self.journal, self.record)
        if self.api.query(self.spec.name) is not None:
            raise CleanupUnverified("test service name already exists")
        created = False
        try:
            self.api.create(self.spec, self.password)
            created = True
            self._save("created")
            if not self._matches(self.api.query(self.spec.name)):
                raise CleanupUnverified("created SCM registration identity is unverified")
            self.api.harden(self.spec.name)
            return self
        except (ConfigConflictError, ConfigDurabilityError) as error:
            raise CleanupUnverified("SCM journal publication needs reconciliation; registration retained") from error
        except BaseException:
            # A failed CreateService with an unknown outcome is not sufficient
            # ownership evidence. Keep the intent for operator reconciliation.
            if created:
                self.cleanup()
            raise
        finally:
            self.password = None

    def cleanup(self):
        observed = self.api.query(self.spec.name)
        if observed is None:
            self._save("cleaned")
            return
        if not self._matches(observed):
            raise CleanupUnverified("test SCM registration changed; refusing cleanup")
        self.api.stop(self.spec.name)
        # Re-read after waiting; do not delete an operator replacement.
        if not self._matches(self.api.query(self.spec.name)):
            raise CleanupUnverified("test SCM registration changed during stop")
        self.api.delete(self.spec.name)
        if self.api.query(self.spec.name) is not None:
            raise CleanupUnverified("test SCM deletion is unconfirmed")
        self._save("cleaned")

    def __exit__(self, *_args):
        self.cleanup()


class NativeTestServices:
    def __init__(self, accounts):
        import os
        if os.name != "nt":
            raise OSError("native Windows is required")
        self.accounts = accounts
        self.adv = ctypes.WinDLL("advapi32", use_last_error=True)

        class Config(ctypes.Structure):
            _fields_ = [
                ("kind", wintypes.DWORD), ("start", wintypes.DWORD), ("error", wintypes.DWORD),
                ("binary", wintypes.LPWSTR), ("group", wintypes.LPWSTR), ("tag", wintypes.DWORD),
                ("dependencies", wintypes.LPWSTR), ("account", wintypes.LPWSTR),
                ("display", wintypes.LPWSTR),
            ]

        class Status(ctypes.Structure):
            _fields_ = [(key, wintypes.DWORD) for key in (
                "kind", "state", "accepted", "win32", "specific", "checkpoint", "wait_hint", "pid", "flags",
            )]

        self.config_type, self.status_type = Config, Status
        signatures = {
            "OpenSCManagerW": ([wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD], wintypes.HANDLE),
            "OpenServiceW": ([wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD], wintypes.HANDLE),
            "CreateServiceW": ([wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                ctypes.c_void_p, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HANDLE),
            "CloseServiceHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "QueryServiceConfigW": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "QueryServiceStatusEx": ([wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p,
                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "ChangeServiceConfig2W": ([wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "SetServiceObjectSecurity": ([wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "ConvertStringSecurityDescriptorToSecurityDescriptorW": ([wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "StartServiceW": ([wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "ControlService": ([wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "DeleteService": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.adv, name)
            function.argtypes, function.restype = args, result

    @contextmanager
    def handle(self, name=None, access=1):
        manager = self.adv.OpenSCManagerW(None, None, 3 if name is None else 1)
        if not manager:
            raise ctypes.WinError(ctypes.get_last_error())
        service = None
        try:
            if name is not None:
                service = self.adv.OpenServiceW(manager, name, access)
                if not service:
                    raise ctypes.WinError(ctypes.get_last_error())
            yield service if name is not None else manager
        finally:
            if service:
                self.adv.CloseServiceHandle(service)
            self.adv.CloseServiceHandle(manager)

    def query(self, name):
        try:
            with self.handle(name, 1) as handle:
                size = wintypes.DWORD()
                self.adv.QueryServiceConfigW(handle, None, 0, ctypes.byref(size))
                if ctypes.get_last_error() != 122:
                    raise ctypes.WinError(ctypes.get_last_error())
                buffer = ctypes.create_string_buffer(size.value)
                if not self.adv.QueryServiceConfigW(handle, buffer, size, ctypes.byref(size)):
                    raise ctypes.WinError(ctypes.get_last_error())
                config = ctypes.cast(buffer, ctypes.POINTER(self.config_type)).contents
                account = config.account
                info = self.accounts.info(account.split("\\")[-1])
                return {"binary": config.binary, "account": account,
                        "display": config.display, "sid": info["sid"] if info else None}
        except OSError as error:
            if error.winerror == 1060:  # ERROR_SERVICE_DOES_NOT_EXIST
                return None
            raise

    def create(self, spec, password):
        with self.handle() as manager:
            service = self.adv.CreateServiceW(
                manager, spec.name, spec.marker, 0xF01FF, 0x10, 3, 1,
                spec.facts["binary"], None, None, None, spec.account, password,
            )
            if not service:
                raise ctypes.WinError(ctypes.get_last_error())
            self.adv.CloseServiceHandle(service)

    def harden(self, name):
        # Only SYSTEM and Administrators may control/reconfigure/delete the
        # registration. The new ordinary service account receives no SCM ACE.
        with self.handle(name, 2 | 0x40000) as handle:
            sid_type = wintypes.DWORD(1)  # SERVICE_SID_TYPE_UNRESTRICTED
            if not self.adv.ChangeServiceConfig2W(handle, 5, ctypes.byref(sid_type)):
                raise ctypes.WinError(ctypes.get_last_error())
            privileges = ctypes.create_unicode_buffer("SeChangeNotifyPrivilege\0")
            class RequiredPrivileges(ctypes.Structure):
                _fields_ = [("value", wintypes.LPWSTR)]
            required = RequiredPrivileges(ctypes.cast(privileges, wintypes.LPWSTR))
            if not self.adv.ChangeServiceConfig2W(handle, 6, ctypes.byref(required)):
                raise ctypes.WinError(ctypes.get_last_error())
            descriptor, size = ctypes.c_void_p(), wintypes.DWORD()
            if not self.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    "D:(A;;GA;;;SY)(A;;GA;;;BA)", 1, ctypes.byref(descriptor), ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self.adv.SetServiceObjectSecurity(handle, 4, descriptor):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.LocalFree.argtypes = [ctypes.c_void_p]
                kernel.LocalFree(descriptor)

    def status(self, name):
        with self.handle(name, 4) as handle:
            status, size = self.status_type(), wintypes.DWORD()
            if not self.adv.QueryServiceStatusEx(handle, 0, ctypes.byref(status), ctypes.sizeof(status), ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            return {key: getattr(status, key) for key in (
                "state", "win32", "specific", "checkpoint", "wait_hint", "pid",
            )}

    def start(self, name, *, timeout=20):
        with self.handle(name, 16) as handle:
            if not self.adv.StartServiceW(handle, 0, None):
                raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        while True:
            state = self.status(name)
            if state["state"] in (1, 4):
                return state
            if time.monotonic() >= deadline:
                raise TimeoutError("SCM startup remained pending")
            time.sleep(.05)

    def stop(self, name, *, timeout=20):
        state = self.status(name)
        if state["state"] == 1:
            return state
        if state["state"] != 3:
            with self.handle(name, 32) as handle:
                status = self.status_type()
                if not self.adv.ControlService(handle, 1, ctypes.byref(status)):
                    raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + timeout
        while True:
            state = self.status(name)
            if state["state"] == 1:
                return state
            if time.monotonic() >= deadline:
                raise TimeoutError("SCM shutdown remained pending")
            time.sleep(.05)

    def delete(self, name):
        with self.handle(name, 0x10000) as handle:
            if not self.adv.DeleteService(handle):
                raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + 10
        while True:
            try:
                if self.query(name) is None:
                    return
            except OSError as error:
                if error.winerror != 1072:  # Marked for deletion; process may still be exiting.
                    raise
            if time.monotonic() >= deadline:
                raise CleanupUnverified("SCM deletion remained pending")
            time.sleep(.05)


class NativeServiceLogonRights:
    """LSA account-right operations, confined by the caller to a new test SID."""
    def __init__(self):
        import os
        if os.name != "nt":
            raise OSError("native Windows is required")
        self.adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class UnicodeString(ctypes.Structure):
            _fields_ = [("length", wintypes.USHORT), ("maximum", wintypes.USHORT), ("buffer", ctypes.c_void_p)]

        class Attributes(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.ULONG), ("root", wintypes.HANDLE), ("name", ctypes.c_void_p),
                ("attributes", wintypes.ULONG), ("security", ctypes.c_void_p), ("quality", ctypes.c_void_p),
            ]

        self.string_type, self.attributes_type = UnicodeString, Attributes
        signatures = {
            "LsaOpenPolicy": ([ctypes.c_void_p, ctypes.POINTER(Attributes), wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)], wintypes.LONG),
            "LsaEnumerateAccountRights": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)], wintypes.LONG),
            "LsaAddAccountRights": ([wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(UnicodeString), wintypes.ULONG], wintypes.LONG),
            "LsaRemoveAccountRights": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.BOOL, ctypes.POINTER(UnicodeString), wintypes.ULONG], wintypes.LONG),
            "LsaNtStatusToWinError": ([wintypes.LONG], wintypes.ULONG),
            "LsaClose": ([wintypes.HANDLE], wintypes.LONG),
            "LsaFreeMemory": ([ctypes.c_void_p], wintypes.LONG),
            "ConvertStringSidToSidW": ([wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.adv, name)
            function.argtypes, function.restype = args, result
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]

    @contextmanager
    def account(self, sid):
        policy, native_sid = wintypes.HANDLE(), ctypes.c_void_p()
        attributes = self.attributes_type()
        attributes.length = ctypes.sizeof(attributes)
        self.check(self.adv.LsaOpenPolicy(None, ctypes.byref(attributes), 0x810, ctypes.byref(policy)))
        try:
            if not self.adv.ConvertStringSidToSidW(sid, ctypes.byref(native_sid)):
                raise ctypes.WinError(ctypes.get_last_error())
            yield policy, native_sid
        finally:
            if native_sid:
                self.kernel.LocalFree(native_sid)
            self.adv.LsaClose(policy)

    def check(self, status):
        if status:
            raise ctypes.WinError(self.adv.LsaNtStatusToWinError(status))

    def list(self, sid):
        with self.account(sid) as (policy, native_sid):
            data, count = ctypes.c_void_p(), wintypes.ULONG()
            status = self.adv.LsaEnumerateAccountRights(policy, native_sid, ctypes.byref(data), ctypes.byref(count))
            if status & 0xFFFFFFFF == 0xC0000034:  # No policy object for this new account.
                return set()
            self.check(status)
            try:
                items = ctypes.cast(data, ctypes.POINTER(self.string_type))
                return {ctypes.wstring_at(items[i].buffer, items[i].length // 2) for i in range(count.value)}
            finally:
                self.adv.LsaFreeMemory(data)

    def change(self, sid, *, remove):
        with self.account(sid) as (policy, native_sid):
            value = ctypes.create_unicode_buffer("SeServiceLogonRight")
            right = self.string_type(len(value.value) * 2, ctypes.sizeof(value), ctypes.cast(value, ctypes.c_void_p))
            if remove:
                status = self.adv.LsaRemoveAccountRights(policy, native_sid, False, ctypes.byref(right), 1)
            else:
                status = self.adv.LsaAddAccountRights(policy, native_sid, ctypes.byref(right), 1)
            self.check(status)


@contextmanager
def disposable_service_logon(rights, *, sid):
    # Only for a freshly created, manifest-owned disposable account. Never
    # adopt/revoke a pre-existing right on a shared or production identity.
    if "SeServiceLogonRight" in rights.list(sid):
        raise CleanupUnverified("test account already has service-logon rights")
    attempted = False
    try:
        attempted = True
        rights.change(sid, remove=False)
        if "SeServiceLogonRight" not in rights.list(sid):
            raise CleanupUnverified("test service-logon grant is unconfirmed")
        yield
    finally:
        if attempted:
            if "SeServiceLogonRight" in rights.list(sid):
                rights.change(sid, remove=True)
            if "SeServiceLogonRight" in rights.list(sid):
                raise CleanupUnverified("test service-logon right cleanup is unconfirmed")
