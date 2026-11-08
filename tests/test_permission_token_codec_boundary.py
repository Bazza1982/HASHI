"""A fresh identification-token reader must not import a codec from disk."""
from pathlib import Path
import subprocess
import sys


def test_reserved_attribute_decode_needs_no_filesystem_codec_lookup():
    root = str(Path(__file__).resolve().parents[1])
    program = """
import codecs, ctypes, encodings, sys
sys.path.insert(0, ROOT)
from orchestrator.permissions.windows_token import _Attribute, _Information, _reserved_lpac_attribute
buffer=ctypes.create_string_buffer(1024)
base=ctypes.addressof(buffer)
info=_Information.from_buffer(buffer)
info.version,info.count,info.attributes=1,1,base+64
item=_Attribute.from_buffer(buffer,64)
text=b''.join(bytes((ord(c),0)) for c in 'WIN://NOALLAPPPKG')
item.name.length=item.name.maximum=len(text)
item.name.buffer=base+256
ctypes.memmove(item.name.buffer,text,len(text))
item.type,item.count,item.values=2,1,base+512
ctypes.c_ulonglong.from_buffer(buffer,512).value=1
codecs.unregister(encodings.search_function)
codecs.register(encodings.search_function)
encodings._cache.pop('utf_16_le',None)
sys.modules.pop('encodings.utf_16_le',None)
class NoCodecFiles:
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='encodings.utf_16_le':
            raise PermissionError('identification token cannot read module files')
sys.meta_path.insert(0,NoCodecFiles())
assert _reserved_lpac_attribute(buffer) is True
print('reserved LPAC read without imports')
""".replace("ROOT", repr(root))
    result = subprocess.run([sys.executable, "-I", "-S", "-c", program],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "reserved LPAC read without imports"
# HASHI-Fingerprint: hfp_188b013ec3c14f0d9c62076cd76be106
