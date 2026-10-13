import ctypes
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QPoint, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDrag, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QLayout,
    QListWidget, QListWidgetItem, QStyle, QStyledItemDelegate, QToolButton,
    QTreeWidget, QTreeWidgetItem, QWidget,
)

from scenegrid_core import natural_key, seconds_text


class FlowLayout(QLayout):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries = []
        self.setContentsMargins(6, 4, 6, 4)
        self.setSpacing(5)

    def addItem(self, item):
        self.entries.append(item)

    def count(self):
        return len(self.entries)

    def itemAt(self, index):
        return self.entries[index] if 0 <= index < len(self.entries) else None

    def takeAt(self, index):
        return self.entries.pop(index) if 0 <= index < len(self.entries) else None

    def expandingDirections(self):
        return Qt.Orientations()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self.arrange(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self.arrange(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for entry in self.entries:
            size = size.expandedTo(entry.minimumSize())
        left, top, right, bottom = self.getContentsMargins()
        return size + QSize(left + right, top + bottom)

    def arrange(self, rect, measure):
        left, top, right, bottom = self.getContentsMargins()
        area = rect.adjusted(left, top, -right, -bottom)
        x, y, height = area.x(), area.y(), 0
        for entry in self.entries:
            size = entry.sizeHint()
            if x + size.width() > area.right() + 1 and height:
                x, y, height = area.x(), y + height + self.spacing(), 0
            if not measure:
                entry.setGeometry(QRect(QPoint(x, y), size))
            x += size.width() + self.spacing()
            height = max(height, size.height())
        return y + height - rect.y() + bottom


class StarStrip(QWidget):
    rated = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rating = 0
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.buttons = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(0)
        for value in range(1, 6):
            button = QToolButton()
            button.setText("★")
            button.setFixedSize(32, 32)
            button.setFocusPolicy(Qt.NoFocus)
            button.setProperty("star", value)
            button.installEventFilter(self)
            button.clicked.connect(lambda checked=False, v=value: self.rated.emit(0 if self.rating == v else v))
            button.setToolTip(f"★{value} / 同じ評価なら解除")
            layout.addWidget(button)
            self.buttons.append(button)
        self.setStyleSheet("StarStrip { background: rgba(0,0,0,170); } QToolButton { border: 0; background: transparent; font-size: 24px; padding: 0; }")

    def set_rating(self, rating):
        self.rating = rating
        self.draw(rating)

    def draw(self, value):
        for index, button in enumerate(self.buttons):
            button.setStyleSheet(f"color: {'#FFC107' if index < value else '#777777'};")

    def eventFilter(self, target, event):
        if event.type() == QEvent.Enter:
            self.draw(target.property("star"))
        elif event.type() == QEvent.Leave:
            self.draw(self.rating)
        return super().eventFilter(target, event)


class ContactSheetView(QGraphicsView):
    seek = Signal(int)
    navigate = Signal(int)
    native_requested = Signal()
    zoom_changed = Signal()
    menu_requested = Signal(object)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QColor("#111111"))
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.images = []
        self.zoom = 1.0
        self.press_position = QPoint()
        self.suppress_release = False
        self.pending_seek = None
        self.seek_timer = QTimer(self)
        self.seek_timer.setSingleShot(True)
        self.seek_timer.timeout.connect(self.commit_seek)
        self.info_text = ""
        self.info = QLabel(self.viewport())
        self.info.setStyleSheet("background: rgba(0,0,0,170); padding: 6px 9px; color: #EEEEEE;")
        self.stars = StarStrip(self.viewport())
        self.stars.resize(self.stars.sizeHint())
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(lambda p: self.menu_requested.emit(self.mapToGlobal(p)))
        self.message("まだ何も開いていません")

    def message(self, text):
        self.seek_timer.stop()
        self.pending_seek = None
        self.images = []
        self.scene().clear()
        label = self.scene().addText(text)
        label.setDefaultTextColor(QColor("#AAAAAA"))
        self.scene().setSceneRect(0, 0, 1120, 712)
        label.setPos((1120 - label.boundingRect().width()) / 2, (712 - label.boundingRect().height()) / 2)
        self.fit()

    def set_images(self, images, keep_zoom=False):
        if not keep_zoom:
            self.seek_timer.stop()
            self.pending_seek = None
        self.images = images
        self.scene().clear()
        self.scene().setSceneRect(0, 0, 1120, 712)
        for index in range(16):
            x, y = index % 4 * 280, index // 4 * 178
            self.scene().addRect(x + 4, y + 4, 272, 170, QPen(QColor("#444444")), QColor("#080808"))
            image = images[index] if index < len(images) else None
            if image is not None and not image.isNull():
                pixmap = QPixmap.fromImage(image)
                tile = self.scene().addPixmap(pixmap)
                scale = min(264 / pixmap.width(), 146 / pixmap.height())
                tile.setScale(scale)
                tile.setPos(x + (280 - pixmap.width() * scale) / 2, y + 8 + (146 - pixmap.height() * scale) / 2)
                caption = seconds_text(int(image.text("position_ms") or 0))
            else:
                caption = "取得できません"
            label = self.scene().addText(caption)
            label.setDefaultTextColor(QColor("#BBBBBB"))
            label.setPos(x + 8, y + 149)
        if not keep_zoom:
            self.zoom = 1
            self.fit()

    def set_info(self, text):
        self.info_text = text
        self.info.setToolTip(text)
        self.position_badges()

    def position_badges(self):
        width = max(1, self.viewport().width() - 20)
        self.info.setText(self.info.fontMetrics().elidedText(self.info_text, Qt.ElideRight, max(1, width - 20)))
        self.info.resize(min(width, self.info.sizeHint().width()), self.info.sizeHint().height())
        self.info.move(8, max(0, self.viewport().height() - self.info.height() - 8))
        self.info.setVisible(bool(self.info_text))
        self.stars.move(max(0, self.viewport().width() - self.stars.width() - 8), 8)
        self.stars.raise_()
        self.info.raise_()

    def fit(self):
        self.resetTransform()
        self.fitInView(self.sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.zoom == 1:
            self.fit()
        self.position_badges()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            old = self.zoom
            self.zoom = min(16, max(1, self.zoom * (1.25 if event.angleDelta().y() > 0 else 1 / 1.25)))
            if self.zoom == 1:
                self.fit()
            else:
                self.scale(self.zoom / old, self.zoom / old)
            if old == 1 and self.zoom > 1:
                self.native_requested.emit()
            self.zoom_changed.emit()
        else:
            self.navigate.emit(-1 if event.angleDelta().y() > 0 else 1)
        event.accept()

    def mousePressEvent(self, event):
        self.press_position = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        suppressed = self.suppress_release
        self.suppress_release = False
        if not suppressed and event.button() == Qt.LeftButton and (event.position().toPoint() - self.press_position).manhattanLength() < QApplication.startDragDistance():
            position = self.mapToScene(event.position().toPoint())
            column, row = int(position.x() // 280), int(position.y() // 178)
            if 0 <= column < 4 and 0 <= row < 4:
                index = row * 4 + column
                if index < len(self.images) and not self.images[index].isNull():
                    position_ms = int(self.images[index].text("position_ms") or 0)
                    if self.zoom > 1:
                        self.pending_seek = position_ms
                        self.seek_timer.start(QApplication.doubleClickInterval())
                    else:
                        self.seek.emit(position_ms)
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.seek_timer.stop()
        self.pending_seek = None
        self.suppress_release = True
        self.zoom = 1
        self.fit()
        self.zoom_changed.emit()
        event.accept()

    def commit_seek(self):
        if self.pending_seek is not None:
            position_ms = self.pending_seek
            self.pending_seek = None
            self.seek.emit(position_ms)


class VideoDelegate(QStyledItemDelegate):
    def __init__(self, view):
        super().__init__(view)
        self.view = view

    def sizeHint(self, option, index):
        return self.view.tile_size()

    def paint(self, painter, option, index):
        controller = self.view.controller
        path = Path(index.data(Qt.UserRole))
        item = controller.item_by_path.get(path)
        if item is None:
            return
        current = controller.current_item()
        displayed = current and current.path == path
        selected = path in controller.selected_paths
        rect = option.rect.adjusted(3, 3, -3, -3)
        painter.save()
        painter.fillRect(rect, QColor("#303030" if selected else "#181818"))
        painter.setPen(QPen(QColor("#FFE84D" if displayed else "#E0A000"), 4 if displayed else 2))
        if selected or displayed:
            painter.drawRect(rect.adjusted(2, 2, -2, -2))
        image_rect = rect.adjusted(9, 8, -9, -44)
        pixmap = controller.thumbnail_for(path, self.view.width_choice)
        if pixmap and not pixmap.isNull():
            scaled = pixmap.scaled(image_rect.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            painter.drawPixmap(image_rect.center().x() - scaled.width() // 2, image_rect.center().y() - scaled.height() // 2, scaled)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(image_rect.adjusted(3, 0, 0, -2), Qt.AlignLeft | Qt.AlignBottom, "▶")
        painter.setPen(QColor("#FFC107" if item.rating else "#888888"))
        painter.drawText(QRect(rect.x() + 9, rect.bottom() - 39, rect.width() - 18, 18), Qt.AlignCenter, "★" * item.rating if item.rating else "未評価")
        painter.setPen(QColor("#EEEEEE"))
        name = option.fontMetrics.elidedText(item.name, Qt.ElideRight, rect.width() - 18)
        painter.drawText(QRect(rect.x() + 9, rect.bottom() - 21, rect.width() - 18, 18), Qt.AlignCenter, name)
        painter.restore()


class VideoList(QListWidget):
    picked = Signal(object, object)
    menu_requested = Signal(object, object)
    activated_file = Signal()
    size_step = Signal(int)

    def __init__(self, controller, grid=False):
        super().__init__()
        self.controller = controller
        self.grid_mode = grid
        self.width_choice = 160
        self.press_position = QPoint()
        self.setViewMode(QListWidget.IconMode)
        self.setFlow(QListWidget.LeftToRight)
        self.setWrapping(grid)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setSelectionMode(QListWidget.NoSelection)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.ElideRight)
        self.setItemDelegate(VideoDelegate(self))
        self.setAcceptDrops(False)
        self.set_size(160)

    def tile_size(self):
        return QSize(self.width_choice, int(self.width_choice * 9 / 16) + 50)

    def set_size(self, width):
        self.width_choice = width
        self.setGridSize(self.tile_size())
        if not self.grid_mode:
            self.setFixedHeight(self.tile_size().height() + 22)
        self.doItemsLayout()

    def populate(self, items):
        self.clear()
        for item in items:
            entry = QListWidgetItem(item.name)
            entry.setData(Qt.UserRole, str(item.path))
            entry.setToolTip(item.name)
            self.addItem(entry)
        self.viewport().update()

    def mousePressEvent(self, event):
        self.press_position = event.position().toPoint()
        entry = self.itemAt(self.press_position)
        if entry:
            path = Path(entry.data(Qt.UserRole))
            if event.button() == Qt.RightButton:
                self.menu_requested.emit(path, self.viewport().mapToGlobal(self.press_position))
            elif event.button() == Qt.LeftButton:
                self.picked.emit(path, event.modifiers())
        event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.LeftButton and (event.position().toPoint() - self.press_position).manhattanLength() >= QApplication.startDragDistance():
            if self.controller.busy or not self.controller.selected_paths:
                return
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(item.path)) for item in self.controller.filtered if item.path in self.controller.selected_paths])
            mime.setData("application/x-scenegrid-source", self.controller.drag_token.encode())
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec(Qt.CopyAction, Qt.CopyAction)
        else:
            super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self.itemAt(event.position().toPoint()):
            self.activated_file.emit()
        event.accept()

    def wheelEvent(self, event):
        if self.grid_mode and event.modifiers() & Qt.ControlModifier:
            self.size_step.emit(1 if event.angleDelta().y() > 0 else -1)
        elif self.grid_mode:
            super().wheelEvent(event)
            return
        else:
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - event.angleDelta().y())
        event.accept()


class FolderTree(QTreeWidget):
    open_requested = Signal(object)
    expand_requested = Signal(object)

    def __init__(self):
        super().__init__()
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.itemExpanded.connect(lambda node: self.expand_requested.emit(node))
        self.itemClicked.connect(self.clicked_folder)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.context_menu)

    def add_node(self, title, path=None, kind="folder", parent=None, hidden=False):
        node = QTreeWidgetItem([title])
        node.setData(0, Qt.UserRole, str(path) if path else "")
        node.setData(0, Qt.UserRole + 1, kind)
        node.setData(0, Qt.UserRole + 2, False)
        node.setToolTip(0, str(path) if path else title)
        icon = QStyle.SP_DriveHDIcon if kind == "drive" else QStyle.SP_DirIcon
        node.setIcon(0, self.style().standardIcon(icon))
        if hidden:
            node.setForeground(0, QColor("#777777"))
        (parent.addChild if parent else self.addTopLevelItem)(node)
        node.addChild(QTreeWidgetItem(["読み込み中..."]))
        return node

    def clicked_folder(self, node):
        path = node.data(0, Qt.UserRole)
        if path and node.data(0, Qt.UserRole + 1) not in ("server", "network", "pc"):
            self.open_requested.emit(Path(path))

    def mousePressEvent(self, event):
        if event.button() != Qt.RightButton:
            super().mousePressEvent(event)

    def context_menu(self, point):
        from PySide6.QtWidgets import QMenu
        import os
        node = self.itemAt(point)
        if not node:
            return
        path = node.data(0, Qt.UserRole)
        menu = QMenu(self)
        action = menu.addAction("エクスプローラーで開く")
        action.setEnabled(bool(path) or node.data(0, Qt.UserRole + 1) in ("pc", "network"))
        if menu.exec(self.viewport().mapToGlobal(point)) == action:
            target = path or ("shell:MyComputerFolder" if node.data(0, Qt.UserRole + 1) == "pc" else "shell:NetworkPlacesFolder")
            try:
                os.startfile(target)
            except OSError:
                pass
