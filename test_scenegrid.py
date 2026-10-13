import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import win32api
from PySide6.QtCore import QPoint, QPointF, Qt, QTimer
from PySide6.QtGui import QImage, QWheelEvent
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QMessageBox

import scenegrid_core as core
from scenegrid_app import ExportDialog, SceneGridWindow, restore_geometry
from scenegrid_tasks import drive_info, same_content, transfer_files


class SceneGridStandardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temporary.name).resolve()
        self.settings = self.root / "settings"
        self.settings.mkdir()
        self.settings_patch = patch.object(core, "appdata_dir", return_value=self.settings)
        self.settings_patch.start()
        self.args_patch = patch("sys.argv", ["SceneGrid"])
        self.args_patch.start()
        self.drive_patch = patch("scenegrid_app.drive_info", side_effect=lambda root: (root[:2], QImage()))
        self.drive_patch.start()
        self.window = SceneGridWindow()
        self.window.show()
        self.window.activateWindow()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.wait(lambda: not self.window.busy)
        self.window.close()
        for pool in (self.window.load_pool, self.window.preview_pool, self.window.thumb_pool, self.window.io_pool):
            pool.waitForDone()
        self.app.processEvents()
        self.drive_patch.stop()
        self.args_patch.stop()
        self.settings_patch.stop()
        self.temporary.cleanup()

    def wait(self, condition, seconds=12):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            QTest.qWait(20)
        self.assertTrue(condition(), "Timed out waiting for application state")

    def video(self, name="video2.mp4"):
        path = self.root / name
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (160, 90))
        self.assertTrue(writer.isOpened())
        for index in range(40):
            writer.write(np.full((90, 160, 3), index * 5, dtype=np.uint8))
        writer.release()
        return path

    def load(self, count=1):
        paths = [self.video(name) for name in ("video2.mp4", "video10.mp4", "long_video_name_" * 8 + ".mp4")[:count]]
        self.window.open_folder(self.root)
        self.wait(lambda: len(self.window.ratings_known) == count and len(self.window.sheet.images) == 16)
        self.wait(lambda: len(self.window.thumbnail_cache) == count)
        return paths

    def rate(self, value):
        self.window.set_rating(value)
        self.wait(lambda: not self.window.busy)
        QTest.qWait(150)

    def test_contact_sheet_rating_and_playback_preservation(self):
        path = self.load()[0]
        modified = path.stat().st_mtime_ns
        point = self.window.sheet.mapFromScene(QPointF(140, 436))
        QTest.mouseClick(self.window.sheet.viewport(), Qt.LeftButton, pos=point)
        self.wait(lambda: self.window.player_window.player.position() >= 2100)
        player = self.window.player_window.player
        player.pause()
        QTest.qWait(80)
        position = player.position()
        self.rate(4)
        self.assertEqual(core.read_rating(path), 4)
        self.assertEqual(path.stat().st_mtime_ns, modified)
        self.assertNotEqual(player.playbackState(), QMediaPlayer.PlayingState)
        self.assertAlmostEqual(player.position(), position, delta=120)
        result = self.window.status.currentMessage()
        self.wait(lambda: len(self.window.thumbnail_cache) == 1)
        self.assertEqual(self.window.status.currentMessage(), result)
        self.window.player_window.close()
        self.rate(3)
        self.assertFalse(self.window.player_window.isVisible())
        self.assertNotEqual(player.playbackState(), QMediaPlayer.PlayingState)
        QTest.mouseClick(self.window.sheet.stars.buttons[2], Qt.LeftButton)
        self.wait(lambda: not self.window.busy)
        self.assertEqual(core.read_rating(path), 0)
        player.play()
        QTest.qWait(100)
        position = player.position()
        self.rate(5)
        self.assertEqual(player.playbackState(), QMediaPlayer.PlayingState)
        self.assertGreaterEqual(player.position(), position)
        for value in (1, 2, 0):
            self.rate(value)
            self.assertEqual(core.read_rating(path), value)

    def test_selection_navigation_sort_and_global_keys(self):
        p2, p10, long = self.load(3)
        self.window.pick_file(p2)
        self.window.pick_file(p10, Qt.ControlModifier)
        self.assertEqual(self.window.selected_paths, {p2, p10})
        self.window.pick_file(p2)
        self.assertEqual(self.window.selected_paths, {p2, p10})
        self.window.move_current(1)
        self.assertEqual(self.window.current_item().path, p10)
        self.window.move_current(1)
        self.assertEqual(self.window.current_item().path, p10)
        self.window.move_current(0, edge="first")
        self.assertEqual(self.window.current_item().path, p2)
        self.window.player_window.player.pause()
        self.wait(lambda: self.window.player_window.pending_position is None)
        self.window.player_window.player.setPosition(1000)
        QTest.qWait(80)
        self.window.set_sort("name_desc")
        self.assertEqual(self.window.selected_paths, {p2, p10})
        self.assertEqual(self.window.current_item().path, p2)
        self.assertNotEqual(self.window.player_window.player.playbackState(), QMediaPlayer.PlayingState)
        self.assertGreaterEqual(self.window.player_window.player.position(), 900)
        self.window.tree.setFocus()
        QTest.keyClick(self.window.tree, Qt.Key_Right)
        self.assertEqual(self.window.current_item().path, p2)
        QTest.keyClick(self.window.tree, Qt.Key_Left)
        self.assertEqual(self.window.current_item().path, p10)
        QTest.keyClick(self.window.tree, Qt.Key_D, Qt.ControlModifier)
        self.assertEqual(self.window.selected_paths, {p10})
        QTest.keyClick(self.window.tree, Qt.Key_3)
        self.wait(lambda: not self.window.busy)
        self.assertEqual(core.read_rating(p10), 3)
        self.window.pick_file(p10, Qt.ControlModifier)
        self.assertEqual(self.window.selected_paths, {p10})
        self.window.set_sort("name_asc")
        self.window.pick_file(long)
        self.window.pick_file(p10, Qt.ShiftModifier)
        self.assertEqual(self.window.selected_paths, {long, p2, p10})

    def test_filters_grid_sync_and_fixed_tiles(self):
        p2, p10, long = self.load(3)
        self.window.item_by_path[p2].rating = 2
        self.window.item_by_path[p10].rating = 5
        self.window.pick_file(p2)
        self.window.pick_file(p10, Qt.ControlModifier)
        self.window.set_filter_rating(2)
        self.assertEqual(self.window.selected_paths, {p2, p10})
        self.window.set_filter_mode("only")
        self.assertEqual([item.path for item in self.window.filtered], [p2])
        self.window.set_filter_mode("at_most")
        self.assertEqual({item.path for item in self.window.filtered}, {p2, long})
        self.window.set_filter_rating(2)
        self.assertEqual(len(self.window.filtered), 3)
        self.window.open_grid()
        grid = self.window.grid_window
        self.assertIsNone(grid.parent())
        self.assertEqual(grid.list_widget.count(), 3)
        entry = next(grid.list_widget.item(i) for i in range(3) if Path(grid.list_widget.item(i).data(Qt.UserRole)) == p10)
        QTest.mouseClick(grid.list_widget.viewport(), Qt.LeftButton, pos=grid.list_widget.visualItemRect(entry).center())
        self.assertEqual(self.window.current_item().path, p10)
        self.window.pick_file(p2, Qt.ControlModifier)
        self.assertEqual(grid.list_widget.controller.selected_paths, {p2, p10})
        grid.set_tile_size(320)
        self.assertEqual(grid.list_widget.gridSize().width(), 320)
        self.assertEqual(self.window.list_widget.gridSize().width(), 160)
        long_entry = next(self.window.list_widget.item(i) for i in range(3) if Path(self.window.list_widget.item(i).data(Qt.UserRole)) == long)
        self.assertEqual(long_entry.toolTip(), long.name)
        self.window.activateWindow()
        self.window.setFocus()
        QTest.keyClick(self.window, Qt.Key_G, Qt.ControlModifier)
        self.assertTrue(grid.isVisible())
        QTest.keyClick(grid.list_widget, Qt.Key_Escape)
        self.assertFalse(grid.isVisible())

    def test_folder_loading_is_async_and_stale_results_are_discarded(self):
        self.video()
        second = self.root / "second"
        second.mkdir()
        gate = threading.Event()
        original = Path.iterdir
        def slow(path):
            if path == self.root:
                gate.wait(5)
            return original(path)
        try:
            with patch.object(Path, "iterdir", slow):
                started = time.monotonic()
                self.window.open_folder(self.root)
                self.assertLess(time.monotonic() - started, 0.2)
                self.assertIn("読み込み中", self.window.status.currentMessage())
                self.window.open_folder(second)
                self.wait(lambda: self.window.folder == second)
                gate.set()
                QTest.qWait(200)
                self.assertEqual(self.window.folder, second)
                self.assertEqual(len(self.window.items), 0)
                self.assertIn("対象の動画がありません", self.window.status.currentMessage())
        finally:
            gate.set()

    def test_rating_advance_and_filter_removal_do_not_start_playback(self):
        p2, p10 = self.load(2)
        self.window.pick_file(p2)
        self.window.player_window.player.pause()
        self.window.toggle_advance(True)
        self.rate(3)
        self.assertEqual(self.window.current_item().path, p10)
        self.assertNotEqual(self.window.player_window.player.playbackState(), QMediaPlayer.PlayingState)
        self.window.toggle_advance(False)
        self.window.set_filter_rating(3)
        self.assertEqual(self.window.current_item().path, p2)
        self.rate(0)
        self.assertIsNone(self.window.current_item())
        self.assertNotEqual(self.window.player_window.player.playbackState(), QMediaPlayer.PlayingState)

    def test_copy_conflicts_and_move(self):
        source = self.root / "source"
        target = self.root / "target"
        source.mkdir()
        target.mkdir()
        a = source / "same.mp4"
        b = target / a.name
        a.write_bytes(b"same")
        b.write_bytes(b"same")
        self.assertTrue(same_content(a, b))
        result = transfer_files([a], target, False, False, "rename", lambda value: None)
        self.assertEqual(len(result["skipped"]), 1)
        b.write_bytes(b"different")
        result = transfer_files([a], target, False, False, "rename", lambda value: None)
        self.assertEqual((target / "same (2).mp4").read_bytes(), b"same")
        self.assertEqual(b.read_bytes(), b"different")
        result = transfer_files([a], target, True, False, "overwrite", lambda value: None)
        self.assertEqual(b.read_bytes(), b"same")
        self.assertFalse(a.exists())
        self.assertEqual(len(result["overwritten"]), 1)

    def test_transfer_uses_every_filtered_file_and_runs_in_background(self):
        p2, p10 = self.load(2)
        self.window.pick_file(p2)
        self.window.pick_file(p10, Qt.ControlModifier)
        self.rate(3)
        self.window.clear_selection()
        self.window.set_filter_rating(3)
        output = self.root / "export"
        with patch.object(ExportDialog, "exec", new=lambda dialog: QDialog.Accepted), patch.object(ExportDialog, "options", new=lambda dialog: (output, False, "rename")):
            self.window.transfer(False)
            self.assertTrue(self.window.busy)
            self.wait(lambda: not self.window.busy)
        self.assertTrue((output / p2.name).exists())
        self.assertTrue((output / p10.name).exists())
        output2 = self.root / "moved"
        with patch.object(ExportDialog, "exec", new=lambda dialog: QDialog.Accepted), patch.object(ExportDialog, "options", new=lambda dialog: (output2, False, "rename")):
            self.window.transfer(True)
            self.wait(lambda: not self.window.busy)
        self.assertFalse(p2.exists())
        self.assertFalse(p10.exists())
        self.assertEqual(self.window.filtered, [])

    def test_first_file_does_not_wait_for_ratings(self):
        path = self.video()
        gate = threading.Event()
        original = core.read_rating
        def delayed_read(path):
            gate.wait(5)
            return original(path)
        try:
            with patch.object(core, "read_rating", side_effect=delayed_read):
                self.window.open_target(path)
                self.wait(lambda: self.window.current_item() is not None)
                self.assertEqual(self.window.current_item().path, path)
                self.assertEqual(len(self.window.ratings_known), 0)
                self.assertEqual(len(self.window.thumbnail_cache), 0)
                gate.set()
                self.wait(lambda: len(self.window.ratings_known) == 1)
        finally:
            gate.set()

    def test_delete_uses_selection_and_ignores_repeat(self):
        p2, p10 = self.load(2)
        self.window.pick_file(p2)
        self.window.pick_file(p10, Qt.ControlModifier)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.window.delete_selected()
        self.assertTrue(p2.exists())
        self.assertIn("取り消し", self.window.status.currentMessage())
        def recycled(paths, owner):
            self.assertEqual(set(paths), {p2, p10})
            for path in paths:
                path.unlink()
            return {"code": 0, "aborted": False, "removed": paths}
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), patch("scenegrid_app.recycle_files", side_effect=recycled):
            self.window.delete_selected()
            self.wait(lambda: not self.window.busy)
        self.assertEqual(len(self.window.items), 0)

    def test_zoom_responsive_layout_and_bad_settings(self):
        self.load()
        point = self.window.sheet.viewport().rect().center()
        wheel = QWheelEvent(QPointF(point), QPointF(self.window.sheet.viewport().mapToGlobal(point)), QPoint(), QPoint(0, 120), Qt.NoButton, Qt.ControlModifier, Qt.NoScrollPhase, False)
        QApplication.sendEvent(self.window.sheet.viewport(), wheel)
        self.assertEqual(self.window.sheet.zoom, 1.25)
        QTest.mouseDClick(self.window.sheet.viewport(), Qt.LeftButton, pos=point)
        self.assertEqual(self.window.sheet.zoom, 1)
        output = Path(__file__).parent / "test-output"
        output.mkdir(exist_ok=True)
        for width in (1240, 800):
            self.window.resize(width, 840)
            QTest.qWait(100)
            groups = [self.window.toolbar.layout().itemAt(i).geometry() for i in range(self.window.toolbar.layout().count())]
            self.assertLessEqual(len({rect.y() for rect in groups}), 2)
            for i, rect in enumerate(groups):
                self.assertLessEqual(rect.bottom(), self.window.toolbar.height())
                for other in groups[i + 1:]:
                    self.assertFalse(rect.intersects(other))
            self.window.grab().save(str(output / f"main-{width}.png"))
        core.write_setting("volume.txt", "broken")
        self.assertEqual(core.setting_number("volume.txt", 70, 0, 100), 70)
        core.write_setting("volume.txt", "nan")
        self.assertEqual(core.setting_number("volume.txt", 70, 0, 100, float), 70)

    def test_right_click_never_starts_playback_in_either_list(self):
        p2, p10 = self.load(2)
        self.window.open_grid()
        player = self.window.player_window.player
        for view in self.window.views():
            self.window.pick_file(p2)
            player.pause()
            self.window.player_window.close()
            entry = next(view.item(i) for i in range(view.count()) if Path(view.item(i).data(Qt.UserRole)) == p10)
            QTimer.singleShot(40, lambda: QApplication.activePopupWidget().close() if QApplication.activePopupWidget() else None)
            QTest.mouseClick(view.viewport(), Qt.RightButton, pos=view.visualItemRect(entry).center())
            QTest.qWait(150)
            self.assertEqual(self.window.current_item().path, p10)
            self.assertEqual(self.window.selected_paths, {p10})
            self.assertNotEqual(player.playbackState(), QMediaPlayer.PlayingState)
            self.assertFalse(self.window.player_window.isVisible())
        self.window.pick_file(p2, Qt.ControlModifier)
        player.pause()
        before = self.window.selected_paths.copy()
        QTimer.singleShot(40, lambda: QApplication.activePopupWidget().close() if QApplication.activePopupWidget() else None)
        self.window.list_menu(p10, QPoint())
        self.assertEqual(self.window.selected_paths, before)
        self.assertNotEqual(player.playbackState(), QMediaPlayer.PlayingState)

    def test_grid_rating_filter_buttons_use_saved_ratings(self):
        p2, p10, long = self.load(3)
        self.window.pick_file(p2)
        self.rate(2)
        self.window.pick_file(p10)
        self.rate(5)
        self.window.open_folder(self.root)
        self.wait(lambda: len(self.window.ratings_known) == 3)
        self.window.open_grid()
        grid = self.window.grid_window
        QTest.mouseClick(grid.filter_buttons[2], Qt.LeftButton)
        self.assertEqual({item.path for item in self.window.filtered}, {p2, p10})
        self.assertEqual(grid.list_widget.count(), 2)
        grid.filter_combo.setCurrentIndex(grid.filter_combo.findData("only"))
        self.assertEqual([item.path for item in self.window.filtered], [p2])
        self.assertEqual(self.window.filter_combo.currentData(), "only")
        grid.filter_combo.setCurrentIndex(grid.filter_combo.findData("at_most"))
        self.assertEqual({item.path for item in self.window.filtered}, {p2, long})
        QTest.mouseClick(grid.filter_buttons[2], Qt.LeftButton)
        self.assertEqual(len(self.window.filtered), 3)
        self.assertFalse(self.window.filter_buttons[2].isChecked())
        self.window.filter_combo.setCurrentIndex(self.window.filter_combo.findData("only"))
        QTest.mouseClick(self.window.filter_buttons[5], Qt.LeftButton)
        self.assertEqual([item.path for item in self.window.filtered], [p10])
        self.assertEqual(grid.list_widget.count(), 1)
        self.assertTrue(grid.filter_buttons[5].isChecked())
        self.assertEqual(grid.filter_combo.currentData(), "only")

    def test_reveal_folder_selects_immediately_and_survives_enumeration(self):
        self.load()
        self.window.open_grid()
        self.window.tree.collapseAll()
        def choose_reveal():
            menu = QApplication.activePopupWidget()
            menu.setActiveAction(menu.actions()[0])
            QTest.keyClick(menu, Qt.Key_Return)
        QTimer.singleShot(40, choose_reveal)
        self.window.context_menu(QPoint())
        self.assertEqual(Path(self.window.tree.currentItem().data(0, Qt.UserRole)), self.root)
        self.assertIn(str(self.root), self.window.status.currentMessage())
        parent = self.window.tree.currentItem().parent()
        while parent:
            self.assertTrue(parent.isExpanded())
            parent = parent.parent()
        self.window.load_pool.waitForDone()
        self.app.processEvents()
        self.assertEqual(Path(self.window.tree.currentItem().data(0, Qt.UserRole)), self.root)
        # A known UNC share can be revealed without server discovery or share enumeration.
        with patch.object(self.window, "expand_tree_node", new=lambda node: None):
            unc = Path(r"\\test-server\share\folder\nested")
            self.window.sync_tree(unc)
        node = self.window.tree.currentItem()
        self.assertEqual(Path(node.data(0, Qt.UserRole)), unc)
        self.assertEqual(node.parent().text(0), "folder")
        self.assertEqual(node.parent().parent().text(0), "share")
        self.assertEqual(node.parent().parent().parent().text(0), "test-server")

    def test_filtered_open_waits_for_ratings_without_playing_excluded_files(self):
        import threading
        p2, p10 = self.video("video2.mp4"), self.video("video10.mp4")
        self.assertTrue(core.write_rating(p10, 5)[0])
        self.window.filter_rating = 5
        self.window.filter_mode = "only"
        gate = threading.Event()
        entered = threading.Event()
        original = core.read_rating
        def delayed(path):
            entered.set()
            gate.wait(5)
            return original(path)
        try:
            with patch.object(core, "read_rating", side_effect=delayed):
                self.window.open_target(p10)
                self.wait(entered.is_set)
                self.assertIsNone(self.window.current_item())
                self.assertEqual(self.window.filtered, [])
                self.assertTrue(self.window.player_window.player.source().isEmpty())
                self.assertFalse(self.window.player_window.controls_widget.isVisible())
                self.assertIn("読み込み中", self.window.status.currentMessage())
                gate.set()
                self.wait(lambda: len(self.window.ratings_known) == 2)
            self.assertEqual(self.window.current_item().path, p10)
            self.assertEqual([item.path for item in self.window.filtered], [p10])
        finally:
            gate.set()

    def test_modification_time_restore_failure_is_reported_and_rating_stays_in_sync(self):
        path = self.load()[0]
        self.window.player_window.player.pause()
        with patch.object(core.os, "utime", side_effect=PermissionError("restore blocked")):
            self.rate(4)
        self.assertEqual(core.read_rating(path), 4)
        self.assertEqual(self.window.current_item().rating, 4)
        self.assertIn("更新日時を戻せません", self.window.status.currentMessage())
        self.assertIn("完了 0", self.window.status.currentMessage())

    def test_zoom_double_click_only_resets_zoom_and_single_click_still_seeks(self):
        self.load()
        player = self.window.player_window.player
        player.pause()
        QTest.qWait(80)
        sheet = self.window.sheet
        sheet.zoom = 1.25
        sheet.scale(1.25, 1.25)
        point = sheet.mapFromScene(QPointF(140, 80))
        seeks = []
        sheet.seek.connect(seeks.append)
        QTest.mouseClick(sheet.viewport(), Qt.LeftButton, pos=point)
        QTest.mouseDClick(sheet.viewport(), Qt.LeftButton, pos=point)
        QTest.mouseRelease(sheet.viewport(), Qt.LeftButton, pos=point)
        QTest.qWait(QApplication.doubleClickInterval() + 50)
        self.assertEqual(sheet.zoom, 1)
        self.assertEqual(seeks, [])
        self.assertNotEqual(player.playbackState(), QMediaPlayer.PlayingState)
        sheet.zoom = 1.25
        sheet.scale(1.25, 1.25)
        point = sheet.mapFromScene(QPointF(140, 80))
        QTest.mouseClick(sheet.viewport(), Qt.LeftButton, pos=point)
        self.wait(lambda: len(seeks) == 1)

    def test_invalid_window_geometry_uses_default_size(self):
        import json
        for value in ({"x": 0, "y": 0, "w": 10**40, "h": 600}, {"x": float("inf"), "y": 0, "w": 800, "h": 600}, {"x": 0, "y": 0, "w": 10**8, "h": 600}):
            with self.subTest(value=value):
                core.write_setting("audit_geometry.txt", json.dumps(value))
                restore_geometry(self.window, "audit_geometry.txt", (800, 600))
                self.assertEqual(self.window.width(), 800)
                self.assertEqual(self.window.height(), 600)

    def test_empty_selection_hides_video_controls(self):
        self.load()
        self.assertTrue(self.window.player_window.controls_widget.isVisible())
        self.window.set_filter_rating(5)
        self.assertIsNone(self.window.current_item())
        self.assertFalse(self.window.player_window.controls_widget.isVisible())
        self.assertEqual(self.window.player_window.windowTitle(), "動画")
        self.window.player_window.toggle_play()
        self.assertNotEqual(self.window.player_window.player.playbackState(), QMediaPlayer.PlayingState)

    def test_partial_delete_reports_remaining_files_and_uses_neutral_wording(self):
        p2, p10 = self.load(2)
        self.window.pick_file(p2)
        self.window.pick_file(p10, Qt.ControlModifier)
        def partial(paths, owner):
            p2.unlink()
            return {"code": 5, "aborted": False, "removed": [p2]}
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), patch("scenegrid_app.recycle_files", side_effect=partial):
            self.window.delete_selected()
            self.wait(lambda: not self.window.busy)
        message = self.window.status.currentMessage()
        self.assertIn("1 本を削除しました", message)
        self.assertIn("1 本は削除できませんでした", message)
        self.assertNotIn("ごみ箱へ移動しました", message)
        self.assertTrue(p10.exists())

    def test_known_tree_node_receives_hidden_attribute_after_enumeration(self):
        folder = self.root / "hidden-folder"
        folder.mkdir()
        self.window.sync_tree(folder)
        node = self.window.tree.currentItem()
        parent = node.parent()
        original_attributes = win32api.GetFileAttributes(str(folder))
        try:
            win32api.SetFileAttributes(str(folder), original_attributes | 2)
            self.window.load_pool.waitForDone()
            self.app.processEvents()
            parent.setData(0, Qt.UserRole + 2, False)
            self.window.expand_tree_node(parent)
            self.wait(lambda: parent.data(0, Qt.UserRole + 2) is True)
            self.assertEqual(node.foreground(0).color().name(), "#777777")
        finally:
            win32api.SetFileAttributes(str(folder), original_attributes)

    def test_drive_info_copies_windows_shell_icon(self):
        from scenegrid_tasks import win32gui
        root = os.environ.get("SystemDrive", "C:") + "\\"
        with patch("scenegrid_tasks.win32gui.DestroyIcon", wraps=win32gui.DestroyIcon) as destroy:
            title, image = drive_info(root)
        self.assertIn(root[:2], title)
        self.assertFalse(image.isNull())
        destroy.assert_called_once()

    def test_native_contact_sheet_preserves_dimensions_above_old_limit(self):
        frame = np.zeros((2, 9000, 3), dtype=np.uint8)
        class Capture:
            def isOpened(self): return True
            def get(self, key): return 1
            def set(self, key, value): return True
            def read(self): return True, frame
            def release(self): pass
        with patch.object(core.cv2, "VideoCapture", return_value=Capture()):
            images = core.extract_contact_sheet(self.root / "native.mp4", None, count=1)
        self.assertEqual(images[0].width(), 9000)
        self.assertEqual(images[0].height(), 2)


if __name__ == "__main__":
    unittest.main()
