#!/usr/bin/env python3
import argparse
import asyncio
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
            async (targetUrl) => {
              const ws = new WebSocket(targetUrl);
              window.__targetWs = ws;
              ws.onopen = () => window.__bridgeEmit("__WS_OPEN__");
              ws.onclose = () => window.__bridgeEmit("__WS_CLOSE__");
              ws.onerror = () => window.__bridgeEmit("__WS_ERROR__");
              ws.onmessage = (event) => {
                if (typeof event.data === "string") {
                  window.__bridgeEmit(event.data);
                } else {
                  window.__bridgeEmit("__WS_BINARY__");
                }
              };
              while (true) {
                const msg = await window.__bridgePullFromPy();
                if (ws.readyState === 1) {
                  ws.send(msg);
                }
              }
            }
            """,
            target_url,
        )

        async def from_local_client():
            async for msg in client_ws:
                # Important: pass arg through evaluate function parameter, not arguments[0].
                await page.evaluate("(m) => window.__bridgePushFromPy(m)", str(msg))

        async def to_local_client():
            while True:
                msg = await outbound.get()
                if msg == "__WS_CLOSE__":
                    await client_ws.close()
                    return
                if msg == "__WS_ERROR__":
                    # keep local socket alive briefly; server might recover/reconnect
                    continue
                await client_ws.send(msg)

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

    async def handler(ws):
        await bridge_handler(ws, args.target_url)

    async with websockets.serve(handler, args.listen_host, args.listen_port):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
