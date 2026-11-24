import logging
import os
import time
from pathlib import Path
from threading import Lock

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .books import MIME, under

logger = logging.getLogger(__name__)
SUFFIXES = {*MIME, "zip", "htm", "xhtml"}


class FolderEvents(FileSystemEventHandler):
    def __init__(self):
        self.lock = Lock()
        self.deadline = None

    def on_any_event(self, event):
        if event.event_type not in {"created", "moved", "modified", "deleted", "closed"}:
            return
        if event.is_directory:
            if event.event_type == "modified":
                return
        elif not any(Path(os.fsdecode(path)).suffix.lower().lstrip(".") in SUFFIXES
                     for path in (event.src_path, event.dest_path) if path):
            return
        with self.lock:
            self.deadline = time.monotonic() + 3

    def ready(self):
        with self.lock:
            if self.deadline is not None and self.deadline <= time.monotonic():
                self.deadline = None
                return True
        return False


class ScanWatch:
    def __init__(self, root):
        self.root = root
        self.observer = Observer()
        self.watches = {}

    def __enter__(self):
        self.observer.start()
        return self

    def __exit__(self, *_):
        self.observer.stop()
        self.observer.join()

    def changed(self, folders):
        paths = {}
        for folder in folders:
            try:
                path = under(self.root, folder)
                if path.is_dir():
                    paths.setdefault(path, set()).add(folder)
            except (OSError, ValueError):
                continue
        for path in self.watches.keys() - paths.keys():
            watch, _ = self.watches.pop(path)
            self.observer.unschedule(watch)
        for path in paths.keys() - self.watches.keys():
            handler = FolderEvents()
            try:
                self.watches[path] = (self.observer.schedule(handler, os.fspath(path), recursive=True), handler)
            except OSError as exc:
                logger.debug("Filesystem watch unavailable for %s: %s", path, type(exc).__name__)
        return {folder for path, (_, handler) in self.watches.items() if handler.ready() for folder in paths[path]}
