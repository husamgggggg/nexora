#!/usr/bin/env python3
"""
NEXORA TRADE Bot — النسخة النهائية الكاملة
✅ صفقة 60 ثانية ثابتة بدون marginal
✅ تحليل شمعات حقيقية من أسعار Quotex الحية
✅ حساب ربح/خسارة من رصيد المنصة الفعلي
✅ إعادة تشغيل البوت بعد الإيقاف
✅ متعدد المشتركين كل بحسابه المستقل
✅ إشعار عند تحقق الهدف
"""
import asyncio, base64, html, json, logging, os, queue, random, re, socket, ssl, subprocess
import secrets, threading, time, traceback
from contextlib import asynccontextmanager
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from threading import Thread

try:
    import websocket  # type: ignore
except Exception:  # pragma: no cover
    websocket = None

try:
    from curl_cffi import requests as curl_requests  # type: ignore
except Exception:  # pragma: no cover
    curl_requests = None

import pydantic, uvicorn
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel


def _load_local_env():
    """تحميل ``.env`` من مجلد ``bot.py`` قبل قراءة ZENROWS_* وغيرها."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    _base = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(_base, ".env"))


_load_local_env()

_QX_IMPORT_ERR = None
try:
    from pyquotex.stable_api import Quotex
    QX = True
except ImportError as e:
    Quotex = None  # type: ignore[misc, assignment]
    QX = False
    _QX_IMPORT_ERR = e

# تشخيص آخر جلب شموع / WebSocket (للوج)
_HUSAAM_WS_LAST: dict = {}


def _install_pyquotex_ws_on_message_fix():
    """
    pyquotex يستدعي message.get() بعد json.loads حتى لو كان message نصًا — يسبب
    'str' object has no attribute 'get' (ظهر مع Playwright bridge / تغيّر شكل الرسائل).
    """
    if not QX:
        return
    try:
        from pyquotex.ws.client import WebsocketClient
        from nexora_pyquotex_ws_on_message import on_message as _fixed_on_message
    except ImportError as e:
        logging.getLogger("NexoraTrade").warning("تعذر تطبيق تصحيح pyquotex on_message: %s", e)
        return
    if getattr(WebsocketClient.on_message, "_nexora_pyquotex_on_message_fix", False):
        return
    WebsocketClient.on_message = _fixed_on_message


_install_pyquotex_ws_on_message_fix()


def _ws_history_asset_matches(api, msg_asset, current_asset) -> bool:
    """السيرفر قد يرسل asset كرقم والجلسة EURUSD_otc — المكتبة تقارن بـ == فتفشل."""
    if msg_asset is None or current_asset is None:
        return False
    if msg_asset == current_asset:
        return True
    codes = getattr(api, "codes_asset", None) or {}
    if isinstance(current_asset, str) and codes.get(current_asset) == msg_asset:
        return True
    if isinstance(msg_asset, int):
        for sym, aid in codes.items():
            if aid == msg_asset and sym == current_asset:
                return True
    return str(msg_asset) == str(current_asset)


def _install_pyquotex_ws_candle_asset_fix():
    """يصل رد history/list/v2 لكن pyquotex لا يخزّن candles_data إذا اختلف نوع مفتاح asset."""
    if not QX:
        return
    try:
        from pyquotex.ws.client import WebsocketClient
    except ImportError:
        return
    if getattr(WebsocketClient.on_message, "_husaam_ws_patch", False):
        return
    _orig = WebsocketClient.on_message

    def _on_message(self, wss, msg):
        _orig(self, wss, msg)
        try:
            if not isinstance(msg, (bytes, bytearray)) or len(msg) < 2:
                return
            raw = msg[1:].decode("utf-8", errors="ignore")
            if "history" not in raw and "candles" not in raw:
                return
            message = json.loads(raw)
        except Exception:
            return
        if not isinstance(message, dict) or "history" not in message:
            return
        ma = message.get("asset")
        ca = getattr(self.api, "current_asset", None)
        hist = message.get("history")
        try:
            hl = len(hist) if hist is not None else 0
        except Exception:
            hl = 0
        _HUSAAM_WS_LAST["last_msg_asset"] = ma
        _HUSAAM_WS_LAST["last_current_asset"] = ca
        _HUSAAM_WS_LAST["last_history_len"] = hl
        _HUSAAM_WS_LAST["last_ts"] = time.time()
        if not _ws_history_asset_matches(self.api, ma, ca):
            log.debug(
                "WS history: asset غير متطابق (msg=%s current=%s) — قد يمنع pyquotex التخزين",
                ma,
                ca,
            )
            return
        patched = False
        if self.api.candles.candles_data is None and hist is not None:
            self.api.candles.candles_data = hist
            patched = True
        if message.get("candles") and ca is not None:
            self.api.candle_v2_data[ca] = message
            if ma is not None and ma != ca:
                self.api.candle_v2_data[ma] = message
            patched = True
        if patched:
            _HUSAAM_WS_LAST["patched_fill"] = True
            log.info(
                "🔧 WS: عُدّل تخزين شموع history (msg_asset=%s current=%s hist_len=%s v2=%s)",
                ma,
                ca,
                hl,
                bool(message.get("candles")),
            )
        else:
            _HUSAAM_WS_LAST["patched_fill"] = False

    _on_message._husaam_ws_patch = True
    WebsocketClient.on_message = _on_message


_install_pyquotex_ws_candle_asset_fix()

os.makedirs("logs", exist_ok=True)
os.makedirs("data", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("NexoraTrade")

if _QX_IMPORT_ERR is not None:
    log.warning("pyquotex غير محمّل — وضع محاكاة. السبب: %s", _QX_IMPORT_ERR)

def _configure_quiet_loggers():
    """يخفي سجل وصول Uvicorn وضجيج websocket (Sending ping) عند أي طريقة تشغيل."""
    logging.getLogger("uvicorn.access").disabled = True
    logging.getLogger("websocket").setLevel(logging.WARNING)


_configure_quiet_loggers()


def _init_zenrows_for_pyquotex():
    """بروكسي pyquotex: ZENROWS_* أو QUOTEX_* أو HTTPS_PROXY — انظر zenrows_pyquotex.py."""
    try:
        from zenrows_pyquotex import configure_zenrows_from_environment

        return configure_zenrows_from_environment()
    except Exception as e:
        log.warning("تهيئة ZenRows: %s", e)
        return None


def _parse_ws_proxy_from_http_proxies(proxies):
    """
    يحوّل proxies الخاصة بـ HTTP إلى kwargs مفهومة من websocket-client.
    """
    if not isinstance(proxies, dict):
        return {}
    raw = str(proxies.get("https") or proxies.get("http") or "").strip()
    raw = raw.strip(" \t\n\r\"'")
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1].strip()
    if not raw:
        return {}
    try:
        u = urllib.parse.urlparse(raw)
    except Exception:
        return {}
    host = u.hostname
    if not host:
        return {}
    port = None
    try:
        port = u.port
    except ValueError:
        port = None
    if port is None and u.netloc:
        tail = u.netloc.split("@")[-1]
        if ":" in tail:
            maybe = tail.rsplit(":", 1)[-1].strip().strip('"').strip("'")
            if maybe.isdigit():
                port = int(maybe)
    if not port:
        return {}

    scheme = (u.scheme or "http").lower()
    if scheme.startswith("socks5"):
        ptype = "socks5"
    elif scheme.startswith("socks4"):
        ptype = "socks4"
    else:
        ptype = "http"

    ws_proxy = {
        "http_proxy_host": host,
        "http_proxy_port": int(port),
        "proxy_type": ptype,
    }
    user = urllib.parse.unquote(u.username) if u.username else None
    pwd = urllib.parse.unquote(u.password) if u.password else None
    if user:
        ws_proxy["http_proxy_auth"] = (user, pwd or "")
    return ws_proxy


# ========== الحل البرمجي الجذري لتجاوز Cloudflare WebSocket ==========
# ملاحظة: هذا القسم اختياري/احتياطي. إذا لم تتوفر المكتبات المطلوبة، يُتخطّى تلقائياً.
def is_cloudflare_ws_failure(error) -> bool:
    txt = str(error or "").lower()
    keys = (
        "handshake status 403",
        "cf-mitigated",
        "cloudflare",
        "connection to remote host was lost",
        "forbidden",
        "challenge",
        "just a moment",
    )
    return any(k in txt for k in keys)


def create_stealth_websocket(url, cookies=None, proxy=None):
    """
    تنشئ WebSocket متخفيًا بثلاث طبقات:
    1) تهيئة جلسة HTTP عبر curl_cffi (إن توفرت) لجلب cookies.
    2) إنشاء websocket-client بهيدرز وSSL context.
    3) fallback لاحق عبر Playwright bridge.
    """
    cookies = cookies or {}
    if curl_requests is None or websocket is None:
        log.warning("Stealth WS: مكتبات غير متوفرة (curl_cffi/websocket-client)")
        return None
    try:
        # no-op: keep structure consistent
        pass
    except Exception as e:
        log.warning("Stealth WS: مكتبة غير متوفرة (%s)", e)
        return None

    session = curl_requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Extensions": "permessage-deflate; client_max_window_bits",
            "Origin": "https://quotex.io",
            "Sec-WebSocket-Key": base64.b64encode(os.urandom(16)).decode(),
        }
    )
    for ck, cv in cookies.items():
        try:
            session.cookies.set(str(ck), str(cv))
        except Exception:
            pass
    try:
        resp = session.get("https://quotex.io", impersonate="chrome120", timeout=30)
        cf_cookie = resp.cookies.get("cf_clearance")
        if cf_cookie:
            session.cookies.set("cf_clearance", cf_cookie)
    except Exception:
        pass

    ws_url = str(url or "").replace("http://", "ws://").replace("https://", "wss://")
    ws_headers = {
        "User-Agent": session.headers.get("User-Agent", ""),
        "Origin": "https://quotex.io",
        "Sec-WebSocket-Key": session.headers.get("Sec-WebSocket-Key", ""),
        "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Extensions": "permessage-deflate; client_max_window_bits",
        "Cookie": "; ".join([f"{k}={v}" for k, v in session.cookies.items()]),
    }

    ssl_context = ssl.create_default_context()
    try:
        ssl_context.set_ciphers(
            "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
            "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384"
        )
    except Exception:
        pass
    is_zen = "zenrows" in str(proxy or "").lower()
    if is_zen:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

    ws = websocket.WebSocketApp(
        ws_url,
        header=[f"{k}: {v}" for k, v in ws_headers.items() if v],
        on_open=lambda w: log.info("WS stealth: مفتوح بنجاح"),
        on_error=lambda w, err: log.warning("WS stealth error: %s", err),
        on_close=lambda w, close_status, close_msg: log.warning("WS stealth: مغلق"),
    )

    # websocket-client لا يقبل "proxy" كسلسلة مباشرة داخل run_forever؛
    # يمرّر http_proxy_host/http_proxy_port. هنا نمرر بدون proxy إذا ZenRows.
    kwargs = {"sslopt": {"context": ssl_context}}
    if proxy and not is_zen:
        p = _parse_ws_proxy_from_http_proxies({"http": proxy, "https": proxy})
        if p:
            kwargs.update(p)

    wst = Thread(target=ws.run_forever, kwargs=kwargs)
    wst.daemon = True
    wst.start()
    time.sleep(2)
    return ws


async def playwright_websocket_bridge(target_wss_url, message_callback):
    """
    Bridge عبر Playwright (ثقيل) — fallback.
    """
    try:
        from playwright.async_api import async_playwright  # type: ignore
        import websockets  # type: ignore
    except Exception as e:
        log.warning("Playwright bridge غير متاح: %s", e)
        return

    async def handle_client(client_ws):
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = await browser.new_page()
            await page.goto("https://quotex.io", wait_until="domcontentloaded")
            await page.evaluate(
                """
                (targetUrl) => {
                  window.__nexora_ws = new WebSocket(targetUrl);
                }
                """,
                target_wss_url,
            )
            try:
                async for message in client_ws:
                    safe = str(message).replace("\\", "\\\\").replace("'", "\\'")
                    await page.evaluate(f"window.__nexora_ws && window.__nexora_ws.send('{safe}')")
                    try:
                        message_callback(message)
                    except Exception:
                        pass
            finally:
                await browser.close()

    server = await websockets.serve(handle_client, "localhost", 8765)
    await server.wait_closed()


def get_websocket_via_tls_client(url):
    try:
        import tls_client  # type: ignore
    except Exception as e:
        log.warning("tls_client غير متاح: %s", e)
        return None
    session = tls_client.Session(
        client_identifier="chrome_120",
        random_tls_extension_order=True,
    )
    try:
        resp = session.get("https://quotex.io")
        return resp.cookies.get("cf_clearance")
    except Exception as e:
        log.warning("tls_client فشل: %s", e)
        return None


def establish_websocket_with_stealth(proxy=None):
    """
    محاولة إنشاء WebSocket بطريقة stealth قبل fallback التقليدي.
    """
    if proxy and "zenrows" in str(proxy).lower():
        proxy = None
        log.warning("ZenRows غير صالح لـ WebSocket، اعتماد stealth بدون بروكسي")
    try:
        ws = create_stealth_websocket("wss://quotex.io/some/ws", cookies={}, proxy=proxy)
        if ws and getattr(ws, "sock", None) and ws.sock and ws.sock.connected:
            return ws
    except Exception as e:
        log.warning("فشلت المحاولة البرمجية الأولى: %s", e)
    # تجنّب RuntimeWarning عند وجود loop شغال: شغّل Playwright bridge في Thread مستقل.
    if os.getenv("ENABLE_PLAYWRIGHT_BRIDGE", "").strip().lower() in ("1", "true", "yes", "on"):
        try:
            def _run_bridge_bg():
                try:
                    asyncio.run(
                        playwright_websocket_bridge(
                            "wss://quotex.io/some/ws",
                            lambda x: log.debug("bridge: %s", x),
                        )
                    )
                except Exception as e:
                    log.warning("Playwright bridge فشل: %s", e)

            t = Thread(target=_run_bridge_bg, daemon=True, name="playwright-ws-bridge")
            t.start()
            log.info("تم تشغيل Playwright bridge في الخلفية.")
        except Exception as e:
            log.warning("تعذر تشغيل Playwright bridge: %s", e)
    else:
        log.info("Playwright bridge معطّل (فعّله عبر ENABLE_PLAYWRIGHT_BRIDGE=1 إذا أردت).")
    return None


_PW_BRIDGE_PROC = None
_PW_BRIDGE_PORT = None


def _pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _wait_port_open(host: str, port: int, timeout_sec: float = 8.0) -> bool:
    """
    يتحقق أن المنفذ يقبل اتصال TCP فقط.
    لا ترسل GET عادياً: خادم websockets يتوقع Upgrade: websocket وإلا InvalidUpgrade
    (missing Connection header) وقد يفسد أول جلسة bridge.
    """
    end = time.time() + timeout_sec
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=1.0) as s:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                return True
        except Exception:
            time.sleep(0.15)
    return False


def _ensure_playwright_bridge(target_ws_url: str):
    global _PW_BRIDGE_PROC, _PW_BRIDGE_PORT
    if not target_ws_url:
        return None
    bridge_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ws_bridge.py")
    if not os.path.isfile(bridge_file):
        log.warning("Playwright bridge file غير موجود: %s", bridge_file)
        return None
    if _PW_BRIDGE_PROC is not None and _PW_BRIDGE_PROC.poll() is None and _PW_BRIDGE_PORT:
        return f"ws://127.0.0.1:{_PW_BRIDGE_PORT}"

    port = _pick_free_port()
    cmd = [
        os.getenv("PYTHON_BIN", "python"),
        bridge_file,
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        str(port),
        "--target-url",
        target_ws_url,
    ]
    try:
        _bp = globals().get("QX_HTTP_PROXIES")
        _bridge_px = ""
        if isinstance(_bp, dict):
            _bridge_px = str(_bp.get("https") or _bp.get("http") or "").strip()
        if _bridge_px:
            cmd.extend(["--proxy-url", _bridge_px])
            log.info("Playwright bridge: تمرير بروكسي pyquotex إلى Chromium (تطابق IP مع جلسة الدخول)")
    except Exception:
        pass
    try:
        logs_dir = os.path.join(os.path.dirname(bridge_file), "logs")
        os.makedirs(logs_dir, exist_ok=True)
        bridge_log_path = os.path.join(logs_dir, "ws_bridge.log")
        bridge_log = open(bridge_log_path, "a", encoding="utf-8")
        _PW_BRIDGE_PROC = subprocess.Popen(
            cmd,
            cwd=os.path.dirname(bridge_file),
            stdout=bridge_log,
            stderr=bridge_log,
        )
        _PW_BRIDGE_PORT = port
    except Exception as e:
        log.warning("تعذر تشغيل Playwright bridge: %s", e)
        _PW_BRIDGE_PROC = None
        _PW_BRIDGE_PORT = None
        return None
    if not _wait_port_open("127.0.0.1", port, 10.0):
        log.warning("Playwright bridge لم يبدأ على المنفذ %s", port)
        try:
            _PW_BRIDGE_PROC.kill()
        except Exception:
            pass
        _PW_BRIDGE_PROC = None
        _PW_BRIDGE_PORT = None
        return None
    log.info("✅ Playwright WS bridge started: ws://127.0.0.1:%s", port)
    return f"ws://127.0.0.1:{port}"


def _install_pyquotex_ws_proxy_patch(proxies):
    """
    pyquotex يمرّر proxy لطلبات HTTP فقط. هذا patch يمرّره أيضاً لـ WebSocket.
    """
    if not QX:
        return
    ws_proxy = _parse_ws_proxy_from_http_proxies(proxies)
    if not ws_proxy:
        log.warning("لم يتم العثور على WS proxy صالح (سيتم الاتصال بـ Quotex مباشرة).")
        return
    try:
        import pyquotex.api as _qx_api_mod
    except Exception as e:
        log.warning("تعذر تحميل pyquotex.api لتفعيل WS proxy patch: %s", e)
        return
    QuotexAPI = getattr(_qx_api_mod, "QuotexAPI", None)
    if QuotexAPI is None:
        return
    if getattr(QuotexAPI.start_websocket, "_nexora_ws_proxy_patch", False):
        return

    _ws_h = (ws_proxy.get("http_proxy_host") or "").lower()
    _insecure_env = os.getenv("QUOTEX_WS_INSECURE_SSL", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    # ZenRows (ومثله) يمرّر TLS عبر وسيط؛ مثل عيّنة Playground التي تستخدم verify=False.
    ws_insecure_ssl = _insecure_env or "zenrows.com" in _ws_h

    async def _start_websocket_with_proxy(self):
        self.state.check_websocket_if_connect = None
        self.state.check_websocket_if_error = False
        self.state.websocket_error_reason = None
        if not self.state.SSID:
            await self.authenticate()
        self.websocket_client = _qx_api_mod.WebsocketClient(self)
        use_bridge = os.getenv("QUOTEX_USE_PLAYWRIGHT_BRIDGE", "").strip().lower() in ("1", "true", "yes", "on")
        bridge_ws_url = None
        if use_bridge:
            try:
                target_ws_url = getattr(self.websocket, "url", "") or ""
                bridge_ws_url = _ensure_playwright_bridge(target_ws_url)
                if bridge_ws_url:
                    self.websocket.url = bridge_ws_url
                    log.info("🔁 WebSocket redirected via Playwright bridge: %s", bridge_ws_url)
            except Exception as e:
                log.warning("Playwright bridge redirect failed: %s", e)
        if ws_insecure_ssl:
            log.warning(
                "WebSocket عبر بروكسي MITM: تعطيل التحقق من شهادة TLS (ZenRows أو QUOTEX_WS_INSECURE_SSL=1)"
            )
            sslopt = {
                "check_hostname": False,
                "cert_reqs": _qx_api_mod.ssl.CERT_NONE,
            }
        else:
            sslopt = {
                "check_hostname": True,
                "cert_reqs": _qx_api_mod.ssl.CERT_REQUIRED,
                "ca_certs": _qx_api_mod.cacert,
                "context": _qx_api_mod.ssl_context,
            }
        if _qx_api_mod.platform.system() == "Linux":
            sslopt["ssl_version"] = _qx_api_mod.ssl.PROTOCOL_TLS
        payload = {
            "suppress_origin": True,
            "ping_interval": 24,
            "ping_timeout": 20,
            "ping_payload": "2",
            "origin": self.https_url if not bridge_ws_url else "http://127.0.0.1",
            "host": (f"ws2.{self.host}" if not bridge_ws_url else f"127.0.0.1:{_PW_BRIDGE_PORT}"),
            "sslopt": sslopt,
        }

        # عند استخدام bridge المحلي لا نمرّر proxy للـWS.
        if not bridge_ws_url:
            payload.update(ws_proxy)

        self.websocket_thread = _qx_api_mod.threading.Thread(
            target=self.websocket.run_forever, kwargs=payload
        )
        self.websocket_thread.daemon = True
        self.websocket_thread.start()
        while True:
            if self.state.check_websocket_if_error:
                return False, self.state.websocket_error_reason
            if self.state.check_websocket_if_connect == 0:
                return False, "Websocket connection closed."
            if self.state.check_websocket_if_connect == 1:
                return True, "Websocket connected successfully!!!"
            if self.state.check_rejected_connection == 1:
                self.state.SSID = None
                return True, "Websocket Token Rejected."
            await asyncio.sleep(0.1)

    _start_websocket_with_proxy._nexora_ws_proxy_patch = True
    QuotexAPI.start_websocket = _start_websocket_with_proxy
    log.info(
        "تم تفعيل WS proxy patch لـ pyquotex: %s:%s (%s)",
        ws_proxy.get("http_proxy_host"),
        ws_proxy.get("http_proxy_port"),
        ws_proxy.get("proxy_type"),
    )


QX_HTTP_PROXIES = _init_zenrows_for_pyquotex()
_install_pyquotex_ws_proxy_patch(QX_HTTP_PROXIES)

USERS_F  = "data/users.json"
ADMIN_PW = os.getenv("ADMIN_PW", "Admin@2024")
_ALLOWED_ORIGINS_RAW = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000",
)
ALLOWED_ORIGINS = [o.strip() for o in _ALLOWED_ORIGINS_RAW.split(",") if o.strip()]
if not ALLOWED_ORIGINS:
    ALLOWED_ORIGINS = ["http://localhost:8000"]
if ADMIN_PW == "Admin@2024":
    log.warning("⚠️ ADMIN_PW يستخدم القيمة الافتراضية. غيّرها بمتغير بيئة قبل الإنتاج.")

def load(f, d):
    try:
        if os.path.exists(f): return json.load(open(f, encoding="utf-8"))
    except: pass
    return d

def save(f, d):
    json.dump(d, open(f,"w",encoding="utf-8"), ensure_ascii=False, indent=2)

USERS        = load(USERS_F, {})
ADMIN_TOKENS = set()
_ADMIN_TOKEN_F = os.path.join("data", "admin_token.txt")
_ADMIN_TOKENS_JSON = os.path.join("data", "admin_tokens.json")


def _load_admin_tokens_into_set():
    try:
        merged = []
        if os.path.exists(_ADMIN_TOKENS_JSON):
            data = json.load(open(_ADMIN_TOKENS_JSON, encoding="utf-8"))
            if isinstance(data, list):
                merged.extend(t for t in data if isinstance(t, str) and len(t) >= 8)
        if os.path.exists(_ADMIN_TOKEN_F):
            t = open(_ADMIN_TOKEN_F, encoding="utf-8").read().strip()
            if len(t) >= 8 and t not in merged:
                merged.append(t)
        for t in merged:
            ADMIN_TOKENS.add(t)
        if merged and not os.path.exists(_ADMIN_TOKENS_JSON):
            json.dump(merged[-20:], open(_ADMIN_TOKENS_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass


def _persist_admin_token(t: str):
    ADMIN_TOKENS.add(t)
    try:
        arr = []
        if os.path.exists(_ADMIN_TOKENS_JSON):
            arr = json.load(open(_ADMIN_TOKENS_JSON, encoding="utf-8"))
            if not isinstance(arr, list):
                arr = []
        if t not in arr:
            arr.append(t)
        arr = arr[-20:]
        json.dump(arr, open(_ADMIN_TOKENS_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        open(_ADMIN_TOKEN_F, "w", encoding="utf-8").write(t)
    except Exception:
        pass


_load_admin_tokens_into_set()

def save_users(): save(USERS_F, USERS)


def _resolve_user_email(raw: str):
    """مفتاح البريد في USERS (نفس السلسلة المحفوظة) مع تجاهل حالة الأحرف والفراغات."""
    e = (raw or "").strip()
    if not e or "@" not in e:
        return None
    if e in USERS:
        return e
    low = e.lower()
    for k in USERS:
        if isinstance(k, str) and k.lower() == low:
            return k
    return None


_DEFAULT_QX_LOGIN_FAIL = (
    "رفضت Quotex تسجيل الدخول. تحقق من البريد وكلمة المرور. "
    "إذا طلبت المنصة تحققاً سيظهر هنا حقل PIN. "
    "إن استمر الفشل: VPN أو شبكة أخرى (حظر IP أو Cloudflare)."
)


def _user_display_id(email: str) -> int:
    """نفس معرّف العرض في /api/status."""
    if not email:
        return 0
    return abs(hash(email)) % 90_000_000 + 10_000_000


def _notify_telegram_new_registration(email: str, name: str, registered_at: str) -> None:
    """يرسل إلى قناة تيليجرام عند طلب انضمام جديد. يتطلب TELEGRAM_BOT_TOKEN و TELEGRAM_CHANNEL_ID."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    channel_id = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
    if not bot_token or not channel_id:
        return
    try:
        uid = _user_display_id(email)
        text = (
            "🔔 <b>طلب انضمام جديد — NEXORA</b>\n\n"
            f"📧 <b>البريد:</b> {html.escape(email)}\n"
            f"👤 <b>الاسم:</b> {html.escape(name or '—')}\n"
            f"🆔 <b>معرف الحساب:</b> <code>{uid}</code>\n"
            f"🕐 <b>وقت الطلب:</b> {html.escape(registered_at)}"
        )
        data = urllib.parse.urlencode(
            {"chat_id": channel_id, "text": text, "parse_mode": "HTML"}
        ).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                log.warning("Telegram: HTTP %s", resp.status)
    except Exception as e:
        log.warning("فشل إشعار تيليجرام: %s", e)


# ── PIN ────────────────────────────────────────────────────────────────────────
_pin_queues: dict = {}

def get_pin_q(email):
    if email not in _pin_queues:
        _pin_queues[email] = queue.Queue()
    return _pin_queues[email]

def drain_pin_queue(email):
    """تفريغ رموز قديمة في الطابور حتى لا يُستهلك رمز خاطئ قبل طلب جديد."""
    q = get_pin_q(email)
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break

def make_pin_input(email, S):
    def _f(prompt=""):
        log.info(f"🔐 PIN مطلوب: {email}")
        S["needs_pin"] = True
        try:
            pin = get_pin_q(email).get(timeout=120)
            S["needs_pin"] = False
            if pin is None:
                return ""
            return re.sub(r"\D", "", str(pin).strip()) or str(pin).strip()
        except queue.Empty:
            S["needs_pin"] = False
            return ""
    return _f

# ── Sessions — كل مشترك مستقل ─────────────────────────────────────────────────
SESSIONS: dict = {}

def new_session(email=""):
    return {
        "logged_in":      False,
        "needs_pin":      False,
        "email":          email,
        "currency":       "USD",
        "real_balance":   0.0,
        "demo_balance":   0.0,
        "start_balance":  0.0,
        "running":        False,
        "current_trade":  None,
        "trades":         [],
        "wins":           0,
        "losses":         0,
        "session_profit": 0.0,
        "client":         None,
        "stop_event":     threading.Event(),
        "sim_mode":       not QX,
        "last_signal":    "",
        "candles_ok":     False,
        "candle_source":  "",   # HUSAAM_EMA10: مصدر الشموع للعرض
        "status_msg":     "",   # رسالة للمشترك
        "_last_bal_sync_ts": 0.0,
        "login_error":    "",   # فشل connect بعد إرسال PIN (للعرض بدل مهلة صامتة)
    }

def _is_session_sim_mode(S: dict) -> bool:
    # المحاكاة تكون فعّالة إذا pyquotex غير متاحة أو لا يوجد client متصل للجلسة.
    if not QX:
        return True
    return not bool(S and S.get("client"))

def get_session(token) -> dict:
    if not token: return None
    if token not in SESSIONS:
        for email, u in USERS.items():
            if u.get("session_token") == token:
                SESSIONS[token] = new_session(email)
                break
    return SESSIONS.get(token)

def get_session_by_email(email) -> dict:
    for S in SESSIONS.values():
        if S["email"] == email and S["logged_in"]:
            return S
    return None

# ── Models ────────────────────────────────────────────────────────────────────
class RegisterReq(BaseModel):
    email: str
    name: str = ""

class LoginReq(BaseModel):
    email: str
    password: str
    token: str

class PinReq(BaseModel):
    pin: str
    token: str

class BotReq(BaseModel):
    asset: str           # الزوج الأول (للتوافق مع الكود القديم)
    assets: list = []    # قائمة الأزواج المختارة (جديد)
    amount: float
    strategy: str
    account_type: str
    profit_limit: float
    stop_loss: float
    token: str

class TokenReq(BaseModel):
    token: str

class AdminLoginReq(BaseModel):
    password: str

class ApproveReq(BaseModel):
    admin_token: str
    email: str
    action: str
    note: str = ""

# ── Async Loop ────────────────────────────────────────────────────────────────
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()

def run_async(coro, timeout=30):
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)

# ── loop مستقل لكل مشترك — يمنع تعليق الكل بسبب مشترك واحد ──────────────────
_session_loops: dict = {}
_session_loop_lock = threading.Lock()

def get_session_loop(email: str):
    with _session_loop_lock:
        if email not in _session_loops:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, daemon=True, name=f"loop-{email}").start()
            _session_loops[email] = loop
        return _session_loops[email]

def run_async_for(email: str, coro, timeout=30):
    loop = get_session_loop(email)
    try:
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)
    except TimeoutError:
        log.warning("run_async_for(%s): انتهت المهلة بعد %.0f ث", email, timeout)
        raise
    except Exception as e:
        log.error("run_async_for(%s): %s", email, e or type(e).__name__, exc_info=True)
        raise

# ══════════════════════════════════════════════════════════════════════════════
# المؤشرات التقنية الحقيقية
# ══════════════════════════════════════════════════════════════════════════════
def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag*(period-1) + gains[i]) / period
        al = (al*(period-1) + losses[i]) / period
    if al == 0: return 100.0
    return round(100 - (100/(1+ag/al)), 2)

def calc_ema(closes, period):
    if not closes or len(closes) < period:
        return closes[-1] if closes else 0
    k = 2/(period+1)
    ema = sum(closes[:period])/period
    for p in closes[period:]: ema = p*k + ema*(1-k)
    return ema

def calc_macd(closes):
    if len(closes) < 35: return 0, 0, 0
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd  = ema12 - ema26
    mv = []
    for i in range(9, 0, -1):
        if len(closes) > i:
            mv.append(calc_ema(closes[:-i],12) - calc_ema(closes[:-i],26))
    mv.append(macd)
    sig  = calc_ema(mv, min(9,len(mv)))
    return round(macd,8), round(sig,8), round(macd-sig,8)


def calc_macd_series(closes):
    """
    MACD(12,26) وخط الإشارة EMA(9) على سلسلة الماكد، والهستوغرام = ماكد − إشارة.
    يُملأ من الفهرس 25 فصاعداً (0-based) ليتوافق مع الشارت القياسي.
    """
    n = len(closes)
    if n < 35:
        return None, None, None
    macd_line = [0.0] * n
    signal_line = [0.0] * n
    hist = [0.0] * n
    for i in range(25, n):
        macd_line[i] = calc_ema(closes[: i + 1], 12) - calc_ema(closes[: i + 1], 26)
    for i in range(25, n):
        mslice = macd_line[25 : i + 1]
        if mslice:
            signal_line[i] = calc_ema(mslice, 9)
            hist[i] = macd_line[i] - signal_line[i]
    return macd_line, signal_line, hist


def calc_bb(closes, period=20):
    if len(closes) < period: return 50.0
    w   = closes[-period:]
    mid = sum(w)/period
    std = (sum((x-mid)**2 for x in w)/period)**0.5
    if std == 0: return 50.0
    return round((closes[-1]-(mid-2*std))/(4*std)*100, 2)

def calc_stoch(closes, k=14):
    """Stochastic Oscillator"""
    if len(closes) < k: return 50.0
    low_k  = min(closes[-k:])
    high_k = max(closes[-k:])
    if high_k == low_k: return 50.0
    return round((closes[-1]-low_k)/(high_k-low_k)*100, 2)

def calc_williams(closes, period=14):
    """Williams %R"""
    if len(closes) < period: return -50.0
    high = max(closes[-period:])
    low  = min(closes[-period:])
    if high == low: return -50.0
    return round((high-closes[-1])/(high-low)*-100, 2)

# ── استراتيجية NEXORA EMA10 (شموع مغلقة 1m فقط، EMA10):
#    CALL (ترند صاعد): سياق فوق EMA → حمراء إشارية على EMA → خضراء تأكيد → CALL
#    PUT  (ترند هابط): سياق تحت EMA → خضراء إشارية على EMA → حمراء تأكيد → PUT
_HUSAAM_EMA10_PERIOD = 10
_HUSAAM_EMA10_MIN_CANDLES = 40
# مصدر التحليل: آخر N شمعة 1m من شارت Quotex (بعد التنظيف) — لا يُبنى على التيكات
_HUSAAM_EMA10_ANALYSIS_BARS = 40
_HUSAAM_EMA10_CONTEXT_BEFORE_SIGNAL = 4
_HUSAAM_EMA10_CONTEXT_ABOVE_MIN = 2
_HUSAAM_EMA10_CONTEXT_BELOW_MIN = 2
_HUSAAM_EMA10_CLOSE_TOL_PCT = 0.0018
_HUSAAM_EMA10_LOW_TOUCH_PCT = 0.0026
_HUSAAM_EMA10_BREAKDOWN_EXTRA_PCT = 0.0014
_HUSAAM_EMA10_MIN_GREEN_BODY_RATIO = 0.18
_HUSAAM_EMA10_MIN_RED_BODY_RATIO = 0.18
_HUSAAM_EMA10_MAX_SIGN_FLIPS = 8
_HUSAAM_EMA10_FLAT_LOOKBACK = 10
_HUSAAM_EMA10_MAX_FLAT_SLOPE_PCT = 0.00006
_HUSAAM_EMA10_SCORE = 10
# شارت Quotex دقيقة — الإشارة بعد إغلاق الشمعة على EMA وليس أثناء تكوّنها
_HUSAAM_EMA10_CANDLE_SECS = 60
# تخزين نتيجة get_candles ناجحة (ثوانٍ) — يقلل الضغط ويحافظ على مصدر واحد مع الشارت
_HUSAAM_QUOTEX_CACHE_SEC = 90.0
# مهلة run_async_for لجلب الشموع (بعد v2 قصير + get_candles قصير لكل دورة)
_HUSAAM_QUOTEX_FETCH_TIMEOUT_SEC = 55.0

# ── استراتيجية NEXORA الخاصة (MACD + هستوغرام، شموع 1m مغلقة) ──
_HUSAAM_PRIVATE_MIN_BARS = 55
_HUSAAM_PRIVATE_TREND_EMA = 21
_HUSAAM_PRIVATE_SCORE = 10

# ── HUSAAM: RSI + MACD (تآزر) + مدى شمعة vs وسيط + نافذة UTC — توازن بين جودة وعدد الإشارات ──
_HUSAAM_STRICT_SCORE = 10
_HUSAAM_STRICT_RSI_OS = 36
_HUSAAM_STRICT_RSI_OB = 64
_HUSAAM_STRICT_RANGE_LOOKBACK = 20
# آخر شمعة: المدى ≥ هذا النسبة من وسيط مدى الشموع السابقة (أقل حساسية من المتوسط)
_HUSAAM_STRICT_RANGE_VS_MEDIAN = 0.78
# OTC — الساعة UTC (توسيعة خفيفة عن 7–22)
_HUSAAM_TRADE_UTC_START = 6
_HUSAAM_TRADE_UTC_END = 23


def _husaam_trade_window_ok_utc() -> bool:
    """True إذا الساعة الحالية UTC ضمن [start, end)."""
    h = datetime.now(timezone.utc).hour
    if _HUSAAM_TRADE_UTC_START <= _HUSAAM_TRADE_UTC_END:
        return _HUSAAM_TRADE_UTC_START <= h < _HUSAAM_TRADE_UTC_END
    return h >= _HUSAAM_TRADE_UTC_START or h < _HUSAAM_TRADE_UTC_END


def _husaam_macd_cross_up(macd_line, signal_line, i: int) -> bool:
    """تقاطع صاعد: خط MACD يقطع خط الإشارة من أسفل إلى فوق."""
    if i < 1:
        return False
    return macd_line[i] > signal_line[i] and macd_line[i - 1] <= signal_line[i - 1]


def _husaam_macd_cross_down(macd_line, signal_line, i: int) -> bool:
    """تقاطع هابط: خط MACD يقطع خط الإشارة من فوق إلى أسفل."""
    if i < 1:
        return False
    return macd_line[i] < signal_line[i] and macd_line[i - 1] >= signal_line[i - 1]


def _husaam_median(vals: list) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    m = len(s) // 2
    if len(s) % 2:
        return float(s[m])
    return (float(s[m - 1]) + float(s[m])) / 2.0


def _husaam_macd_bullish_confirm(macd_line, signal_line, hist, n: int) -> bool:
    """تأكيد صعود: تقاطع أو زخم هستو يتحسّن مع ماكد فوق الإشارة، أو عبور هستو فوق الصفر."""
    if n < 26:
        return False
    if _husaam_macd_cross_up(macd_line, signal_line, n):
        return True
    if macd_line[n] > signal_line[n] and hist[n] > hist[n - 1]:
        return True
    if hist[n] > 0 and hist[n - 1] <= 0:
        return True
    return False


def _husaam_macd_bearish_confirm(macd_line, signal_line, hist, n: int) -> bool:
    """تأكيد هبوط: تقاطع أو هستو يضعف مع ماكد تحت الإشارة، أو عبور هستو تحت الصفر."""
    if n < 26:
        return False
    if _husaam_macd_cross_down(macd_line, signal_line, n):
        return True
    if macd_line[n] < signal_line[n] and hist[n] < hist[n - 1]:
        return True
    if hist[n] < 0 and hist[n - 1] >= 0:
        return True
    return False


def _analyze_husaam_strict(candles) -> str:
    """
    CALL: RSI < منطقة oversold + تأكيد MACD (تقاطع أو زخم/عبور هستو) + مدى آخر شمعة ≥ نسبة من وسيط المدى السابق.
    PUT: RSI > منطقة overbought + تأكيد MACD الهابط + نفس فلتر المدى.
    نافذة UTC لتقليل التذبذب خارج أوقات السيولة.
    """
    if not _husaam_trade_window_ok_utc():
        return "wait"
    kit = _candles_ohlc_kit(_ema10_closed_only(candles))
    lb = _HUSAAM_STRICT_RANGE_LOOKBACK
    if len(kit) < max(35, lb + 1):
        return "wait"
    closes = [float(c["close"]) for c in kit]
    ranges = [max(float(c["high"]) - float(c["low"]), 1e-12) for c in kit]
    last_r = ranges[-1]
    prev = ranges[-(lb + 1) : -1]
    med_r = _husaam_median(prev)
    if med_r <= 0 or last_r < med_r * _HUSAAM_STRICT_RANGE_VS_MEDIAN:
        return "wait"

    rsi = calc_rsi(closes)
    macd_line, signal_line, hist = calc_macd_series(closes)
    if macd_line is None:
        return "wait"
    n = len(closes) - 1
    if n < 26:
        return "wait"

    if rsi < _HUSAAM_STRICT_RSI_OS and _husaam_macd_bullish_confirm(
        macd_line, signal_line, hist, n
    ):
        log.info(
            "🏆 NEXORA CALL | RSI=%s MACD confirm | range=%.6f med=%.6f (min=%.6f)",
            rsi,
            last_r,
            med_r,
            med_r * _HUSAAM_STRICT_RANGE_VS_MEDIAN,
        )
        return "call"
    if rsi > _HUSAAM_STRICT_RSI_OB and _husaam_macd_bearish_confirm(
        macd_line, signal_line, hist, n
    ):
        log.info(
            "🏆 NEXORA PUT | RSI=%s MACD confirm | range=%.6f med=%.6f (min=%.6f)",
            rsi,
            last_r,
            med_r,
            med_r * _HUSAAM_STRICT_RANGE_VS_MEDIAN,
        )
        return "put"
    return "wait"


def _candles_ohlc_kit(candles) -> list:
    out = []
    for c in candles:
        if isinstance(c, dict):
            cl = float(c.get("close", c.get("c", 0)) or 0)
            if cl <= 0:
                continue
            o = float(c.get("open", cl) or cl)
            h = float(c.get("high", cl) or cl)
            l = float(c.get("low", cl) or cl)
            hi = max(h, cl, o)
            lo = min(l, cl, o)
            out.append({"open": o, "high": hi, "low": lo, "close": cl})
        elif hasattr(c, "close"):
            cl = float(getattr(c, "close", 0) or 0)
            if cl > 0:
                out.append({"open": cl, "high": cl, "low": cl, "close": cl})
    return out


def _ema10_closed_only(candles) -> list:
    """شموع مكتملة فقط: تيكات محلية = إسقاط آخر شمعة؛ من API = إسقاط الدقيقة الجارية إن وُجدت."""
    if not candles:
        return []
    last = candles[-1]
    if isinstance(last, dict) and last.get("time") is not None:
        t0 = float(last["time"])
        if time.time() < t0 + _HUSAAM_EMA10_CANDLE_SECS:
            return candles[:-1]
        return candles[:]
    if len(candles) < 2:
        return []
    return candles[:-1]


def _husaam_ema10_ema_at(closes, idx: int, p: int) -> float:
    """EMA(p) عند إغلاق الشمعة idx (باستخدام closes[0..idx])."""
    if idx < p - 1 or idx >= len(closes):
        return 0.0
    return calc_ema(closes[: idx + 1], p)


def _husaam_ema10_sign_flips(closes, p: int, last_idx: int, lookback: int) -> int:
    """تقلّبات جهة (close−EMA) — رفض السوق المتذبذب بدون اتجاه."""
    if last_idx < p - 1:
        return 99
    start = max(p - 1, last_idx - lookback + 1)
    flips = 0
    prev_d = None
    for i in range(start, last_idx + 1):
        ema_i = _husaam_ema10_ema_at(closes, i, p)
        d = closes[i] - ema_i
        if prev_d is not None and prev_d * d < 0:
            flips += 1
        prev_d = d
    return flips


def _husaam_ema10_red_near_ema(
    close_sig: float,
    low_sig: float,
    ema_sig: float,
    close_tol: float,
    touch_tol: float,
) -> bool:
    """إغلاق/لمس قريب من EMA دون كسر واضح للأسفل (يُكمّل مع فحص الكسر منفصلاً)."""
    if close_sig < ema_sig - 1.5 * close_tol:
        return False
    if abs(close_sig - ema_sig) <= 1.5 * close_tol:
        return True
    if low_sig <= ema_sig + touch_tol and low_sig >= ema_sig - 2.0 * touch_tol:
        return True
    return False


def _husaam_ema10_green_near_ema(
    close_sig: float,
    high_sig: float,
    ema_sig: float,
    close_tol: float,
    touch_tol: float,
) -> bool:
    """إغلاق/لمس قريب من EMA دون كسر واضح للأعلى (ترند هابط — رفض صعود)."""
    if close_sig > ema_sig + 1.5 * close_tol:
        return False
    if abs(close_sig - ema_sig) <= 1.5 * close_tol:
        return True
    if high_sig >= ema_sig - touch_tol and high_sig <= ema_sig + 2.0 * touch_tol:
        return True
    return False


def _husaam_ema10_reject_flat_or_choppy(closes, p: int, ent: int) -> bool:
    if len(closes) >= p + _HUSAAM_EMA10_FLAT_LOOKBACK:
        ema_now = calc_ema(closes, p)
        ema_old = calc_ema(closes[: len(closes) - _HUSAAM_EMA10_FLAT_LOOKBACK], p)
        if ema_now > 0 and abs(ema_now - ema_old) / ema_now < _HUSAAM_EMA10_MAX_FLAT_SLOPE_PCT:
            return True
    if _husaam_ema10_sign_flips(closes, p, ent, lookback=15) > _HUSAAM_EMA10_MAX_SIGN_FLIPS:
        return True
    return False


def _analyze_husaam_ema10_signal(candles) -> tuple:
    """
    CALL: ترند صاعد — حمراء إشارية على EMA ثم خضراء تأكيد (شموع مغلقة).
    PUT:  ترند هابط — خضراء إشارية على EMA ثم حمراء تأكيد (لا دخول قبل إغلاق الخضراء).
    آخر 40 شمعة 1m؛ EMA10 على نفس السلسلة؛ لا تيكات.
    """
    closed = _ema10_closed_only(candles)
    kit = _candles_ohlc_kit(closed)
    p = _HUSAAM_EMA10_PERIOD
    if len(kit) < _HUSAAM_EMA10_ANALYSIS_BARS:
        return "wait", 0
    kit = kit[-_HUSAAM_EMA10_ANALYSIS_BARS :]
    closes = [float(c["close"]) for c in kit]
    if len(closes) < _HUSAAM_EMA10_ANALYSIS_BARS:
        return "wait", 0

    sig = len(kit) - 2
    ent = len(kit) - 1
    if sig < _HUSAAM_EMA10_CONTEXT_BEFORE_SIGNAL:
        return "wait", 0

    o_s = float(kit[sig]["open"])
    h_s = float(kit[sig]["high"])
    low_s = float(kit[sig]["low"])
    cl_s = float(kit[sig]["close"])
    o_e = float(kit[ent]["open"])
    h_e = float(kit[ent]["high"])
    low_e = float(kit[ent]["low"])
    cl_e = float(kit[ent]["close"])
    if min(cl_s, cl_e) <= 0 or h_s < low_s or h_e < low_e:
        return "wait", 0

    call_pattern = cl_s < o_s and cl_e > o_e
    put_pattern = cl_s > o_s and cl_e < o_e
    if not call_pattern and not put_pattern:
        return "wait", 0

    ema_sig = _husaam_ema10_ema_at(closes, sig, p)
    ema_ent = _husaam_ema10_ema_at(closes, ent, p)
    if ema_sig <= 0 or ema_ent <= 0:
        return "wait", 0

    close_tol = max(ema_sig * _HUSAAM_EMA10_CLOSE_TOL_PCT, 1e-12)
    touch_tol = max(ema_sig * _HUSAAM_EMA10_LOW_TOUCH_PCT, 1e-12)
    break_tol = max(ema_sig * (_HUSAAM_EMA10_BREAKDOWN_EXTRA_PCT + _HUSAAM_EMA10_CLOSE_TOL_PCT * 0.5), 1e-12)

    if call_pattern:
        # كسر واضح أسفل EMA على الشمعة الحمراء → رفض
        if cl_s < ema_sig - break_tol:
            return "wait", 0
        if not _husaam_ema10_red_near_ema(cl_s, low_s, ema_sig, close_tol, touch_tol):
            return "wait", 0
        above = 0
        for k in range(sig - _HUSAAM_EMA10_CONTEXT_BEFORE_SIGNAL, sig):
            ema_k = _husaam_ema10_ema_at(closes, k, p)
            if ema_k <= 0:
                return "wait", 0
            ct = max(ema_k * _HUSAAM_EMA10_CLOSE_TOL_PCT, 1e-12)
            if closes[k] >= ema_k - 0.5 * ct:
                above += 1
        if above < _HUSAAM_EMA10_CONTEXT_ABOVE_MIN:
            return "wait", 0
        rng_e = h_e - low_e
        if rng_e <= 1e-12:
            return "wait", 0
        body_e = cl_e - o_e
        if body_e / rng_e < _HUSAAM_EMA10_MIN_GREEN_BODY_RATIO:
            return "wait", 0
        touch_tol_ent = max(ema_ent * _HUSAAM_EMA10_LOW_TOUCH_PCT, 1e-12)
        if cl_e < ema_ent - 0.35 * touch_tol_ent:
            return "wait", 0
        if o_e < ema_ent - 3.5 * touch_tol_ent:
            return "wait", 0
        if _husaam_ema10_reject_flat_or_choppy(closes, p, ent):
            return "wait", 0
        log.debug(
            "📌 NEXORA_EMA10 CALL red@sig=%s green@ent=%s ema_sig=%.5f ema_ent=%.5f",
            sig,
            ent,
            ema_sig,
            ema_ent,
        )
        return "call", _HUSAAM_EMA10_SCORE

    # PUT: كسر واضح للأعلى على الشمعة الخضراء الإشارية → رفض (ليس رفضاً للصعود)
    if cl_s > ema_sig + break_tol:
        return "wait", 0
    if not _husaam_ema10_green_near_ema(cl_s, h_s, ema_sig, close_tol, touch_tol):
        return "wait", 0
    below = 0
    for k in range(sig - _HUSAAM_EMA10_CONTEXT_BEFORE_SIGNAL, sig):
        ema_k = _husaam_ema10_ema_at(closes, k, p)
        if ema_k <= 0:
            return "wait", 0
        ct = max(ema_k * _HUSAAM_EMA10_CLOSE_TOL_PCT, 1e-12)
        if closes[k] <= ema_k + 0.5 * ct:
            below += 1
    if below < _HUSAAM_EMA10_CONTEXT_BELOW_MIN:
        return "wait", 0
    rng_e = h_e - low_e
    if rng_e <= 1e-12:
        return "wait", 0
    body_down = o_e - cl_e
    if body_down / rng_e < _HUSAAM_EMA10_MIN_RED_BODY_RATIO:
        return "wait", 0
    touch_tol_ent = max(ema_ent * _HUSAAM_EMA10_LOW_TOUCH_PCT, 1e-12)
    if cl_e > ema_ent + 0.35 * touch_tol_ent:
        return "wait", 0
    if o_e > ema_ent + 3.5 * touch_tol_ent:
        return "wait", 0
    if _husaam_ema10_reject_flat_or_choppy(closes, p, ent):
        return "wait", 0
    log.debug(
        "📌 NEXORA_EMA10 PUT green@sig=%s red@ent=%s ema_sig=%.5f ema_ent=%.5f",
        sig,
        ent,
        ema_sig,
        ema_ent,
    )
    return "put", _HUSAAM_EMA10_SCORE


def _analyze_husaam_private_signal(candles) -> tuple:
    """
    استراتيجية NEXORA الخاصة — MACD + هستوغرام على شموع 1m مغلقة فقط.
    هابط: تحت EMA + هستوغرام < 0 + أول شمعة خضراء بالهستوغرام + شمعة سعر خضراء + الخطان فوق الهستو → PUT.
    صاعد: فوق EMA + هستوغرام > 0 + 6 شموع خضراء صاعدة + شمعة سعر حمراء + الخطان تحت الهستو → CALL.
    """
    kit = _candles_ohlc_kit(_ema10_closed_only(candles))
    if len(kit) < _HUSAAM_PRIVATE_MIN_BARS:
        return "wait", 0
    closes = [float(c["close"]) for c in kit]
    macd_line, signal_line, hist = calc_macd_series(closes)
    if macd_line is None:
        return "wait", 0
    i = len(kit) - 1
    o_i = float(kit[i]["open"])
    c_i = float(kit[i]["close"])
    ema_t = _husaam_ema10_ema_at(closes, i, _HUSAAM_PRIVATE_TREND_EMA)
    if ema_t <= 0:
        return "wait", 0

    # CALL: ترند صاعد، هستو فوق الصفر، 6 هستوغرامات خضراء متتالية صاعدة، شمعة حمراء، خطا MACD تحت الهستو
    if i >= 32 and c_i < o_i and c_i > ema_t:
        ok = True
        for k in range(i - 6, i):
            if k < 26:
                ok = False
                break
            if hist[k] <= 0 or hist[k] <= hist[k - 1]:
                ok = False
                break
        if ok and macd_line[i] < hist[i] and signal_line[i] < hist[i]:
            log.debug(
                "📌 NEXORA_PRIVATE CALL i=%s hist=%.8f macd=%.8f sig=%.8f",
                i,
                hist[i],
                macd_line[i],
                signal_line[i],
            )
            return "call", _HUSAAM_PRIVATE_SCORE

    # PUT: ترند هابط، هستو تحت الصفر، أول شمعة خضراء بالهستوغرام، شمعة سعر خضراء، خطا MACD فوق الهستو
    if i >= 27 and c_i > o_i and c_i < ema_t:
        if (
            hist[i] < 0
            and hist[i] > hist[i - 1]
            and hist[i - 1] <= hist[i - 2]
            and macd_line[i] > hist[i]
            and signal_line[i] > hist[i]
        ):
            log.debug(
                "📌 NEXORA_PRIVATE PUT i=%s hist=%.8f macd=%.8f sig=%.8f",
                i,
                hist[i],
                macd_line[i],
                signal_line[i],
            )
            return "put", _HUSAAM_PRIVATE_SCORE

    return "wait", 0


def analyze(candles, strategy) -> str:
    if strategy == "HUSAAM_EMA10":
        d, _ = _analyze_husaam_ema10_signal(candles)
        return d
    if strategy == "HUSAAM_PRIVATE":
        d, _ = _analyze_husaam_private_signal(candles)
        return d
    if strategy == "HUSAAM":
        return _analyze_husaam_strict(candles)
    if len(candles) < 35: return "wait"
    closes = []
    for c in candles:
        p = 0
        if isinstance(c, dict):
            p = float(c.get("close", c.get("c", 0)) or 0)
        elif hasattr(c, "close"):
            p = float(c.close or 0)
        if p > 0: closes.append(p)
    if len(closes) < 35: return "wait"

    rsi         = calc_rsi(closes)
    _, _, hist  = calc_macd(closes)
    bb          = calc_bb(closes)
    ema9        = calc_ema(closes, 9)
    ema21       = calc_ema(closes, 21)
    ema5        = calc_ema(closes, 5)
    stoch       = calc_stoch(closes)
    williams    = calc_williams(closes)
    # اتجاه الشمعات الأخيرة
    trend_up    = sum(1 for i in range(-5,-1) if closes[i] < closes[i+1])
    trend_dn    = sum(1 for i in range(-5,-1) if closes[i] > closes[i+1])

    log.info(f"📊 RSI={rsi} MACD={hist:.6f} BB%={bb} Stoch={stoch} W%R={williams} EMA5/9/21={ema5:.5f}/{ema9:.5f}/{ema21:.5f}")

    cp = pp = 0

    # ── استراتيجية النخبة — تجمع كل المؤشرات بأوزان (HUSAAM لها منطق منفصل أعلاه) ──
    if strategy == "ELITE":
        # ── RSI — مؤشر الزخم (وزن عالي) ──
        if rsi < 20:     cp += 5
        elif rsi < 28:   cp += 4
        elif rsi < 35:   cp += 3
        elif rsi < 42:   cp += 1
        if rsi > 80:     pp += 5
        elif rsi > 72:   pp += 4
        elif rsi > 65:   pp += 3
        elif rsi > 58:   pp += 1

        # ── MACD — اتجاه الزخم (وزن عالي) ──
        if hist > 0.0001:   cp += 4
        elif hist > 0:      cp += 2
        if hist < -0.0001:  pp += 4
        elif hist < 0:      pp += 2

        # ── Bollinger Bands — حدود السعر ──
        if bb < 5:     cp += 5
        elif bb < 15:  cp += 4
        elif bb < 25:  cp += 2
        if bb > 95:    pp += 5
        elif bb > 85:  pp += 4
        elif bb > 75:  pp += 2

        # ── Stochastic — تأكيد الزخم ──
        if stoch < 15:   cp += 4
        elif stoch < 25: cp += 3
        elif stoch < 35: cp += 1
        if stoch > 85:   pp += 4
        elif stoch > 75: pp += 3
        elif stoch > 65: pp += 1

        # ── Williams %R — نقاط الانعكاس ──
        if williams < -85:   cp += 4
        elif williams < -70: cp += 3
        elif williams < -60: cp += 1
        if williams > -15:   pp += 4
        elif williams > -30: pp += 3
        elif williams > -40: pp += 1

        # ── EMA Cross — اتجاه السوق ──
        if ema5 > ema9 > ema21:   cp += 4
        elif ema5 < ema9 < ema21: pp += 4
        elif ema5 > ema9:  cp += 2
        elif ema5 < ema9:  pp += 2
        elif ema9 > ema21: cp += 1
        elif ema9 < ema21: pp += 1

        # ── اتجاه الشمعات الأخيرة ──
        if trend_up >= 4:   cp += 3
        elif trend_up >= 3: cp += 2
        elif trend_up >= 2: cp += 1
        if trend_dn >= 4:   pp += 3
        elif trend_dn >= 3: pp += 2
        elif trend_dn >= 2: pp += 1

        # ── تأكيد إضافي: RSI + MACD + EMA في نفس الاتجاه ──
        if (rsi < 40) and (hist > 0) and (ema9 > ema21): cp += 3
        if (rsi > 60) and (hist < 0) and (ema9 < ema21): pp += 3

        # ── Stoch + Williams في نفس الاتجاه ──
        if stoch < 30 and williams < -70: cp += 2
        if stoch > 70 and williams > -30: pp += 2

        log.info(f"🏆 NEXORA: CALL={cp} PUT={pp} diff={abs(cp-pp)}")
        # عتبة 6 نقاط للدخول — توازن بين الدقة والكمية
        if cp >= pp + 6: return "call"
        if pp >= cp + 6: return "put"
        return "wait"

    # ── الاستراتيجيات الأخرى ──────────────────────────────────────────────
    if strategy in ("RSI","RSI_MACD","ALL"):
        if rsi < 25:   cp += 3
        elif rsi < 35: cp += 2
        elif rsi < 45: cp += 1
        if rsi > 75:   pp += 3
        elif rsi > 65: pp += 2
        elif rsi > 55: pp += 1
    if strategy in ("MACD","RSI_MACD","ALL"):
        if hist > 0:   cp += 2
        elif hist < 0: pp += 2
    if strategy in ("BB","ALL"):
        if bb < 15:    cp += 3
        elif bb < 30:  cp += 2
        elif bb < 40:  cp += 1
        if bb > 85:    pp += 3
        elif bb > 70:  pp += 2
        elif bb > 60:  pp += 1
    if strategy == "ALL":
        if ema9 > ema21: cp += 1
        else:            pp += 1

    log.info(f"🗳️ CALL={cp} PUT={pp}")
    if cp >= pp+2: return "call"
    if pp >= cp+2: return "put"
    return "wait"

def analyze_score(candles, strategy) -> tuple:
    """ترجع (direction, score) — score كلما كبر كلما كانت الإشارة أقوى"""
    if strategy == "HUSAAM_EMA10":
        return _analyze_husaam_ema10_signal(candles)
    if strategy == "HUSAAM_PRIVATE":
        return _analyze_husaam_private_signal(candles)
    if strategy == "HUSAAM":
        d = _analyze_husaam_strict(candles)
        return (d, _HUSAAM_STRICT_SCORE) if d != "wait" else ("wait", 0)
    if len(candles) < 35: return "wait", 0
    closes = []
    for c in candles:
        p = 0
        if isinstance(c, dict):
            p = float(c.get("close", c.get("c", 0)) or 0)
        elif hasattr(c, "close"):
            p = float(c.close or 0)
        if p > 0: closes.append(p)
    if len(closes) < 35: return "wait", 0
    rsi        = calc_rsi(closes)
    _, _, hist = calc_macd(closes)
    bb         = calc_bb(closes)
    ema9       = calc_ema(closes, 9)
    ema21      = calc_ema(closes, 21)
    ema5       = calc_ema(closes, 5)
    stoch      = calc_stoch(closes)
    williams   = calc_williams(closes)
    trend_up   = sum(1 for i in range(-5,-1) if closes[i] < closes[i+1])
    trend_dn   = sum(1 for i in range(-5,-1) if closes[i] > closes[i+1])
    cp = pp = 0
    if strategy == "ELITE":
        if rsi < 20:     cp += 5
        elif rsi < 28:   cp += 4
        elif rsi < 35:   cp += 3
        elif rsi < 42:   cp += 1
        if rsi > 80:     pp += 5
        elif rsi > 72:   pp += 4
        elif rsi > 65:   pp += 3
        elif rsi > 58:   pp += 1
        if hist > 0.0001:   cp += 4
        elif hist > 0:      cp += 2
        if hist < -0.0001:  pp += 4
        elif hist < 0:      pp += 2
        if bb < 5:     cp += 5
        elif bb < 15:  cp += 4
        elif bb < 25:  cp += 2
        if bb > 95:    pp += 5
        elif bb > 85:  pp += 4
        elif bb > 75:  pp += 2
        if stoch < 15:   cp += 4
        elif stoch < 25: cp += 3
        elif stoch < 35: cp += 1
        if stoch > 85:   pp += 4
        elif stoch > 75: pp += 3
        elif stoch > 65: pp += 1
        if williams < -85:   cp += 4
        elif williams < -70: cp += 3
        elif williams < -60: cp += 1
        if williams > -15:   pp += 4
        elif williams > -30: pp += 3
        elif williams > -40: pp += 1
        if ema5 > ema9 > ema21:   cp += 4
        elif ema5 < ema9 < ema21: pp += 4
        elif ema5 > ema9:  cp += 2
        elif ema5 < ema9:  pp += 2
        elif ema9 > ema21: cp += 1
        elif ema9 < ema21: pp += 1
        if trend_up >= 4:   cp += 3
        elif trend_up >= 3: cp += 2
        elif trend_up >= 2: cp += 1
        if trend_dn >= 4:   pp += 3
        elif trend_dn >= 3: pp += 2
        elif trend_dn >= 2: pp += 1
        if (rsi < 40) and (hist > 0) and (ema9 > ema21): cp += 3
        if (rsi > 60) and (hist < 0) and (ema9 < ema21): pp += 3
        if stoch < 30 and williams < -70: cp += 2
        if stoch > 70 and williams > -30: pp += 2
        diff = cp - pp
        if diff >= 6:  return "call", diff
        if diff <= -6: return "put",  abs(diff)
        return "wait", 0
    if strategy in ("RSI","RSI_MACD","ALL"):
        if rsi < 25: cp += 3
        elif rsi < 35: cp += 2
        elif rsi < 45: cp += 1
        if rsi > 75: pp += 3
        elif rsi > 65: pp += 2
        elif rsi > 55: pp += 1
    if strategy in ("MACD","RSI_MACD","ALL"):
        if hist > 0: cp += 2
        elif hist < 0: pp += 2
    if strategy in ("BB","ALL"):
        if bb < 15: cp += 3
        elif bb < 30: cp += 2
        elif bb < 40: cp += 1
        if bb > 85: pp += 3
        elif bb > 70: pp += 2
        elif bb > 60: pp += 1
    if strategy == "ALL":
        if ema9 > ema21: cp += 1
        else:            pp += 1
    diff = cp - pp
    if diff >= 2:  return "call", diff
    if diff <= -2: return "put",  abs(diff)
    return "wait", 0

def _fallback_direction_from_candles(candles) -> tuple:
    """إشارة بديلة محسّنة: تدخل فقط مع توافق اتجاه واضح."""
    closes = []
    for c in candles or []:
        p = 0.0
        if isinstance(c, dict):
            p = float(c.get("close", c.get("c", 0)) or 0)
        elif hasattr(c, "close"):
            p = float(c.close or 0)
        if p > 0:
            closes.append(p)
    if len(closes) < 20:
        return "wait", 0

    ema9 = calc_ema(closes, 9)
    ema21 = calc_ema(closes, 21)
    mom = closes[-1] - closes[-4]
    up = sum(1 for i in range(-6, -1) if closes[i] > closes[i - 1])
    dn = sum(1 for i in range(-6, -1) if closes[i] < closes[i - 1])
    ema_gap_pct = abs(ema9 - ema21) / max(abs(ema21), 1e-12)
    last_range = max(closes[-6:]) - min(closes[-6:])
    if last_range <= 0:
        return "wait", 0
    last_body_strength = abs(closes[-1] - closes[-2]) / last_range

    # شروط fallback الصارمة لتقليل العشوائية:
    # 1) اتجاه EMA واضح
    # 2) زخم آخر 4 شموع بنفس الاتجاه
    # 3) أغلب آخر 5 انتقالات بنفس الاتجاه
    # 4) حركة آخر شمعة ليست ضعيفة جداً
    if ema9 > ema21 and ema_gap_pct >= 0.00040 and mom > 0 and up >= 4 and last_body_strength >= 0.26:
        return "call", 2
    if ema9 < ema21 and ema_gap_pct >= 0.00040 and mom < 0 and dn >= 4 and last_body_strength >= 0.26:
        return "put", 2
    return "wait", 0

# ── مجمع الأسعار الحية ────────────────────────────────────────────────────────
# - جميع الاستراتيجيات (ما عدا الحاجة الخاصة): سحب شموع 1m من شارت Quotex مثل EMA10 — لا تيكات للإشارة.
# - HUSAAM_PRIVATE: نفس السحب لكن آخر 55 شمعة بعد التنظيف.
# - التيكات / realtime: مراقبة وواجهة فقط.
_price_buffers: dict = {}
# احتفاظ أطول لبناء شموع 5ث (35 شمعة ≈ 175ث جدارياً على الأقل)
_PRICE_BUFFER_RETENTION_SEC = 1800
# تدفئة: تيكات للمراقبة/الواجهة فقط — لـ EMA10 لا تُستخدم في الإشارة
_WARMUP_COLLECT_SEC = 180
_WARMUP_COLLECT_SEC_EMA10 = 25


def _quotex_tick_keys(client, asset: str):
    """مفاتيح تيك السعر: الرمز أولاً (غالباً يطابق message[0][0]) ثم رقم الأصل."""
    keys = [asset]
    if getattr(client, "codes_asset", None):
        aid = client.codes_asset.get(asset)
        if aid is not None and aid not in keys:
            keys.append(aid)
    seen = set()
    out = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _parse_rt_price(price) -> float:
    if isinstance(price, dict):
        return float(price.get("price", price.get("close", 0)) or 0)
    if isinstance(price, list) and len(price) > 0:
        last = price[-1]
        if isinstance(last, dict):
            return float(last.get("price", 0) or 0)
        try:
            return float(last or 0)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(price or 0)
    except (TypeError, ValueError):
        return 0.0


async def _ensure_quotex_assets(client):
    try:
        if hasattr(client, "get_all_assets") and len(getattr(client, "codes_asset", {}) or {}) == 0:
            await client.get_all_assets()
    except Exception as e:
        log.debug("get_all_assets: %s", e)


async def _start_price_stream(client, asset):
    """يشترك في التيكات بدون انتظار start_realtime_price (حلقة لا نهائية في المكتبة + مهلة قصيرة كانت تُلغي الاشتراك)."""
    try:
        if not hasattr(client, "start_candles_stream"):
            return
        keys = _quotex_tick_keys(client, asset)
        k0 = keys[0]
        # pyquotex Stable.period_default=60؛ الفترة 0 تُرسل للسيرفر كاشتراك غير صالح → لا تيكات ولا شموع حية.
        stream_period = max(1, int(os.getenv("QUOTEX_STREAM_PERIOD_SEC", str(_HUSAAM_EMA10_CANDLE_SECS)) or _HUSAAM_EMA10_CANDLE_SECS))
        client.start_candles_stream(k0, stream_period)
        # المكتبة تُلحق التيك بـ message[0][0]؛ إن اختلف المفتاح عن المشترك يحدث KeyError ويُبتلع — نُهيئ كل المفاتيح
        api = getattr(client, "api", None)
        if api is not None:
            for k in keys:
                api.realtime_price.setdefault(k, [])
        await asyncio.sleep(0.35)
        log.info("📡 stream: %s (مفتاح التيك=%s)", asset, k0)
    except Exception as e:
        log.warning("start_candles_stream(%s): %s", asset, e)


def _append_price_tick(asset: str, p: float) -> None:
    if p <= 0:
        return
    if asset not in _price_buffers:
        _price_buffers[asset] = []
    _price_buffers[asset].append({"price": p, "time": time.time()})
    cutoff = time.time() - _PRICE_BUFFER_RETENTION_SEC
    _price_buffers[asset] = [x for x in _price_buffers[asset] if x["time"] > cutoff]


def _price_from_realtime_candles_tuple(client) -> float:
    """آخر تيك من realtime_candles (رسالة طول 4 في pyquotex)."""
    try:
        ca = getattr(client.api, "current_asset", None)
        if not ca:
            return 0.0
        rc = client.api.realtime_candles.get(ca)
        if isinstance(rc, (list, tuple)) and len(rc) >= 3:
            return float(rc[2] or 0)
    except Exception:
        pass
    return 0.0


async def _collect_price_async(client, asset) -> float:
    """يقرأ التيك من أي مفتاح مطابق (رقم أو رمز) مع انتظار قصير بين المحاولات."""
    keys = _quotex_tick_keys(client, asset)
    try:
        for _ in range(55):
            for key in keys:
                try:
                    price = await client.get_realtime_price(key)
                except Exception:
                    continue
                p = _parse_rt_price(price)
                if p > 0:
                    _append_price_tick(asset, p)
                    return p
            p2 = _price_from_realtime_candles_tuple(client)
            if p2 > 0:
                _append_price_tick(asset, p2)
                return p2
            await asyncio.sleep(0.2)
    except Exception:
        pass
    return 0

def _collect_price(client, asset, email: str = "") -> float:
    """جلب سعر حي — يجب تشغيله على نفس event loop الخاصة بجلسة Quotex (connect)."""
    try:
        if email and QX:
            p = run_async_for(email, _collect_price_async(client, asset), timeout=12)
        else:
            p = run_async(_collect_price_async(client, asset), timeout=12)
        return p or 0
    except Exception:
        return 0

def _build_candles(asset, candle_secs=5) -> list:
    if asset not in _price_buffers or len(_price_buffers[asset]) < 10:
        return []
    prices = sorted(_price_buffers[asset], key=lambda x: x["time"])
    candles, buf, t0 = [], [], prices[0]["time"] - (prices[0]["time"] % candle_secs)
    for e in prices:
        ct = e["time"] - (e["time"] % candle_secs)
        if ct == t0:
            buf.append(e["price"])
        else:
            if buf:
                candles.append({"open":buf[0],"high":max(buf),"low":min(buf),"close":buf[-1]})
            buf = [e["price"]]; t0 = ct
    if buf:
        candles.append({"open":buf[0],"high":max(buf),"low":min(buf),"close":buf[-1]})
    log.debug("📊 %s شمعة من %s سعر", len(candles), len(prices))
    return candles

_husaam_api_candle_cache: dict = {}  # asset -> {t, candles, source: "quotex"}
_husaam_ema10_analysis_log_ts: dict = {}  # asset -> وقت آخر log تفصيلي (تخفيف تكرار الكاش)


def _normalize_quotex_candle_row(c):
    """يحوّل عنصر شمعة من Quotex إلى {open,high,low,close,time?}."""
    if isinstance(c, dict):
        cl = float(c.get("close", 0) or 0)
        if cl <= 0:
            return None
        o = float(c.get("open", cl) or cl)
        h = float(c.get("high", cl) or cl)
        l = float(c.get("low", cl) or cl)
        row = {"open": o, "high": h, "low": l, "close": cl}
        ts = c.get("time")
        if ts is not None:
            row["time"] = float(ts)
        return row
    if isinstance(c, (list, tuple)) and len(c) >= 5:
        try:
            t0, o, cl, h, l = (float(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]))
        except (TypeError, ValueError):
            return None
        if cl <= 0:
            return None
        row = {"open": o, "high": h, "low": l, "close": cl, "time": t0}
        return row
    return None


def _sort_candles_by_time(rows: list) -> list:
    """أقدم → أحدث حسب time (للشموع من v2 قد يعيد السيرفر ترتيباً عشوائياً)."""
    if not rows:
        return []
    return sorted(
        rows,
        key=lambda r: float(r.get("time", 0)) if isinstance(r, dict) else 0.0,
    )


def _prepare_husaam_ema10_candles_for_analysis(rows: list, max_bars=None) -> list:
    """ترتيب زمني أقدم→أحدث، إزالة تكرار الوقت، OHLC صالحة، آخر max_bars شمعة (افتراضي 40)."""
    if not rows:
        return []
    n = max_bars if max_bars is not None else _HUSAAM_EMA10_ANALYSIS_BARS
    if n < 1:
        n = _HUSAAM_EMA10_ANALYSIS_BARS
    tmp = []
    for r in rows:
        row = _normalize_quotex_candle_row(r)
        if not row:
            continue
        o = float(row.get("open", 0) or 0)
        cl = float(row.get("close", 0) or 0)
        h = float(row.get("high", 0) or 0)
        l = float(row.get("low", 0) or 0)
        if cl <= 0:
            continue
        hi = max(o, h, cl, l)
        lo = min(o, h, cl, l)
        d = {"open": o, "high": hi, "low": lo, "close": cl}
        ts = row.get("time")
        if ts is not None:
            d["time"] = float(ts)
        tmp.append(d)
    tmp.sort(key=lambda x: x.get("time", 0.0))
    by_t = {}
    for d in tmp:
        t = d.get("time")
        if t is None:
            continue
        by_t[t] = d
    out = sorted(by_t.values(), key=lambda x: x["time"])
    return out[-n:]


async def _quotex_prepare_candles_session(client, asset: str, period: int) -> None:
    """ضبط current_asset والاشتراك في الستريم قبل get_candle_v2 / get_candles (مطلوب pyquotex)."""
    try:
        client.api.current_asset = asset
        client.start_candles_stream(asset, period)
        await asyncio.sleep(1.0)
    except Exception as e:
        log.debug("quotex_prepare_candles_session: %s", e)


def _extract_v2_chart_ohlc_candles(client, asset: str) -> list:
    """شموع 1m جاهزة من history/list/v2 في candle_v2_data['candles'] — ليست ناتج prepare_candles من التيكات."""
    api = getattr(client, "api", None)
    if api is None:
        return []
    keys = [asset]
    codes = getattr(api, "codes_asset", None) or {}
    aid = codes.get(asset)
    if aid is not None and aid not in keys:
        keys.append(aid)
    msg = None
    for k in keys:
        m = api.candle_v2_data.get(k) if hasattr(api, "candle_v2_data") else None
        if isinstance(m, dict) and m.get("candles"):
            msg = m
            break
    if not msg:
        return []
    out = []
    for c in msg.get("candles") or []:
        row = _normalize_quotex_candle_row(c)
        if row:
            out.append(row)
    return _sort_candles_by_time(out)


async def _fetch_quotex_minute_once(client, asset: str) -> list:
    """شموع 1m: أصل نصّي — ترتيب: تهيئة أصل + stream + انتظار → v2 ثم history/load (offset صغير)."""
    period = _HUSAAM_EMA10_CANDLE_SECS
    need_slots = max(_HUSAAM_EMA10_ANALYSIS_BARS, _HUSAAM_PRIVATE_MIN_BARS) + 5
    off = max(20, need_slots) * period

    try:
        if hasattr(client, "get_all_assets") and len(getattr(client, "codes_asset", {}) or {}) == 0:
            await client.get_all_assets()
    except Exception:
        pass

    _HUSAAM_WS_LAST["patched_fill"] = False
    await _quotex_prepare_candles_session(client, asset, period)
    stream_ok = getattr(getattr(client, "api", None), "current_asset", None) == asset
    cur = getattr(getattr(client, "api", None), "current_asset", None)

    best: list = []

    try:
        try:
            client.api.candle_v2_data[asset] = None
        except Exception:
            pass
        log.info(
            "📊 EMA10: طلب get_candle_v2 بعد stream | asset=%s current=%s period=%s",
            asset,
            cur,
            period,
        )
        raw_v2 = await asyncio.wait_for(client.get_candle_v2(asset, period), timeout=5.0)
    except Exception as e:
        log.debug("get_candle_v2(%s): %s", asset, e)
        raw_v2 = []
    if raw_v2:
        out_v2 = []
        for c in raw_v2:
            row = _normalize_quotex_candle_row(c)
            if row:
                out_v2.append(row)
        if len(out_v2) > len(best):
            best = out_v2
    v2_ohlc = _extract_v2_chart_ohlc_candles(client, asset)
    if len(v2_ohlc) > len(best):
        best = v2_ohlc
        log.info(
            "📊 EMA10: شموع OHLC من candle_v2_data (عدد=%s) — أوضح من تجميع التيكات في prepare_candles",
            len(best),
        )
    if len(best) >= _HUSAAM_EMA10_ANALYSIS_BARS:
        best = _sort_candles_by_time(best)
        _log_husaam_fetch_diag(asset, cur, period, need_slots, off, best, stream_ok, False, False)
        return best

    try:
        client.api.candles.candles_data = None
    except Exception:
        pass
    log.info(
        "📊 EMA10: طلب get_candles (history/load) | asset=%s offset_sec=%s period=%s stream_ok=%s",
        asset,
        off,
        period,
        stream_ok,
    )
    try:
        raw = await asyncio.wait_for(
            client.get_candles(asset, int(time.time()), off, period),
            timeout=8.0,
        )
    except asyncio.TimeoutError:
        log.debug("get_candles timeout (%s)", asset)
        raw = []
    except Exception as e:
        log.warning("get_candles(%s): %s", asset, e)
        raw = []
    if raw:
        out = []
        for c in raw:
            row = _normalize_quotex_candle_row(c)
            if row:
                out.append(row)
        if len(out) > len(best):
            best = out
    v2_ohlc2 = _extract_v2_chart_ohlc_candles(client, asset)
    if len(v2_ohlc2) > len(best):
        best = v2_ohlc2
        log.info("📊 EMA10: بعد get_candles — أخذنا %s شمعة من candle_v2_data", len(best))

    best = _sort_candles_by_time(best)
    hist_sent = True
    _log_husaam_fetch_diag(asset, cur, period, need_slots, off, best, stream_ok, hist_sent, True)
    return best


def _log_husaam_fetch_diag(
    asset: str,
    current_asset,
    period: int,
    need_slots: int,
    off_sec: int,
    best: list,
    stream_ok: bool,
    history_sent: bool,
    after_get_candles: bool,
) -> None:
    n = len(best)
    first_ts = last_ts = None
    if best:
        times = [
            float(x["time"])
            for x in best
            if isinstance(x, dict) and x.get("time") is not None
        ]
        if times:
            first_ts = int(min(times))
            last_ts = int(max(times))
    _diag_n = max(_HUSAAM_EMA10_ANALYSIS_BARS, _HUSAAM_PRIVATE_MIN_BARS)
    prep = _prepare_husaam_ema10_candles_for_analysis(best, max_bars=_diag_n) if best else []
    cleaned = len(prep)
    ok40 = cleaned >= _HUSAAM_EMA10_ANALYSIS_BARS
    ws = _HUSAAM_WS_LAST
    log.info(
        "📊 EMA10 pipeline | مطلوب=%s | current=%s | stream_ok=%s | history_sent=%s | "
        "after_ws_get_candles=%s | ws_last_hist_len=%s | ws_patched=%s | "
        "offset_sec=%s period=%s | raw_rows=%s | cleaned=%s | 40/40=%s | first_ts=%s last_ts=%s",
        asset,
        current_asset,
        stream_ok,
        history_sent,
        after_get_candles,
        ws.get("last_history_len"),
        ws.get("patched_fill"),
        off_sec,
        period,
        n,
        cleaned,
        ok40,
        first_ts,
        last_ts,
    )


async def _fetch_quotex_minute_candles_async(client, asset: str) -> list:
    """محاولة ثانية خفيفة إذا بقي العدد أقل من 40 شمعة خام."""
    best = await _fetch_quotex_minute_once(client, asset)
    if len(best) >= _HUSAAM_EMA10_ANALYSIS_BARS:
        return best
    await asyncio.sleep(1.5)
    try:
        if hasattr(client, "get_all_assets"):
            await client.get_all_assets()
    except Exception:
        pass
    best2 = await _fetch_quotex_minute_once(client, asset)
    if len(best2) > len(best):
        best = best2
    return best


def _get_husaam_ema10_candles(client, email: str, asset: str, need_len: int, analysis_bars=None) -> tuple:
    """يعيد (شموع للتحليل, مصدر). التحليل من آخر N شمعة 1m من Quotex بعد التنظيف — لا تيكات."""
    tgt = analysis_bars if analysis_bars is not None else _HUSAAM_EMA10_ANALYSIS_BARS
    if not QX or not client:
        return _build_candles(asset, candle_secs=_HUSAAM_EMA10_CANDLE_SECS), "sim_tick_1m"
    now = time.time()
    ce = _husaam_api_candle_cache.get(asset)

    def _finalize(raw: list, label: str) -> tuple:
        prep = _prepare_husaam_ema10_candles_for_analysis(raw, max_bars=tgt)
        if len(prep) < tgt:
            return [], label + "_insufficient"
        out = prep[-tgt:] if len(prep) > tgt else prep
        cur = getattr(getattr(client, "api", None), "current_asset", None)
        t0 = out[0].get("time")
        t1 = out[-1].get("time")
        _tl = time.time()
        _throttle = label == "quotex_1m_cache" and (_tl - _husaam_ema10_analysis_log_ts.get(asset, 0) < 30)
        if not _throttle:
            log.info(
                "📊 EMA10 analysis: asset=%s current_asset=%s period=60 need=%s got=%s first_ts=%s last_ts=%s src=%s",
                asset,
                cur,
                tgt,
                len(out),
                int(t0) if t0 is not None else None,
                int(t1) if t1 is not None else None,
                label,
            )
            _husaam_ema10_analysis_log_ts[asset] = _tl
        return out, label

    if (
        ce
        and ce.get("source") == "quotex"
        and (now - ce["t"]) < _HUSAAM_QUOTEX_CACHE_SEC
    ):
        prep0 = _prepare_husaam_ema10_candles_for_analysis(
            ce.get("candles", []), max_bars=tgt
        )
        if len(prep0) >= tgt:
            return _finalize(ce["candles"], "quotex_1m_cache")

    try:
        lst = run_async_for(email, _fetch_quotex_minute_candles_async(client, asset), _HUSAAM_QUOTEX_FETCH_TIMEOUT_SEC)
    except Exception as e:
        log.warning("جلب شموع Quotex: %s", e)
        lst = []

    if lst:
        _husaam_api_candle_cache[asset] = {"t": now, "candles": lst, "source": "quotex"}
        got, lab = _finalize(lst, "quotex_1m")
        if got:
            return got, lab

    if ce and ce.get("candles"):
        log.warning("⚠️ fallback: كاش شموع قديم لـ %s (أقل من %s بعد التنظيف قد يفشل)", asset, tgt)
        got, lab = _finalize(ce["candles"], "quotex_stale_cache")
        if got:
            return got, lab

    log.warning(
        "⚠️ بيانات غير كافية لجلب شموع 1m — لا توجد %s شمعة دقيقة من الشارت لـ %s (بعد التنظيف)",
        tgt,
        asset,
    )
    return [], "quotex_insufficient"

# ══════════════════════════════════════════════════════════════════════════════
# Quotex Operations
# ══════════════════════════════════════════════════════════════════════════════
def _extract_balance(p):
    real = demo = 0.0
    try:
        if isinstance(p, dict):
            real = float(p.get("live_balance", p.get("liveBalance", p.get("real_balance",0))) or 0)
            demo = float(p.get("demo_balance", p.get("demoBalance", p.get("practice_balance",0))) or 0)
        else:
            for a in ["live_balance","liveBalance","real_balance"]:
                v = getattr(p,a,None)
                if v is not None: real=float(v or 0); break
            for a in ["demo_balance","demoBalance","practice_balance"]:
                v = getattr(p,a,None)
                if v is not None: demo=float(v or 0); break
    except: pass
    return real, demo

def _extract_currency(p):
    try:
        if isinstance(p, dict): return p.get("currency", p.get("currency_char","USD"))
        for a in ["currency","currency_char"]:
            v = getattr(p,a,None)
            if v: return str(v)
    except: pass
    return "USD"

async def _get_balances(client):
    real = demo = 0.0
    try:
        p = await client.get_profile()
        log.info(f"📋 profile type={type(p).__name__}")
        real, demo = _extract_balance(p)
        if real > 0 or demo > 0: return real, demo
    except Exception as e:
        log.warning(f"get_profile: {e}")
    try:
        await client.change_account("REAL")
        await asyncio.sleep(3)
        real = float(await client.get_balance() or 0)
        await asyncio.sleep(2)
        await client.change_account("PRACTICE")
        await asyncio.sleep(3)
        demo = float(await client.get_balance() or 0)
        if real == demo and real > 0: real = 0.0
        return real, demo
    except Exception as e:
        log.error(f"_get_balances: {e}")
        return 0.0, 0.0

async def _get_single_balance(client, mode) -> float:
    try:
        await client.change_account(mode)
        await asyncio.sleep(1)
        return float(await client.get_balance() or 0)
    except: return 0.0

def _invalidate_pyquotex_disk_session(email: str) -> None:
    """
    pyquotex يقرأ session.json (في cwd). إن وُجد token قديم/منتهٍ يتخطّى HTTP login
    فيُرفض WebSocket برسالة authorization/reject → Token rejected.
    مسح التوكن يفرض authenticate() كاملاً عند كل تسجيل دخول من الواجهة.
    عطّل بـ QUOTEX_KEEP_DISK_SESSION=1 إن احتجت إعادة استخدام الجلسة المحفوظة.
    """
    if os.getenv("QUOTEX_KEEP_DISK_SESSION", "").strip().lower() in ("1", "true", "yes"):
        return
    path = os.path.join(os.getcwd(), "session.json")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or email not in data:
            return
        ent = data.get(email) or {}
        ua = ent.get("user_agent") if isinstance(ent, dict) else None
        data[email] = {"cookies": None, "token": None, "user_agent": ua or ""}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        log.info("🧹 مُسح token/cookies المحفوظة في session.json لهذا البريد — إعادة تسجيل دخول Quotex كاملة")
    except Exception as e:
        log.warning("تعذر تحديث session.json: %s", e)


async def _login_qx(email, password, S):
    try:
        S["login_error"] = ""
        _invalidate_pyquotex_disk_session(email)
        client = Quotex(
            email=email,
            password=password,
            lang="en",
            proxies=QX_HTTP_PROXIES,
        )
        S["client"] = client
        import builtins
        orig = builtins.input
        builtins.input = make_pin_input(email, S)
        try: check, msg = await client.connect()
        finally: builtins.input = orig
        if not check:
            if is_cloudflare_ws_failure(msg):
                log.warning("Cloudflare WS detected. تشغيل Stealth ثم إعادة connect مرة واحدة...")
                try:
                    stealth_proxy = str(QX_HTTP_PROXIES.get("https") or QX_HTTP_PROXIES.get("http") or "") if isinstance(QX_HTTP_PROXIES, dict) else None
                    establish_websocket_with_stealth(proxy=stealth_proxy)
                    import builtins as _b2
                    _orig2 = _b2.input
                    _b2.input = make_pin_input(email, S)
                    try:
                        check, msg = await client.connect()
                    finally:
                        _b2.input = _orig2
                except Exception as _e:
                    log.warning("Stealth reconnect failed: %s", _e)
            if S["needs_pin"]:
                return {"ok":False,"pin":True,"msg":"أدخل PIN من بريدك"}
            err = str(msg) or "فشل الاتصال"
            S["login_error"] = err[:800]
            return {"ok":False,"pin":False,"msg":err}
        real, demo = await _get_balances(client)
        cur = "USD"
        try:
            p = await client.get_profile()
            cur = _extract_currency(p)
        except: pass
        # عند إرجاع needs_pin مبكراً من /api/login لا يُكمَل الهاندلر — يجب حفظ الجلسة هنا بعد نجاح connect + الأرصدة
        S["real_balance"] = real
        S["demo_balance"] = demo
        S["currency"] = cur
        S["logged_in"] = True
        S["needs_pin"] = False
        S["email"] = email
        S["login_error"] = ""
        return {"ok":True,"client":client,"real":real,"demo":demo,"cur":cur}
    except Exception as e:
        log.error(traceback.format_exc())
        err = str(e)
        S["login_error"] = err[:800]
        return {"ok":False,"pin":False,"msg":err}

async def _do_trade(client, asset, amount, direction, acc, duration_sec=60):
    try:
        await client.change_account("REAL" if acc=="real" else "PRACTICE")
        await asyncio.sleep(0.5)
        ok, info = await client.buy(
            amount=amount,
            asset=asset,
            direction=direction,
            duration=int(duration_sec),
        )
        if ok:
            tid = info.get("id") if isinstance(info,dict) else None
            log.info(f"✅ صفقة: {asset} {direction.upper()} {amount} id={tid}")
            return {"ok":True,"id":tid}
        log.warning(f"⚠️ رُفضت: {info}")
        return {"ok":False,"msg":str(info)}
    except Exception as e:
        log.error(traceback.format_exc())
        return {"ok":False,"msg":str(e)}


def _is_time_insufficient_msg(msg: str) -> bool:
    m = str(msg or "").lower()
    return any(
        x in m
        for x in (
            "insufficient",
            "not enough time",
            "time is over",
            "purchase time",
            "وقت غير كافي",
            "الوقت غير كافي",
        )
    )

async def _check_win(client, tid):
    try:
        win = await client.check_win(tid)
        prf = client.get_profit() if hasattr(client,"get_profit") else 0
        return {"win":bool(win),"profit":float(prf or 0)}
    except: return {"win":False,"profit":0.0}

async def _close(client):
    try:
        if client: await client.close()
    except: pass

# ══════════════════════════════════════════════════════════════════════════════
# BOT WORKER
# ══════════════════════════════════════════════════════════════════════════════
def wait_for_minute_start(stop):
    """
    ينتظر حتى بداية الدقيقة التالية بالضبط
    يستخدم time.sleep للدقة العالية
    """
    now   = datetime.now()
    secs  = now.second
    usecs = now.microsecond / 1_000_000
    remaining = 60.0 - secs - usecs
    if remaining < 0:
        remaining = 0.0
    log.info(f"⏳ انتظار {remaining:.2f}ث لبداية الدقيقة (الثانية {secs})")
    # انتظار بخطوات صغيرة للتحقق من stop
    end_time = time.time() + remaining
    while time.time() < end_time:
        if stop.is_set(): return
        left = end_time - time.time()
        time.sleep(min(0.5, left) if left > 0 else 0)

def bot_worker(req: BotReq, S: dict, stop: threading.Event):
    # قائمة الأزواج — دعم متعدد الأزواج
    all_assets = list(req.assets) if req.assets else [req.asset]
    if req.asset and req.asset not in all_assets:
        all_assets.insert(0, req.asset)
    all_assets = list(dict.fromkeys(all_assets))  # إزالة المكررات
    log.info(f"🤖 {S['email']} | {all_assets} | {req.amount} | {req.strategy} | {req.account_type}")
    # مدة الصفقة: افتراضياً 60 ثانية. بعض حسابات Quotex ترفض 90s وتسبب timeout.
    trade_duration_sec = int(os.getenv("TRADE_DURATION_SEC", "60") or 60)
    if trade_duration_sec not in (60,):
        log.warning("TRADE_DURATION_SEC=%s غير مدعوم بثبات حالياً — سيتم استخدام 60s", trade_duration_sec)
    trade_duration_sec = 60
    # الدخول مسموح فقط ضمن نافذة زمنية محددة قبل إغلاق الصفقة:
    # افتراضياً بين 59 و 90 ثانية (كما طلبت).
    entry_window_min_sec = float(os.getenv("ENTRY_WINDOW_MIN_SEC", "59") or 59)
    entry_window_max_sec = float(os.getenv("ENTRY_WINDOW_MAX_SEC", "90") or 90)
    if entry_window_max_sec < entry_window_min_sec:
        entry_window_min_sec, entry_window_max_sec = entry_window_max_sec, entry_window_min_sec
    # منع تكرار نفس الصفقة المتتالية بسرعة
    entry_cooldown_sec = max(30, trade_duration_sec)

    # يمنع بقاء worker قديم يعمل بعد Stop/Start جديد.
    def _should_stop() -> bool:
        return (
            stop.is_set()
            or not bool(S.get("running", False))
            or (S.get("stop_event") is not stop)
        )

    # تتبع آخر زوج استُخدم لتجنب التكرار
    last_used_asset = None
    # منع تكرار نفس الإشارة أكثر من مرة في نفس دقيقة الشمعة
    signal_attempted = {}

    # ── بدء تدفق الأسعار الحية لكل الأزواج ───────────────────────────────────
    if QX and S["client"]:
        try:
            run_async_for(S["email"], _ensure_quotex_assets(S["client"]), 45)
        except Exception:
            pass
        for a in all_assets:
            try:
                run_async_for(S["email"], _start_price_stream(S["client"], a), 20)
            except Exception:
                pass

    # ── جمع تيكات للمراقبة فقط — التحليل من شموع 1m من Quotex لجميع الاستراتيجيات عند الاتصال
    _wu = (
        _WARMUP_COLLECT_SEC_EMA10
        if QX and S["client"]
        else _WARMUP_COLLECT_SEC
    )
    S["status_msg"] = "⏳ تجهيز المراقبة (أسعار حية للواجهة)..."
    log.info("⏳ جمع أسعار حية للمراقبة — %s ثانية...", _wu)
    warmup = time.time()
    while time.time() - warmup < _wu and not _should_stop():
        if QX and S["client"]:
            for a in all_assets:
                _collect_price(S["client"], a, S["email"])
        stop.wait(timeout=1)
    if _should_stop(): return

    _tick_total = sum(len(_price_buffers.get(a, [])) for a in all_assets)
    log.info("✅ مراقبة: %s تيك محفوظ — بدء الحلقة (%s)", _tick_total, req.strategy)
    S["status_msg"] = ""

    # ── رصيد البداية — قراءة بعد warmup مباشرة ──────────────────────────────
    start_bal = 0.0
    if QX and S["client"]:
        try:
            mode = "REAL" if req.account_type=="real" else "PRACTICE"
            log.info("⏳ قراءة رصيد البداية...")
            # قراءة متعددة حتى نحصل على قيمة ثابتة متكررة
            readings = []
            prev = 0.0
            for i in range(8):
                try:
                    bal = run_async_for(S["email"], _get_single_balance(S["client"],mode), 10)
                    if bal > 0:
                        readings.append(bal)
                        # إذا تكررت نفس القيمة مرتين متتاليتين فهي صحيحة
                        if len(readings) >= 2 and readings[-1] == readings[-2]:
                            start_bal = readings[-1]
                            break
                except: pass
                time.sleep(1)
            if start_bal == 0 and readings:
                start_bal = readings[-1]
            log.info(f"💰 رصيد البداية: {start_bal} (قراءات: {readings})")
            if req.account_type=="real": S["real_balance"] = start_bal
            else: S["demo_balance"] = start_bal
        except Exception as e:
            log.error(f"خطأ رصيد البداية: {e}")
            start_bal = S["demo_balance"] if req.account_type=="demo" else S["real_balance"]
    else:
        start_bal = S["demo_balance"] if req.account_type=="demo" else S["real_balance"]
    S["start_balance"] = start_bal
    log.info(f"🏁 رصيد البداية المعتمد: {start_bal}")


    while not _should_stop():
        try:
            # ── جمع الأسعار الحية ──────────────────────────────────────────
            if QX and S["client"]: _collect_price(S["client"], req.asset, S["email"])

            # جمع سعر إضافي
            if QX and S["client"]: _collect_price(S["client"], req.asset, S["email"])

            # ── تحليل الشمعات ─────────────────────────────────────────────
            direction = "wait"
            chosen_asset = all_assets[0]
            burst_count = 1
            any_candles = False

            if QX and S["logged_in"] and S["client"]:
                # جمع أسعار لكل الأزواج
                for a in all_assets:
                    _collect_price(S["client"], a, S["email"])

                # تحليل كل الأزواج واختيار الأقوى إشارة
                best_score = 0
                best_dir   = "wait"
                best_asset = None
                any_candles = False
                candles_by_asset = {}

                _min_bars = (
                    _HUSAAM_PRIVATE_MIN_BARS
                    if req.strategy == "HUSAAM_PRIVATE"
                    else _HUSAAM_EMA10_ANALYSIS_BARS
                )
                _need_len = _min_bars
                _max_len = 0
                ema_src_label = ""
                for a in all_assets:
                    if req.strategy == "HUSAAM_PRIVATE":
                        candles, _csrc = _get_husaam_ema10_candles(
                            S["client"],
                            S["email"],
                            a,
                            _need_len,
                            analysis_bars=_HUSAAM_PRIVATE_MIN_BARS,
                        )
                        if _csrc in ("quotex_1m", "quotex_1m_cache"):
                            ema_src_label = "شموع Quotex 1m — NEXORA Private"
                        elif _csrc == "quotex_stale_cache":
                            ema_src_label = "شموع 1m — كاش احتياطي"
                        elif _csrc == "sim_tick_1m":
                            ema_src_label = "محاكاة (تيك)"
                        else:
                            ema_src_label = str(_csrc)
                    else:
                        candles, _csrc = _get_husaam_ema10_candles(
                            S["client"], S["email"], a, _need_len
                        )
                        if _csrc in ("quotex_1m", "quotex_1m_cache"):
                            ema_src_label = "شموع Quotex 1m (شارت)"
                        elif _csrc == "quotex_stale_cache":
                            ema_src_label = "شموع 1m — كاش احتياطي"
                        elif _csrc == "sim_tick_1m":
                            ema_src_label = "محاكاة (تيك)"
                        else:
                            ema_src_label = str(_csrc)
                    _max_len = max(_max_len, len(candles))
                    candles_by_asset[a] = candles
                    if len(candles) >= _need_len:
                        any_candles = True
                        d, score = analyze_score(candles, req.strategy)
                        _sig = f"{a}:{d}:{score}"
                        _tn = time.time()
                        if _tn - S.get("_last_scan_sig_ts", 0) >= 12 or S.get("_last_scan_sig") != _sig:
                            log.info(f"🔍 {a}: {d.upper()} score={score}")
                            S["_last_scan_sig_ts"] = _tn
                            S["_last_scan_sig"] = _sig
                        if d != "wait" and score > best_score:
                            # تجنب تكرار نفس الزوج إذا وجد بديل بنفس القوة
                            if score > best_score or a != last_used_asset:
                                best_score = score
                                best_dir   = d
                                best_asset = a

                S["candles_ok"] = any_candles
                S["candle_source"] = ema_src_label

                if best_asset and best_dir != "wait":
                    direction    = best_dir
                    chosen_asset = best_asset
                    S["_wait_streak"] = 0
                    log.info(f"🏆 أفضل زوج: {chosen_asset} → {direction.upper()} (score={best_score})")
                elif not any_candles:
                    _now = time.time()
                    if _now - S.get("_last_candle_warn_ts", 0) >= 15:
                        log.warning(
                            "⚠️ شمعات غير كافية للتحليل — لديك %s/%s شمعة 1m (مطلوب من الشارت لا من التيك)",
                            _max_len,
                            _need_len,
                        )
                        S["_last_candle_warn_ts"] = _now
                    S["status_msg"] = (
                        f"⏳ شموع دقيقة: {_max_len}/{_need_len} · {ema_src_label}"
                    )
                    S["_wait_streak"] = 0
                    stop.wait(timeout=1.0)
                    continue
                else:
                    S["_wait_streak"] = int(S.get("_wait_streak", 0)) + 1
                    if S["_wait_streak"] >= 12:
                        fb_best_score = 0
                        fb_best_dir = "wait"
                        fb_best_asset = None
                        for a, cs in candles_by_asset.items():
                            d2, s2 = _fallback_direction_from_candles(cs)
                            if d2 != "wait" and s2 >= fb_best_score:
                                fb_best_score = s2
                                fb_best_dir = d2
                                fb_best_asset = a
                        if fb_best_asset and fb_best_dir != "wait":
                            direction = fb_best_dir
                            chosen_asset = fb_best_asset
                            S["_wait_streak"] = 0
                            S["status_msg"] = "تم تفعيل دخول بديل بعد تكرار WAIT"
                            log.info(f"⚡ Fallback entry: {chosen_asset} → {direction.upper()} (score={fb_best_score})")
                        else:
                            _now = time.time()
                            if _now - S.get("_last_no_trade_signal_ts", 0) >= 20:
                                log.info("⏸️ شموع كافية — لا إشارة دخول (WAIT) حسب الاستراتيجية في أي زوج")
                                S["_last_no_trade_signal_ts"] = _now
                    else:
                        _now = time.time()
                        if _now - S.get("_last_no_trade_signal_ts", 0) >= 20:
                            log.info("⏸️ شموع كافية — لا إشارة دخول (WAIT) حسب الاستراتيجية في أي زوج")
                            S["_last_no_trade_signal_ts"] = _now
            else:
                log.warning("⚠️ لا اتصال — تخطّ")
                stop.wait(timeout=1.0)
                continue

            if direction == "wait":
                # any_candles=True: شموع كافية لكن الاستراتيجية أعطت WAIT — ليس «نقص شموع»
                if any_candles:
                    wt = 4.0
                    stop.wait(timeout=wt)
                else:
                    log.info("⏸️ انتظار (لا بيانات تحليل كافية)")
                    stop.wait(timeout=1.0)
                continue
            else:
                skips = 0

            last_used_asset  = chosen_asset
            S["last_signal"] = direction
            entry_key = f"{chosen_asset}:{direction}:{req.account_type}"
            now_entry = time.time()
            if (
                S.get("_last_entry_key") == entry_key
                and (now_entry - float(S.get("_last_entry_ts", 0) or 0)) < entry_cooldown_sec
            ):
                left_cd = int(entry_cooldown_sec - (now_entry - float(S.get("_last_entry_ts", 0) or 0)))
                log.info("⏸️ تجاهل تكرار صفقة %s — تبقّى %ss", entry_key, max(1, left_cd))
                stop.wait(timeout=1.0)
                continue
            # لا تحاول نفس الإشارة أكثر من مرة في نفس الدقيقة
            candle_bucket = int(now_entry // 60)
            signal_key = f"{entry_key}:{candle_bucket}"
            if signal_key in signal_attempted:
                stop.wait(timeout=1.0)
                continue
            # تنظيف قديم
            if len(signal_attempted) > 300:
                cutoff = int(time.time()) - 600
                for k, ts in list(signal_attempted.items()):
                    if ts < cutoff:
                        signal_attempted.pop(k, None)
            signal_attempted[signal_key] = int(now_entry)
            # تحقّق نافذة الدخول: فقط إذا الوقت المتبقي بين 59 و90 ثانية.
            now_dt = datetime.now()
            sec_left_this = 60.0 - (now_dt.second + now_dt.microsecond / 1_000_000)
            # بعض المنصات تنفّذ على الدورة التالية؛ نعتبر نافذة الدورة التالية أيضاً.
            sec_left_next = sec_left_this + 60.0
            eff_left = sec_left_this if sec_left_this >= entry_window_min_sec else sec_left_next
            if not (entry_window_min_sec <= eff_left <= entry_window_max_sec):
                S["status_msg"] = (
                    f"⏸️ تخطّي الإشارة: نافذة الدخول خارج المدى "
                    f"({eff_left:.1f}s، المطلوب {entry_window_min_sec:.0f}-{entry_window_max_sec:.0f}s)"
                )
                log.info(
                    "⏸️ skip entry: left_this=%.2fs left_next=%.2fs eff=%.2fs (required %.0f-%.0fs)",
                    sec_left_this, sec_left_next, eff_left, entry_window_min_sec, entry_window_max_sec
                )
                stop.wait(timeout=1.0)
                continue

            # ── تنفيذ فوري عند ظهور الإشارة (بدون انتظار بداية الدقيقة)
            log.info(
                f"🎯 {direction.upper()} | تنفيذ فوري | {datetime.now().strftime('%H:%M:%S')} | مدة={trade_duration_sec}s"
            )

            # ── تسجيل/تنفيذ الصفقات (Burst) ────────────────────────────────
            opened_trades = []
            for i in range(max(1, int(burst_count))):
                trade = {
                    "id":         int(time.time()*1000) + i,
                    "asset":      chosen_asset,
                    "direction":  direction,
                    "amount":     req.amount,
                    "duration":   trade_duration_sec,
                    "status":     "pending",
                    "profit":     0,
                    "started_at": datetime.now().isoformat(),
                    "account":    req.account_type,
                    "strategy":   req.strategy,
                }
                S["current_trade"] = trade
                tid = None
                if QX and S["logged_in"] and S["client"]:
                    try:
                        r = run_async_for(
                            S["email"],
                            _do_trade(
                                S["client"],
                                chosen_asset,
                                req.amount,
                                direction,
                                req.account_type,
                                trade_duration_sec,
                            ),
                            25,
                        )
                    except TimeoutError:
                        S["status_msg"] = "⏸️ تعذر تنفيذ الصفقة (مهلة اتصال Quotex) — تخطي الإشارة"
                        log.warning("⚠️ Timeout أثناء buy — تخطّي الإشارة الحالية")
                        continue
                    except Exception as ex:
                        S["status_msg"] = "⏸️ تعذر تنفيذ الصفقة حالياً — تخطي الإشارة"
                        log.warning("⚠️ buy failed: %s", ex)
                        continue
                    if r["ok"]:
                        tid = r.get("id")
                    else:
                        msg = str(r.get("msg",""))
                        log.warning(f"⚠️ {msg}")
                        if _is_time_insufficient_msg(msg):
                            S["status_msg"] = "⏸️ المنصة رفضت الدخول: الوقت غير كافٍ لهذه الإشارة"
                            break
                        if "not_money" in msg.lower():
                            log.warning("💸 رصيد غير كافٍ — توقف")
                            S["current_trade"] = None
                            stop.set()
                            break
                        continue
                else:
                    log.info(f"🔵 محاكاة {chosen_asset} {direction.upper()} ({i+1}/{max(1, int(burst_count))})")
                opened_trades.append((trade, tid))

            if not opened_trades:
                S["current_trade"] = None
                if _should_stop():
                    break
                stop.wait(timeout=2.0)
                continue
            # تم فتح صفقة فعلية — ثبّت بصمة آخر دخول لتجنّب التكرار السريع
            S["_last_entry_key"] = entry_key
            S["_last_entry_ts"] = time.time()

            # ── انتظار مدة الصفقة كاملة ───────────────────────────────────
            # مع صفقات مفتوحة: لا نخرج مبكراً بسبب stop — وإلا تُفتح صفقات على Quotex ولا تُسجَّل في wins/losses
            log.info("⏳ انتظار %s ثانية...", trade_duration_sec)
            trade_end = time.time() + trade_duration_sec
            while time.time() < trade_end:
                if QX and S["client"]:
                    for a in all_assets:
                        _collect_price(S["client"], a, S["email"])
                left = trade_end - time.time()
                if left <= 0:
                    break
                stop.wait(timeout=min(1.0, left))

            # ── النتائج لكل صفقة في الـ Burst ─────────────────────────────
            total_prf = 0.0
            for trade, tid in opened_trades:
                try:
                    if QX and tid and S["client"]:
                        r2 = run_async_for(S["email"], _check_win(S["client"], tid), 25)
                        win = r2["win"]
                        prf = r2["profit"] if win else -(r2.get("profit", 0) or req.amount)
                    else:
                        win = random.random() < 0.58
                        prf = round(req.amount * 0.80, 2) if win else -req.amount
                except Exception as ex:
                    log.warning("⚠️ تعذر تسوية صفقة (id=%s): %s — تُحتسب خسارة تقريبية", tid, ex)
                    win = False
                    prf = -float(req.amount)

                prf = round(prf, 2)
                total_prf += prf
                trade.update({"status": "win" if win else "loss",
                              "profit": prf, "ended_at": datetime.now().isoformat()})
                S["trades"].insert(0, dict(trade))
                if len(S["trades"]) > 200:
                    S["trades"].pop()

                if win:
                    S["wins"] += 1
                    log.info(f"✅ فوز +{prf}")
                else:
                    S["losses"] += 1
                    log.info(f"❌ خسارة {prf}")

            S["current_trade"] = None

            # ── قراءة الرصيد الفعلي من المنصة ──────────────────────────
            current_bal = 0.0
            if QX and S["client"]:
                mode = "REAL" if req.account_type=="real" else "PRACTICE"
                prev_bal = S["real_balance"] if req.account_type=="real" else S["demo_balance"]
                # قراءة سريعة غير حاجزة: لا ننتظر دقيقة كاملة حتى لا تتأخر الدورة
                for wait_attempt in range(2):
                    try:
                        bal = run_async_for(S["email"], _get_single_balance(S["client"], mode), 8)
                        if bal > 0:
                            current_bal = bal
                            if bal != prev_bal:
                                log.info(f"💰 رصيد تحدّث سريعاً: {prev_bal} → {current_bal}")
                                break
                    except Exception:
                        pass
                    time.sleep(1)
                if current_bal > 0:
                    if req.account_type=="real": S["real_balance"] = current_bal
                    else: S["demo_balance"] = current_bal
                else:
                    current_bal = prev_bal
                    log.info("ℹ️ الرصيد لم يتحدّث فوراً — المتابعة بدون تأخير")
            else:
                # محاكاة
                if req.account_type == "demo":
                    S["demo_balance"] = round((S.get("demo_balance") or start_bal) + total_prf, 2)
                    current_bal = S["demo_balance"]
                else:
                    S["real_balance"] = round((S.get("real_balance") or start_bal) + total_prf, 2)
                    current_bal = S["real_balance"]

            # صافي الجلسة يجب أن يساوي مجموع أرباح/خسائر الصفقات المسجّلة فعلياً
            # (أدق من الاعتماد على فرق الرصيد فقط عند تأخر تحديث المنصة)
            real_profit = round(sum(float(t.get("profit", 0) or 0) for t in S["trades"]), 2)
            S["session_profit"] = real_profit
            bal_delta = round(current_bal - start_bal, 2)
            log.info(
                f"💰 رصيد={current_bal} | بداية={start_bal} | Δرصيد={bal_delta:+.2f} | صافي صفقات={real_profit:+.2f}"
            )

            # ── فحص الحدود ────────────────────────────────────────────────
            if req.profit_limit > 0 and real_profit >= req.profit_limit:
                msg = f"🎯 وصلت لهدف الربح! +{real_profit} {S['currency']} (الهدف: +{req.profit_limit})"
                log.info(msg)
                S["status_msg"] = msg
                S["running"] = False
                stop.set(); break

            if req.stop_loss > 0 and real_profit <= -req.stop_loss:
                msg = f"🛑 تم الوصول لحد الخسارة! {real_profit} {S['currency']} (الحد: -{req.stop_loss})"
                log.info(msg)
                S["status_msg"] = msg
                S["running"] = False
                stop.set(); break

            if _should_stop():
                log.info("🛑 إيقاف بعد تسوية الصفقات — خروج من حلقة البوت")
                break

            # صفقة كل دقيقة — لا انتظار إضافي

        except Exception as e:
            log.error(f"خطأ:\n{traceback.format_exc()}")
            stop.wait(timeout=6)

    S["running"] = False
    S["current_trade"] = None
    log.info(f"🤖 توقف: {S['email']}")

# ══════════════════════════════════════════════════════════════════════════════
# FastAPI
# ══════════════════════════════════════════════════════════════════════════════
@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    _configure_quiet_loggers()
    yield


app = FastAPI(title="NEXORA TRADE", lifespan=_app_lifespan)
app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS,
                   allow_methods=["*"], allow_headers=["*"])

if os.getenv("DEBUG_API_LOG", "").strip().lower() in ("1", "true", "yes"):
    @app.middleware("http")
    async def _log_api_requests(request, call_next):
        if request.url.path.startswith("/api"):
            log.info("➡️ API %s %s", request.method, request.url.path)
        return await call_next(request)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
_fe = os.path.join(APP_DIR, "frontend.html")
_ad = os.path.join(APP_DIR, "admin.html")
HTML  = open(_fe,encoding="utf-8").read() if os.path.exists(_fe) else "<h1>frontend.html مفقود</h1>"
ADMIN = open(_ad,encoding="utf-8").read() if os.path.exists(_ad) else "<h1>admin.html مفقود</h1>"


@app.get("/", response_class=HTMLResponse)
async def ui(): return HTML

@app.get("/admin", response_class=HTMLResponse)
async def admin_ui(): return ADMIN


@app.get("/manifest.json")
async def pwa_manifest():
    p = os.path.join(APP_DIR, "manifest.json")
    if not os.path.isfile(p):
        raise HTTPException(404, "manifest.json مفقود")
    return FileResponse(
        p,
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=300, must-revalidate"},
    )


@app.get("/sw.js")
async def pwa_service_worker():
    p = os.path.join(APP_DIR, "sw.js")
    if not os.path.isfile(p):
        raise HTTPException(404, "sw.js مفقود")
    return FileResponse(
        p,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


def _icon_response(name: str) -> FileResponse:
    p = os.path.join(APP_DIR, name)
    if not os.path.isfile(p):
        raise HTTPException(404, f"{name} مفقود")
    return FileResponse(
        p,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600, must-revalidate"},
    )


@app.get("/icon-192.png")
async def pwa_icon_192():
    return _icon_response("icon-192.png")


@app.get("/icon-512.png")
async def pwa_icon_512():
    return _icon_response("icon-512.png")


@app.get("/pwa/icon-192.png")
async def pwa_public_icon_192():
    """مسار ثابت للـ PWA/APK — يُفضّل في manifest لتوافق PWABuilder والكاش."""
    return _icon_response("icon-192.png")


@app.get("/pwa/icon-512.png")
async def pwa_public_icon_512():
    return _icon_response("icon-512.png")


@app.get("/pwa/screenshot-narrow.png")
async def pwa_public_shot_narrow():
    return _icon_response("pwa-screenshot-narrow.png")


@app.get("/pwa/screenshot-wide.png")
async def pwa_public_shot_wide():
    return _icon_response("pwa-screenshot-wide.png")


@app.get("/pwa-screenshot-narrow.png")
async def pwa_screenshot_narrow():
    return _icon_response("pwa-screenshot-narrow.png")


@app.get("/pwa-screenshot-wide.png")
async def pwa_screenshot_wide():
    return _icon_response("pwa-screenshot-wide.png")

# ── Admin ─────────────────────────────────────────────────────────────────────
@app.post("/api/admin/login")
async def admin_login(req: AdminLoginReq):
    if req.password != ADMIN_PW: raise HTTPException(401,"كلمة مرور خاطئة")
    t = secrets.token_hex(16)
    _persist_admin_token(t)
    return {"success": True, "token": t}

@app.get("/api/admin/users")
async def admin_users(admin_token: str):
    if admin_token not in ADMIN_TOKENS: raise HTTPException(403,"غير مصرح")
    result = []
    for email,u in USERS.items():
        S = get_session_by_email(email)
        result.append({
            "email":email,"name":u.get("name",""),"status":u["status"],
            "registered_at":u.get("registered_at",""),"approved_at":u.get("approved_at",""),
            "note":u.get("note",""),"online":bool(S and S["logged_in"]),
            "running":bool(S and S["running"]),"wins":S["wins"] if S else 0,
            "losses":S["losses"] if S else 0,"session_profit":S["session_profit"] if S else 0,
            "real_balance":S["real_balance"] if S else 0,"demo_balance":S["demo_balance"] if S else 0,
            "candles_ok":S.get("candles_ok",False) if S else False,
            "candle_source":S.get("candle_source","") if S else "",
        })
    return {"users":result}

@app.post("/api/admin/approve")
async def admin_approve(req: ApproveReq):
    if req.admin_token not in ADMIN_TOKENS: raise HTTPException(403,"غير مصرح")
    if req.email not in USERS: raise HTTPException(404,"مستخدم غير موجود")
    if req.action == "approve":
        token = secrets.token_hex(20)
        USERS[req.email].update({"status":"approved","session_token":token,
            "approved_at":datetime.now().isoformat(),"note":req.note})
        SESSIONS[token] = new_session(req.email)
        save_users()
        log.info(f"✅ قبول: {req.email}")
        return {"success":True,"message":f"تم قبول {req.email}","token":token}
    elif req.action == "reject":
        old = USERS[req.email].get("session_token","")
        if old in SESSIONS: SESSIONS[old]["stop_event"].set(); del SESSIONS[old]
        USERS[req.email].update({"status":"rejected","note":req.note})
        save_users()
        return {"success":True,"message":f"تم رفض {req.email}"}
    raise HTTPException(400,"action خاطئ")

@app.post("/api/admin/stop_user")
async def admin_stop(req: ApproveReq):
    if req.admin_token not in ADMIN_TOKENS: raise HTTPException(403,"غير مصرح")
    S = get_session_by_email(req.email)
    if S: S["stop_event"].set(); S["running"]=False; return {"success":True}
    return {"success":False,"message":"غير متصل"}

@app.post("/api/admin/delete_user")
async def admin_delete(req: ApproveReq):
    if req.admin_token not in ADMIN_TOKENS: raise HTTPException(403,"غير مصرح")
    if req.email not in USERS: raise HTTPException(404,"مستخدم غير موجود")
    # إيقاف الجلسة إن وجدت
    S = get_session_by_email(req.email)
    if S:
        S["stop_event"].set()
        S["running"] = False
        if S.get("client"):
            try: run_async_for(req.email, _close(S["client"]), 5)
            except: pass
    # حذف من SESSIONS
    token = USERS[req.email].get("session_token","")
    if token and token in SESSIONS:
        del SESSIONS[token]
    # حذف من USERS
    del USERS[req.email]
    save_users()
    log.info(f"🗑️ حُذف المشترك: {req.email}")
    return {"success":True,"message":f"تم حذف {req.email}"}

# ── User ──────────────────────────────────────────────────────────────────────
@app.post("/api/register")
async def register(req: RegisterReq, background_tasks: BackgroundTasks):
    if "@" not in req.email: raise HTTPException(400,"بريد غير صحيح")
    if req.email in USERS:
        s = USERS[req.email]["status"]
        msgs = {"approved":"حسابك مفعّل — سجّل دخولك",
                "pending":"طلبك قيد المراجعة","rejected":"تم رفض طلبك"}
        return {"success":False,"status":s,"message":msgs.get(s,"")}
    registered_at = datetime.now().isoformat()
    USERS[req.email] = {"name":req.name,"status":"pending",
        "registered_at":registered_at,"session_token":"","approved_at":"","note":""}
    save_users()
    log.info(f"📝 طلب: {req.email}")
    background_tasks.add_task(
        _notify_telegram_new_registration, req.email, req.name or "", registered_at
    )
    return {"success":True,"status":"pending","message":"تم إرسال طلبك — انتظر موافقة المسؤول"}

@app.get("/api/check_status")
async def check_status(email: str):
    if email not in USERS: raise HTTPException(404,"البريد غير مسجل")
    u = USERS[email]
    r = {"status":u["status"],"email":email}
    if u["status"] == "approved":
        token = u.get("session_token","")
        if token and token not in SESSIONS:
            SESSIONS[token] = new_session(email)
        r["session_token"] = token
        r["message"] = "تم قبول طلبك — سجّل دخولك"
    elif u["status"] == "pending":
        r["message"] = "طلبك قيد المراجعة"
    else:
        r["message"] = "تم رفض طلبك: " + u.get("note","")
    return r

@app.post("/api/login")
async def login(req: LoginReq):
    qx_email = (req.email or "").strip()
    if "@" not in qx_email:
        raise HTTPException(400, "بريد غير صحيح")
    if len(req.password) < 4:
        raise HTTPException(400, "كلمة مرور قصيرة")
    email_key = _resolve_user_email(qx_email)
    if not email_key:
        raise HTTPException(403, "غير مسجّل — أرسل طلب انضمام")
    if USERS[email_key]["status"] != "approved":
        raise HTTPException(403, "لم يتم قبول طلبك بعد")
    if USERS[email_key].get("session_token", "") != req.token:
        raise HTTPException(
            403,
            "انتهت الجلسة أو الرمز غير متطابق — من صفحة التسجيل اضغط «تحقق من الحالة» بالبريد نفسه ثم أعد تسجيل الدخول",
        )
    S = get_session(req.token)
    if not S:
        SESSIONS[req.token] = new_session(email_key)
        S = SESSIONS[req.token]
    if QX:
        if S.get("client"):
            try:
                run_async_for(email_key, _close(S["client"]), 5)
            except Exception:
                pass
        S["email"] = email_key
        S["needs_pin"] = False
        S["login_error"] = ""
        drain_pin_queue(email_key)
        # connect() يستدعي input() ويُعلّق على queue حتى يصل PIN من /api/pin.
        # إذا انتظرنا result(150) هنا، لا تُرسل الاستجابة أبداً → الواجهة لا تظهر حقل PIN.
        _loop = get_session_loop(email_key)
        _fut = asyncio.run_coroutine_threadsafe(
            _login_qx(email_key, req.password, S), _loop
        )
        _deadline = time.monotonic() + 150
        r = None
        while time.monotonic() < _deadline:
            if _fut.done():
                try:
                    r = _fut.result()
                except Exception as e:
                    log.exception("login: نتيجة مهمة الدخول")
                    raise HTTPException(500, str(e) or "فشل تسجيل الدخول")
                break
            if S.get("needs_pin"):
                return {
                    "success": False,
                    "needs_pin": True,
                    "message": "أدخل رمز التحقق (PIN) المرسل إلى بريدك أو تطبيق Quotex",
                }
            await asyncio.sleep(0.15)
        else:
            raise HTTPException(408, "انتهت مهلة تسجيل الدخول — أعد المحاولة")
        if r.get("pin"):
            S["needs_pin"]=True
            return {"success":False,"needs_pin":True,"message":r.get("msg") or "أدخل PIN"}
        if not r["ok"]:
            msg = str(r.get("msg") or "").strip()
            if not msg or msg.lower() in ("false", "none", "unknown", "unknown error"):
                msg = _DEFAULT_QX_LOGIN_FAIL
            # عند فشل Quotex لا ندخل محاكاة تلقائياً: يجب أن يكون الدخول حقيقي فقط.
            ml = msg.lower()
            blocked_region = any(
                x in ml
                for x in (
                    "service unavailable",
                    "not available in your region",
                    "your region",
                    "cloudflare",
                    "websocket",
                    "handshake status 403",
                    "403 forbidden",
                    "connection reset",
                    "connection refused",
                    "timed out",
                    "timeout",
                    "ssl",
                    "certificate",
                    "network is unreachable",
                    "errno",
                )
            )
            if blocked_region:
                raise HTTPException(
                    403,
                    "Quotex رفض اتصال WebSocket (غالباً حماية Cloudflare أو IP/منطقة). جرّب VPN أو شبكة أخرى، أو تشغيل البوت من مكان يصل فيه المتصفح إلى Quotex.",
                )
            log.warning("login Quotex فشل: %s", msg[:500])
            raise HTTPException(401, msg)
        S["client"]=r["client"]; S["real_balance"]=r["real"]
        S["demo_balance"]=r["demo"]; S["currency"]=r.get("cur","USD")
    else:
        S["real_balance"]=0.0; S["demo_balance"]=10_000.0; S["currency"]="USD"
    S["logged_in"]=True; S["needs_pin"]=False; S["email"]=email_key
    S["status_msg"] = ""
    return {"success":True,"needs_pin":False,"email":email_key,
            "user_id":abs(hash(email_key))%90_000_000+10_000_000,
            "real_balance":S["real_balance"],"demo_balance":S["demo_balance"],
            "currency":S["currency"],"sim_mode":_is_session_sim_mode(S)}

@app.post("/api/pin")
async def pin_ep(req: PinReq):
    S = get_session(req.token)
    if not S: raise HTTPException(403,"جلسة غير صالحة")
    p = re.sub(r"\D", "", str(req.pin or "").strip())
    if len(p) < 4:
        raise HTTPException(400,"PIN قصير")
    if not QX:
        S["logged_in"]=True; S["needs_pin"]=False
        return {"success":True,"email":S["email"],"user_id":12345678,
                "real_balance":0.0,"demo_balance":10_000.0,"currency":"USD","sim_mode":True}
    get_pin_q(S["email"]).put(p)
    # إرجاع فوري حتى لا يقطع Nginx الطلب (504) بسبب proxy_read_timeout الافتراضي (~60s).
    # الواجهة تستطلع /api/status حتى يصبح logged_in=true بعد انتهاء connect() في الخلفية.
    return {"success": True, "pending": True}

@app.post("/api/logout")
async def logout(req: TokenReq):
    S = get_session(req.token)
    if S:
        if S.get("running"): S["stop_event"].set(); S["running"]=False
        if S.get("client"):
            try: run_async_for(S.get("email","_"), _close(S["client"]),5)
            except: pass
        S.update({"logged_in":False,"needs_pin":False,"login_error":"","trades":[],"wins":0,
                  "losses":0,"session_profit":0.0,"current_trade":None,"client":None})
    return {"success":True}

@app.post("/api/bot/start")
async def start(req: BotReq):
    S = get_session(req.token)
    if not S: raise HTTPException(403,"جلسة غير صالحة")
    if not S["logged_in"]: raise HTTPException(401,"سجّل الدخول أولاً")
    if S["running"]: raise HTTPException(400,"البوت يعمل بالفعل")
    # ── إعادة تعيين كاملة لكل جلسة جديدة ──────────────────────────────────
    # أوقف أي thread قديم أولاً
    old_stop = S.get("stop_event")
    if old_stop:
        old_stop.set()
    time.sleep(0.8)  # انتظر حتى يتوقف thread القديم

    # إعادة تعيين كاملة مع الحفاظ على client
    S["running"]        = True
    S["stop_event"]     = threading.Event()
    S["session_profit"] = 0.0
    S["start_balance"]  = 0.0
    S["wins"]           = 0
    S["losses"]         = 0
    S["trades"]         = []
    S["status_msg"]     = ""   # امسح رسالة الهدف القديمة
    S["current_trade"]  = None
    S["candles_ok"]     = False
    S["candle_source"]  = ""
    threading.Thread(target=bot_worker, args=(req,S,S["stop_event"]),
                     daemon=True).start()
    return {"success":True}

@app.post("/api/bot/stop")
async def stop_ep(req: TokenReq):
    S = get_session(req.token)
    if S:
        S["stop_event"].set()
        S["running"] = False
        S["status_msg"] = ""
        S["candle_source"] = ""
    return {"success":True}

@app.get("/api/status")
async def status(token: str=""):
    S = get_session(token)
    if not S: return {"logged_in":False,"running":False,"needs_pin":False,"login_error":""}
    # تحديث دوري للرصيدين من Quotex (لإظهار الرصيد الحقيقي حتى بدون تشغيل البوت).
    if QX and S.get("logged_in") and S.get("client"):
        now = time.time()
        if now - float(S.get("_last_bal_sync_ts", 0.0) or 0.0) >= 20.0:
            try:
                real, demo = await _get_balances(S["client"])
                if real > 0 or demo > 0:
                    S["real_balance"] = round(float(real), 2)
                    S["demo_balance"] = round(float(demo), 2)
                S["_last_bal_sync_ts"] = now
            except Exception:
                pass
    total = S["wins"]+S["losses"]
    _em = S.get("email") or ""
    _uid = abs(hash(_em)) % 90_000_000 + 10_000_000 if _em else 0
    return {
        "logged_in":      S["logged_in"],
        "needs_pin":      S["needs_pin"],
        "email":          S["email"],
        "user_id":        _uid,
        "real_balance":   S["real_balance"],
        "demo_balance":   S["demo_balance"],
        "currency":       S["currency"],
        "running":        S["running"],
        "wins":           S["wins"],
        "losses":         S["losses"],
        "total":          total,
        "win_rate":       round(S["wins"]/total*100,1) if total else 0,
        "session_profit": S["session_profit"],
        "start_balance":  S["start_balance"],
        "current_trade":  S["current_trade"],
        "trades":         S["trades"][:50],
        "sim_mode":       _is_session_sim_mode(S),
        "last_signal":    S["last_signal"],
        "candles_ok":     S.get("candles_ok",False),
        "candle_source":  S.get("candle_source",""),
        "status_msg":     S.get("status_msg",""),
        "login_error":    (S.get("login_error") or "")[:800],
    }

if __name__ == "__main__":
    log.info("ℹ️ للجلسات وPIN: شغّل عملية واحدة فقط (worker واحد) حتى لا تُفقد SESSIONS بين الطلبات")
    log.info("🚀 NEXORA TRADE — http://localhost:8000")
    log.info("👤 Admin: http://localhost:8000/admin")
    log.info(f"🌐 CORS origins: {ALLOWED_ORIGINS}")
    log.info(f"📦 pyquotex: {'✅' if QX else '❌ محاكاة'}")
    log.info(f"📦 pydantic : v{pydantic.VERSION}")
    if os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHANNEL_ID", "").strip():
        log.info("📱 تيليجرام: إشعارات طلبات الانضمام → القناة مفعّلة")
    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        access_log=False,
    )
