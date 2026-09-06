"""
HASHI Flow — Artifact Store
工件管理：注册、获取、版本化所有步骤产出的文件
"""

import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nagare.paths import validate_path_component


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ArtifactStore:
    """管理工作流运行期间产生的所有工件"""

    def __init__(self, run_id: str, runs_root: str | Path = "flow/runs"):
        self.run_id = validate_path_component(run_id, label="run_id")
        self.runs_root = Path(runs_root)
        self.base_dir = self.runs_root / self.run_id / "artifacts"
        self._lock = threading.RLock()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.base_dir / "_index.json"
        if not self.index_path.exists():
            self._write_index({})

    def register(
        self,
        key: str,
        source_path: str | list[str],
        step_id: str = None,
        required: bool = False,
    ):
        """注册单个文件/目录或一组文件，并将稳定副本写入工件目录。"""
        with self._lock:
            key = validate_path_component(key, label="artifact key")
            if isinstance(source_path, list):
                self._register_many(key, source_path, step_id=step_id, required=required)
                return

            source = Path(source_path)
            if not source.exists():
                if required:
                    raise FileNotFoundError(f"必需工件文件不存在: {source_path}")
                import logging
                logging.getLogger("nagare.artifact_store").warning(
                    f"工件文件不存在，跳过注册: {key} → {source_path}"
                )
                return

            # 保存副本到工件目录。重试写同一 key 时先移除旧副本，避免目录残留。
            dest = self.base_dir / key / source.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            self._remove_existing(dest)
            if source.is_dir():
                shutil.copytree(source, dest)
            else:
                shutil.copy2(source, dest)

            # 更新索引
            index = self._read_index()
            index[key] = {
                "path": str(dest),
                "original_path": str(source),
                "step_id": step_id,
                "size_bytes": self._path_size(dest),
                "registered_at": utc_now()
            }
            self._write_index(index)

    def _register_many(
        self,
        key: str,
        source_paths: list[str],
        *,
        step_id: str | None,
        required: bool,
    ) -> None:
        """把多文件工件复制到以 key 命名的目录，并以该目录作为工件路径。"""
        sources = [Path(value) for value in source_paths]
        missing = [str(path) for path in sources if not path.exists()]
        if missing:
            if required:
                raise FileNotFoundError(f"必需工件文件不存在: {missing}")
            import logging

            logging.getLogger("nagare.artifact_store").warning(
                "工件文件不存在，跳过注册: %s → %s", key, missing
            )
            return

        names = [source.name for source in sources]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"多文件工件包含重复文件名，无法无损注册: {duplicates}"
            )

        dest_dir = self.base_dir / key
        self._remove_existing(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for source in sources:
            destination = dest_dir / source.name
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination)

        total_size = self._path_size(dest_dir)
        index = self._read_index()
        index[key] = {
            "path": str(dest_dir),
            "original_path": [str(path) for path in sources],
            "step_id": step_id,
            "size_bytes": total_size,
            "registered_at": utc_now(),
        }
        self._write_index(index)

    def get(self, key: str) -> Optional[Path]:
        """获取工件路径"""
        with self._lock:
            index = self._read_index()
            if key not in index:
                return None
            return Path(index[key]["path"])

    def exists(self, key: str) -> bool:
        with self._lock:
            index = self._read_index()
            if key not in index:
                return False
            return Path(index[key]["path"]).exists()

    def list_all(self) -> dict:
        with self._lock:
            return self._read_index()

    def get_summary(self) -> dict:
        with self._lock:
            index = self._read_index()
            return {
                "count": len(index),
                "artifacts": list(index.keys()),
                "total_size_bytes": sum(v.get("size_bytes", 0) for v in index.values())
            }

    def _read_index(self) -> dict:
        with open(self.index_path) as f:
            return json.load(f)

    def _write_index(self, index: dict):
        # Atomic write: write to temp file then rename to prevent partial reads on crash
        tmp = self.index_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        tmp.replace(self.index_path)

    @staticmethod
    def _remove_existing(path: Path) -> None:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()

    @staticmethod
    def _path_size(path: Path) -> int:
        if path.is_file():
            return path.stat().st_size
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
