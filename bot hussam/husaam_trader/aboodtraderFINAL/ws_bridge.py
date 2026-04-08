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
"""
import argparse
import asyncio
import base64
import os
import traceback
import urllib.parse
from typing import Any, Dict, Optional

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


async def bridge_handler(client_ws, target_url: str, proxy_url: str = ""):
    outbound = asyncio.Queue()
    resolved_proxy = _resolve_proxy_url(proxy_url)
    proxy_cfg = _playwright_proxy_config(resolved_proxy)
    if proxy_cfg:
        print(f"[Bridge] Playwright context proxy: {proxy_cfg.get('server')}", flush=True)
    else:
        print("[Bridge] Playwright context: no proxy (same IP as VPS — may mismatch pyquotex login IP)", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx_kw: Dict[str, Any] = {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        }
        if proxy_cfg:
            ctx_kw["proxy"] = proxy_cfg
        context = await browser.new_context(**ctx_kw)
        page = await context.new_page()
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
                await page.goto("about:blank")

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

        await page.evaluate(
            """
            () => {
              window.__targetWs = null;
              window.__targetPending = [];

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

              const connectTarget = (targetUrl) => {
                const ws = new WebSocket(targetUrl);
                window.__targetWs = ws;
                ws.binaryType = "arraybuffer";
                ws.onopen = () => {
                  window.__bridgeEmit({ k: "t", d: "__WS_OPEN__" });
                  flushPending();
                };
                ws.onclose = () => {
                  window.__bridgeEmit({ k: "t", d: "__WS_CLOSE__" });
                  setTimeout(() => connectTarget(targetUrl), 1200);
                };
                ws.onerror = () => {
                  window.__bridgeEmit({ k: "t", d: "__WS_ERROR__" });
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

              window.__connectTarget = connectTarget;
            }
            """
        )

        await page.evaluate("(targetUrl) => window.__connectTarget(targetUrl)", target_url)

        async def from_local_client():
            async for msg in client_ws:
                try:
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
                    if data == "__WS_CLOSE__":
                        print("[Bridge] upstream Quotex WebSocket CLOSE", flush=True)
                        continue
                    if data == "__WS_ERROR__":
                        print("[Bridge] upstream Quotex WebSocket ERROR", flush=True)
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
