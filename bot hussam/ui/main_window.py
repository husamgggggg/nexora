"""
Main desktop window: toolbar, WebEngine view, status sidebar, trading panel.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtWebEngineWidgets import QWebEngineView

from core.app_state import AppState
from core.balance_reader import BalanceReader
from core.browser_controller import BrowserController
from core.login_detector import LoginDetector
from core import selectors as sel
from core.trade_executor import TradeExecutor

log = logging.getLogger("nexora.ui")

# Dark palette — minimal, readable
_STYLESHEET = """
QMainWindow, QWidget { background-color: #1e1e1e; color: #e0e0e0; }
QLineEdit, QPushButton {
    background-color: #2d2d2d;
    border: 1px solid #404040;
    border-radius: 4px;
    padding: 6px 10px;
    min-height: 22px;
}
QLineEdit:focus { border-color: #5a9fd4; }
QPushButton:hover { background-color: #3d3d3d; }
QPushButton:pressed { background-color: #505050; }
QPushButton#buyBtn { background-color: #1b4332; border-color: #2d6a4f; }
QPushButton#sellBtn { background-color: #432323; border-color: #6a2d2d; }
QLabel#sectionTitle { font-weight: 600; color: #a0a0a0; font-size: 11px; }
QFrame#sidePanel { background-color: #252526; border-left: 1px solid #333; }
"""


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Nexora — Quotex (WebView)")
        self.resize(1280, 800)
        self.setStyleSheet(_STYLESHEET)

        self._state = AppState()
        self._view = QWebEngineView()
        self._browser = BrowserController(self._view, self._state)
        self._login_detector = LoginDetector(self._browser, self._state)
        self._balance_reader = BalanceReader(self._browser, self._state)
        self._trade_executor = TradeExecutor(self._browser, self._state)

        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("https://…")
        self._url_edit.setText(sel.DEFAULT_QUOTEX_URL)

        self._lbl_title = QLabel("—")
        self._lbl_title.setWordWrap(True)
        self._lbl_login = QLabel("—")
        self._lbl_browser = QLabel("—")
        self._lbl_demo = QLabel("—")
        self._lbl_real = QLabel("—")
        self._lbl_acct = QLabel("—")
        self._lbl_error = QLabel("")
        self._lbl_error.setStyleSheet("color: #c94c4c;")
        self._lbl_error.setWordWrap(True)

        self._asset_edit = QLineEdit()
        self._asset_edit.setPlaceholderText("EURUSD …")
        self._amount_edit = QLineEdit()
        self._amount_edit.setText("1")

        self._build_ui()
        self._wire_signals()

        self._state.changed.connect(self._on_state_changed)
        self._was_logged_in: bool = False

        # Periodic login probe on Quotex pages (DOM may update after SPA load)
        self._login_timer = QTimer()
        self._login_timer.setInterval(2800)
        self._login_timer.timeout.connect(self._maybe_probe_login)
        self._login_timer.start()

        self._browser.load_url(sel.DEFAULT_QUOTEX_URL)

    def _section_label(self, text: str) -> QLabel:
        lb = QLabel(text)
        lb.setObjectName("sectionTitle")
        return lb

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # --- Top bar -----------------------------------------------------
        top = QHBoxLayout()
        btn_go = QPushButton("فتح الرابط")
        btn_go.clicked.connect(self._on_go_clicked)
        btn_qx = QPushButton("فتح Quotex")
        btn_qx.clicked.connect(self._on_open_quotex)
        top.addWidget(QLabel("الرابط:"), 0)
        top.addWidget(self._url_edit, 1)
        top.addWidget(btn_go, 0)
        top.addWidget(btn_qx, 0)
        root.addLayout(top)

        meta = QGridLayout()
        meta.addWidget(QLabel("عنوان الصفحة:"), 0, 0)
        meta.addWidget(self._lbl_title, 0, 1)
        meta.addWidget(QLabel("حالة الدخول:"), 1, 0)
        meta.addWidget(self._lbl_login, 1, 1)
        meta.addWidget(QLabel("المتصفح:"), 2, 0)
        meta.addWidget(self._lbl_browser, 2, 1)
        root.addLayout(meta)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._view)
        splitter.setStretchFactor(0, 4)

        side = QFrame()
        side.setObjectName("sidePanel")
        side.setMinimumWidth(260)
        side_l = QVBoxLayout(side)
        side_l.setSpacing(10)

        side_l.addWidget(self._section_label("الرصيد والحساب"))
        g = QGridLayout()
        g.addWidget(QLabel("Demo Balance"), 0, 0)
        g.addWidget(self._lbl_demo, 0, 1)
        g.addWidget(QLabel("Real Balance"), 1, 0)
        g.addWidget(self._lbl_real, 1, 1)
        g.addWidget(QLabel("Account Type"), 2, 0)
        g.addWidget(self._lbl_acct, 2, 1)
        side_l.addLayout(g)

        side_l.addWidget(self._section_label("تداول (تجريبي)"))
        tg = QGridLayout()
        tg.addWidget(QLabel("Asset"), 0, 0)
        tg.addWidget(self._asset_edit, 0, 1)
        tg.addWidget(QLabel("Amount"), 1, 0)
        tg.addWidget(self._amount_edit, 1, 1)
        side_l.addLayout(tg)

        row_btn = QHBoxLayout()
        buy = QPushButton("شراء")
        buy.setObjectName("buyBtn")
        sell = QPushButton("بيع")
        sell.setObjectName("sellBtn")
        buy.clicked.connect(self._on_buy)
        sell.clicked.connect(self._on_sell)
        row_btn.addWidget(buy)
        row_btn.addWidget(sell)
        side_l.addLayout(row_btn)

        side_l.addWidget(self._section_label("آخر خطأ"))
        side_l.addWidget(self._lbl_error)
        side_l.addStretch(1)

        splitter.addWidget(side)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    def _wire_signals(self) -> None:
        page = self._view.page()
        page.loadFinished.connect(self._on_load_finished)

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            self._login_detector.probe()
            if self._state.is_logged_in:
                self._balance_reader.read_once()

    def _maybe_probe_login(self) -> None:
        if not sel.is_quotex_url(self._state.current_url):
            return
        if not self._state.is_browser_ready:
            return
        self._login_detector.probe()

    def _on_state_changed(self) -> None:
        self._lbl_title.setText(self._state.page_title or "—")
        self._lbl_login.setText(
            f"{'نعم' if self._state.is_logged_in else 'لا'} — {self._state.login_status_text}"
        )
        self._lbl_browser.setText(self._state.browser_status_text)
        self._lbl_demo.setText(self._state.demo_balance)
        self._lbl_real.setText(self._state.real_balance)
        self._lbl_acct.setText(self._state.account_type)
        self._lbl_error.setText(self._state.last_error or "—")
        # Sync URL bar from navigations
        if (
            self._state.current_url
            and not self._url_edit.hasFocus()
            and self._url_edit.text() != self._state.current_url
        ):
            self._url_edit.blockSignals(True)
            self._url_edit.setText(self._state.current_url)
            self._url_edit.blockSignals(False)

        li = self._state.is_logged_in
        prev = self._was_logged_in
        self._was_logged_in = li
        if li and not prev:
            self._balance_reader.start_polling()
        elif not li and prev:
            self._balance_reader.stop_polling()
            self._state.demo_balance = "—"
            self._state.real_balance = "—"
            self._state.account_type = "—"

    def _on_go_clicked(self) -> None:
        self._browser.load_url(self._url_edit.text())

    def _on_open_quotex(self) -> None:
        self._url_edit.setText(sel.DEFAULT_QUOTEX_URL)
        self._browser.load_url(sel.DEFAULT_QUOTEX_URL)

    def _parse_amount(self) -> float | None:
        raw = self._amount_edit.text().strip().replace(",", ".")
        try:
            v = float(raw)
            if v <= 0:
                return None
            return v
        except ValueError:
            return None

    def _on_buy(self) -> None:
        self._state.selected_asset = self._asset_edit.text().strip()
        self._state.trade_amount = self._amount_edit.text().strip()
        amt = self._parse_amount()
        if amt is None:
            QMessageBox.warning(self, "مبلغ", "أدخل مبلغاً رقماً موجباً.")
            return

        def _done(ok: bool, msg: str) -> None:
            if not ok:
                QMessageBox.warning(self, "تنفيذ", msg)
            else:
                log.info("%s", msg)

        self._trade_executor.execute_buy(amt, _done)

    def _on_sell(self) -> None:
        self._state.selected_asset = self._asset_edit.text().strip()
        self._state.trade_amount = self._amount_edit.text().strip()
        amt = self._parse_amount()
        if amt is None:
            QMessageBox.warning(self, "مبلغ", "أدخل مبلغاً رقماً موجباً.")
            return

        def _done(ok: bool, msg: str) -> None:
            if not ok:
                QMessageBox.warning(self, "تنفيذ", msg)
            else:
                log.info("%s", msg)

        self._trade_executor.execute_sell(amt, _done)
