"""
JavaScript snippets injected via QWebEnginePage.runJavaScript.

Keep scripts as plain strings; Python builds them from selectors in selectors.py.
Results are JSON-serializable dicts for QWebEngine callback parsing.
"""

from __future__ import annotations

import json

from core import selectors as sel


def _js_str_list(py_list: tuple[str, ...]) -> str:
    return json.dumps(list(py_list))


def login_probe_script() -> str:
    """Return JS that reports login heuristics from the current document."""
    form_sels = _js_str_list(sel.LOGIN_FORM_SELECTORS)
    bal_hints = _js_str_list(sel.BALANCE_CONTAINER_HINTS)
    demo_sels = _js_str_list(sel.DEMO_LABEL_SELECTORS)
    real_sels = _js_str_list(sel.REAL_LABEL_SELECTORS)
    return f"""
    (function() {{
        const formSelectors = {form_sels};
        const balanceHints = {bal_hints};
        const demoSels = {demo_sels};
        const realSels = {real_sels};

        function anyVisible(selectorList) {{
            for (const s of selectorList) {{
                try {{
                    const el = document.querySelector(s);
                    if (el) {{
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        if (r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none')
                            return true;
                    }}
                }} catch (e) {{}}
            }}
            return false;
        }}

        function countMatches(selectorList) {{
            let n = 0;
            for (const s of selectorList) {{
                try {{
                    n += document.querySelectorAll(s).length;
                }} catch (e) {{}}
            }}
            return n;
        }}

        const hasPasswordField = anyVisible(['input[type="password"]']);
        const loginFormVisible = anyVisible(formSelectors);
        const balanceLikeCount = countMatches(balanceHints);
        const title = document.title || '';
        const href = window.location.href || '';

        return JSON.stringify({{
            hasPasswordField: hasPasswordField,
            loginFormVisible: loginFormVisible,
            balanceLikeCount: balanceLikeCount,
            demoMarkers: countMatches(demoSels),
            realMarkers: countMatches(realSels),
            title: title,
            href: href
        }});
    }})()
    """


def balance_read_script() -> str:
    """Heuristic read of demo/real balance and account hints from DOM text."""
    demo_sels = _js_str_list(sel.DEMO_LABEL_SELECTORS)
    real_sels = _js_str_list(sel.REAL_LABEL_SELECTORS)
    bal_hints = _js_str_list(sel.BALANCE_CONTAINER_HINTS)
    return f"""
    (function() {{
        const demoSels = {demo_sels};
        const realSels = {real_sels};
        const balHints = {bal_hints};

        function textFrom(el) {{
            if (!el) return '';
            return (el.innerText || el.textContent || '').trim();
        }}

        function firstVisibleText(selectorList) {{
            for (const s of selectorList) {{
                try {{
                    const els = document.querySelectorAll(s);
                    for (const el of els) {{
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        if (r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none') {{
                            const t = textFrom(el);
                            if (t.length > 0 && t.length < 500) return t;
                        }}
                    }}
                }} catch (e) {{}}
            }}
            return '';
        }}

        function gatherBalanceSnippets() {{
            const snippets = [];
            for (const s of balHints) {{
                try {{
                    document.querySelectorAll(s).forEach(el => {{
                        const t = textFrom(el);
                        if (/\\d/.test(t) && t.length < 200) snippets.push(t);
                    }});
                }} catch (e) {{}}
            }}
            return snippets.slice(0, 12);
        }}

        const demoContext = firstVisibleText(demoSels);
        const realContext = firstVisibleText(realSels);
        const snippets = gatherBalanceSnippets();

        let accountType = 'unknown';
        if (demoContext && !realContext) accountType = 'demo';
        else if (realContext && !demoContext) accountType = 'real';
        else if (demoContext && realContext) accountType = 'mixed';

        return JSON.stringify({{
            demoContext: demoContext,
            realContext: realContext,
            accountType: accountType,
            balanceSnippets: snippets,
            href: window.location.href || '',
            title: document.title || ''
        }});
    }})()
    """


def trade_fill_and_click_script(amount: float, side: str) -> str:
    """
    Attempt to set amount and click buy (call) or sell (put).
    side: 'buy' | 'sell'
    """
    side = "buy" if side.lower() == "buy" else "sell"
    amount_s = json.dumps(str(amount))
    buy_sels = _js_str_list(sel.TRADE_BUY_BUTTON_SELECTORS)
    sell_sels = _js_str_list(sel.TRADE_SELL_BUTTON_SELECTORS)
    amt_sels = _js_str_list(sel.TRADE_AMOUNT_INPUT_SELECTORS)
    return f"""
    (function() {{
        const amountStr = {amount_s};
        const buySelectors = {buy_sels};
        const sellSelectors = {sell_sels};
        const amountSelectors = {amt_sels};
        const side = {json.dumps(side)};

        function clickFirstVisible(selectorList) {{
            for (const s of selectorList) {{
                try {{
                    const els = document.querySelectorAll(s);
                    for (const el of els) {{
                        const r = el.getBoundingClientRect();
                        const st = window.getComputedStyle(el);
                        if (r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none') {{
                            el.click();
                            return {{ ok: true, selector: s }};
                        }}
                    }}
                }} catch (e) {{}}
            }}
            return {{ ok: false, reason: 'no_visible_button' }};
        }}

        function setAmount() {{
            for (const s of amountSelectors) {{
                try {{
                    const el = document.querySelector(s);
                    if (el && el.tagName === 'INPUT') {{
                        el.focus();
                        el.value = amountStr;
                        el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        return {{ ok: true, selector: s }};
                    }}
                }} catch (e) {{}}
            }}
            return {{ ok: false, reason: 'no_amount_input' }};
        }}

        const amtResult = setAmount();
        const btnList = side === 'buy' ? buySelectors : sellSelectors;
        const clickResult = clickFirstVisible(btnList);

        return JSON.stringify({{
            side: side,
            amountSet: amtResult,
            click: clickResult
        }});
    }})()
    """
