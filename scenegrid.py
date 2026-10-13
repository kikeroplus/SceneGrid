import sys

from PySide6.QtWidgets import QApplication

from scenegrid_core import APP_NAME, read_rating, write_rating
from scenegrid_app import SceneGridWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    window = SceneGridWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
