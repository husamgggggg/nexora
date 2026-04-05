"""
Central application state for the desktop client.

Qt widgets bind to AppState via pyqtSignal updates (single source of truth).

الحقول المطلوبة في المواصفات + حقل إضافي لعرض «حالة المتصفح» في الواجهة.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class AppState(QObject):
    """Holds navigational, auth, balance, and trading UI state."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._current_url: str = ""
        self._page_title: str = ""
        self._is_logged_in: bool = False
        self._login_status_text: str = "لم يتم تسجيل الدخول"
        self._demo_balance: str = "—"
        self._real_balance: str = "—"
        self._account_type: str = "—"
        self._selected_asset: str = ""
        self._trade_amount: str = "1"
        self._last_error: str = ""
        self._is_browser_ready: bool = False
        self._browser_status_text: str = "جاري التهيئة…"

    def _notify(self) -> None:
        self.changed.emit()

    @property
    def current_url(self) -> str:
        return self._current_url

    @current_url.setter
    def current_url(self, v: str) -> None:
        if self._current_url != v:
            self._current_url = v or ""
            self._notify()

    @property
    def page_title(self) -> str:
        return self._page_title

    @page_title.setter
    def page_title(self, v: str) -> None:
        if self._page_title != v:
            self._page_title = v or ""
            self._notify()

    @property
    def is_logged_in(self) -> bool:
        return self._is_logged_in

    @is_logged_in.setter
    def is_logged_in(self, v: bool) -> None:
        if self._is_logged_in != v:
            self._is_logged_in = bool(v)
            self._notify()

    @property
    def login_status_text(self) -> str:
        return self._login_status_text

    @login_status_text.setter
    def login_status_text(self, v: str) -> None:
        if self._login_status_text != v:
            self._login_status_text = v or ""
            self._notify()

    @property
    def demo_balance(self) -> str:
        return self._demo_balance

    @demo_balance.setter
    def demo_balance(self, v: str) -> None:
        if self._demo_balance != v:
            self._demo_balance = v or "—"
            self._notify()

    @property
    def real_balance(self) -> str:
        return self._real_balance

    @real_balance.setter
    def real_balance(self, v: str) -> None:
        if self._real_balance != v:
            self._real_balance = v or "—"
            self._notify()

    @property
    def account_type(self) -> str:
        return self._account_type

    @account_type.setter
    def account_type(self, v: str) -> None:
        if self._account_type != v:
            self._account_type = v or "—"
            self._notify()

    @property
    def selected_asset(self) -> str:
        return self._selected_asset

    @selected_asset.setter
    def selected_asset(self, v: str) -> None:
        if self._selected_asset != v:
            self._selected_asset = v or ""
            self._notify()

    @property
    def trade_amount(self) -> str:
        return self._trade_amount

    @trade_amount.setter
    def trade_amount(self, v: str) -> None:
        if self._trade_amount != v:
            self._trade_amount = v or ""
            self._notify()

    @property
    def last_error(self) -> str:
        return self._last_error

    @last_error.setter
    def last_error(self, v: str) -> None:
        if self._last_error != v:
            self._last_error = v or ""
            self._notify()

    @property
    def is_browser_ready(self) -> bool:
        return self._is_browser_ready

    @is_browser_ready.setter
    def is_browser_ready(self, v: bool) -> None:
        if self._is_browser_ready != v:
            self._is_browser_ready = bool(v)
            self._notify()

    @property
    def browser_status_text(self) -> str:
        return self._browser_status_text

    @browser_status_text.setter
    def browser_status_text(self, v: str) -> None:
        if self._browser_status_text != v:
            self._browser_status_text = v or ""
            self._notify()
