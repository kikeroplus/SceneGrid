import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pythoncom

import cv2
from PySide6.QtCore import QDir, QObject, QRunnable, QThreadPool, Qt, QUrl, Signal, QSize
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QIcon, QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QSizePolicy,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStatusBar,
    QStyle,
    QToolBar,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
    QFileSystemModel,
)

try:
    from win32com.propsys import propsys, pscon
    from win32com.shell import shellcon

    HAS_PROPSYS = True
except Exception:
    HAS_PROPSYS = False


APP_NAME = "SceneGrid"
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".wmv",
    ".webm",
    ".mpg",
    ".mpeg",
}
RATING_TO_WINDOWS = {0: None, 1: 1, 2: 25, 3: 50, 4: 75, 5: 99}
WINDOWS_TO_RATING = [(99, 5), (75, 4), (50, 3), (25, 2), (1, 1)]


def appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_setting(name: str, default: str = "") -> str:
    try:
        return (appdata_dir() / name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return default


def write_setting(name: str, value: str) -> None:
    try:
        (appdata_dir() / name).write_text(value, encoding="utf-8")
    except OSError:
        pass


def natural_key(text: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def seconds_text(ms: int) -> str:
    total = max(0, int(ms / 1000))
    return f"{total // 60}:{total % 60:02d}"


def frame_to_image(frame, target: QSize | None) -> QImage:
    if frame is None:
        return QImage()
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w, ch = rgb.shape
    image = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888).copy()
    return image if target is None else image.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)


def extract_contact_sheet(path: Path, tile_size: QSize | None, count: int = 16, cancelled=None) -> list[QImage]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return []

    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
    duration = frame_count / fps if frame_count > 0 and fps > 0 else 0
    images: list[QImage] = []

    for index in range(count):
        if cancelled is not None and cancelled.is_set():
            break
        if frame_count > 1:
            target_frame = int((index + 0.5) * frame_count / count)
            capture.set(cv2.CAP_PROP_POS_FRAMES, min(target_frame, frame_count - 1))
        elif duration > 0:
            capture.set(cv2.CAP_PROP_POS_MSEC, (index + 0.5) * duration * 1000 / count)
        ok, frame = capture.read()
        if not ok:
            images.append(QImage())
        else:
            image = frame_to_image(frame, tile_size)
            position_ms = target_frame / fps * 1000 if frame_count > 1 and fps > 0 else 0
            image.setText("position_ms", str(int(position_ms)))
            image.setText("dimensions", f"{frame.shape[1]} × {frame.shape[0]}")
            images.append(image)

    capture.release()
    return images


def extract_single_thumbnail(path: Path, size: QSize) -> QImage:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return QImage()
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if frame_count > 4:
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_count // 5)
    ok, frame = capture.read()
    capture.release()
    if not ok:
        return QImage()
    return frame_to_image(frame, size)


def read_rating(path: Path) -> int:
    if HAS_PROPSYS:
        try:
            store = propsys.SHGetPropertyStoreFromParsingName(
                str(path), None, shellcon.GPS_DEFAULT, propsys.IID_IPropertyStore
            )
            value = store.GetValue(pscon.PKEY_Rating).GetValue()
            if value is None:
                return 0
            for threshold, rating in WINDOWS_TO_RATING:
                if int(value) >= threshold:
                    return rating
        except Exception:
            pass
    return 0


def write_rating(path: Path, rating: int) -> tuple[bool, str]:
    if not HAS_PROPSYS:
        return False, "Windows の評価プロパティを扱えません"
    try:
        previous_stat = path.stat()
    except OSError as exc:
        return False, f"評価を書けませんでした: {exc}"
    value = RATING_TO_WINDOWS.get(rating)
    success = False
    message = ""
    try:
        store = propsys.SHGetPropertyStoreFromParsingName(
            str(path), None, shellcon.GPS_READWRITE, propsys.IID_IPropertyStore
        )
        variant = propsys.PROPVARIANTType(value, pythoncom.VT_EMPTY if value is None else pythoncom.VT_UI4)
        store.SetValue(pscon.PKEY_Rating, variant)
        store.Commit()
        store = None
        if read_rating(path) != rating:
            message = "この動画形式では Windows の評価を保存できません"
        else:
            success = True
            message = "評価を保存しました"
    except Exception as exc:
        message = f"評価を書けませんでした: {exc}"
    finally:
        store = None
        try:
            os.utime(path, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
        except OSError as exc:
            success = False
            message += f" / 更新日時を戻せませんでした: {exc}"
    return success, message


@dataclass
class VideoItem:
    path: Path
    rating: int = 0
    mtime: float = 0

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def modified(self) -> float:
        return self.mtime


class WorkerSignals(QObject):
    contact_ready = Signal(str, str, list)
    thumbnail_ready = Signal(str, object)
    failed = Signal(str, str)


class FrameTile(QLabel):
    clicked = Signal(int)

    def __init__(self):
        super().__init__("読み込み中...")
        self.position_ms = None
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.position_ms is not None:
            self.clicked.emit(self.position_ms)
        super().mouseReleaseEvent(event)


class ContactSheetJob(QRunnable):
    def __init__(self, token: str, path: Path, tile_size: QSize):
        super().__init__()
        self.token = token
        self.path = path
        self.tile_size = tile_size
        self.signals = WorkerSignals()

    def run(self):
        try:
            images = extract_contact_sheet(self.path, self.tile_size)
            self.signals.contact_ready.emit(self.token, str(self.path), images)
        except Exception as exc:
            self.signals.failed.emit(self.token, str(exc))


class ThumbnailJob(QRunnable):
    def __init__(self, path: Path, size: QSize):
        super().__init__()
        self.path = path
        self.size = size
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.thumbnail_ready.emit(str(self.path), extract_single_thumbnail(self.path, self.size))
        except Exception:
            self.signals.thumbnail_ready.emit(str(self.path), QImage())


class ClickableVideo(QVideoWidget):
    clicked = Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def setting_number(name, default, minimum, maximum, cast=int):
    try:
        value = cast(read_setting(name, str(default)))
        return min(maximum, max(minimum, value)) if math.isfinite(value) else default
    except (ValueError, OverflowError):
        return default


class VideoPlayerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("動画")
        self.resize(900, 600)
        self.video = ClickableVideo()
        self.video.clicked.connect(self.toggle_play)
        self.player = QMediaPlayer(self)
        self.pending_position = None
        self.audio = QAudioOutput(self)
        self.player.setVideoOutput(self.video)
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(setting_number("volume.txt", 70, 0, 100, float) / 100)
        self.muted = read_setting("muted.txt", "0") == "1"
        self.audio.setMuted(self.muted)

        self.play_button = QPushButton()
        self.play_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.play_button.clicked.connect(self.toggle_play)
        self.position = QSlider(Qt.Horizontal)
        self.position.sliderMoved.connect(self.player.setPosition)
        self.time_label = QLabel("0:00 / 0:00")
        self.mute_button = QPushButton("消音")
        self.mute_button.setCheckable(True)
        self.mute_button.setChecked(self.muted)
        self.mute_button.clicked.connect(self.set_muted)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(0, 100)
        self.volume.setValue(int(self.audio.volume() * 100))
        self.volume.valueChanged.connect(self.set_volume)

        controls = QHBoxLayout()
        controls.addWidget(self.play_button)
        controls.addWidget(self.position, 1)
        controls.addWidget(self.time_label)
        controls.addWidget(self.mute_button)
        controls.addWidget(self.volume)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.video, 1)
        self.controls_widget = QWidget()
        self.controls_widget.setLayout(controls)
        self.controls_widget.hide()
        layout.addWidget(self.controls_widget)
        self.setCentralWidget(container)

        self.player.positionChanged.connect(self.update_position)
        self.player.durationChanged.connect(self.update_duration)
        self.player.playbackStateChanged.connect(self.update_play_icon)
        self.player.mediaStatusChanged.connect(self.loop_if_finished)

    def play_file(self, path: Path, position_ms: int = 0, autoplay: bool = True, show_window: bool = True):
        self.setWindowTitle(f"動画 - {path.name}")
        self.controls_widget.show()
        source = QUrl.fromLocalFile(str(path))
        if self.player.source() == source:
            if self.player.mediaStatus() in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
                self.pending_position = None
                self.player.setPosition(position_ms)
            else:
                self.pending_position = position_ms
        else:
            self.pending_position = position_ms
            self.player.setSource(source)
        if show_window:
            self.show()
            self.raise_()
        if autoplay:
            self.player.play()
        else:
            self.player.pause()

    def stop_and_release(self):
        self.pending_position = None
        self.player.stop()
        self.player.setSource(QUrl())
        self.controls_widget.hide()
        self.setWindowTitle("動画")

    def toggle_play(self):
        if self.player.source().isEmpty():
            return
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def set_muted(self, muted: bool):
        self.audio.setMuted(muted)
        write_setting("muted.txt", "1" if muted else "0")

    def set_volume(self, value: int):
        self.audio.setVolume(value / 100)
        write_setting("volume.txt", str(value))

    def update_position(self, value: int):
        self.position.blockSignals(True)
        self.position.setValue(value)
        self.position.blockSignals(False)
        self.time_label.setText(f"{seconds_text(value)} / {seconds_text(self.player.duration())}")

    def update_duration(self, value: int):
        self.position.setRange(0, max(0, value))
        self.time_label.setText(f"{seconds_text(self.player.position())} / {seconds_text(value)}")

    def update_play_icon(self):
        icon = QStyle.SP_MediaPause if self.player.playbackState() == QMediaPlayer.PlayingState else QStyle.SP_MediaPlay
        self.play_button.setIcon(self.style().standardIcon(icon))

    def loop_if_finished(self, status):
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia) and self.pending_position is not None:
            position = self.pending_position
            self.pending_position = None
            self.player.setPosition(position)
        if status == QMediaPlayer.EndOfMedia:
            self.player.setPosition(0)
            self.player.play()

    def closeEvent(self, event):
        self.player.pause()
        super().closeEvent(event)
