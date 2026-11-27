"""StudySource over a local (or mounted) directory tree."""
import io
import os
from pathlib import Path
from typing import BinaryIO, Optional

from ..domain import Entry, normalize_rel_path
from ..ports import ByteRange


class FilesystemSource:
    def __init__(self, root: str):
        self._root = Path(root).resolve()
        if not self._root.is_dir():
            raise NotADirectoryError(f"source root is not a directory: {root}")

    def _resolve(self, rel_path: str) -> Path:
        target = (self._root / normalize_rel_path(rel_path)).resolve()
        if not target.is_relative_to(self._root):
            raise ValueError("path escapes the source root")
        return target

    def list_dir(self, path: str = "") -> list:
        base = self._resolve(path)
        prefix = normalize_rel_path(path)
        entries = []
        with os.scandir(base) as it:
            for item in it:
                is_dir = item.is_dir(follow_symlinks=False)
                size = 0 if is_dir else item.stat(follow_symlinks=False).st_size
                rel = f"{prefix}/{item.name}" if prefix else item.name
                entries.append(Entry(name=item.name, path=rel, is_dir=is_dir, size=size))
        return sorted(entries, key=lambda e: e.name)

    def open(self, path: str, byte_range: Optional[ByteRange] = None) -> BinaryIO:
        target = self._resolve(path)
        if byte_range is None:
            return open(target, "rb")
        start, end = byte_range
        with open(target, "rb") as fh:
            fh.seek(start)
            return io.BytesIO(fh.read(end - start + 1))
