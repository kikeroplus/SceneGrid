import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from scenegrid_core import APP_NAME, read_rating, write_rating
from scenegrid_app import SceneGridWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "scenegrid.ico")))
    window = SceneGridWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
