"""
تكامل اختياري مع ZenRows و/أو بروكسي عام لطلبات HTTP الخاصة بـ pyquotex نحو Quotex/qxbroker.

مهم:
  - يدعم **ZenRows** و**QUOTEX_PROXY_*** و**HTTPS_PROXY** (حسب الأولوية أدناه).
  - Universal Scraper اختياري (``ZENROWS_USE_UNIVERSAL=1``) لكنه ليس بديلاً عن proxy session
    في كل الحالات.

المتغيرات البيئية:

  ``ZENROWS_PROXY_URL``
      رابط بروكسي واحد يطبَّق على http/https معاً (أولوية أعلى).

  ``ZENROWS_PROXY_HTTP`` / ``ZENROWS_PROXY_HTTPS``
      إذا أردت الفصل بين البروتوكولين.

  ``QUOTEX_PROXY_URL`` / ``QUOTEX_PROXY_HTTP`` / ``QUOTEX_PROXY_HTTPS``
      بروكسي عام لـ pyquotex (أي مزود) إذا لم تُعرّف متغيرات ZenRows للبروكسي.

  ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY``
      يُستخدم تلقائياً إذا لم يُضبط أي مما سبق (سلوك شائع على السيرفرات).

  ``ZENROWS_API_KEY``
      مفتاح ZenRows (مثل تبويب SDK في اللوحة). إذا وُجد **بدون** ``ZENROWS_PROXY_URL``
      يُفعَّل **Universal Scraper** تلقائياً لطلبات HTTP نحو qxbroker/quotex.

  ``ZENROWS_USE_UNIVERSAL``
      ``0`` / ``false`` / ``no`` لتعطيل Universal رغم وجود المفتاح.
      ``1`` / ``true`` لتفعيله صراحةً عند استخدام بروكسي HTTP أيضاً (قد يتداخل).

  ``ZENROWS_EXTRA_PARAMS``
      JSON اختياري لمعاملات إضافية لـ Universal API (مثل {"js_render": "true"}).

لا تضع مفتاح API في الكود؛ استخدم المتغيرات البيئية أو انسخ ``env.template`` إلى ``.env`` (مُستثنى من git).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger("NexoraTrade.zenrows")

_BROWSER_PATCHED = False


def pyquotex_proxies_from_env() -> dict[str, str] | None:
    """
    يبني قاموس ``proxies`` لـ ``Quotex(..., proxies=…)``.

    الأولوية: ZenRows → QUOTEX_* → متغيرات النظام HTTP(S)_PROXY / ALL_PROXY.
    """
    proxy_url = os.getenv("ZENROWS_PROXY_URL", "").strip()
    http = os.getenv("ZENROWS_PROXY_HTTP", "").strip() or proxy_url
    https = os.getenv("ZENROWS_PROXY_HTTPS", "").strip() or proxy_url or http
    source = "ZENROWS" if (http or https) else ""

    if not (http or https):
        q_url = os.getenv("QUOTEX_PROXY_URL", "").strip()
        http = os.getenv("QUOTEX_PROXY_HTTP", "").strip() or q_url
        https = os.getenv("QUOTEX_PROXY_HTTPS", "").strip() or q_url or http
        if http or https:
            source = "QUOTEX"

    if not (http or https):
        all_p = os.getenv("ALL_PROXY", "").strip()
        https = os.getenv("HTTPS_PROXY", "").strip() or all_p
        http = os.getenv("HTTP_PROXY", "").strip() or https
        if not https:
            https = http
        if http or https:
            source = "HTTP_PROXY"

    if not https and not http:
        return None
    h = http or https
    s = https or http
    log.debug("pyquotex proxies من المصدر: %s", source or "?")
    return {"http": h, "https": s}


def _extra_zenrows_params() -> dict[str, Any]:
    raw = os.getenv("ZENROWS_EXTRA_PARAMS", "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("ZENROWS_EXTRA_PARAMS ليس JSON صالحاً — يُتجاهل")
        return {}


def install_universal_scraper_patch(api_key: str) -> bool:
    """
    يستبدل ``Browser.send_request`` لتوجيه طلبات qxbroker/quotex عبر ZenRows Universal API.
    يُستدعى مرة واحدة فقط.
    """
    global _BROWSER_PATCHED
    if _BROWSER_PATCHED:
        return True
    try:
        from zenrows import ZenRowsClient
        from pyquotex.http.navigator import Browser
    except ImportError as e:
        log.warning("تعذر تحميل zenrows أو pyquotex لتفعيل Universal patch: %s", e)
        return False

    extra = _extra_zenrows_params()
    zr = ZenRowsClient(api_key.strip(), retries=2)
    orig = Browser.send_request

    def send_request(self, method, url, headers=None, **kwargs):  # type: ignore[no-untyped-def]
        if not any(h in url for h in ("qxbroker.com", "quotex.com")):
            return orig(self, method, url, headers=headers, **kwargs)

        merged = self.headers.copy()
        if headers:
            merged.update(headers)

        params = dict(extra)
        data = kwargs.get("data")
        m = str(method).upper()
        pass_kw = {k: v for k, v in kwargs.items() if k not in ("data", "headers")}

        try:
            if m == "GET":
                resp = zr.get(url, params=params, headers=merged, **pass_kw)
            elif m == "POST":
                resp = zr.post(url, params=params, headers=merged, data=data, **pass_kw)
            elif m == "PUT":
                resp = zr.put(url, params=params, headers=merged, data=data, **pass_kw)
            else:
                log.warning("ZenRows patch: طريقة غير مدعومة %s — إعادة للسلوك الأصلي", m)
                return orig(self, method, url, headers=headers, **kwargs)
        except Exception:
            log.exception("ZenRows universal request failed for %s %s", m, url)
            raise

        self.response = resp
        try:
            self.cookies.update(resp.cookies)
        except Exception:
            pass

        if getattr(self, "debug", False):
            log.debug("ZenRows → %s %s status=%s", m, url, resp.status_code)
        return resp

    Browser.send_request = send_request  # type: ignore[method-assign]
    _BROWSER_PATCHED = True
    log.info("ZenRows Universal Scraper: تم تفعيل patch لـ pyquotex.Browser.send_request")
    return True


def _env_bool_true(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_bool_false(v: str) -> bool:
    return v.strip().lower() in ("0", "false", "no", "off")


def configure_zenrows_from_environment() -> dict[str, str] | None:
    """
    يُستدعى مرة عند بدء التطبيق.
    يعيد ``proxies`` إن وُجدت في البيئة (لتمريرها إلى ``Quotex``).
    """
    key = os.getenv("ZENROWS_API_KEY", "").strip()
    univ_raw = os.getenv("ZENROWS_USE_UNIVERSAL", "").strip()

    proxies = pyquotex_proxies_from_env()
    if proxies:
        log.info("تم تفعيل بروكسي pyquotex (HTTP/S) — راجع ZENROWS_* أو QUOTEX_* أو HTTPS_PROXY")
    elif not key:
        log.warning(
            "بروكسي pyquotex غير مفعّل: عيّن ZENROWS_API_KEY (Universal) أو ZENROWS_PROXY_URL أو HTTPS_PROXY"
        )

    # Universal SDK: افتراضي عند وجود المفتاح فقط (بدون رابط بروكسي HTTP).
    want_univ_explicit = _env_bool_true(univ_raw) if univ_raw else False
    disable_univ = _env_bool_false(univ_raw) if univ_raw else False

    if key:
        if proxies:
            if want_univ_explicit:
                log.warning(
                    "ZENROWS_USE_UNIVERSAL مفعّل مع بروكسي HTTP: قد يتداخلان — "
                    "جرّب بروكسي Residential من لوحة ZenRows فقط أولاً"
                )
                install_universal_scraper_patch(key)
            elif not disable_univ:
                log.debug(
                    "ZENROWS_API_KEY + بروكسي HTTP: Universal معطّل افتراضياً. "
                    "لتفعيله مع البروكسي عيّن ZENROWS_USE_UNIVERSAL=1"
                )
        else:
            if not disable_univ:
                install_universal_scraper_patch(key)
            else:
                log.info("ZENROWS_USE_UNIVERSAL=0 — Universal معطّل رغم وجود ZENROWS_API_KEY")

    return proxies
