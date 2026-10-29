"""LPAC token fallback checks OS-owned values and rejects malformed layouts."""
import ctypes

import pytest

from orchestrator.permissions.windows_token import _Attribute, _Information, _reserved_lpac_attribute


def fixture(*, value_type=2, value=1, flags=0):
    buffer = ctypes.create_string_buffer(1024)
    base = ctypes.addressof(buffer)
    info = _Information.from_buffer(buffer)
    info.version, info.count, info.attributes = 1, 1, base + 64
    item = _Attribute.from_buffer(buffer, 64)
    text = "WIN://NOALLAPPPKG".encode("utf-16-le")
    item.name.length = item.name.maximum = len(text)
    item.name.buffer = base + 256
    ctypes.memmove(item.name.buffer, text, len(text))
    item.type, item.count, item.flags, item.values = value_type, 1, flags, base + 512
    ctypes.c_ulonglong.from_buffer(buffer, 512).value = value
    return buffer


def test_kernel_uint64_and_boolean_lpac_attributes_are_observed():
    assert _reserved_lpac_attribute(fixture(value_type=2))
    assert _reserved_lpac_attribute(fixture(value_type=6))
    assert not _reserved_lpac_attribute(fixture(value=0))


@pytest.mark.parametrize("options", [{"value_type": 3}, {"value": 2}, {"flags": 16}])
def test_invalid_or_disabled_lpac_marker_never_grants(options):
    with pytest.raises(OSError):
        _reserved_lpac_attribute(fixture(**options))


def test_native_pointer_cannot_escape_returned_buffer():
    buffer = fixture()
    _Attribute.from_buffer(buffer, 64).values = 1
    with pytest.raises(OSError):
        _reserved_lpac_attribute(buffer)
