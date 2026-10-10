"""Read immutable Call previews. Selecting a voice never invokes a provider."""
import json
from pathlib import Path

from orchestrator import ui_language
from orchestrator.voice_preview_bundle import validate_voice_preview_bundle, VoicePreviewBundleError
from .voice_catalog import voice_label

PREVIEW_ROOT = Path(__file__).resolve().parent / "preview_assets"
PREVIEW_VERSION = "v1"
PREVIEW_TEXT = {
    "zh-CN": "您好，这是我的通话声音。我们可以用自然的语气，慢慢聊您感兴趣的事情。",
    "en": "Hello, this is my call voice. We can take our time and talk about what interests you.",
}


def get_call_preview(target, voice, *, locale=None, root=None, telegram=False):
    """Return a matching verified sample, never another model's approximation."""
    root = Path(root or PREVIEW_ROOT)
    try:
        manifest = json.loads((root / PREVIEW_VERSION / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            return ()
        if any(manifest.get(key) != target.get(key) for key in ("adapter", "base_url", "model")):
            return ()
        assets = validate_voice_preview_bundle(root=root, version=PREVIEW_VERSION,
                                              require_complete=False, allowed_formats=("ogg", "mp3"))
        renderer = "call" if telegram else "call_mp3"
        path = assets.get((locale or ui_language.current_locale(), voice, renderer))
    except (OSError, ValueError, TypeError, VoicePreviewBundleError):
        return ()
    return ((renderer, path),) if path else ()


def preview_caption(target, voice):
    return ui_language.tr("call.voice.preview.caption", voice=voice_label(target, voice))
