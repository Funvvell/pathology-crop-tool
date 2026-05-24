"""病理裁剪工具 入口点"""

import sys
from PySide6.QtWidgets import QApplication
from liver_portal_crop.app import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("病理裁剪工具")
    app.setOrganizationName("病理裁剪工具")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
