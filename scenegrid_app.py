import ctypes
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import win32api
import win32net
from PySide6.QtCore import QEvent, QObject, QPoint, QSize, QStandardPaths, Qt, QThreadPool, QTimer, QUrl
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
    QMessageBox, QProgressBar, QPushButton, QSplitter, QStatusBar, QToolBar,
    QToolButton, QVBoxLayout, QWidget,
)

import scenegrid_core as core
from scenegrid_tasks import Task, drive_info, recycle_files, transfer_files
from scenegrid_widgets import ContactSheetView, FlowLayout, FolderTree, VideoList


STYLE = """
QMainWindow, QWidget { background: #202020; color: #EEEEEE; font-size: 12px; }
QWidget#toolbar, QToolBar, QStatusBar { background: #2B2B2B; }
QToolButton, QPushButton, QComboBox { background: #3A3A3A; border: 1px solid #555555; padding: 5px 8px; }
QToolButton:hover, QPushButton:hover { background: #505050; }
QToolButton:checked { background: #E0A000; color: #111111; }
QToolButton:disabled, QPushButton:disabled { color: #777777; }
QListWidget, QTreeWidget { background: #181818; border: 0; }
QTreeWidget::item:selected { background: #504414; }
QLineEdit { background: #181818; border: 1px solid #555555; padding: 5px; }
QProgressBar { background: #181818; border: 0; max-height: 6px; }
QProgressBar::chunk { background: #E0A000; }
QToolTip { background: #303030; color: #EEEEEE; border: 1px solid #777777; }
QWidget#toolbarGroup { border-right: 1px solid #555555; }
"""


def restore_geometry(window, name, default_size):
    window.resize(*default_size)
    try:
        data = json.loads(core.read_setting(name, "{}"))
        x, y, width, height = (int(data[key]) for key in ("x", "y", "w", "h"))
        if width < 300 or height < 200:
            return
        from PySide6.QtCore import QRect
        screens = QApplication.screens()
        available = QRect()
        for screen in screens:
            available = available.united(screen.availableGeometry())
        if any(value < -(2**31) or value > 2**31 - 1 for value in (x, y, width, height)):
            return
        if width > available.width() + 64 or height > available.height() + 64:
            return
        rect = QRect(x, y, width, height)
        if any(screen.availableGeometry().intersected(rect).width() >= 160 and screen.availableGeometry().intersected(rect).height() >= 100 for screen in QApplication.screens()):
            window.setGeometry(rect)
            if data.get("maximized") is True:
                window.setWindowState(window.windowState() | Qt.WindowMaximized)
    except (ValueError, TypeError, KeyError, OverflowError):
        pass


def save_geometry(window, name):
    rect = window.normalGeometry() if window.isMaximized() else window.geometry()
    core.write_setting(name, json.dumps({"x": rect.x(), "y": rect.y(), "w": rect.width(), "h": rect.height(), "maximized": window.isMaximized()}))


class ExportDialog(QDialog):
    def __init__(self, controller, move):
        super().__init__(controller)
        self.setWindowTitle("表示中を移動" if move else "表示中をコピー")
        self.resize(560, 260)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"{controller.filter_description()} / {len(controller.filtered)} 本"))
        form = QFormLayout()
        destination_row = QWidget()
        row = QHBoxLayout(destination_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.destination = QLineEdit(core.read_setting("exportfolder.txt", str(controller.folder or Path.home())))
        choose = QPushButton("参照...")
        choose.clicked.connect(self.choose_destination)
        row.addWidget(self.destination, 1)
        row.addWidget(choose)
        form.addRow("出力先フォルダ", destination_row)
        self.new_name = QLineEdit()
        form.addRow("新しいフォルダ名", self.new_name)
        self.identical = QComboBox()
        self.identical.addItems(["スキップ", "上書き"])
        form.addRow("中身が同じファイル", self.identical)
        self.conflict = QComboBox()
        self.conflict.addItems(["別名で保存", "スキップ", "上書き"])
        form.addRow("同じ名前で中身が違うファイル", self.conflict)
        layout.addLayout(form)
        self.error = QLabel()
        self.error.setStyleSheet("color: #FFC107;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("移動する" if move else "コピーする")
        buttons.button(QDialogButtonBox.Cancel).setText("キャンセル")
        buttons.accepted.connect(self.validate)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choose_destination(self):
        folder = QFileDialog.getExistingDirectory(self, "出力先フォルダ", self.destination.text())
        if folder:
            self.destination.setText(folder)

    def validate(self):
        name = self.new_name.text().strip()
        if not self.destination.text().strip() or (name and (name in (".", "..") or any(c in name for c in '<>:"/\\|?*') or name.endswith((".", " ")))):
            self.error.setText("出力先とフォルダ名を確認してください")
            return
        self.accept()

    def options(self):
        path = Path(self.destination.text()).expanduser()
        if self.new_name.text().strip():
            path /= self.new_name.text().strip()
        return path, self.identical.currentIndex() == 1, ("rename", "skip", "overwrite")[self.conflict.currentIndex()]


class GridWindow(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("SceneGrid — グリッド一覧")
        self.setStyleSheet(STYLE)
        self.setAcceptDrops(True)
        restore_geometry(self, "grid_geometry.txt", (1000, 720))
        toolbar = QToolBar()
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self.size_buttons = {}
        for title, width in (("小", 160), ("中", 240), ("大", 320)):
            button = QToolButton()
            button.setText(title)
            button.setCheckable(True)
            button.setFocusPolicy(Qt.NoFocus)
            button.clicked.connect(lambda checked=False, w=width: self.set_tile_size(w))
            toolbar.addWidget(button)
            self.size_buttons[width] = button
        self.list_widget = VideoList(controller, grid=True)
        controller.connect_list(self.list_widget, grid=True)
        self.list_widget.size_step.connect(self.step_size)
        self.setCentralWidget(self.list_widget)
        toolbar.addSeparator()
        toolbar.addWidget(controller.button("すべて表示", controller.clear_filter))
        self.filter_buttons = {}
        for value in range(1, 6):
            button = controller.button(f"★{value}", lambda checked=False, v=value: controller.set_filter_rating(v), checkable=True)
            self.filter_buttons[value] = button
            toolbar.addWidget(button)
        self.filter_combo = QComboBox()
        self.filter_combo.setFocusPolicy(Qt.NoFocus)
        for title, value in (("以上", "at_least"), ("以下", "at_most"), ("のみ", "only")):
            self.filter_combo.addItem(title, value)
        self.filter_combo.currentIndexChanged.connect(lambda i: controller.set_filter_mode(self.filter_combo.itemData(i)))
        toolbar.addWidget(self.filter_combo)
        self.filter_status = QLabel()
        self.statusBar().addPermanentWidget(self.filter_status)
        width = core.setting_number("grid_tile.txt", 160, 160, 320)
        self.set_tile_size(width if width in (160, 240, 320) else 160)

    def set_tile_size(self, width):
        self.list_widget.set_size(width)
        for value, button in self.size_buttons.items():
            button.setChecked(value == width)
        core.write_setting("grid_tile.txt", str(width))
        if self.isVisible():
            self.controller.queue_thumbnails()

    def step_size(self, delta):
        choices = (160, 240, 320)
        self.set_tile_size(choices[max(0, min(2, choices.index(self.list_widget.width_choice) + delta))])

    def closeEvent(self, event):
        save_geometry(self, "grid_geometry.txt")
        self.controller.large_thumbnails.clear()
        self.controller.queue_thumbnails(grid_closing=True)
        super().closeEvent(event)

    def dragEnterEvent(self, event):
        self.controller.dragEnterEvent(event)

    def dropEvent(self, event):
        self.controller.dropEvent(event)


class SceneGridWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(core.APP_NAME)
        self.setMinimumSize(760, 560)
        self.setAcceptDrops(True)
        self.setStyleSheet(STYLE)
        self.items = []
        self.filtered = []
        self.item_by_path = {}
        self.current_index = -1
        self.selected_paths = set()
        self.selection_anchor = None
        self.folder = None
        self.pending_open_selection = None
        self.tree_target = None
        self.generation = 0
        self.media_epoch = 0
        self.busy = False
        self.has_interacted = False
        self.closing = False
        self.drag_token = uuid.uuid4().hex
        self.tasks = set()
        self.folder_task = None
        self.rating_load_task = None
        self.ratings_known = set()
        self.thumb_revision = 0
        self.thumb_task = None
        self.contact_jobs = {}
        self.contact_cache = {}
        self.thumbnail_cache = {}
        self.large_thumbnails = {}
        self.dimensions = {}
        self.grid_window = None
        self.sort_mode = core.read_setting("sort.txt", "name_asc")
        if self.sort_mode not in ("name_asc", "name_desc", "date_asc", "date_desc"):
            self.sort_mode = "name_asc"
        self.filter_rating = core.setting_number("filter_rating.txt", 0, 0, 5)
        self.filter_mode = core.read_setting("filter_mode.txt", "at_least")
        if self.filter_mode not in ("at_least", "at_most", "only"):
            self.filter_mode = "at_least"
        self.advance_after_rating = core.read_setting("advance_after_rating.txt", "0") == "1"
        self.load_pool = QThreadPool(self)
        self.load_pool.setMaxThreadCount(4)
        self.preview_pool = QThreadPool(self)
        self.preview_pool.setMaxThreadCount(2)
        self.thumb_pool = QThreadPool(self)
        self.thumb_pool.setMaxThreadCount(1)
        self.io_pool = QThreadPool(self)
        self.io_pool.setMaxThreadCount(1)
        self.pool = self.preview_pool
        self.player_window = core.VideoPlayerWindow()
        self.player_window.setStyleSheet(STYLE)
        restore_geometry(self.player_window, "video_geometry.txt", (900, 600))
        self.build_ui()
        restore_geometry(self, "main_geometry.txt", (1240, 840))
        QApplication.instance().installEventFilter(self)
        self.restore_last_folder()

    def submit(self, pool, work, done=None, failed=None, progress=None):
        task = Task(work)
        self.tasks.add(task)
        if progress:
            task.signals.progress.connect(progress)
        def complete(result):
            self.tasks.discard(task)
            if not self.closing and done:
                done(result)
        def error(message):
            self.tasks.discard(task)
            if not self.closing:
                (failed or self.show_result)(message)
        task.signals.finished.connect(complete)
        task.signals.failed.connect(error)
        pool.start(task)
        return task

    def button(self, title, callback, tip=None, checkable=False):
        button = QToolButton()
        button.setText(title)
        button.setToolTip(tip or title)
        button.setFocusPolicy(Qt.NoFocus)
        button.setCheckable(checkable)
        button.clicked.connect(callback)
        return button

    def group(self, *widgets):
        group = QWidget()
        group.setObjectName("toolbarGroup")
        layout = QHBoxLayout(group)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(4)
        for widget in widgets:
            layout.addWidget(widget)
        return group

    def build_ui(self):
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.toolbar = QWidget()
        self.toolbar.setObjectName("toolbar")
        flow = FlowLayout(self.toolbar)
        self.open_button = self.button("フォルダを開く", self.choose_folder, "フォルダを開く (Ctrl+O)")
        self.grid_button = self.button("グリッド一覧", self.open_grid, "グリッド一覧 (Ctrl+G)")
        self.video_button = self.button("動画窓を表示", self.show_video)
        flow.addWidget(self.group(self.open_button, self.grid_button, self.video_button))
        self.sort_combo = QComboBox()
        self.sort_combo.setFocusPolicy(Qt.NoFocus)
        for title, value in (("名前 (昇順)", "name_asc"), ("名前 (降順)", "name_desc"), ("更新日時 (古い順)", "date_asc"), ("更新日時 (新しい順)", "date_desc")):
            self.sort_combo.addItem(title, value)
        self.sort_combo.setCurrentIndex(self.sort_combo.findData(self.sort_mode))
        self.sort_combo.currentIndexChanged.connect(lambda i: self.set_sort(self.sort_combo.itemData(i)))
        flow.addWidget(self.group(self.sort_combo))
        self.copy_button = self.button("表示中をコピー...", lambda: self.transfer(False))
        self.move_button = self.button("表示中を移動...", lambda: self.transfer(True))
        flow.addWidget(self.group(self.copy_button, self.move_button))
        self.filter_buttons = {}
        filters = [self.button("すべて表示", self.clear_filter)]
        for value in range(1, 6):
            button = self.button(f"★{value}", lambda checked=False, v=value: self.set_filter_rating(v), checkable=True)
            self.filter_buttons[value] = button
            filters.append(button)
        self.filter_combo = QComboBox()
        self.filter_combo.setFocusPolicy(Qt.NoFocus)
        for title, value in (("以上", "at_least"), ("以下", "at_most"), ("のみ", "only")):
            self.filter_combo.addItem(title, value)
        self.filter_combo.setCurrentIndex(self.filter_combo.findData(self.filter_mode))
        self.filter_combo.currentIndexChanged.connect(lambda i: self.set_filter_mode(self.filter_combo.itemData(i)))
        filters.append(self.filter_combo)
        flow.addWidget(self.group(*filters))
        self.advance_check = QCheckBox("評価後に次へ")
        self.advance_check.setFocusPolicy(Qt.NoFocus)
        self.advance_check.setChecked(self.advance_after_rating)
        self.advance_check.toggled.connect(self.toggle_advance)
        self.clear_selection_button = self.button("選択解除", self.clear_selection, "表示中の1つだけを選択 (Ctrl+D)")
        flow.addWidget(self.group(self.advance_check, self.clear_selection_button))
        layout.addWidget(self.toolbar)
        self.tree = FolderTree()
        self.tree.open_requested.connect(self.open_folder)
        self.tree.expand_requested.connect(self.expand_tree_node)
        self.build_tree()
        self.sheet = ContactSheetView()
        self.sheet.seek.connect(self.play_from_thumbnail)
        self.sheet.navigate.connect(self.move_current)
        self.sheet.native_requested.connect(self.request_native)
        self.sheet.zoom_changed.connect(self.update_info)
        self.sheet.menu_requested.connect(self.context_menu)
        self.sheet.stars.rated.connect(self.set_rating)
        self.overlay = self.sheet.info
        self.list_widget = VideoList(self)
        self.connect_list(self.list_widget)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self.sheet, 1)
        right_layout.addWidget(self.list_widget)
        self.splitter = QSplitter()
        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(right)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)
        width = core.setting_number("tree_width.txt", 250, 100, 600)
        self.splitter.setSizes([width, 1000])
        self.splitter.splitterMoved.connect(lambda: core.write_setting("tree_width.txt", str(self.splitter.sizes()[0])))
        layout.addWidget(self.splitter, 1)
        self.setCentralWidget(container)
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.progress_text = QLabel()
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(90)
        self.progress_bar.setTextVisible(False)
        self.filter_status = QLabel()
        self.status.addPermanentWidget(self.progress_text)
        self.status.addPermanentWidget(self.progress_bar)
        self.status.addPermanentWidget(self.filter_status)
        self.hide_progress()
        self.show_result("Ctrl+O: 開く / 1〜5: 評価 / 0: 解除 / Space: 再生・一時停止 / Del: ごみ箱")
        self.update_controls()

    def connect_list(self, view, grid=False):
        view.picked.connect(self.pick_file)
        view.menu_requested.connect(self.list_menu)
        view.activated_file.connect(self.bring_main if grid else self.show_video)

    def views(self):
        return [self.list_widget] + ([self.grid_window.list_widget] if self.grid_window else [])

    def show_result(self, text):
        self.status.showMessage(text)

    def progress(self, title, values):
        done, total = values
        self.progress_text.setText(f"{title} {done} / {total}")
        self.progress_bar.setRange(0, total)
        self.progress_bar.setValue(done)
        self.progress_text.show()
        self.progress_bar.show()

    def hide_progress(self):
        self.progress_text.hide()
        self.progress_bar.hide()

    def current_item(self):
        return self.filtered[self.current_index] if 0 <= self.current_index < len(self.filtered) else None

    def restore_last_folder(self):
        if len(sys.argv) > 1:
            self.open_target(Path(sys.argv[1]).expanduser())
        else:
            saved = core.read_setting("lastfolder.txt")
            if saved:
                self.open_folder(Path(saved))

    def choose_folder(self):
        if self.busy:
            return
        folder = QFileDialog.getExistingDirectory(self, "フォルダを開く", str(self.folder or Path.home()))
        if folder:
            self.open_folder(Path(folder))

    def open_folder(self, folder):
        self.open_target(Path(folder), folder_only=True)

    def cancel_previews(self):
        self.media_epoch += 1
        for task in self.contact_jobs.values():
            task.cancelled.set()
        self.contact_jobs.clear()
        if self.thumb_task:
            self.thumb_task.cancelled.set()

    def open_target(self, target, folder_only=False):
        if self.busy:
            return
        target = Path(os.path.abspath(os.fspath(target)))
        self.generation += 1
        generation = self.generation
        self.has_interacted = False
        for task in (self.folder_task, self.rating_load_task):
            if task:
                task.cancelled.set()
        self.cancel_previews()
        self.player_window.stop_and_release()
        self.items, self.filtered = [], []
        self.item_by_path.clear()
        self.selected_paths.clear()
        self.current_index = -1
        self.pending_open_selection = None
        self.contact_cache.clear()
        self.ratings_known.clear()
        self.thumbnail_cache.clear()
        self.large_thumbnails.clear()
        for view in self.views():
            view.populate([])
        self.sheet.message("読み込み中...")
        self.sheet.set_info("")
        self.show_result("読み込み中...")
        self.hide_progress()
        self.update_controls()
        def scan(progress, cancelled):
            folder = target if folder_only or target.is_dir() else target.parent
            select = None if folder_only or target == folder else target
            items = []
            for child in folder.iterdir():
                if cancelled.is_set():
                    return None
                if core.is_video(child) and child.is_file():
                    items.append(core.VideoItem(child, 0, child.stat().st_mtime))
            return folder, select, items
        def ready(result):
            if generation != self.generation or result is None:
                return
            self.folder, select, self.items = result
            self.item_by_path = {item.path: item for item in self.items}
            core.write_setting("lastfolder.txt", str(self.folder))
            self.pending_open_selection = select
            self.sort_items()
            if not self.filter_rating or not self.items:
                self.apply_sort_filter(skip_rating=True, preferred=select)
                self.pending_open_selection = None
            else:
                self.update_controls()
            self.sync_tree(self.folder)
            self.load_ratings(announce=True)
        self.folder_task = self.submit(self.load_pool, scan, ready, lambda message: self.show_empty(f"フォルダを開けません: {message}") if generation == self.generation else None)

    def load_ratings(self, announce=False):
        generation = self.generation
        snapshot = [item for item in self.items if item.path not in self.ratings_known]
        def ratings(progress, cancelled):
            result = {}
            for index, item in enumerate(snapshot):
                if cancelled.is_set():
                    break
                result[item.path] = core.read_rating(item.path)
                progress((index + 1, len(snapshot)))
            return result
        def ready(values):
            if generation != self.generation:
                return
            for path, rating in values.items():
                if path in self.item_by_path and path not in self.ratings_known:
                    self.item_by_path[path].rating = rating
                    self.ratings_known.add(path)
            if not self.busy:
                self.apply_sort_filter(preferred=self.pending_open_selection)
                if len(self.ratings_known) == len(self.items):
                    self.pending_open_selection = None
                self.hide_progress()
                self.queue_thumbnails()
                if announce and not self.has_interacted:
                    self.show_result(f"{len(self.items)} 本の動画を読み込みました")
        if snapshot:
            self.rating_load_task = self.submit(self.load_pool, ratings, ready, progress=lambda value: self.progress("評価読み込み中", value) if generation == self.generation and not self.busy else None)
        elif not self.busy:
            self.queue_thumbnails()

    def matches(self, item):
        if not self.filter_rating:
            return True
        if self.filter_mode == "at_most":
            return item.rating <= self.filter_rating
        if self.filter_mode == "only":
            return item.rating == self.filter_rating
        return item.rating >= self.filter_rating

    def sort_items(self):
        self.items.sort(key=lambda item: core.natural_key(item.name), reverse=self.sort_mode == "name_desc")
        if self.sort_mode.startswith("date_"):
            self.items.sort(key=lambda item: item.modified, reverse=self.sort_mode == "date_desc")

    def apply_sort_filter(self, skip_rating=False, preferred=None, update_player=True):
        previous = self.current_item()
        previous_path = previous.path if previous else None
        previous_index = max(0, self.current_index)
        selection = self.selected_paths.copy()
        self.sort_items()
        if not skip_rating and self.filter_rating and any(item.path not in self.ratings_known for item in self.items):
            self.filtered = []
            self.selected_paths.clear()
            self.current_index = -1
            self.player_window.stop_and_release()
            for view in self.views():
                view.populate([])
            self.sheet.message("読み込み中...")
            self.update_info()
            self.update_controls()
            return
        self.filtered = [item for item in self.items if skip_rating or self.matches(item)]
        visible = {item.path for item in self.filtered}
        self.selected_paths = selection & visible
        for view in self.views():
            view.populate(self.filtered)
        target = preferred if preferred in visible else previous_path
        if not self.filtered:
            self.current_index = -1
            self.selected_paths.clear()
            self.player_window.stop_and_release()
            self.show_empty("フィルター条件に一致する動画がありません" if self.items else "このフォルダには対象の動画がありません")
            return
        self.current_index = next((i for i, item in enumerate(self.filtered) if item.path == target), min(previous_index, len(self.filtered) - 1))
        current = self.current_item()
        self.selected_paths.add(current.path)
        changed = previous_path != current.path
        self.sync_selection()
        if changed or not self.sheet.images:
            self.show_current(update_player=update_player and changed)
        else:
            self.update_info()
        self.update_controls()

    def sync_selection(self):
        current = self.current_item()
        if current:
            self.selected_paths.add(current.path)
        for view in self.views():
            if current:
                for row in range(view.count()):
                    entry = view.item(row)
                    if Path(entry.data(Qt.UserRole)) == current.path:
                        view.setCurrentRow(row)
                        view.scrollToItem(entry)
                        break
            view.viewport().update()
        self.update_info()
        self.update_controls()

    def pick_file(self, path, modifiers=Qt.NoModifier, update_player=True):
        if self.busy or path not in self.item_by_path:
            return
        current = self.current_item()
        old_path = current.path if current else None
        if modifiers & Qt.ShiftModifier and self.selection_anchor in {item.path for item in self.filtered}:
            paths = [item.path for item in self.filtered]
            start, end = sorted((paths.index(self.selection_anchor), paths.index(path)))
            selection = set(paths[start:end + 1])
            self.selected_paths = self.selected_paths | selection if modifiers & Qt.ControlModifier else selection
        elif modifiers & Qt.ControlModifier:
            if path in self.selected_paths:
                if len(self.selected_paths) > 1:
                    self.selected_paths.remove(path)
                    if path == old_path:
                        path = next(item.path for item in self.filtered if item.path in self.selected_paths)
            else:
                self.selected_paths.add(path)
            self.selection_anchor = path
        else:
            if path not in self.selected_paths:
                self.selected_paths = {path}
            self.selection_anchor = path
        self.current_index = next(i for i, item in enumerate(self.filtered) if item.path == path)
        self.sync_selection()
        if old_path != path:
            self.show_current(update_player=update_player)

    def clear_selection(self):
        if self.busy:
            return
        current = self.current_item()
        self.selected_paths = {current.path} if current else set()
        self.selection_anchor = current.path if current else None
        self.sync_selection()

    def move_current(self, delta, update_player=True, edge=None):
        if self.busy or not self.filtered:
            return
        choices = [i for i, item in enumerate(self.filtered) if len(self.selected_paths) < 2 or item.path in self.selected_paths]
        position = choices.index(self.current_index) if self.current_index in choices else 0
        next_index = choices[0 if edge == "first" else -1] if edge else choices[max(0, min(len(choices) - 1, position + delta))]
        if next_index == self.current_index:
            return
        self.current_index = next_index
        if len(self.selected_paths) < 2:
            self.selected_paths = {self.current_item().path}
            self.selection_anchor = self.current_item().path
        self.sync_selection()
        self.show_current(update_player=update_player)

    def show_empty(self, message):
        self.sheet.message(message)
        self.sheet.set_info("")
        self.show_result(message)
        self.update_controls()

    def show_current(self, update_player=True):
        item = self.current_item()
        if not item:
            return
        self.sheet.zoom = 1
        cached = self.contact_cache.get(item.path)
        if cached:
            self.sheet.set_images(cached)
        else:
            self.sheet.message("読み込み中...")
            self.request_contact(item.path)
        self.update_info()
        if update_player:
            self.player_window.play_file(item.path, show_window=False)
            if not self.player_window.isVisible():
                self.player_window.show()
        for offset in (-1, 1):
            index = self.current_index + offset
            if 0 <= index < len(self.filtered):
                self.request_contact(self.filtered[index].path)

    def request_contact(self, path, native=False):
        key = (path, native)
        if key in self.contact_jobs or (not native and path in self.contact_cache) or self.busy:
            return
        generation, epoch = self.generation, self.media_epoch
        def work(progress, cancelled):
            return core.extract_contact_sheet(path, None if native else QSize(520, 292), cancelled=cancelled)
        def ready(images):
            if generation != self.generation or epoch != self.media_epoch:
                return
            self.contact_jobs.pop(key, None)
            if not native:
                self.contact_cache[path] = images
                while len(self.contact_cache) > 3:
                    removable = next((p for p in self.contact_cache if not self.current_item() or p != self.current_item().path), None)
                    if removable is None:
                        break
                    self.contact_cache.pop(removable)
            if images and not images[0].isNull():
                self.dimensions[path] = images[0].text("dimensions")
            if self.current_item() and self.current_item().path == path:
                if not native or self.sheet.zoom > 1:
                    self.sheet.set_images(images, keep_zoom=native)
                    self.update_info()
        self.contact_jobs[key] = self.submit(self.preview_pool, work, ready)

    def request_native(self):
        if self.current_item():
            self.request_contact(self.current_item().path, native=True)

    def update_info(self):
        item = self.current_item()
        self.sheet.stars.setVisible(item is not None)
        if not item:
            self.sheet.set_info("")
            return
        parts = [f"{self.current_index + 1} / {len(self.filtered)}", item.name]
        if self.dimensions.get(item.path):
            parts.append(self.dimensions[item.path])
        if self.sheet.zoom > 1:
            parts.append(f"拡大 ×{self.sheet.zoom:.1f}")
        if len(self.selected_paths) > 1:
            parts.append(f"{len(self.selected_paths)} 個を選択中")
        self.sheet.set_info("    ".join(parts))
        self.sheet.stars.set_rating(item.rating)

    def thumbnail_for(self, path, width):
        return self.large_thumbnails.get((path, width)) or self.thumbnail_cache.get(path)

    def queue_thumbnails(self, grid_closing=False):
        if self.busy or self.closing:
            return
        if any(item.path not in self.ratings_known for item in self.items):
            return
        if self.thumb_task:
            self.thumb_task.cancelled.set()
        self.thumb_revision += 1
        revision = self.thumb_revision
        generation, epoch = self.generation, self.media_epoch
        width = self.grid_window.list_widget.width_choice if self.grid_window and self.grid_window.isVisible() and not grid_closing else 160
        if width == 160:
            self.large_thumbnails.clear()
        paths = [item.path for item in self.filtered] + [item.path for item in self.items if item not in self.filtered]
        requests = [(p, 160) for p in paths if p not in self.thumbnail_cache]
        if width > 160:
            requests = [(p, width) for p in paths if (p, width) not in self.large_thumbnails] + requests
        def work(progress, cancelled):
            for index, (path, size) in enumerate(requests):
                if cancelled.is_set():
                    break
                image = core.extract_single_thumbnail(path, QSize(size - 24, int((size - 24) * 9 / 16)))
                progress((path, size, image, index + 1, len(requests)))
            return None
        def progress(value):
            if generation != self.generation or epoch != self.media_epoch or revision != self.thumb_revision:
                return
            path, size, image, done, total = value
            from PySide6.QtGui import QPixmap
            pixmap = QPixmap.fromImage(image)
            if size == 160:
                self.thumbnail_cache[path] = pixmap
            elif self.grid_window and self.grid_window.isVisible() and self.grid_window.list_widget.width_choice == size:
                self.large_thumbnails[path, size] = pixmap
            for view in self.views():
                view.viewport().update()
            self.progress("サムネール作成中", (done, total))
        def finished(result):
            if generation == self.generation and epoch == self.media_epoch and revision == self.thumb_revision:
                self.hide_progress()
        if requests:
            self.thumb_task = self.submit(self.thumb_pool, work, finished, progress=progress)
        else:
            self.hide_progress()

    def filter_description(self):
        return "すべて" if not self.filter_rating else f"★{self.filter_rating} { {'at_least': '以上', 'at_most': '以下', 'only': 'のみ'}[self.filter_mode]}"

    def update_controls(self):
        current = self.current_item()
        for value, button in self.filter_buttons.items():
            button.setChecked(value == self.filter_rating)
        controls = [(self.filter_combo, self.filter_mode)]
        if self.grid_window:
            for value, button in self.grid_window.filter_buttons.items():
                button.setChecked(value == self.filter_rating)
            controls.append((self.grid_window.filter_combo, self.filter_mode))
            self.grid_window.filter_status.setText(f"フィルター: {self.filter_description()}    表示 {len(self.filtered)} / 全 {len(self.items)} 本")
        for combo, value in controls:
            blocked = combo.blockSignals(True)
            combo.setCurrentIndex(combo.findData(value))
            combo.blockSignals(blocked)
        self.clear_selection_button.setEnabled(len(self.selected_paths) > 1 and not self.busy)
        self.clear_selection_button.setToolTip("表示中の1つだけを選択 (Ctrl+D)" if len(self.selected_paths) > 1 else "複数選択中のみ使用できます")
        for button in (self.copy_button, self.move_button):
            button.setEnabled(bool(self.filter_rating and self.filtered) and not self.busy)
            button.setToolTip("一覧に表示中のすべてが対象です" if self.filter_rating else "評価で絞り込んだときのみ使用できます")
        self.video_button.setEnabled(current is not None and not self.busy)
        self.video_button.setToolTip("表示中の動画窓を表示" if current else "動画を選択してください")
        self.sheet.stars.setEnabled(current is not None and not self.busy)
        self.sheet.stars.setVisible(current is not None)
        self.filter_status.setText(f"フィルター: {self.filter_description()}    表示 {len(self.filtered)} / 全 {len(self.items)} 本")

    def set_sort(self, mode):
        if self.busy:
            return
        self.sort_mode = mode
        core.write_setting("sort.txt", mode)
        self.apply_sort_filter()
        self.queue_thumbnails()

    def set_filter_rating(self, rating):
        if self.busy:
            return
        self.filter_rating = 0 if self.filter_rating == rating else rating
        core.write_setting("filter_rating.txt", str(self.filter_rating))
        self.apply_sort_filter()
        self.queue_thumbnails()

    def set_filter_mode(self, mode):
        if self.busy:
            return
        self.filter_mode = mode
        core.write_setting("filter_mode.txt", mode)
        self.apply_sort_filter()
        self.queue_thumbnails()

    def clear_filter(self):
        self.filter_rating = 0
        core.write_setting("filter_rating.txt", "0")
        self.apply_sort_filter()
        self.queue_thumbnails()

    def toggle_advance(self, enabled):
        self.advance_after_rating = enabled
        core.write_setting("advance_after_rating.txt", "1" if enabled else "0")

    def show_video(self):
        if self.busy or not self.current_item():
            return
        source = QUrl.fromLocalFile(str(self.current_item().path))
        if self.player_window.player.source() != source:
            self.player_window.play_file(self.current_item().path)
        self.player_window.show()
        self.player_window.raise_()
        self.player_window.activateWindow()

    def play_from_thumbnail(self, position):
        if not self.busy and self.current_item():
            self.player_window.play_file(self.current_item().path, position)

    def open_grid(self):
        if self.busy:
            return
        if self.grid_window is None:
            self.grid_window = GridWindow(self)
            self.grid_window.list_widget.populate(self.filtered)
        self.grid_window.show()
        self.grid_window.raise_()
        self.grid_window.activateWindow()
        self.sync_selection()
        self.queue_thumbnails()

    def bring_main(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def build_tree(self):
        desktop = Path(QStandardPaths.writableLocation(QStandardPaths.DesktopLocation))
        pictures = Path(QStandardPaths.writableLocation(QStandardPaths.PicturesLocation))
        self.desktop_node = self.tree.add_node("デスクトップ", desktop)
        self.pictures_node = self.tree.add_node("ピクチャ", pictures)
        self.pc_node = self.tree.add_node("PC", kind="pc")
        self.pc_node.takeChildren()
        self.pc_node.setData(0, Qt.UserRole + 2, True)
        self.network_node = self.tree.add_node("ネットワーク", kind="network")
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        drives = []
        for number in range(26):
            if mask & (1 << number):
                root = f"{chr(65 + number)}:\\"
                node = self.tree.add_node(root[:2], Path(root), "drive", self.pc_node)
                drives.append((node, root))
        def labels(progress, cancelled):
            for node, root in drives:
                if cancelled.is_set():
                    break
                try:
                    name, image = drive_info(root)
                    progress((node, name, image))
                except Exception:
                    pass
        def apply_drive_info(value):
            node, title, image = value
            node.setText(0, title)
            if not image.isNull():
                node.setIcon(0, QIcon(QPixmap.fromImage(image)))
        self.submit(self.load_pool, labels, progress=apply_drive_info)

    def expand_tree_node(self, node):
        if self.busy or node.data(0, Qt.UserRole + 2):
            return
        node.setData(0, Qt.UserRole + 2, "loading")
        path = node.data(0, Qt.UserRole)
        kind = node.data(0, Qt.UserRole + 1)
        def enumerate_nodes(progress, cancelled):
            entries = []
            if kind == "network":
                from win32com.shell import shell, shellcon
                desktop = shell.SHGetDesktopFolder()
                pidl = shell.SHGetSpecialFolderLocation(0, shellcon.CSIDL_NETWORK)
                network = desktop.BindToObject(pidl, None, shell.IID_IShellFolder)
                for child in network.EnumObjects(0, shellcon.SHCONTF_FOLDERS):
                    if cancelled.is_set():
                        break
                    name = network.GetDisplayNameOf(child, shellcon.SHGDN_FORPARSING)
                    if name.startswith("\\\\"):
                        entries.append((name.lstrip("\\"), name, "server", False))
            elif kind == "server":
                resume = 0
                while True:
                    shares, total, resume = win32net.NetShareEnum(path, 1, resume)
                    for share in shares:
                        if share["type"] & 0xFFFF == 0 and not share["netname"].endswith("$"):
                            entries.append((share["netname"], str(Path(path) / share["netname"]), "folder", False))
                    if not resume or cancelled.is_set():
                        break
            else:
                for child in Path(path).iterdir():
                    if cancelled.is_set():
                        break
                    try:
                        if child.is_dir():
                            attrs = getattr(child.stat(), "st_file_attributes", 0)
                            if attrs & 4:
                                continue
                            entries.append((child.name, str(child), "folder", bool(attrs & 2)))
                    except OSError:
                        pass
            entries.sort(key=lambda entry: core.natural_key(entry[0]))
            return entries
        def ready(entries):
            existing = {os.path.normcase(node.child(i).data(0, Qt.UserRole)): node.child(i) for i in range(node.childCount()) if node.child(i).data(0, Qt.UserRole)}
            for index in reversed(range(node.childCount())):
                if not node.child(index).data(0, Qt.UserRole):
                    node.takeChild(index)
            for title, child_path, child_kind, hidden in entries:
                found = existing.get(os.path.normcase(child_path))
                if found is not None:
                    found.setText(0, title)
                    found.setForeground(0, QColor("#777777" if hidden else "#EEEEEE"))
                else:
                    self.tree.add_node(title, child_path, child_kind, node, hidden)
            node.setData(0, Qt.UserRole + 2, True)
            if self.tree_target:
                self.sync_tree(self.tree_target)
        def failed(message):
            node.setData(0, Qt.UserRole + 2, False)
            node.setToolTip(0, f"{path}\n開けません: {message}")
        self.submit(self.load_pool, enumerate_nodes, ready, failed)

    def sync_tree(self, folder):
        folder = Path(os.path.abspath(os.fspath(folder)))
        self.tree_target = folder
        pending = []
        def child(parent, title, path, kind="folder"):
            node = next((parent.child(i) for i in range(parent.childCount()) if parent.child(i).data(0, Qt.UserRole) and os.path.normcase(parent.child(i).data(0, Qt.UserRole)) == os.path.normcase(str(path))), None)
            return node or self.tree.add_node(title, path, kind, parent)
        blocked = self.tree.blockSignals(True)
        try:
            node = next((root for root in (self.desktop_node, self.pictures_node) if folder.is_relative_to(Path(root.data(0, Qt.UserRole)))), None)
            if node is None and str(folder).startswith("\\\\"):
                server_name, share_name = folder.drive.strip("\\").split("\\", 1)
                self.network_node.setExpanded(True)
                server = child(self.network_node, server_name, "\\\\" + server_name, "server")
                server.setExpanded(True)
                pending.append(server)
                node = child(server, share_name, Path(folder.anchor))
            elif node is None:
                self.pc_node.setExpanded(True)
                node = child(self.pc_node, folder.drive, Path(folder.anchor), "drive")
            base = Path(node.data(0, Qt.UserRole))
            for component in folder.relative_to(base).parts:
                node.setExpanded(True)
                pending.append(node)
                base /= component
                node = child(node, component, base)
            self.tree.setCurrentItem(node)
            self.tree.scrollToItem(node)
        finally:
            self.tree.blockSignals(blocked)
        for ancestor in pending:
            self.expand_tree_node(ancestor)

    def reveal_folder(self):
        if self.current_item():
            folder = self.current_item().path.parent
            self.sync_tree(folder)
            self.bring_main()
            self.show_result(f"フォルダを表示: {folder}")

    def eventFilter(self, target, event):
        if event.type() != QEvent.KeyPress or not isinstance(target, QWidget):
            return super().eventFilter(target, event)
        window = target.window()
        if window not in (self, self.player_window, self.grid_window) or QApplication.activeModalWidget():
            return super().eventFilter(target, event)
        key, modifiers = event.key(), event.modifiers()
        known = key in (Qt.Key_Right, Qt.Key_Left, Qt.Key_Up, Qt.Key_Down, Qt.Key_PageDown, Qt.Key_PageUp, Qt.Key_Backspace, Qt.Key_Home, Qt.Key_End, Qt.Key_Space, Qt.Key_Delete) or Qt.Key_0 <= key <= Qt.Key_5 or (modifiers & Qt.ControlModifier and key in (Qt.Key_O, Qt.Key_G, Qt.Key_D)) or (window == self.grid_window and key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Escape))
        if not known:
            return super().eventFilter(target, event)
        if self.busy:
            return True
        if modifiers & Qt.ControlModifier:
            if key == Qt.Key_O:
                self.choose_folder()
            elif key == Qt.Key_G:
                self.open_grid()
            elif key == Qt.Key_D:
                self.clear_selection()
            return True
        if Qt.Key_0 <= key <= Qt.Key_5:
            self.set_rating(key - Qt.Key_0)
        elif key == Qt.Key_Delete:
            if not event.isAutoRepeat():
                self.delete_selected()
        elif key == Qt.Key_Space:
            if self.current_item():
                self.player_window.toggle_play()
        elif key in (Qt.Key_Home, Qt.Key_End):
            self.move_current(0, edge="first" if key == Qt.Key_Home else "last")
        elif window == self.grid_window and key in (Qt.Key_Return, Qt.Key_Enter):
            self.bring_main()
        elif window == self.grid_window and key == Qt.Key_Escape:
            self.grid_window.close()
        else:
            delta = 1 if key in (Qt.Key_Right, Qt.Key_Down, Qt.Key_PageDown) else -1
            if window == self.grid_window and key in (Qt.Key_Up, Qt.Key_Down) and len(self.selected_paths) < 2:
                width = self.grid_window.list_widget.gridSize().width()
                delta *= max(1, self.grid_window.list_widget.viewport().width() // width)
            self.move_current(delta)
        return True

    def list_menu(self, path, point):
        if self.busy:
            return
        if path not in self.selected_paths:
            self.pick_file(path, update_player=False)
            self.player_window.play_file(path, autoplay=False, show_window=False)
        self.context_menu(point)

    def context_menu(self, point):
        item = self.current_item()
        if self.busy or not item:
            return
        menu = QMenu(self)
        reveal = menu.addAction("フォルダを表示")
        explorer = menu.addAction("エクスプローラーで開く")
        menu.addSeparator()
        video = menu.addAction("動画窓を表示")
        menu.addSeparator()
        actions = {}
        for rating in range(1, 6):
            action = menu.addAction(f"★{rating}\t{rating}")
            action.setCheckable(True)
            action.setChecked(item.rating == rating)
            actions[action] = rating
        clear = menu.addAction("評価を解除\t0")
        clear.setEnabled(any(self.item_by_path[path].rating for path in self.selected_paths))
        menu.addSeparator()
        deselect = menu.addAction("複数選択を解除\tCtrl+D")
        deselect.setEnabled(len(self.selected_paths) > 1)
        menu.addSeparator()
        count = len(self.selected_paths)
        delete = menu.addAction((f"選択中の {count} 個のファイルを削除 (ごみ箱へ)" if count > 1 else "ファイルを削除 (ごみ箱へ)") + "\tDel")
        chosen = menu.exec(point)
        if chosen == reveal:
            self.reveal_folder()
        elif chosen == explorer:
            subprocess.Popen(["explorer.exe", "/select,", str(item.path)])
        elif chosen == video:
            self.show_video()
        elif chosen in actions:
            self.set_rating(actions[chosen])
        elif chosen == clear:
            self.set_rating(0)
        elif chosen == deselect:
            self.clear_selection()
        elif chosen == delete:
            self.delete_selected()

    def begin_operation(self):
        if self.busy:
            return None
        self.busy = True
        self.has_interacted = True
        player = self.player_window.player
        snapshot = (player.source(), player.position(), player.playbackState() == core.QMediaPlayer.PlayingState)
        self.cancel_previews()
        if self.rating_load_task:
            self.rating_load_task.cancelled.set()
        self.player_window.stop_and_release()
        self.toolbar.setEnabled(False)
        self.tree.setEnabled(False)
        self.sheet.setEnabled(False)
        self.list_widget.setEnabled(False)
        self.player_window.setEnabled(False)
        if self.grid_window:
            self.grid_window.setEnabled(False)
        self.update_controls()
        return snapshot

    def finish_operation(self, snapshot, allow_next_play=False):
        self.busy = False
        self.toolbar.setEnabled(True)
        self.tree.setEnabled(True)
        self.sheet.setEnabled(True)
        self.list_widget.setEnabled(True)
        self.player_window.setEnabled(True)
        if self.grid_window:
            self.grid_window.setEnabled(True)
        self.hide_progress()
        current = self.current_item()
        if current:
            source, position, playing = snapshot
            same = source == QUrl.fromLocalFile(str(current.path))
            self.player_window.play_file(current.path, position if same else 0, autoplay=(same and playing) or (not same and allow_next_play), show_window=False)
            if current.path not in self.contact_cache:
                self.request_contact(current.path)
        self.update_controls()
        self.load_ratings()

    def operation_task(self, work, done, snapshot):
        def execute(progress, cancelled):
            # Release all decoder handles on worker threads before modifying files.
            self.preview_pool.waitForDone()
            self.thumb_pool.waitForDone()
            return work(progress, cancelled)
        def failed(message):
            self.finish_operation(snapshot)
            self.show_result(f"操作できませんでした: {message}")
        # The next event-loop turn lets Qt release the player's old media backend.
        QTimer.singleShot(50, lambda: self.submit(self.io_pool, execute, done, failed, lambda value: self.progress("処理中", value)))

    def set_rating(self, rating):
        if self.busy or not self.current_item():
            return
        targets = [item for item in self.filtered if item.path in self.selected_paths]
        previous_path = self.current_item().path
        snapshot = self.begin_operation()
        self.show_result("評価を保存中...")
        def write(progress, cancelled):
            results = []
            for index, item in enumerate(targets):
                ok, message = core.write_rating(item.path, rating)
                actual_rating = core.read_rating(item.path)
                try:
                    modified = item.path.stat().st_mtime
                except OSError:
                    modified = item.mtime
                results.append((item.path, ok, message, actual_rating, modified))
                progress((index + 1, len(targets)))
            return results
        def done(results):
            successes = 0
            errors = []
            for path, ok, message, actual_rating, modified in results:
                self.item_by_path[path].rating = actual_rating
                self.item_by_path[path].mtime = modified
                self.ratings_known.add(path)
                if ok:
                    successes += 1
                else:
                    errors.append(f"{path.name}: {message}")
            self.apply_sort_filter(update_player=False)
            if successes and self.advance_after_rating and len(targets) == 1 and self.current_item() and self.current_item().path == previous_path:
                # Internal movement is allowed only after the write has completed.
                self.busy = False
                self.move_current(1, update_player=False)
                self.busy = True
            self.finish_operation(snapshot)
            label = "未評価" if rating == 0 else f"★{rating}"
            if errors:
                self.show_result(f"評価処理: 完了 {successes} / 全 {len(targets)} 本 / エラー: {'; '.join(errors)}")
            else:
                self.show_result(f"{successes} 本を {label} にしました")
        self.operation_task(write, done, snapshot)

    def delete_selected(self):
        if self.busy or not self.current_item():
            return
        paths = [item.path for item in self.filtered if item.path in self.selected_paths]
        if len(paths) > 1 and QMessageBox.question(self, "ごみ箱へ削除", f"選択中の {len(paths)} 個のファイルをごみ箱へ送りますか？", QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            self.show_result("削除を取り消しました")
            return
        snapshot = self.begin_operation()
        owner = int(self.winId())
        self.show_result("ごみ箱へ移動中...")
        def done(result):
            removed = set(result["removed"])
            self.items = [item for item in self.items if item.path not in removed]
            self.item_by_path = {item.path: item for item in self.items}
            self.selected_paths -= removed
            self.apply_sort_filter(update_player=False)
            self.finish_operation(snapshot, allow_next_play=True)
            remaining = len(paths) - len(removed)
            parts = [f"{len(removed)} 本を削除しました"] if removed else []
            if result["aborted"]:
                parts.append(f"{remaining} 本の削除を取り消しました")
            elif remaining:
                parts.append(f"{remaining} 本は削除できませんでした (コード {result['code']})")
            elif result["code"]:
                parts.append(f"処理エラー (コード {result['code']})")
            self.show_result(" / ".join(parts))
        self.operation_task(lambda progress, cancelled: recycle_files(paths, owner), done, snapshot)

    def transfer(self, move):
        if self.busy or not self.filter_rating or not self.filtered:
            return
        dialog = ExportDialog(self, move)
        if dialog.exec() != QDialog.Accepted:
            return
        destination, identical_overwrite, conflict = dialog.options()
        core.write_setting("exportfolder.txt", str(destination))
        paths = [item.path for item in self.filtered]
        snapshot = self.begin_operation()
        verb = "移動" if move else "コピー"
        self.show_result(f"{verb}中...")
        def done(result):
            if move:
                removed = set(result["done"])
                self.items = [item for item in self.items if item.path not in removed]
                self.item_by_path = {item.path: item for item in self.items}
                self.selected_paths -= removed
                self.apply_sort_filter(update_player=False)
            self.finish_operation(snapshot, allow_next_play=move)
            summary = f"{len(result['done'])} 本を{verb}しました / スキップ {len(result['skipped'])} / 上書き {len(result['overwritten'])} / 失敗 {len(result['failed'])}"
            self.show_result(summary)
            if result["skipped"] or result["overwritten"] or result["failed"]:
                message = QMessageBox(self)
                message.setWindowTitle(f"{verb}の結果")
                message.setText(summary)
                message.setDetailedText("\n\n".join(f"{title}\n" + "\n".join(result[key]) for key, title in (("skipped", "スキップ"), ("overwritten", "上書き"), ("failed", "失敗")) if result[key]))
                message.exec()
        self.operation_task(lambda progress, cancelled: transfer_files(paths, destination, move, identical_overwrite, conflict, progress), done, snapshot)

    def dragEnterEvent(self, event):
        if self.busy or bytes(event.mimeData().data("application/x-scenegrid-source")).decode(errors="ignore") == self.drag_token:
            event.ignore()
        elif event.mimeData().hasUrls() and event.mimeData().urls()[0].isLocalFile():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self.busy or bytes(event.mimeData().data("application/x-scenegrid-source")).decode(errors="ignore") == self.drag_token:
            event.ignore()
            return
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            self.open_target(Path(urls[0].toLocalFile()))
            event.acceptProposedAction()

    def closeEvent(self, event):
        if self.busy:
            self.show_result("処理が完了してから閉じてください")
            event.ignore()
            return
        self.closing = True
        self.generation += 1
        for task in self.tasks:
            task.cancelled.set()
        save_geometry(self, "main_geometry.txt")
        save_geometry(self.player_window, "video_geometry.txt")
        core.write_setting("tree_width.txt", str(self.splitter.sizes()[0]))
        QApplication.instance().removeEventFilter(self)
        self.player_window.stop_and_release()
        self.player_window.close()
        if self.grid_window:
            self.grid_window.close()
        super().closeEvent(event)
