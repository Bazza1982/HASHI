# Veritas Setup Guide

## Quick Start (text-based PDFs)

pdfminer.six is already installed and works for all standard academic PDFs:
```
pip install pymupdf pdfminer.six
```
No further setup needed. Run a workflow and the `mineru-extractor` adapter will
automatically use pdfminer as the extraction backend.

## MinerU (optional — for scanned/image PDFs)

MinerU provides better structure extraction for scanned PDFs. It requires
downloading ~3GB of model weights.

### Install
```bash
pip install "magic-pdf[full]" --extra-index-url https://wheels.myhloli.com
pip install pycocotools
```

### Config
`~/magic-pdf.json` is already configured at `/home/lily/magic-pdf.json`.

### Download models (~3GB)
```bash
python3 -c "
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='opendatalab/PDF-Extract-Kit-1.0',
    local_dir='/home/lily/models/MinerU',
    ignore_patterns=['*.git*', '*.md'],
    local_dir_use_symlinks=False
)
"
```

Expected directory structure after download:
```
/home/lily/models/MinerU/
  Layout/YOLO/doclayout_yolo_docstructbench_imgsz1280_2501.pt  ✓ (already downloaded)
  OCR/paddleocr_torch/ch_PP-OCRv3_det_infer.pth
  OCR/paddleocr_torch/ch_PP-OCRv3_rec_infer.pth
  MFD/YOLO/yolo_v8_ft.pt
  ...
```

### When to use MinerU vs pdfminer

| PDF type               | pdfminer | MinerU |
|------------------------|----------|--------|
| Text-based (journal)   | ✓ great  | ✓ better structure |
| Scanned / image-only   | ✗ fails  | ✓ OCR  |
| Tables                 | partial  | ✓ (enable in config) |
| Equations              | partial  | ✓ (enable in config) |

The extractor tries MinerU first, then falls back to pdfminer automatically.

## Obsidian Vault

Default vault path: `/home/lily/obsidian/Research`

To use a different vault, pass it in workflow pre_flight:
```yaml
pre_flight:
  vault_path: /path/to/your/obsidian/vault
```

## library_index.jsonl

Located at `<vault_path>/library_index.jsonl`.
One JSON entry per paper, used for deduplication (DOI or title+year match).
