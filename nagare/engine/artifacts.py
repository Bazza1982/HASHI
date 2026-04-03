"""
HASHI Flow — Artifact Store
工件管理：注册、获取、版本化所有步骤产出的文件
"""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ArtifactStore:
    """管理工作流运行期间产生的所有工件"""

    def __init__(self, run_id: str, runs_root: str | Path = "flow/runs"):
        self.run_id = run_id
        self.runs_root = Path(runs_root)
        self.base_dir = self.runs_root / run_id / "artifacts"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.base_dir / "_index.json"
        if not self.index_path.exists():
            self._write_index({})

    def register(self, key: str, source_path: str, step_id: str = None, required: bool = False):
        """注册一个工件。required=True 时文件不存在抛出异常，否则只记录警告跳过。"""
        source = Path(source_path)
        if not source.exists():
            if required:
                raise FileNotFoundError(f"必需工件文件不存在: {source_path}")
            import logging
            logging.getLogger("nagare.artifact_store").warning(
                f"工件文件不存在，跳过注册: {key} → {source_path}"
            )
            return

        # 保存副本到工件目录
        dest = self.base_dir / key / source.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)

        # 更新索引
        index = self._read_index()
        index[key] = {
            "path": str(dest),
            "original_path": str(source),
            "step_id": step_id,
            "size_bytes": dest.stat().st_size,
            "registered_at": utc_now()
        }
        self._write_index(index)

    def get(self, key: str) -> Path:
        """获取工件路径"""
        index = self._read_index()
        if key not in index:
            return None
        return Path(index[key]["path"])

    def exists(self, key: str) -> bool:
        index = self._read_index()
        if key not in index:
            return False
        return Path(index[key]["path"]).exists()

    def list_all(self) -> dict:
        return self._read_index()

    def get_summary(self) -> dict:
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
        with open(self.index_path, "w") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
