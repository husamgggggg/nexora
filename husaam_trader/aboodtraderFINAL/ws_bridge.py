#!/usr/bin/env python3
"""
جسر WebSocket: Playwright (متصفح) ↔ websockets (محلي) ↔ pyquotex/websocket-client.

مهم: Socket.IO قد يرسل إطارات نصية أو ثنائية؛ تحويل الثنائي إلى نص يفسد البروتوكول.
نمرّر النص كما هو، والثنائي كـ base64 بين JS وPython ثم نعيد bytes إلى العميل المحلي.

إن كان تسجيل الدخول عبر pyquotex يستخدم بروكسي (QUOTEX_PROXY_URL وغيره) فلا بد أن يمرّ
نفس البروكسي إلى Chromium هنا؛ وإلا يخرج الـ WebSocket من IP مختلف وقد يرفضه السيرفر
(يظهر لدى العميل: connection rejected).

يُمرَّر عبر ``--proxy-url`` من bot.py أو من البيئة:
``QUOTEX_WS_BRIDGE_PROXY`` ثم ``QUOTEX_PROXY_URL`` ثم ``HTTPS_PROXY`` / ``HTTP_PROXY``.

كوكيز جلسة pyquotex تُقرأ من رأس ``Cookie`` في اتصال WebSocket المحلي وتُحقَن في Chromium
(بدونها قد يُغلق الـ WSS فوراً ويظهر ``Connection is already closed`` عند ``send_ssid``).
"""
import argparse
import asyncio
import base64
import json
import os
import traceback
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import websockets
from playwright.async_api import async_playwright


def _resolve_proxy_url(cli_value: str) -> str:
    s = (cli_value or "").strip().strip('"').strip("'")
    if s:
        return s
    for key in (
        "QUOTEX_WS_BRIDGE_PROXY",
        "QUOTEX_PROXY_URL",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "ZENROWS_PROXY_URL",
    ):
        v = (os.environ.get(key) or "").strip().strip('"').strip("'")
        if v:
            return v
    return ""


def _playwright_proxy_config(proxy_url: str) -> Optional[Dict[str, Any]]:
    """يحوّل http(s):// أو socks5:// إلى صيغة Playwright ``proxy``."""
    raw = (proxy_url or "").strip().strip('"').strip("'")
    if not raw:
        return None
    try:
        u = urllib.parse.urlparse(raw)
    except Exception:
        return None
    if not u.hostname:
        return None
    port = u.port
    if port is None:
        tail = (u.netloc or "").split("@")[-1]
        if ":" in tail:
            maybe = tail.rsplit(":", 1)[-1]
            if maybe.isdigit():
                port = int(maybe)
    if not port:
        return None
    scheme = (u.scheme or "http").lower()
    if scheme.startswith("socks5"):
        server = f"socks5://{u.hostname}:{port}"
    elif scheme.startswith("socks4"):
        server = f"socks4://{u.hostname}:{port}"
    else:
        server = f"http://{u.hostname}:{port}"
    cfg: dict = {"server": server}
    if u.username is not None and str(u.username) != "":
        cfg["username"] = urllib.parse.unquote(u.username)
        cfg["password"] = urllib.parse.unquote(u.password or "")
    return cfg


def _incoming_ws_request_headers(ws: Any) -> Dict[str, str]:
    """رؤوس ترقية WebSocket من عميل websockets (Cookie/User-Agent من pyquotex)."""
    out: Dict[str, str] = {}
    try:
        req = getattr(ws, "request", None)
        if req is not None:
            hs = getattr(req, "headers", None)
            if hs is not None:
                for k, v in hs.items():
                    out[str(k)] = str(v)
                return out
    except Exception:
        pass
    try:
        rh = getattr(ws, "request_headers", None)
        if rh is not None:
            for k, v in rh.items():
                out[str(k)] = str(v)
    except Exception:
        pass
    return out


def _parse_cookie_header_pairs(cookie_header: str) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for part in (cookie_header or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            pairs.append((k, v))
    return pairs


def _cookie_domain_for_target_wss(target_url: str) -> str:
    env_d = (os.environ.get("QUOTEX_BRIDGE_COOKIE_DOMAIN") or "").strip()
    if env_d:
        return env_d if env_d.startswith(".") else f".{env_d.lstrip('.')}"
    try:
        host = (urllib.parse.urlparse(target_url).hostname or "").lower()
    except Exception:
        host = ""
    if not host:
        return ".qxbroker.com"
    parts = host.split(".")
    if len(parts) >= 2:
        return "." + ".".join(parts[-2:])
    return "." + host


def _load_session_json_entry() -> Dict[str, Any]:
    path = os.path.join(os.getcwd(), "session.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    for _, entry in data.items():
        if isinstance(entry, dict) and (
            (entry.get("cookies") and str(entry.get("cookies")).strip())
            or (entry.get("user_agent") and str(entry.get("user_agent")).strip())
        ):
            return entry
    return {}


def _cookie_header_for_bridge(headers: Dict[str, str]) -> Tuple[str, str]:
    cookie_header = headers.get("Cookie") or headers.get("cookie") or ""
    if cookie_header.strip():
        return cookie_header, "local-handshake"
    sess = _load_session_json_entry()
    cookie_header = str(sess.get("cookies") or "").strip()
    if cookie_header:
        return cookie_header, "session.json"
    return "", ""


def _user_agent_for_bridge(headers: Dict[str, str], default_ua: str) -> str:
    ua = headers.get("User-Agent") or headers.get("user-agent") or ""
    if str(ua).strip():
        return str(ua).strip()
    sess = _load_session_json_entry()
    ua = str(sess.get("user_agent") or "").strip()
    if ua:
        print("[Bridge] using user_agent from session.json", flush=True)
        return ua
    return default_ua


async def _playwright_inject_session_cookies(
    context: Any, headers: Dict[str, str], target_url: str
) -> int:
    """
    بدون هذا: Chromium يفتح WSS بلا كوكيز جلسة pyquotex → إغلاق سريع (opcode=8) و
    Connection is already closed عند send_ssid.
    """
    cookie_header, cookie_source = _cookie_header_for_bridge(headers)
    if not cookie_header.strip():
        print(
            "[Bridge] WARNING: no Cookie on local WS handshake — "
            "and no cookies in session.json",
            flush=True,
        )
        return 0
    domain = _cookie_domain_for_target_wss(target_url)
    pairs = _parse_cookie_header_pairs(cookie_header)
    if not pairs:
        return 0
    full = []
    for name, value in pairs:
        full.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/",
                "secure": True,
                "sameSite": "Lax",
            }
        )
    try:
        await context.add_cookies(full)
    except Exception as e:
        print(f"[Bridge] add_cookies(full) failed ({e}); retry minimal", flush=True)
        try:
            minimal = [
                {"name": n, "value": v, "domain": domain, "path": "/"} for n, v in pairs
            ]
            await context.add_cookies(minimal)
        except Exception as e2:
            print(f"[Bridge] add_cookies(minimal) failed: {e2}", flush=True)
            return 0
    print(
        f"[Bridge] injected {len(pairs)} cookie name(s) | source={cookie_source or 'unknown'} | domain={domain}",
        flush=True,
    )
    return len(pairs)


async def _wait_bridge_runtime_ready(page: Any, timeout_ms: int = 15000) -> None:
    try:
        await page.wait_for_function(
            """
            () => Boolean(
              window.__bridgeReady &&
              typeof window.__bridgeSend === "function" &&
              typeof window.__bridgeSendBin === "function" &&
              typeof window.__connectTarget === "function"
            )
            """,
            timeout=timeout_ms,
        )
    except Exception:
        try:
            state = await page.evaluate(
                """
                () => ({
                  bridgeReady: Boolean(window.__bridgeReady),
                  hasBridgeSend: typeof window.__bridgeSend === "function",
                  hasBridgeSendBin: typeof window.__bridgeSendBin === "function",
                  hasConnectTarget: typeof window.__connectTarget === "function",
                  href: location.href,
                  readyState: document.readyState,
                })
                """
            )
            print(f"[Bridge] runtime readiness snapshot: {state}", flush=True)
        except Exception as snap_err:
            print(f"[Bridge] runtime readiness snapshot failed: {snap_err}", flush=True)
        raise


_BRIDGE_RUNTIME_JS = r"""
(() => {
  if (window.__bridgeRuntimeInstalled) {
    return;
  }
  window.__bridgeRuntimeInstalled = true;
  window.__targetWs = window.__targetWs || null;
  window.__targetPending = window.__targetPending || [];

  window.__bridgeSend = (txt) => {
    if (window.__targetWs && window.__targetWs.readyState === 1) {
      window.__targetWs.send(txt);
    } else {
      window.__targetPending.push({ type: "t", data: txt });
    }
  };

  window.__bridgeSendBin = (b64) => {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    if (window.__targetWs && window.__targetWs.readyState === 1) {
      window.__targetWs.send(bytes.buffer);
    } else {
      window.__targetPending.push({ type: "b", data: b64 });
    }
  };

  const flushPending = () => {
    if (!window.__targetWs || window.__targetWs.readyState !== 1) return;
    while (window.__targetPending.length > 0) {
      const item = window.__targetPending.shift();
      if (item.type === "b") {
        const raw = atob(item.data);
        const bytes = new Uint8Array(raw.length);
        for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
        window.__targetWs.send(bytes.buffer);
      } else {
        window.__targetWs.send(item.data);
      }
    }
  };

  window.__connectTarget = (targetUrl) => {
    const ws = new WebSocket(targetUrl);
    window.__targetWs = ws;
    ws.binaryType = "arraybuffer";
    ws.onopen = () => {
      window.__bridgeEmit({ k: "t", d: "__WS_OPEN__" });
      flushPending();
    };
    ws.onclose = (event) => {
      const reason = event && typeof event.reason === "string" ? event.reason : "";
      const code = event && typeof event.code === "number" ? event.code : 0;
      const clean = Boolean(event && event.wasClean);
      window.__bridgeEmit({ k: "t", d: `__WS_CLOSE__|code=${code}|reason=${reason}|clean=${clean}` });
      setTimeout(() => window.__connectTarget(targetUrl), 1200);
    };
    ws.onerror = () => {
      const rs = ws ? ws.readyState : -1;
      window.__bridgeEmit({ k: "t", d: `__WS_ERROR__|readyState=${rs}` });
    };
    ws.onmessage = async (event) => {
      try {
        if (typeof event.data === "string") {
          window.__bridgeEmit({ k: "t", d: event.data });
          return;
        }
        let ab;
        if (event.data instanceof ArrayBuffer) {
          ab = event.data;
        } else if (event.data instanceof Blob) {
          ab = await event.data.arrayBuffer();
        } else {
          window.__bridgeEmit({ k: "t", d: String(event.data) });
          return;
        }
        const bytes = new Uint8Array(ab);
        let binary = "";
        for (let i = 0; i < bytes.length; i++) {
          binary += String.fromCharCode(bytes[i]);
        }
        window.__bridgeEmit({ k: "b", d: btoa(binary) });
      } catch (e) {
        window.__bridgeEmit({ k: "t", d: "__WS_BRIDGE_ERR__" });
      }
    };
  };

  window.__bridgeReady = true;
})();
"""


async def bridge_handler(client_ws, target_url: str, proxy_url: str = ""):
    outbound = asyncio.Queue()
    client_headers = _incoming_ws_request_headers(client_ws)
    resolved_proxy = _resolve_proxy_url(proxy_url)
    proxy_cfg = _playwright_proxy_config(resolved_proxy)
    if proxy_cfg:
        print(f"[Bridge] Playwright launch proxy: {proxy_cfg.get('server')}", flush=True)
    else:
        print("[Bridge] Playwright context: no proxy (same IP as VPS — may mismatch pyquotex login IP)", flush=True)

    async with async_playwright() as p:
        launch_kw: Dict[str, Any] = {
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        }
        if proxy_cfg:
            launch_kw["proxy"] = proxy_cfg
        browser = await p.chromium.launch(**launch_kw)
        default_ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
        ua = _user_agent_for_bridge(client_headers, default_ua)
        ctx_kw: Dict[str, Any] = {"user_agent": ua}
        context = await browser.new_context(**ctx_kw)
        await _playwright_inject_session_cookies(context, client_headers, target_url)
        await context.add_init_script(_BRIDGE_RUNTIME_JS)
        page = await context.new_page()
        await page.add_init_script(_BRIDGE_RUNTIME_JS)
        async def emit_to_python(payload):
            """payload من JS: {k:'t', d: str} أو {k:'b', d: base64}"""
            try:
                if not isinstance(payload, dict):
                    await outbound.put(("str", str(payload)))
                    return
                kind = payload.get("k")
                if kind == "b":
                    raw = base64.b64decode(payload.get("d") or "")
                    await outbound.put(("bin", raw))
                else:
                    await outbound.put(("str", payload.get("d") or ""))
            except Exception:
                print("[Bridge] emit_to_python failed")
                print(traceback.format_exc())

        await page.expose_function("__bridgeEmit", emit_to_python)
        await page.evaluate(_BRIDGE_RUNTIME_JS)
        # عبر بروكسي سكني قد يتأخر domcontentloaded دقائق؛ «commit» أسرع. بدون صفحة يصلح Origin أحياناً.
        nav_url = (os.environ.get("QUOTEX_BRIDGE_NAV_URL") or "https://qxbroker.com").strip()
        skip_nav = os.environ.get("QUOTEX_BRIDGE_SKIP_PAGE_NAV", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        try:
            nav_timeout = int(os.environ.get("QUOTEX_BRIDGE_NAV_TIMEOUT_MS", "90000") or 90000)
        except ValueError:
            nav_timeout = 90000
        nav_timeout = max(5000, min(nav_timeout, 300000))
        if skip_nav or not nav_url:
            await page.goto("about:blank")
            print("[Bridge] page: about:blank (skip nav أو URL فارغ)", flush=True)
        else:
            try:
                await page.goto(nav_url, wait_until="commit", timeout=nav_timeout)
                print(f"[Bridge] page: committed {nav_url}", flush=True)
            except Exception as nav_err:
                print(
                    f"[Bridge] page.goto failed ({nav_err}) — fallback about:blank",
                    flush=True,
                )
                try:
                    await page.goto("about:blank", wait_until="commit", timeout=5000)
                except Exception as blank_err:
                    print(f"[Bridge] about:blank fallback failed: {blank_err}", flush=True)
        await _wait_bridge_runtime_ready(page)
        await page.evaluate("(targetUrl) => window.__connectTarget(targetUrl)", target_url)

        async def from_local_client():
            async for msg in client_ws:
                try:
                    await _wait_bridge_runtime_ready(page, timeout_ms=5000)
                    if isinstance(msg, (bytes, bytearray)):
                        b64 = base64.b64encode(bytes(msg)).decode("ascii")
                        await page.evaluate("(b64) => window.__bridgeSendBin(b64)", b64)
                    else:
                        await page.evaluate("(m) => window.__bridgeSend(m)", str(msg))
                except Exception:
                    print("[Bridge] from_local_client send failed")
                    print(traceback.format_exc())

        async def to_local_client():
            while True:
                item = await outbound.get()
                if not isinstance(item, tuple) or len(item) != 2:
                    continue
                kind, data = item
                if kind == "str":
                    if data == "__WS_OPEN__":
                        print("[Bridge] upstream Quotex WebSocket OPEN", flush=True)
                        continue
                    if data.startswith("__WS_CLOSE__"):
                        print(f"[Bridge] upstream Quotex WebSocket CLOSE {data}", flush=True)
                        continue
                    if data.startswith("__WS_ERROR__"):
                        print(f"[Bridge] upstream Quotex WebSocket ERROR {data}", flush=True)
                        continue
                    if data == "__WS_BRIDGE_ERR__":
                        continue
                    try:
                        await client_ws.send(data)
                    except Exception:
                        print("[Bridge] to_local_client str send failed")
                        print(traceback.format_exc())
                        return
                else:
                    try:
                        await client_ws.send(data)
                    except Exception:
                        print("[Bridge] to_local_client bin send failed")
                        print(traceback.format_exc())
                        return

        try:
            await asyncio.gather(from_local_client(), to_local_client())
        finally:
            await browser.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8765)
    parser.add_argument("--target-url", required=True)
    parser.add_argument(
        "--proxy-url",
        default="",
        help="نفس بروكسي pyquotex (http/https/socks5) لتطابق IP جلسة الـ WebSocket",
    )
    args = parser.parse_args()

    async def handler(ws, *rest):
        try:
            await bridge_handler(ws, args.target_url, proxy_url=args.proxy_url)
        except Exception as e:
            print(f"[Bridge] handler crash: {e}")
            raise

    async with websockets.serve(handler, args.listen_host, args.listen_port):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
