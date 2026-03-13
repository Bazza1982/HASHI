from __future__ import annotations
from pathlib import Path

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
NORMALIZE_TO_JPEG = {".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


def is_image_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def normalize_image_file(local_path: Path, filename: str) -> tuple[Path, str]:
    ext = Path(filename).suffix.lower()
    if ext not in NORMALIZE_TO_JPEG or Image is None:
        return local_path, filename

    converted_name = f"{Path(filename).stem}.jpg"
    converted_path = local_path.with_suffix(".jpg")

    try:
        with Image.open(local_path) as img:
            if getattr(img, "is_animated", False):
                img.seek(0)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            elif img.mode == "L":
                img = img.convert("RGB")
            img.save(converted_path, format="JPEG", quality=92)
        local_path.unlink(missing_ok=True)
        return converted_path, converted_name
    except Exception:
        return local_path, filename
