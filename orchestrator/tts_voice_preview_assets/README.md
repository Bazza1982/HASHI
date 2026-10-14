# TTS voice preview assets

This directory contains immutable, versioned samples for the concrete voices
shown by `/voice`. Each sample is synthesized offline with the exact registered
provider, voice, and native language. Selecting a voice only validates and
publishes the sample; it never calls a TTS provider.

Telegram uses OGG/Opus and browser-based Frontend Connectors use MP3. Missing,
corrupt, or provenance-mismatched samples produce a clear unavailable notice
without rolling back the saved voice selection. No substitute voice is used.
