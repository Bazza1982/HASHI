# Voice preview assets

This directory contains immutable, versioned Function assets used by the
`/voice` profile-preview UI. Each version is validated against its manifest at
deployment and before it is used as a product fallback.

An instance may override an asset from its local `media/_voice_previews`
directory. Product bundles contain no credentials, user data, or mutable
runtime state.
