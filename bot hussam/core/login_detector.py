"""
Detects manual Quotex login success inside WebView using DOM/title heuristics.

No bypass: user authenticates normally; we only observe the page.
"""

from __future__ import annotations

import logging
from typing import Any

from core import js_scripts
from core import selectors as sel
from core.app_state import AppState
from core.browser_controller import BrowserController

log = logging.getLogger("nexora.login")


class LoginDetector:
    def __init__(self, browser: BrowserController, state: AppState) -> None:
        self._browser = browser
        self._state = state

    def probe(self) -> None:
        """Schedule a DOM probe; updates AppState when result returns."""
        self._browser.run_json_script(js_scripts.login_probe_script(), self._on_probe_result)

    def _on_probe_result(self, data: dict[str, Any] | None) -> None:
        if not data:
            return

        url = str(data.get("href") or self._state.current_url)
        title = str(data.get("title") or "")
        has_pw = bool(data.get("hasPasswordField"))
        login_form = bool(data.get("loginFormVisible"))
        balance_like = int(data.get("balanceLikeCount") or 0)

        on_quotex = sel.is_quotex_url(url)
        url_hint = sel.url_suggests_logged_in_area(url)

        # Heuristic: logged in if we're on Quotex and trading-like signals dominate.
        # Strong: URL path suggests platform; medium: balance-like nodes without obvious login form.
        is_in = False
        reasons: list[str] = []

        if not on_quotex:
            self._apply(False, "ليس موقع Quotex", title)
            return

        if url_hint:
            is_in = True
            reasons.append("مسار يشير لمنطقة التداول")

        if balance_like >= 1 and not login_form and not has_pw:
            is_in = True
            reasons.append("عناصر رصيد بدون نموذج دخول")

        if balance_like >= 2:
            is_in = True
            reasons.append("عدة عناصر رصيد")

        # Still on login page: password visible and no balance hints
        if has_pw and balance_like == 0 and not url_hint:
            is_in = False
            reasons.clear()
            reasons.append("نموذج دخول ظاهر")

        status = "مسجل الدخول" if is_in else "غير مسجل / صفحة دخول"
        if reasons:
            status = f"{status} ({'; '.join(reasons)})"

        self._apply(is_in, status, title)

    def _apply(self, is_logged_in: bool, login_text: str, title: str) -> None:
        prev = self._state.is_logged_in
        self._state.is_logged_in = is_logged_in
        self._state.login_status_text = login_text
        if title:
            self._state.page_title = title
        if is_logged_in != prev:
            log.info("Login state: %s — %s", is_logged_in, login_text)
