"""
CSS selectors and URL patterns for Quotex web UI.

The site DOM may change; adjust values here without touching detector logic.
All attribute selectors use contains() for resilience against hashed class names.
"""

from __future__ import annotations

import re

# --- URLs ---------------------------------------------------------------
QUOTEX_HOST_PATTERNS: tuple[str, ...] = (
    "quotex.com",
    "qxbroker.com",
)

# Default landing page (user logs in manually in WebView).
DEFAULT_QUOTEX_URL: str = "https://qxbroker.com/en"

# Paths that often appear after successful session (hints only).
POST_LOGIN_PATH_HINTS: tuple[str, ...] = (
    "/trade",
    "/en/trade",
    "/platform",
    "/cabinet",
    "/profile",
)

# --- Login page signals (visible before login) --------------------------
# Login forms often expose email/password inputs.
LOGIN_FORM_SELECTORS: tuple[str, ...] = (
    'input[type="password"]',
    'input[name*="pass" i]',
    'input[autocomplete="current-password"]',
    'form[action*="login" i]',
)

# --- Post-login / trading UI signals ------------------------------------
# Balance-like blocks: adjust if Quotex changes markup.
BALANCE_CONTAINER_HINTS: tuple[str, ...] = (
    '[class*="balance" i]',
    '[class*="wallet" i]',
    '[data-test*="balance" i]',
    '[class*="header-balance" i]',
)

# Demo / real toggles or labels (heuristic).
DEMO_LABEL_SELECTORS: tuple[str, ...] = (
    '[class*="demo" i]',
    '[data-account*="demo" i]',
)

REAL_LABEL_SELECTORS: tuple[str, ...] = (
    '[class*="real" i]',
    '[data-account*="real" i]',
)

# Trading panel: amount input and call/put style buttons.
# TODO: Verify on live Quotex UI and tighten selectors.
TRADE_AMOUNT_INPUT_SELECTORS: tuple[str, ...] = (
    'input[type="text"][class*="amount" i]',
    'input[class*="invest" i]',
    'input[placeholder*="amount" i]',
    'input[placeholder*="invest" i]',
)

TRADE_BUY_BUTTON_SELECTORS: tuple[str, ...] = (
    'button[class*="call" i]',
    'button[class*="up" i]',
    'button[class*="green" i]',
    '[data-direction="call"]',
)

TRADE_SELL_BUTTON_SELECTORS: tuple[str, ...] = (
    'button[class*="put" i]',
    'button[class*="down" i]',
    'button[class*="red" i]',
    '[data-direction="put"]',
)

# Asset picker / current pair (very site-specific; may need user tuning).
ASSET_DISPLAY_SELECTORS: tuple[str, ...] = (
    '[class*="pair" i]',
    '[class*="asset" i]',
    '[class*="symbol" i]',
)


def is_quotex_url(url: str) -> bool:
    if not url:
        return False
    u = url.lower()
    return any(h in u for h in QUOTEX_HOST_PATTERNS)


def url_suggests_logged_in_area(url: str) -> bool:
    if not url or not is_quotex_url(url):
        return False
    path = url.split("://", 1)[-1]
    if "/" in path:
        path = "/" + path.split("/", 1)[-1]
    path_l = path.lower()
    return any(hint in path_l for hint in POST_LOGIN_PATH_HINTS)


def extract_number_from_text(text: str) -> str | None:
    """Best-effort parse of currency / number from messy UI text."""
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.strip())
    m = re.search(
        r"[\d,]+(?:\.\d+)?",
        t.replace("\u00a0", " "),
    )
    return m.group(0) if m else None
