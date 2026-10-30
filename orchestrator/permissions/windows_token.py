"""Native LPAC observation, including Windows builds rejecting token class 46.

The fallback reads the kernel-produced reserved WIN security attribute. Unknown
layouts and unsupported queries fail closed; requested creation flags never
stand in for an observed token.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes


class _Unicode(ctypes.Structure):
    _fields_ = [("length", wintypes.WORD), ("maximum", wintypes.WORD),
                ("buffer", ctypes.c_void_p)]


class _Attribute(ctypes.Structure):
    _fields_ = [("name", _Unicode), ("type", wintypes.WORD),
                ("reserved", wintypes.WORD), ("flags", wintypes.DWORD),
                ("count", wintypes.DWORD), ("values", ctypes.c_void_p)]


class _Information(ctypes.Structure):
    _fields_ = [("version", wintypes.WORD), ("reserved", wintypes.WORD),
                ("count", wintypes.DWORD), ("attributes", ctypes.c_void_p)]


def _reserved_lpac_attribute(buffer):
    start, length = ctypes.addressof(buffer), ctypes.sizeof(buffer)

    def bounded(address, size):
        if not address or size < 0 or address < start or address + size > start + length:
            raise OSError("token attribute points outside its native result")
        return address

    bounded(start, ctypes.sizeof(_Information))
    info = _Information.from_buffer(buffer)
    if info.version != 1 or info.reserved or info.count > 4096:
        raise OSError("unknown token security attribute layout")
    if not info.count:
        return False
    bounded(info.attributes, info.count * ctypes.sizeof(_Attribute))
    found = None
    for index in range(info.count):
        item = _Attribute.from_address(info.attributes + index * ctypes.sizeof(_Attribute))
        if item.name.length % 2 or item.name.length > item.name.maximum:
            raise OSError("invalid native token attribute name")
        bounded(item.name.buffer, item.name.length)
        name = ctypes.string_at(item.name.buffer, item.name.length).decode("utf-16-le")
        if name.casefold() != "WIN://NOALLAPPPKG".casefold():
            continue
        if (found is not None or item.type not in {2, 6} or item.reserved or item.count != 1
                or item.flags & 0x18):
            raise OSError(f"invalid reserved LPAC token attribute: type={item.type}, flags={item.flags}, count={item.count}, reserved={item.reserved}")
        bounded(item.values, 8)
        value = ctypes.c_ulonglong.from_address(item.values).value
        if value not in {0, 1}:
            raise OSError("invalid LPAC boolean")
        found = bool(value)
    return found is True


def token_is_lpac(token, security):
    value, returned = wintypes.DWORD(), wintypes.DWORD()
    if security.GetTokenInformation(token, 46, ctypes.byref(value),
                                    ctypes.sizeof(value), ctypes.byref(returned)):
        if returned.value != ctypes.sizeof(value) or value.value not in {0, 1}:
            raise OSError("invalid native LPAC result")
        return bool(value.value)
    error = ctypes.get_last_error()
    if error != 87:
        raise ctypes.WinError(error)
    size = wintypes.DWORD()
    security.GetTokenInformation(token, 39, None, 0, ctypes.byref(size))
    if ctypes.get_last_error() != 122 or not ctypes.sizeof(_Information) <= size.value <= 1024 * 1024:
        raise OSError("native LPAC observation is unavailable")
    buffer = ctypes.create_string_buffer(size.value)
    if not security.GetTokenInformation(token, 39, buffer, size.value, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _reserved_lpac_attribute(buffer)
