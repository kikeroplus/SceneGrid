import hashlib
import os
import shutil
import threading
import time
from pathlib import Path

import pythoncom
import win32gui
from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtGui import QImage
from win32com.shell import shell, shellcon


def drive_info(root):
    flags = shellcon.SHGFI_ICON | shellcon.SHGFI_SMALLICON | shellcon.SHGFI_DISPLAYNAME
    result, info = shell.SHGetFileInfo(root, 0, flags)
    if not result:
        return root[:2], QImage()
    handle = info[0]
    try:
        return info[3] or root[:2], QImage.fromHICON(handle) if handle else QImage()
    finally:
        if handle:
            win32gui.DestroyIcon(handle)


class TaskSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(object)


class Task(QRunnable):
    def __init__(self, work):
        super().__init__()
        self.signals = TaskSignals()
        self.cancelled = threading.Event()
        self.work = work

    def run(self):
        pythoncom.CoInitialize()
        try:
            result = self.work(self.signals.progress.emit, self.cancelled)
            self.signals.finished.emit(result)
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        finally:
            pythoncom.CoUninitialize()


def checksum(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def same_content(source, destination):
    return source.stat().st_size == destination.stat().st_size and checksum(source) == checksum(destination)


def unique_path(path):
    original = path
    index = 2
    while path.exists():
        path = original.with_name(f"{original.stem} ({index}){original.suffix}")
        index += 1
    return path


def transfer_files(paths, destination, move, identical_overwrite, conflict, progress):
    destination.mkdir(parents=True, exist_ok=True)
    result = {"done": [], "skipped": [], "overwritten": [], "failed": []}
    for index, source in enumerate(paths):
        temporary = None
        try:
            target = destination / source.name
            overwrite = False
            if os.path.normcase(str(source.resolve())) == os.path.normcase(str(target.resolve())):
                result["skipped"].append(f"{source.name} (同じ場所)")
                continue
            if target.exists():
                identical = same_content(source, target)
                if (identical and not identical_overwrite) or (not identical and conflict == "skip"):
                    result["skipped"].append(f"{source.name} ({'中身が同じ' if identical else '中身が違う'})")
                    continue
                if not identical and conflict == "rename":
                    target = unique_path(target)
                else:
                    overwrite = True
            # Copy to a sibling first so a failed transfer cannot destroy an existing destination.
            import tempfile
            descriptor, name = tempfile.mkstemp(prefix=".scenegrid-", dir=destination)
            os.close(descriptor)
            temporary = Path(name)
            shutil.copy2(source, temporary)
            if overwrite:
                os.replace(temporary, target)
            else:
                os.rename(temporary, target)
            temporary = None
            if move:
                source.unlink()
            result["done"].append(source)
            if overwrite:
                result["overwritten"].append(source.name)
        except Exception as exc:
            result["failed"].append(f"{source.name}: {exc}")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            progress((index + 1, len(paths)))
    return result


def recycle_files(paths, owner):
    flags = shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT | shellcon.FOF_WANTNUKEWARNING | shellcon.FOF_NOERRORUI
    code, aborted = shell.SHFileOperation((owner, shellcon.FO_DELETE, "\0".join(str(p) for p in paths) + "\0", None, flags, None, None))[:2]
    return {"code": code, "aborted": aborted, "removed": [p for p in paths if not p.exists()]}
