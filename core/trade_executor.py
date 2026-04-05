"""
Trade execution via in-page JavaScript (same session as WebView).

Selectors are best-effort; real Quotex UI may require tuning in selectors.py.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from core import js_scripts
from core.app_state import AppState
from core.browser_controller import BrowserController
from core import selectors as sel

log = logging.getLogger("nexora.trade")


class TradeExecutor:
    def __init__(self, browser: BrowserController, state: AppState) -> None:
        self._browser = browser
        self._state = state

    def _validate(self, amount: float) -> str | None:
        if not self._state.is_logged_in:
            return "المستخدم غير مسجل الدخول"
        if not self._state.is_browser_ready:
            return "المتصفح غير جاهز"
        if amount <= 0 or amount != amount:  # NaN check
            return "مبلغ غير صالح"
        url = self._state.current_url
        if not sel.is_quotex_url(url):
            return "الصفحة ليست Quotex"
        # TODO: tighten check — e.g. URL contains /trade or trading container visible via probe.
        return None

    def execute_buy(
        self,
        amount: float,
        done: Callable[[bool, str], None] | None = None,
    ) -> None:
        self._execute_side("buy", amount, done)

    def execute_sell(
        self,
        amount: float,
        done: Callable[[bool, str], None] | None = None,
    ) -> None:
        self._execute_side("sell", amount, done)

    def _execute_side(
        self,
        side: str,
        amount: float,
        done: Callable[[bool, str], None] | None,
    ) -> None:
        err = self._validate(amount)
        if err:
            self._state.last_error = err
            log.warning("Trade blocked: %s", err)
            if done:
                done(False, err)
            return

        script = js_scripts.trade_fill_and_click_script(amount, side)

        def _on_result(data: dict[str, Any] | None) -> None:
            if not data:
                msg = "لم يُرجع السكربت نتيجة — راجع selectors في core/selectors.py"
                self._state.last_error = msg
                log.warning(msg)
                if done:
                    done(False, msg)
                return
            amt = data.get("amountSet") or {}
            clk = data.get("click") or {}
            amt_ok = isinstance(amt, dict) and amt.get("ok")
            clk_ok = isinstance(clk, dict) and clk.get("ok")
            if amt_ok and clk_ok:
                msg = f"تم إرسال {side} (تجريبي — تحقق من المنصة)"
                log.info(msg)
                if done:
                    done(True, msg)
            else:
                msg = (
                    f"تعذر إكمال الصفقة: amount={amt!r} click={clk!r}. "
                    "TODO: حدّد محددات حقول المبلغ والأزرار في core/selectors.py"
                )
                self._state.last_error = msg
                log.warning(msg)
                if done:
                    done(False, msg)

        self._browser.run_json_script(script, _on_result)
