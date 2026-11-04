# Review notes

The initial public package hardens configuration types and finite option bounds, OpenAI-compatible endpoint normalization, response shape/UTF-8 handling, redirect and error-body privacy, image-byte limits, temporary crop cleanup and framing input/skip/negation behavior. Crop evidence is retained and observed token usage is accumulated.

Focused tests generate their own image fixtures and use synthetic answers or temporary loopback HTTP endpoints. No private images, prompts, observed results, benchmark histories, credentials, local endpoint assignments or machine paths are release assets. Passing offline checks does not establish a vision model's accuracy or live-provider acceptance.
