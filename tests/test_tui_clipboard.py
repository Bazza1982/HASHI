"""Exercise CF_UNICODETEXT bytes and allocation ownership without changing the clipboard."""
import ctypes
from types import SimpleNamespace

import pytest

from tui.clipboard import _copy_native_windows


@pytest.mark.parametrize('accepted', [True, False])
def test_windows_clipboard_preserves_utf16_and_transfers_ownership(monkeypatch, accepted):
    allocations = {}
    freed = []
    published = []
    closed = []

    def allocate(_flags, size):
        buffer = ctypes.create_string_buffer(size)
        address = ctypes.addressof(buffer)
        allocations[address] = buffer
        return address

    def free(address):
        freed.append(address)
        return 0

    def publish(format_id, address):
        assert format_id == 13
        published.append(allocations[address].raw)
        return address if accepted else 0

    def lock(address):
        return address

    def unlock(_address):
        return 1

    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(
        kernel32=SimpleNamespace(GlobalAlloc=allocate, GlobalLock=lock,
            GlobalUnlock=unlock, GlobalFree=free),
        user32=SimpleNamespace(OpenClipboard=lambda _: 1, EmptyClipboard=lambda: 1,
            SetClipboardData=publish, CloseClipboard=lambda: closed.append(True)),
    ), raising=False)
    text = 'HASHI 中文 🌸 𠮷 尾\n'
    assert _copy_native_windows(text) is accepted
    assert published == [text.encode('utf-16-le') + b'\x00\x00']
    assert len(freed) == (0 if accepted else 1)
    assert closed == [True]
