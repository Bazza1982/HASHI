---
name: inspect-images
description: Inspect objects, people, actions, scenes, and spatial relationships in an attached or authorized local image with HASHI's vision_inspect tool. Use when the active model cannot natively see images and the answer depends on non-text visual meaning; use OCR instead when only visible text is needed.
---

# Inspect Images

Use `vision_inspect` only when visual evidence is necessary.

1. Prefer native image input when the active backend supports it.
2. Use OCR instead when the request concerns only visible text.
3. Otherwise call `vision_inspect` with the `image_ref` shown in the attachment summary and a specific visual question.
4. Start with `detail: standard`. Use `brief` for simple identification and retry once with `detailed` only when important evidence remains unclear.
5. Treat observations as visual evidence and preserve every stated uncertainty. Never turn an inference into a confirmed fact.
6. Do not infer image content from filenames, captions, or OCR alone.

When comparing multiple images, inspect each image separately and identify each result by its attachment reference.

Read [references/observation-contract.md](references/observation-contract.md) when interpreting or relaying structured results.
