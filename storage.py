from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from abc import ABC, abstractmethod
from functools import lru_cache
from io import BufferedIOBase
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO


class StorageError(RuntimeError):
    pass


class StorageBackend(ABC):
    """Storage contract used by upload, cache, and parsed-artifact code."""

    @abstractmethod
    def put_bytes(self, key: str, data: bytes) -> None:
        pass

    @abstractmethod
    def put_file(self, key: str, source: BinaryIO) -> None:
        """Stream a file-like object into storage without duplicating it in memory."""

    @abstractmethod
    def get_bytes(self, key: str) -> bytes | None:
        pass

    @abstractmethod
    def exists(self, key: str) -> bool:
        pass

    @abstractmethod
    def delete(self, key: str) -> None:
        pass

    @abstractmethod
    def local_path(self, key: str) -> Path:
        """Return a local path for parsers that require filename-based input."""

    def put_text(self, key: str, value: str) -> None:
        self.put_bytes(key, value.encode("utf-8"))

    def get_text(self, key: str) -> str | None:
        value = self.get_bytes(key)
        return value.decode("utf-8") if value is not None else None

    def put_json(self, key: str, value: Any) -> None:
        self.put_text(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))

    def get_json(self, key: str) -> Any | None:
        value = self.get_text(key)
        return json.loads(value) if value is not None else None


class LocalStorage(StorageBackend):
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, key: str, data: bytes) -> None:
        self.put_file(key, BufferedIOBaseAdapter(data))

    def put_file(self, key: str, source: BinaryIO) -> None:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(file_descriptor, "wb") as temp_file:
                shutil.copyfileobj(source, temp_file, length=1024 * 1024)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_name, target)
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def get_bytes(self, key: str) -> bytes | None:
        target = self._resolve(key)
        return target.read_bytes() if target.is_file() else None

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)

    def local_path(self, key: str) -> Path:
        return self._resolve(key)

    def _resolve(self, key: str) -> Path:
        normalized = PurePosixPath(str(key).replace("\\", "/"))
        if normalized.is_absolute() or not normalized.parts or any(part in {"", ".", ".."} for part in normalized.parts):
            raise StorageError(f"Invalid storage key: {key!r}")
        target = self.root.joinpath(*normalized.parts).resolve()
        if target != self.root and self.root not in target.parents:
            raise StorageError(f"Storage key escapes root: {key!r}")
        return target


class BufferedIOBaseAdapter(BufferedIOBase):
    """Expose bytes through the same streaming path used by uploaded files."""

    def __init__(self, data: bytes):
        self._data = memoryview(data)
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._data):
            return b""
        end = len(self._data) if size < 0 else min(self._offset + size, len(self._data))
        chunk = self._data[self._offset:end].tobytes()
        self._offset = end
        return chunk


def _safe_user_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip(".-")
    return cleaned or "default"


@lru_cache(maxsize=32)
def get_storage(user_id: str | None = None) -> StorageBackend:
    backend = os.getenv("STORAGE_BACKEND", "local").strip().lower()
    if backend != "local":
        raise StorageError(f"Unsupported STORAGE_BACKEND: {backend}. Only 'local' is available in the demo.")
    base_dir = Path(os.getenv("LOCAL_STORAGE_DIR", ".data"))
    resolved_user = _safe_user_id(user_id or os.getenv("STORAGE_USER_ID", "default"))
    return LocalStorage(base_dir / "users" / resolved_user)
