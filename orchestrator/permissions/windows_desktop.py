"""Task-private noninteractive desktops for the sealed Windows LPAC runtime.

USER32 is a dependency of the qualified Python native libraries. Session-0
services must explicitly admit the lowbox to their noninteractive station.
Only READ_CONTROL/ACCESSGLOBALATOMS and private desktop READ/WRITEOBJECTS
are required (native minimum-rights qualification); no interactive desktop,
clipboard, window creation, hooks, switching, ACL writing or ownership grant.
"""
from __future__ import annotations

import threading
from uuid import uuid4

STATION_RUNTIME_ACCESS = 0x00020020
DESKTOP_RUNTIME_ACCESS = 0x00000081


class DesktopCleanupFailed(OSError):
    def __init__(self, desktop):
        self.desktop = desktop
        super().__init__("private desktop cleanup remains unverified")


class _DesktopLease:
    def __init__(self, owner, sid):
        self._owner, self.sid = owner, sid
        self.desktop_name = "HashiTask_" + uuid4().hex
        self.name = owner.station_name + "\\" + self.desktop_name
        self._handle = None
        self._closed = False

    def close(self):
        self._owner.release(self)


class DesktopAclCoordinator:
    """Own exact station-ACL leases; preserve independent changes on conflict."""

    def __init__(self, api):
        self.api = api
        description = api.describe()
        if (description.get("session_id") != 0 or description.get("visible") is not False
                or not str(description.get("name", "")).lower().startswith("service-")):
            raise PermissionError("only the current noninteractive service station is supported")
        self.station_name = description["name"]
        self._description = description
        self._lock = threading.RLock()
        self._baseline = self._expected = None
        self._leases = {}

    def _check(self):
        if self.api.describe() != self._description:
            raise OSError("the owning service station changed")
        if self._expected is not None and self.api.read_security() != self._expected:
            raise OSError("the service station ACL changed; reconciliation required")

    def _target(self, excluded=None):
        value = self._baseline
        for sid, lease in self._leases.items():
            if lease is not excluded:
                value = self.api.with_station_read(value, sid)
        return value

    def _write(self, value):
        self._check()
        observed = self.api.write_security(value)
        if not isinstance(observed, bytes):
            raise OSError("station ACL persistence is unverified")
        self._expected = observed

    def open(self, sid):
        parts = sid.split("-") if isinstance(sid, str) else []
        if (len(parts) != 11 or parts[:4] != ["S", "1", "15", "2"]
                or any(not part.isascii() or not part.isdigit() or int(part) > 0xFFFFFFFF
                       for part in parts[4:])):
            raise ValueError("an exact task AppContainer SID is required")
        with self._lock:
            self._check()
            if sid in self._leases:
                raise PermissionError("the task desktop lease already exists")
            if not self._leases:
                self._baseline = self._expected = self.api.read_security()
            lease = _DesktopLease(self, sid)
            self._leases[sid] = lease
            try:
                self._write(self._target())
                lease._handle = self.api.create_desktop(lease.desktop_name, sid)
                if lease._handle is None:
                    raise OSError("private desktop creation is unverified")
                return lease
            except BaseException:
                try:
                    lease.close()
                except BaseException as cleanup:
                    raise DesktopCleanupFailed(lease) from cleanup
                raise

    def release(self, lease):
        with self._lock:
            if lease._closed:
                return
            if self._leases.get(lease.sid) is not lease:
                raise OSError("private desktop ownership changed")
            if lease._handle is not None:
                self.api.close_desktop(lease._handle)
                lease._handle = None
            self._write(self._target(excluded=lease))
            if len(self._leases) == 1 and self._expected != self._baseline:
                raise OSError("original station ACL restoration is unverified")
            self._leases.pop(lease.sid)
            lease._closed = True
            if not self._leases:
                self._baseline = self._expected = None


class _NativeDesktopAPI:
    def __init__(self, expected_user_sid):
        import ctypes
        from ctypes import wintypes as W
        from orchestrator.permissions.windows_ipc import current_process_user_sid

        if current_process_user_sid() != expected_user_sid:
            raise PermissionError("the desktop owner differs from the verified service")
        self.ctypes, self.W, self.owner_sid = ctypes, W, expected_user_sid
        P = ctypes.c_void_p
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        class SA(ctypes.Structure):
            _fields_ = [("size", W.DWORD), ("descriptor", P), ("inherit", W.BOOL)]

        class Trustee(ctypes.Structure):
            _fields_ = [("multiple", P), ("operation", W.DWORD), ("form", W.DWORD),
                        ("kind", W.DWORD), ("sid", P)]

        class EA(ctypes.Structure):
            _fields_ = [("permissions", W.DWORD), ("mode", W.DWORD),
                        ("inheritance", W.DWORD), ("trustee", Trustee)]

        self.SA, self.Trustee, self.EA = SA, Trustee, EA
        for dll, name, args, result in (
            (self.user, "GetProcessWindowStation", [], W.HANDLE),
            (self.user, "GetUserObjectInformationW", [W.HANDLE, ctypes.c_int, P, W.DWORD, ctypes.POINTER(W.DWORD)], W.BOOL),
            (self.user, "GetUserObjectSecurity", [W.HANDLE, ctypes.POINTER(W.DWORD), P, W.DWORD, ctypes.POINTER(W.DWORD)], W.BOOL),
            (self.user, "SetUserObjectSecurity", [W.HANDLE, ctypes.POINTER(W.DWORD), P], W.BOOL),
            (self.user, "CreateDesktopW", [W.LPCWSTR, W.LPCWSTR, P, W.DWORD, W.DWORD, ctypes.POINTER(SA)], W.HANDLE),
            (self.user, "CloseDesktop", [W.HANDLE], W.BOOL),
            (self.adv, "ConvertStringSidToSidW", [W.LPCWSTR, ctypes.POINTER(P)], W.BOOL),
            (self.adv, "GetLengthSid", [P], W.DWORD),
            (self.adv, "GetSecurityDescriptorDacl", [P, ctypes.POINTER(W.BOOL), ctypes.POINTER(P), ctypes.POINTER(W.BOOL)], W.BOOL),
            (self.adv, "GetSecurityDescriptorControl", [P, ctypes.POINTER(W.WORD), ctypes.POINTER(W.DWORD)], W.BOOL),
            (self.adv, "SetSecurityDescriptorControl", [P, W.WORD, W.WORD], W.BOOL),
            (self.adv, "SetEntriesInAclW", [W.ULONG, ctypes.POINTER(EA), P, ctypes.POINTER(P)], W.DWORD),
            (self.adv, "InitializeSecurityDescriptor", [P, W.DWORD], W.BOOL),
            (self.adv, "SetSecurityDescriptorDacl", [P, W.BOOL, P, W.BOOL], W.BOOL),
            (self.adv, "MakeSelfRelativeSD", [P, P, ctypes.POINTER(W.DWORD)], W.BOOL),
            (self.adv, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [W.LPCWSTR, W.DWORD, ctypes.POINTER(P), P], W.BOOL),
            (self.kernel, "GetCurrentProcessId", [], W.DWORD),
            (self.kernel, "ProcessIdToSessionId", [W.DWORD, ctypes.POINTER(W.DWORD)], W.BOOL),
            (self.kernel, "LocalFree", [P], P),
        ):
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result
        self.station = self.user.GetProcessWindowStation()  # Borrowed, never close.
        if not self.station:
            raise ctypes.WinError(ctypes.get_last_error())

    def _checked(self, result):
        if not result:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return result

    def describe(self):
        c, W = self.ctypes, self.W
        if self.user.GetProcessWindowStation() != self.station:
            raise OSError("the owning service station changed")
        name, size, flags, session = c.create_unicode_buffer(512), W.DWORD(), (W.DWORD * 3)(), W.DWORD()
        self._checked(self.user.GetUserObjectInformationW(self.station, 2, name, c.sizeof(name), c.byref(size)))
        self._checked(self.user.GetUserObjectInformationW(self.station, 1, flags, c.sizeof(flags), c.byref(size)))
        self._checked(self.kernel.ProcessIdToSessionId(self.kernel.GetCurrentProcessId(), c.byref(session)))
        return {"name": name.value, "visible": bool(flags[2] & 1), "session_id": session.value}

    def read_security(self):
        c, W = self.ctypes, self.W
        size, security = W.DWORD(), W.DWORD(4)
        self.user.GetUserObjectSecurity(self.station, c.byref(security), None, 0, c.byref(size))
        if not 20 <= size.value <= 1024 * 1024:
            raise OSError("the service station security descriptor is unavailable")
        value = c.create_string_buffer(size.value)
        self._checked(self.user.GetUserObjectSecurity(self.station, c.byref(security), value,
                                                     size.value, c.byref(size)))
        return value.raw[:size.value]

    def _acl(self, value):
        c, W = self.ctypes, self.W
        buffer = c.create_string_buffer(value)
        present, defaulted, acl = W.BOOL(), W.BOOL(), c.c_void_p()
        self._checked(self.adv.GetSecurityDescriptorDacl(buffer, c.byref(present), c.byref(acl), c.byref(defaulted)))
        if not present.value or not acl.value:
            raise PermissionError("an explicit non-null service station DACL is required")
        size = c.c_ushort.from_address(acl.value + 2).value
        if not 8 <= size <= len(value):
            raise OSError("the service station ACL bounds are invalid")
        return buffer, acl, c.string_at(acl, size)

    def with_station_read(self, original, sid_text):
        c, W = self.ctypes, self.W
        old, acl, acl_bytes = self._acl(original)
        sid, updated = c.c_void_p(), c.c_void_p()
        self._checked(self.adv.ConvertStringSidToSidW(sid_text, c.byref(sid)))
        try:
            length = self.adv.GetLengthSid(sid)
            if not length or c.string_at(sid, length) in acl_bytes:
                raise PermissionError("a pre-existing task station ACE cannot be adopted")
            entry = self.EA(STATION_RUNTIME_ACCESS, 1, 0, self.Trustee(None, 0, 0, 0, sid))
            status = self.adv.SetEntriesInAclW(1, c.byref(entry), acl, c.byref(updated))
            if status:
                raise OSError(status, "cannot prepare the task station ACL")
            descriptor = c.create_string_buffer(64)
            self._checked(self.adv.InitializeSecurityDescriptor(descriptor, 1))
            self._checked(self.adv.SetSecurityDescriptorDacl(descriptor, True, updated, False))
            control, revision = W.WORD(), W.DWORD()
            self._checked(self.adv.GetSecurityDescriptorControl(old, c.byref(control), c.byref(revision)))
            self._checked(self.adv.SetSecurityDescriptorControl(descriptor, 0x1500, control.value & 0x1500))
            size = W.DWORD()
            self.adv.MakeSelfRelativeSD(descriptor, None, c.byref(size))
            if not 20 <= size.value <= 1024 * 1024:
                raise OSError("cannot size the task station descriptor")
            value = c.create_string_buffer(size.value)
            self._checked(self.adv.MakeSelfRelativeSD(descriptor, value, c.byref(size)))
            return value.raw[:size.value]
        finally:
            self.kernel.LocalFree(sid)
            if updated:
                self.kernel.LocalFree(updated)

    def write_security(self, value):
        c, W = self.ctypes, self.W
        _, _, expected_acl = self._acl(value)
        security, descriptor = W.DWORD(4), c.create_string_buffer(value)
        self._checked(self.user.SetUserObjectSecurity(self.station, c.byref(security), descriptor))
        observed = self.read_security()
        _, _, actual_acl = self._acl(observed)
        if actual_acl != expected_acl:
            raise OSError("service station ACL persistence is unverified")
        return observed

    def create_desktop(self, name, sid):
        c = self.ctypes
        descriptor = c.c_void_p()
        sddl = (f"D:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;{self.owner_sid})"
                f"(A;;0x{DESKTOP_RUNTIME_ACCESS:08x};;;{sid})")
        self._checked(self.adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None))
        try:
            attributes = self.SA(c.sizeof(self.SA), descriptor, False)
            return self._checked(self.user.CreateDesktopW(name, None, None, 0, 0x000F01FF, c.byref(attributes)))
        finally:
            self.kernel.LocalFree(descriptor)

    def close_desktop(self, handle):
        self._checked(self.user.CloseDesktop(handle))


_COORDINATORS = {}
_COORDINATORS_LOCK = threading.RLock()


def open_runtime_desktop(*, expected_user_sid, appcontainer_sid):
    with _COORDINATORS_LOCK:
        api = _NativeDesktopAPI(expected_user_sid)
        key = (api.station, expected_user_sid)
        owner = _COORDINATORS.get(key)
        if owner is None:
            owner = DesktopAclCoordinator(api)
            _COORDINATORS[key] = owner
        return owner.open(appcontainer_sid)
# HASHI-Fingerprint: hfp_3d709d39daa7474da20647687c20e989
