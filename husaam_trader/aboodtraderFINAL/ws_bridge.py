#!/usr/bin/env python3
"""
جسر WebSocket: Playwright (متصفح) ↔ websockets (محلي) ↔ pyquotex/websocket-client.

كل اتصال محلي (client_ws) = جلسة مستقلة: upstream واحد في الصفحة، وعند إغلاق المحلي
يُوقف إعادة الاتصال في JS ويُغلق upstream فوراً — لا retry بعد انتهاء الجلسة.

يُمرَّر عبر ``--proxy-url`` من bot.py أو من البيئة:
``QUOTEX_WS_BRIDGE_PROXY`` ثم ``QUOTEX_PROXY_URL`` ثم ``HTTPS_PROXY`` / ``HTTP_PROXY``.
"""
import argparse
import asyncio
import base64
import os
import traceback
import urllib.parse
import uuid
from typing import Any, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosed

try:
    from websockets.protocol import State as _WsState
except Exception:  # pragma: no cover
    _WsState = None  # type: ignore

from playwright.async_api import async_playwright

_SESSION_END = object()


def _ws_still_open(conn) -> bool:
    """True إذا كان بإمكاننا إرسال إطارات نحو العميل المحلي."""
    if _WsState is not None:
        try:
            return conn.state == _WsState.OPEN
        except Exception:
            pass
    try:
        code = getattr(conn, "close_code", None)
        return code is None
    except Exception:
        return False


def _playwright_target_gone(exc: BaseException) -> bool:
    if type(exc).__name__ == "TargetClosedError":
        return True
    msg = str(exc).lower()
    return "has been closed" in msg and (
        "target page" in msg or "browser has been closed" in msg or "context" in msg
    )


async def _safe_close_playwright(page, context, browser) -> None:
    for target, _ in (
        (page, "page"),
        (context, "context"),
        (browser, "browser"),
    ):
        try:
            if target is not None:
                await target.close()
        except Exception:
            pass


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
    session_id = uuid.uuid4().hex[:12]
    outbound: asyncio.Queue = asyncio.Queue()
    session_done = asyncio.Event()
    end_lock = asyncio.Lock()

    print(f"[Bridge] session={session_id} begin | upstream target len={len(target_url or '')}", flush=True)

    resolved_proxy = _resolve_proxy_url(proxy_url)
    proxy_cfg = _playwright_proxy_config(resolved_proxy)
    if proxy_cfg:
        print(f"[Bridge] session={session_id} Playwright proxy: {proxy_cfg.get('server')}", flush=True)
    else:
        print(
            f"[Bridge] session={session_id} Playwright: no proxy (IP may mismatch pyquotex login)",
            flush=True,
        )

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
            print(f"[Bridge] session={session_id} page: about:blank", flush=True)
        else:
            try:
                await page.goto(nav_url, wait_until="commit", timeout=nav_timeout)
                print(f"[Bridge] session={session_id} page: committed {nav_url}", flush=True)
            except Exception as nav_err:
                print(f"[Bridge] session={session_id} page.goto failed ({nav_err}) — about:blank", flush=True)
                await page.goto("about:blank")

        async def emit_to_python(payload):
            if session_done.is_set():
                return
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
            except Exception as e:
                if _playwright_target_gone(e):
                    return
                print(f"[Bridge] session={session_id} emit_to_python failed")
                print(traceback.format_exc())

        await page.expose_function("__bridgeEmit", emit_to_python)

        async def end_session(reason: str) -> None:
            async with end_lock:
                if session_done.is_set():
                    return
                session_done.set()
                print(f"[Bridge] session={session_id} end | {reason}", flush=True)
                try:
                    await page.evaluate(
                        "() => { if (window.__bridgeStopUpstream) window.__bridgeStopUpstream(); }"
                    )
                except Exception:
                    pass
                try:
                    await outbound.put(_SESSION_END)
                except Exception:
                    pass

        await page.evaluate(
            """
            () => {
              window.__targetWs = null;
              window.__targetPending = [];
              window.__bridgeSessionActive = true;
              window.__reconnectTimer = null;

              window.__bridgeStopUpstream = () => {
                window.__bridgeSessionActive = false;
                if (window.__reconnectTimer != null) {
                  clearTimeout(window.__reconnectTimer);
                  window.__reconnectTimer = null;
                }
                try {
                  const w = window.__targetWs;
                  window.__targetWs = null;
                  if (w) {
                    w.onopen = null;
                    w.onclose = null;
                    w.onerror = null;
                    w.onmessage = null;
                    if (w.readyState === 0 || w.readyState === 1) {
                      w.close(1000, "local bridge session ended");
                    }
                  }
                } catch (e) {}
                window.__targetPending = [];
              };

              window.__bridgeSend = (txt) => {
                if (!window.__bridgeSessionActive) return;
                if (window.__targetWs && window.__targetWs.readyState === 1) {
                  window.__targetWs.send(txt);
                } else {
                  window.__targetPending.push({ type: "t", data: txt });
                }
              };

              window.__bridgeSendBin = (b64) => {
                if (!window.__bridgeSessionActive) return;
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
                if (!window.__bridgeSessionActive) return;
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

              const scheduleReconnect = (targetUrl) => {
                if (!window.__bridgeSessionActive) return;
                if (window.__reconnectTimer != null) clearTimeout(window.__reconnectTimer);
                window.__reconnectTimer = setTimeout(() => {
                  window.__reconnectTimer = null;
                  if (window.__bridgeSessionActive) connectTarget(targetUrl);
                }, 1200);
              };

              const connectTarget = (targetUrl) => {
                if (!window.__bridgeSessionActive) return;
                const ws = new WebSocket(targetUrl);
                window.__targetWs = ws;
                ws.binaryType = "arraybuffer";
                ws.onopen = () => {
                  if (!window.__bridgeSessionActive) {
                    try { ws.close(1000, "session inactive"); } catch (e) {}
                    return;
                  }
                  window.__bridgeEmit({ k: "t", d: "__WS_OPEN__" });
                  flushPending();
                };
                ws.onclose = (ev) => {
                  const code = ev && ev.code != null ? ev.code : "?";
                  const reason = (ev && ev.reason) ? String(ev.reason) : "";
                  window.__bridgeEmit({ k: "t", d: "__WS_DIAG__close|" + code + "|" + reason });
                  window.__bridgeEmit({ k: "t", d: "__WS_CLOSE__" });
                  scheduleReconnect(targetUrl);
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
            try:
                async for msg in client_ws:
                    if session_done.is_set():
                        break
                    if not _ws_still_open(client_ws):
                        break
                    try:
                        if isinstance(msg, (bytes, bytearray)):
                            b64 = base64.b64encode(bytes(msg)).decode("ascii")
                            await page.evaluate("(b64) => window.__bridgeSendBin(b64)", b64)
                        else:
                            await page.evaluate("(m) => window.__bridgeSend(m)", str(msg))
                    except ConnectionClosed:
                        break
                    except Exception as e:
                        if _playwright_target_gone(e):
                            break
                        print(f"[Bridge] session={session_id} from_local_client failed")
                        print(traceback.format_exc())
            except ConnectionClosed:
                pass
            finally:
                await end_session("local reader finished")

        async def to_local_client():
            while True:
                item = await outbound.get()
                if item is _SESSION_END:
                    break
                if not isinstance(item, tuple) or len(item) != 2:
                    continue
                if session_done.is_set():
                    break
                if not _ws_still_open(client_ws):
                    await end_session("local closed before forward")
                    break
                kind, data = item
                if kind == "str":
                    if isinstance(data, str) and data.startswith("__WS_DIAG__"):
                        print(f"[Bridge] session={session_id} {data}", flush=True)
                        continue
                    if data == "__WS_OPEN__":
                        print(f"[Bridge] session={session_id} upstream Quotex WebSocket OPEN", flush=True)
                        continue
                    if data == "__WS_CLOSE__":
                        print(f"[Bridge] session={session_id} upstream Quotex WebSocket CLOSE", flush=True)
                        continue
                    if data == "__WS_ERROR__":
                        print(f"[Bridge] session={session_id} upstream Quotex WebSocket ERROR", flush=True)
                        continue
                    if data == "__WS_BRIDGE_ERR__":
                        continue
                    if not _ws_still_open(client_ws):
                        await end_session("local closed (str payload)")
                        break
                    try:
                        await client_ws.send(data)
                    except ConnectionClosed as e:
                        print(
                            f"[Bridge] session={session_id} local closed ({e.code}); stop upstream→local",
                            flush=True,
                        )
                        await end_session("ConnectionClosed on str send")
                        break
                    except Exception:
                        print(f"[Bridge] session={session_id} to_local_client str send failed")
                        print(traceback.format_exc())
                        await end_session("str send exception")
                        break
                else:
                    if not _ws_still_open(client_ws):
                        await end_session("local closed (bin payload)")
                        break
                    try:
                        await client_ws.send(data)
                    except ConnectionClosed as e:
                        print(
                            f"[Bridge] session={session_id} local closed ({e.code}); stop upstream→local",
                            flush=True,
                        )
                        await end_session("ConnectionClosed on bin send")
                        break
                    except Exception:
                        print(f"[Bridge] session={session_id} to_local_client bin send failed")
                        print(traceback.format_exc())
                        await end_session("bin send exception")
                        break

        try:
            t_in = asyncio.create_task(from_local_client())
            t_out = asyncio.create_task(to_local_client())
            done, pending = await asyncio.wait(
                {t_in, t_out},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(t_in, t_out, return_exceptions=True)
            await end_session("bridge tasks joined")
        finally:
            await _safe_close_playwright(page, context, browser)

    print(f"[Bridge] session={session_id} browser closed", flush=True)


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
        peer = getattr(ws, "remote_address", None)
        print(f"[Bridge] local TCP/WebSocket peer={peer}", flush=True)
        try:
            await bridge_handler(ws, args.target_url, proxy_url=args.proxy_url)
        except Exception as e:
            print(f"[Bridge] handler crash: {e}")
            raise

    async with websockets.serve(handler, args.listen_host, args.listen_port):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
