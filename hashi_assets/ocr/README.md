# HASHI local OCR package

`media_read` runs OCR locally before exposing an image to a provider. This is
the text-evidence path for text-only providers such as DeepSeek, while
vision-capable providers may also consume the normalized image block.

## Supported languages

The default route covers English (`eng`), Simplified Chinese (`chi_sim`),
Traditional Chinese (`chi_tra`), Japanese (`jpn`), Korean (`kor`), Arabic
(`ara`), Russian (`rus`), French (`fra`), and German (`deu`).

- `PP-OCRv6_medium_rec`: English, Simplified/Traditional Chinese, Japanese,
  French, German, and other supported Latin text.
- `korean_PP-OCRv5_mobile_rec`: Korean.
- `eslav_PP-OCRv5_mobile_rec`: Russian and East Slavic text.
- `arabic_PP-OCRv5_mobile_rec`: Arabic.
- Tesseract `tessdata_fast`: bounded fallback for all nine languages.

PaddleOCR runs in a short-lived subprocess. This keeps its native runtime and
roughly 2.5 GiB peak working set out of each long-lived agent process. A single
Paddle text detector is shared by the selected recognition models during one
OCR invocation.

## Provisioning

Install the pinned optional runtime and verify its dependency closure:

```bash
python -m pip install -e '.[ocr]'
python -m pip check
```

Provision and verify the pinned model packs:

```bash
python scripts/provision_paddle_ocr_models.py
python scripts/provision_paddle_ocr_models.py --check
python scripts/provision_ocr_models.py
python scripts/provision_ocr_models.py --check
```

Both manifests pin exact sizes and SHA-256 digests. Runtime inference receives
explicit local model directories and uses offline environment flags, so it
does not fetch models while handling a user request.

## Tool behavior

Images use `ocr_mode=auto` by default. `ocr_mode=required` fails closed when a
requested language route is incomplete; `ocr_mode=off` disables OCR. Optional
`ocr_languages` hints can narrow the recognition models. OCR text is bounded,
marked as untrusted image evidence, and placed before the image content block.
