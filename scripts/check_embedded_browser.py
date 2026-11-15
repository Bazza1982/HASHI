"""Focused offline branch checks. Does not install or restart any live runtime."""
from pathlib import Path
import subprocess
import sys
root=Path(__file__).resolve().parent.parent
for name in ['tools/embedded_browser_bridge.py','tools/embedded_browser_pipe.py','tools/device_control_worker.py','tools/browser_extension_bridge.py','tools/registry.py','tools/schemas.py','orchestrator/capability_broker.py']:
    compile((root/name).read_text(encoding='utf-8'),str(root/name),'exec')
result=subprocess.run([sys.executable,'-B','-m','pytest','-q','tests/test_embedded_browser_bridge.py','tests/test_capability_broker.py','tests/test_device_control_worker.py','tests/test_tool_registry_device_capabilities.py'],cwd=root)
raise SystemExit(result.returncode)
