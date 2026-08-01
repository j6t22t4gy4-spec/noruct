from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from types import TracebackType
from typing import Literal


LockMode = Literal["shared", "exclusive"]


class KnowledgeStateBusyError(ValueError):
    """Raised when a Knowledge state lifecycle lock cannot be acquired safely."""


_REGISTRY_LOCK = threading.Lock()
_LOCAL_SHARED: dict[Path, int] = {}
_LOCAL_EXCLUSIVE: set[Path] = set()


def knowledge_lock_path(database_path: str | Path) -> Path:
    """Return the stable lock path for one Knowledge DB authority."""

    database = Path(database_path).expanduser()
    parent = database.parent.resolve()
    return parent / f".{database.name}.lifecycle.lock"


def knowledge_mutation_marker_path(database_path: str | Path) -> Path:
    """Return the crash-recovery marker path for one Knowledge DB authority."""

    database = Path(database_path).expanduser()
    parent = database.parent.resolve()
    return parent / f".{database.name}.mutation.json"


def _reserve_local(path: Path, mode: LockMode) -> None:
    with _REGISTRY_LOCK:
        shared = _LOCAL_SHARED.get(path, 0)
        exclusive = path in _LOCAL_EXCLUSIVE
        if mode == "exclusive":
            if shared or exclusive:
                raise KnowledgeStateBusyError(
                    "Knowledge state is open in another operation; close it before restore or deletion"
                )
            _LOCAL_EXCLUSIVE.add(path)
            return
        if exclusive:
            raise KnowledgeStateBusyError(
                "Knowledge state is undergoing an exclusive lifecycle operation"
            )
        _LOCAL_SHARED[path] = shared + 1


def _release_local(path: Path, mode: LockMode) -> None:
    with _REGISTRY_LOCK:
        if mode == "exclusive":
            _LOCAL_EXCLUSIVE.discard(path)
            return
        count = _LOCAL_SHARED.get(path, 0)
        if count <= 1:
            _LOCAL_SHARED.pop(path, None)
        else:
            _LOCAL_SHARED[path] = count - 1


def _lock_descriptor(descriptor: int, mode: LockMode) -> None:
    if os.name == "posix":
        import fcntl

        operation = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
        fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
        return
    if os.name == "nt":  # pragma: no cover - exercised by Windows release CI
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        operation = msvcrt.LK_NBRLCK if mode == "shared" else msvcrt.LK_NBLCK
        msvcrt.locking(descriptor, operation, 1)
        return
    raise OSError("Knowledge lifecycle locking is unsupported on this platform")


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return
    if os.name == "nt":  # pragma: no cover - exercised by Windows release CI
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    raise OSError("Knowledge lifecycle locking is unsupported on this platform")


class KnowledgeStateLock:
    """Non-blocking, cross-process shared/exclusive lock for one Knowledge DB.

    The persistent lock file is intentionally not removed on release. Removing a
    lock inode creates a race in which two processes can lock different inodes at
    the same pathname. OS locks disappear automatically after a crash.
    """

    __slots__ = ("database_path", "mode", "path", "_descriptor", "_reserved")

    def __init__(
        self,
        database_path: str | Path,
        *,
        mode: LockMode,
        create_parent: bool = False,
    ) -> None:
        if mode not in ("shared", "exclusive"):
            raise ValueError("Knowledge state lock mode is invalid")
        requested = Path(database_path).expanduser()
        if create_parent:
            requested.parent.mkdir(parents=True, exist_ok=True)
        if not requested.parent.exists() or not requested.parent.is_dir():
            raise KnowledgeStateBusyError("Knowledge state directory is unavailable")
        self.database_path = requested.parent.resolve() / requested.name
        self.mode = mode
        self.path = knowledge_lock_path(self.database_path)
        self._descriptor: int | None = None
        self._reserved = False

    def acquire(self) -> "KnowledgeStateLock":
        if self._descriptor is not None:
            return self
        if self.path.is_symlink():
            raise KnowledgeStateBusyError("Knowledge lifecycle lock path is unsafe")
        _reserve_local(self.path, self.mode)
        self._reserved = True
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(self.path, flags, 0o600)
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise OSError("Knowledge lifecycle lock is not a regular file")
            if details.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, 0o600)
                else:  # pragma: no cover - Windows has no descriptor chmod
                    self.path.chmod(0o600)
            except OSError:
                pass
            _lock_descriptor(descriptor, self.mode)
            if self.mode == "shared":
                marker = knowledge_mutation_marker_path(self.database_path)
                if marker.is_symlink() or marker.exists():
                    raise OSError(
                        "Knowledge state has an incomplete lifecycle mutation requiring recovery"
                    )
        except (BlockingIOError, OSError) as error:
            if descriptor is not None:
                os.close(descriptor)
            _release_local(self.path, self.mode)
            self._reserved = False
            raise KnowledgeStateBusyError(
                "Knowledge state is open in another process or lifecycle operation"
            ) from error
        self._descriptor = descriptor
        return self

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            if self._reserved:
                _release_local(self.path, self.mode)
                self._reserved = False
            return
        try:
            _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)
            self._descriptor = None
            if self._reserved:
                _release_local(self.path, self.mode)
                self._reserved = False

    def __enter__(self) -> "KnowledgeStateLock":
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
