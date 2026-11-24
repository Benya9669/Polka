import shutil
from datetime import timedelta

from .books import save_atomic
from .db import DATA, now


AVAILABILITY = timedelta(hours=24)
REMOVAL_GRACE = timedelta(hours=1)


def artifact_path(task_id):
    return DATA / "opds-downloads" / f"{task_id}.bin"


def save_artifact(task_id, source):
    save_atomic(artifact_path(task_id), lambda temporary: shutil.copyfile(source, temporary))
    return now() + AVAILABILITY


def clean_artifacts():
    folder = DATA / "opds-downloads"
    if not folder.is_dir():
        return
    cutoff = (now() - AVAILABILITY - REMOVAL_GRACE).timestamp()
    for path in folder.iterdir():
        if not (path.name.startswith("tmp") or path.suffix == ".bin" and path.stem.isascii() and path.stem.isdecimal()):
            continue
        try:
            if path.is_file() and path.stat().st_mtime <= cutoff:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
