#!/usr/bin/env python3
"""
Manual Cloudflare verifier:
- Opens real Chromium window (headful)
- User solves challenge once
- Saves Playwright storage_state to file for ws_bridge reuse
"""
import argparse
import json
import os
import sys
import time
import urllib.parse


def _proxy_cfg(proxy_url: str):
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
        return None
    scheme = (u.scheme or "http").lower()
    if scheme.startswith("socks5"):
        server = f"socks5://{u.hostname}:{port}"
    elif scheme.startswith("socks4"):
        server = f"socks4://{u.hostname}:{port}"
    else:
        server = f"http://{u.hostname}:{port}"
    cfg = {"server": server}
    if u.username:
        cfg["username"] = urllib.parse.unquote(u.username)
        cfg["password"] = urllib.parse.unquote(u.password or "")
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://qxbroker.com/en/sign-in")
    ap.add_argument("--state-path", required=True)
    ap.add_argument("--done-path", required=True)
    ap.add_argument("--timeout-sec", type=int, default=900)
    ap.add_argument("--proxy-url", default="")
    args = ap.parse_args()

    state_path = os.path.abspath(args.state_path)
    done_path = os.path.abspath(args.done_path)
    timeout_sec = max(120, min(int(args.timeout_sec or 900), 7200))

    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    os.makedirs(os.path.dirname(done_path), exist_ok=True)
    try:
        if os.path.isfile(done_path):
            os.remove(done_path)
    except Exception:
        pass

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"[manual_cf] playwright import failed: {e}", flush=True)
        return 2

    proxy_cfg = _proxy_cfg(args.proxy_url)
    print(f"[manual_cf] start url={args.url} timeout={timeout_sec}s", flush=True)
    if proxy_cfg:
        print(f"[manual_cf] proxy={proxy_cfg.get('server')}", flush=True)
    else:
        print("[manual_cf] proxy=none", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx_kw = {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        }
        if proxy_cfg:
            ctx_kw["proxy"] = proxy_cfg
        context = browser.new_context(**ctx_kw)
        page = context.new_page()
        try:
            page.goto(args.url, wait_until="commit", timeout=120000)
        except Exception as e:
            print(f"[manual_cf] goto failed: {e}", flush=True)

        print("[manual_cf] solve Cloudflare manually in opened browser...", flush=True)
        end = time.time() + timeout_sec
        ok = False
        while time.time() < end:
            try:
                cookies = context.cookies(["https://qxbroker.com", "https://www.qxbroker.com"])
            except Exception:
                cookies = []
            cf_cookie = None
            for c in cookies:
                if str(c.get("name") or "").strip() == "cf_clearance":
                    cf_cookie = c
                    break
            if cf_cookie and str(cf_cookie.get("value") or "").strip():
                ok = True
                break
            time.sleep(2.0)

        if not ok:
            print("[manual_cf] timeout: cf_clearance not detected", flush=True)
            try:
                browser.close()
            except Exception:
                pass
            return 3

        context.storage_state(path=state_path)
        with open(done_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "saved_at": int(time.time()),
                    "state_path": state_path,
                    "note": "manual cloudflare verification done",
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"[manual_cf] success: storage_state saved -> {state_path}", flush=True)
        try:
            browser.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
