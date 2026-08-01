"""Portable, opt-in polling for registered local Knowledge Folders."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .folder_models import KnowledgeFolderScanReport, KnowledgeFolderStatus
from .folder_service import KnowledgeFolderScanControl, KnowledgeFolderService


@dataclass(frozen=True, slots=True)
class KnowledgeFolderWatchEvent:
    folder_id: str
    changed: bool
    report: KnowledgeFolderScanReport | None
    stopped: bool = False


class KnowledgeFolderWatcher:
    """Explicit local polling; it owns no Knowledge state or daemon."""

    def __init__(self, service: KnowledgeFolderService, *, interval_seconds: float = 5.0) -> None:
        if not 0.25 <= interval_seconds <= 3600:
            raise ValueError("Knowledge Folder watch interval must be between 0.25 and 3600 seconds")
        self.service = service
        self.interval_seconds = interval_seconds
        self._fingerprints: dict[str, tuple[int, int, int]] = {}

    def _fingerprint(self, folder_id: str) -> tuple[int, int, int]:
        folder = self.service.store.knowledge_folder(folder_id)
        if folder is None:
            raise ValueError(f"Knowledge Folder was not found: {folder_id}")
        if folder.status != KnowledgeFolderStatus.ACTIVE:
            return (-1, -1, -1)
        root = self.service.validate_root(folder.root_path)
        files = newest = total = 0
        for directory, directories, names in os.walk(root, followlinks=False):
            directories[:] = [item for item in directories if item not in {".git", ".noruct", "__pycache__"}]
            for name in names:
                if name == ".DS_Store":
                    continue
                path = Path(directory, name)
                try:
                    stat = path.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not path.is_file() or path.is_symlink():
                    continue
                files += 1
                total += stat.st_size
                newest = max(newest, stat.st_mtime_ns)
        return files, newest, total

    def poll_once(self, folder_id: str, **scan_options: object) -> KnowledgeFolderWatchEvent:
        current = self._fingerprint(folder_id)
        previous = self._fingerprints.get(folder_id)
        self._fingerprints[folder_id] = current
        folder = self.service.store.knowledge_folder(folder_id)
        assert folder is not None  # _fingerprint verified the registration.
        initial_scan_required = previous is None and folder.last_scan_status == "NEVER"
        if (not initial_scan_required and previous is None) or current == (-1, -1, -1) or current == previous:
            return KnowledgeFolderWatchEvent(folder_id=folder_id, changed=False, report=None)
        report = self.service.scan(folder_id, **scan_options)
        self._fingerprints[folder_id] = self._fingerprint(folder_id)
        return KnowledgeFolderWatchEvent(folder_id=folder_id, changed=True, report=report)

    def watch(
        self, folder_id: str, *, control: KnowledgeFolderScanControl | None = None,
        max_cycles: int | None = None, on_event: Callable[[KnowledgeFolderWatchEvent], None] | None = None,
        **scan_options: object,
    ) -> tuple[KnowledgeFolderWatchEvent, ...]:
        if max_cycles is not None and not 1 <= max_cycles <= 100_000:
            raise ValueError("Knowledge Folder watch cycle limit is invalid")
        stop = control or KnowledgeFolderScanControl()
        events: list[KnowledgeFolderWatchEvent] = []
        cycles = 0
        while not stop.cancelled and (max_cycles is None or cycles < max_cycles):
            event = self.poll_once(folder_id, control=stop, **scan_options)
            events.append(event)
            if on_event:
                on_event(event)
            cycles += 1
            if stop.cancelled or (max_cycles is not None and cycles >= max_cycles):
                break
            time.sleep(self.interval_seconds)
        return tuple(events)
