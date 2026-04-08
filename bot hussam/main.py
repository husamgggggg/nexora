#!/usr/bin/env python3
"""
Nexora desktop entry — PySide6 + Qt WebEngine.

الخادم FastAPI و bot.py القديم يبقيان في المستودع؛ مسار التشغيل الجديد هو هذا الملف.

تشغيل من جذر المشروع:
    python main.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Package root on path (core/, ui/)
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtCore import Qt, QCoreApplication
from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # WebEngine / OpenGL sharing (recommended before QApplication)
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

    app = QApplication(sys.argv)
    app.setApplicationName("Nexora")
    app.setStyle("Fusion")

    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
