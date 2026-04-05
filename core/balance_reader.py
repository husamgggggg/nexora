"""
Polls balance-related DOM text after login (2–5 s interval).

Failures log a warning and keep the last good values.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from PySide6.QtCore import QTimer

from core import js_scripts
from core import selectors as sel
from core.app_state import AppState
from core.browser_controller import BrowserController

log = logging.getLogger("nexora.balance")


class BalanceReader:
    def __init__(self, browser: BrowserController, state: AppState) -> None:
        self._browser = browser
        self._state = state
        self._timer = QTimer()
        self._timer.setInterval(3500)
        self._timer.timeout.connect(self._tick)

    def start_polling(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop_polling(self) -> None:
        self._timer.stop()

    def set_interval_ms(self, ms: int) -> None:
        self._timer.setInterval(max(2000, min(ms, 5000)))

    def read_once(self) -> None:
        if not self._state.is_logged_in:
            return
        self._browser.run_json_script(js_scripts.balance_read_script(), self._on_data)

    def _tick(self) -> None:
        if not self._state.is_logged_in:
            return
        if not self._state.is_browser_ready:
            return
        self.read_once()

    def _on_data(self, data: dict[str, Any] | None) -> None:
        if not data:
            log.warning("Balance read returned no data; keeping last values")
            return

        try:
            demo_ctx = str(data.get("demoContext") or "")
            real_ctx = str(data.get("realContext") or "")
            snippets: list[str] = []
            raw_snips = data.get("balanceSnippets")
            if isinstance(raw_snips, list):
                snippets = [str(x) for x in raw_snips if x]

            acct = str(data.get("accountType") or "")
            if not acct or acct == "unknown":
                if demo_ctx and real_ctx:
                    acct = "mixed"
                elif demo_ctx:
                    acct = "demo"
                elif real_ctx:
                    acct = "real"
                else:
                    acct = self._state.account_type

            demo_bal = self._pick_balance(demo_ctx, snippets, prefer_demo=True)
            real_bal = self._pick_balance(real_ctx, snippets, prefer_demo=False)

            if demo_bal:
                self._state.demo_balance = demo_bal
            if real_bal:
                self._state.real_balance = real_bal
            if acct and acct != "—":
                self._state.account_type = acct
        except Exception:
            log.warning("Balance parse failed; keeping last values", exc_info=True)

    def _pick_balance(
        self,
        context: str,
        snippets: list[str],
        *,
        prefer_demo: bool,
    ) -> str | None:
        for text in ([context] if context else []) + snippets:
            num = sel.extract_number_from_text(text)
            if num:
                if prefer_demo and re.search(r"demo", text, re.I):
                    return num
                if not prefer_demo and re.search(r"real", text, re.I):
                    return num
        for text in snippets:
            num = sel.extract_number_from_text(text)
            if num:
                return num
        return sel.extract_number_from_text(context)
