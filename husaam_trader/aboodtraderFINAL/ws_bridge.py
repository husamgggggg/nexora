#!/usr/bin/env python3
import argparse
import asyncio
import traceback
from playwright.async_api import async_playwright
import websockets


async def bridge_handler(client_ws, target_url: str):
    outbound = asyncio.Queue()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()
        await page.goto("https://qxbroker.com", wait_until="domcontentloaded")

        async def emit_to_python(message):
            await outbound.put(str(message))

        await page.expose_function("__bridgeEmit", emit_to_python)
        await page.evaluate(
            """
            () => {
              window.__bridgeQueue = [];
              window.__bridgeResolvers = [];
              window.__bridgePushFromPy = (msg) => {
                if (window.__bridgeResolvers.length > 0) {
                  const fn = window.__bridgeResolvers.shift();
                  fn(msg);
                } else {
                  window.__bridgeQueue.push(msg);
                }
              };
              window.__bridgePullFromPy = () => {
                return new Promise((resolve) => {
                  if (window.__bridgeQueue.length > 0) {
                    resolve(window.__bridgeQueue.shift());
                  } else {
                    window.__bridgeResolvers.push(resolve);
                  }
                });
              };
            }
            """
        )

        await page.evaluate(
            """
            (targetUrl) => {
              window.__targetWs = null;
              window.__targetOpen = false;
              window.__targetPending = [];

              window.__bridgeSend = (msg) => {
                if (window.__targetWs && window.__targetWs.readyState === 1) {
                  window.__targetWs.send(msg);
                } else {
                  window.__targetPending.push(msg);
                }
              };

              const flushPending = () => {
                if (!window.__targetWs || window.__targetWs.readyState !== 1) return;
                while (window.__targetPending.length > 0) {
                  const m = window.__targetPending.shift();
                  window.__targetWs.send(m);
                }
              };

              const connectTarget = () => {
                const ws = new WebSocket(targetUrl);
                window.__targetWs = ws;
                ws.onopen = () => {
                  window.__targetOpen = true;
                  window.__bridgeEmit("__WS_OPEN__");
                  flushPending();
                };
                ws.onclose = () => {
                  window.__targetOpen = false;
                  window.__bridgeEmit("__WS_CLOSE__");
                  setTimeout(connectTarget, 1200);
                };
                ws.onerror = () => {
                  window.__bridgeEmit("__WS_ERROR__");
                };
                ws.onmessage = (event) => {
                  if (typeof event.data === "string") {
                    window.__bridgeEmit(event.data);
                  } else {
                    window.__bridgeEmit("__WS_BINARY__");
                  }
                };
              };

              connectTarget();
            }
            """,
            target_url,
        )

        async def from_local_client():
            async for msg in client_ws:
                try:
                    if isinstance(msg, (bytes, bytearray)):
                        try:
                            payload = msg.decode("utf-8")
                        except Exception:
                            payload = msg.decode("latin-1", errors="ignore")
                    else:
                        payload = str(msg)
                    await page.evaluate("(m) => window.__bridgeSend(m)", payload)
                except Exception:
                    print("[Bridge] from_local_client send failed")
                    print(traceback.format_exc())

        async def to_local_client():
            while True:
                msg = await outbound.get()
                if msg == "__WS_OPEN__":
                    continue
                if msg == "__WS_CLOSE__":
                    # target closed; keep local open while browser auto-reconnects
                    continue
                if msg == "__WS_ERROR__":
                    continue
                try:
                    await client_ws.send(msg)
                except Exception:
                    print("[Bridge] to_local_client send failed")
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
    args = parser.parse_args()

    async def handler(ws, *rest):
        # Compatible with both websockets APIs:
        # - new versions: handler(ws)
        # - old versions: handler(ws, path)
        try:
            await bridge_handler(ws, args.target_url)
        except Exception as e:
            print(f"[Bridge] handler crash: {e}")
            raise

    async with websockets.serve(handler, args.listen_host, args.listen_port):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
