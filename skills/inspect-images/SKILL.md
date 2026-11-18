---
name: inspect-images
description: Inspect objects, people, actions, scenes, and spatial relationships in an attached or authorized local image with HASHI's vision_inspect tool. Use when the active model cannot natively see images and the answer depends on non-text visual meaning; use OCR instead when only visible text is needed.
---

# Inspect Images

Use `vision_inspect` only when visual evidence is necessary.

1. Prefer native image input when the active backend supports it.
2. Use OCR instead when the request concerns only visible text.
3. Otherwise call `vision_inspect` with the `image_ref` shown in the attachment summary and a specific visual question.
4. As an economical default, start with `detail: standard` or use `brief` for simple identification. Use detailed or additional focused inspections when they materially resolve the authorized question.
5. Treat observations as visual evidence and preserve every stated uncertainty. Never turn an inference into a confirmed fact.
6. Do not infer image content from filenames, captions, or OCR alone.

For comparisons, inspect images individually or jointly when the selected provider and tool support it; retain the exact attachment references and distinguish uncertainty for each image.

Read [references/observation-contract.md](references/observation-contract.md) when interpreting or relaying structured results.
