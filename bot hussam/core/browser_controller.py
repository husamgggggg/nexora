"""
Wraps QWebEngineView: navigation helpers, JS execution, signal wiring.

Keeps UI code free of low-level WebEngine details.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView

from core.app_state import AppState

log = logging.getLogger("nexora.browser")


class BrowserController:
    def __init__(self, view: QWebEngineView, state: AppState) -> None:
        self._view = view
        self._state = state
        self._page = view.page()
        self._page.loadStarted.connect(self._on_load_started)
        self._page.loadFinished.connect(self._on_load_finished)
        view.urlChanged.connect(self._on_url_changed)
        view.titleChanged.connect(self._on_title_changed)

    @property
    def view(self) -> QWebEngineView:
        return self._view

    def load_url(self, url: str) -> None:
        if not url.strip():
            log.warning("load_url: empty URL ignored")
            return
        q = QUrl.fromUserInput(url.strip())
        if not q.isValid():
            self._state.last_error = f"رابط غير صالح: {url}"
            log.warning("Invalid URL: %s", url)
            return
        self._view.setUrl(q)

    def reload(self) -> None:
        self._view.reload()

    def run_javascript(
        self,
        script: str,
        callback: Callable[[Any], None] | None = None,
    ) -> None:
        """
        Run script in the main frame. Callback receives Python object or None on failure.
        """
        def _wrapped(result: object) -> None:
            if callback is None:
                return
            try:
                callback(result)
            except Exception:
                log.exception("run_javascript callback failed")

        try:
            self._page.runJavaScript(script, _wrapped)
        except Exception:
            log.exception("runJavaScript failed to queue")
            if callback:
                callback(None)

    def run_json_script(
        self,
        script: str,
        callback: Callable[[dict[str, Any] | None], None],
    ) -> None:
        """Expect script to return JSON.stringify(...). Parses into dict."""

        def _parse(raw: object) -> None:
            if raw is None:
                callback(None)
                return
            if not isinstance(raw, str):
                log.warning("JS result not str: %r", type(raw))
                callback(None)
                return
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    callback(data)
                else:
                    log.warning("JS JSON root not object: %s", type(data))
                    callback(None)
            except json.JSONDecodeError:
                log.warning("Failed to parse JS JSON: %s…", raw[:120])
                callback(None)

        self.run_javascript(script, _parse)

    # --- Qt slots -------------------------------------------------------
    def _on_load_started(self) -> None:
        self._state.is_browser_ready = False
        self._state.browser_status_text = "جاري التحميل…"

    def _on_load_finished(self, ok: bool) -> None:
        self._state.is_browser_ready = bool(ok)
        self._state.browser_status_text = "جاهز" if ok else "فشل التحميل"
        if not ok:
            self._state.last_error = "فشل تحميل الصفحة"
            log.warning("loadFinished ok=False")

    def _on_url_changed(self, qurl: QUrl) -> None:
        self._state.current_url = qurl.toString()

    def _on_title_changed(self, title: str) -> None:
        self._state.page_title = title or ""
