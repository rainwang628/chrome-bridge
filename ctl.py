#!/usr/bin/env python3
"""chrome-bridge 命令行客户端。

用法示例：
  python3 ctl.py status
  python3 ctl.py tabs
  python3 ctl.py goto https://example.com
  python3 ctl.py text h1
  python3 ctl.py fill '#name' 张三
  python3 ctl.py fillm '{"#user":"abc","#pass":"123"}'
  python3 ctl.py click 'button[type=submit]'
  python3 ctl.py wait '.result' --timeout 10
  python3 ctl.py shot ~/rain/tmp/shot.png

也可以当库用：import ctl; ctl.click('#ok')
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("BRIDGE_PORT", "8777"))
BASE = f"http://127.0.0.1:{PORT}"


def _token():
    p = os.path.join(HERE, "token")
    if not os.path.exists(p):
        raise SystemExit("缺少 token 文件，先启动 server.py")
    return open(p, encoding="utf-8").read().strip()


TOKEN = None


def _post(path, payload, timeout=40):
    global TOKEN
    if TOKEN is None:
        TOKEN = _token()
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Bridge-Token": TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")
    except urllib.error.URLError:
        raise SystemExit(f"桥接服务没在跑（{BASE}），先执行：python3 {os.path.join(HERE, 'server.py')} &")


def call(method, params=None, timeout=30):
    out = _post("/cmd", {"method": method, "params": params or {}, "timeout": timeout}, timeout=timeout + 10)
    if not out.get("ok"):
        err = out.get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(msg)
    return out.get("result")


def status():
    global TOKEN
    if TOKEN is None:
        TOKEN = _token()
    req = urllib.request.Request(BASE + "/status", headers={"X-Bridge-Token": TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except urllib.error.URLError:
        return {"connected": False, "version": None, "server": False}


# --------------------------------------------------------------- 页面操作
def ev(code, tab=None, cdp=False, timeout=30):
    p = {"code": code}
    if tab is not None:
        p["tabId"] = tab
    if cdp:
        p["cdp"] = True
    return call("eval", p, timeout=timeout).get("value")


def click(selector, tab=None):
    code = (
        "(() => { const el = document.querySelector(%s);"
        " if(!el) throw new Error('找不到元素: '+%s);"
        " el.scrollIntoView({block:'center', inline:'center'});"
        " const r = el.getBoundingClientRect();"
        " const o = {bubbles:true, cancelable:true, view:window,"
        "   clientX:r.left+r.width/2, clientY:r.top+r.height/2, button:0};"
        " el.dispatchEvent(new PointerEvent('pointerdown', o));"
        " el.dispatchEvent(new MouseEvent('mousedown', o));"
        " el.dispatchEvent(new PointerEvent('pointerup', o));"
        " el.dispatchEvent(new MouseEvent('mouseup', o));"
        " el.click();"
        " return {tag:el.tagName, text:(el.innerText||'').trim().slice(0,60)}; })()"
    ) % (json.dumps(selector), json.dumps(selector))
    return ev(code, tab)


FILL_JS = (
    "(sel, val) => { const el = document.querySelector(sel);"
    " if(!el) throw new Error('找不到元素: '+sel);"
    " el.focus();"
    " if (el.isContentEditable) { el.textContent = val;"
    "   el.dispatchEvent(new InputEvent('input', {bubbles:true, data:val, inputType:'insertText'})); }"
    " else { const d = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');"
    "   if (d && d.set) d.set.call(el, val); else el.value = val;"
    "   el.dispatchEvent(new Event('input', {bubbles:true}));"
    "   el.dispatchEvent(new Event('change', {bubbles:true})); }"
    " el.dispatchEvent(new Event('blur', {bubbles:true}));"
    " return {sel:sel, value:('value' in el ? el.value : el.textContent).slice(0,100)}; }"
)


def fill(selector, value, tab=None):
    code = "(() => { const f = %s; return f(%s, %s); })()" % (
        FILL_JS,
        json.dumps(selector),
        json.dumps(value),
    )
    return ev(code, tab)


def fillm(mapping, tab=None):
    """mapping: {selector: value} 或 [(selector, value), ...]"""
    pairs = list(mapping.items()) if isinstance(mapping, dict) else list(mapping)
    body = ", ".join("[%s, %s]" % (json.dumps(s), json.dumps(v)) for s, v in pairs)
    code = (
        "(() => { const f = %s; const out = [];"
        " for (const [sel, val] of [%s]) { try { out.push(f(sel, val)); }"
        "   catch(e) { out.push({sel:sel, error:String(e)}); } } return out; })()"
    ) % (FILL_JS, body)
    return ev(code, tab)


def text(selector, tab=None):
    code = (
        "(() => { const el = document.querySelector(%s);"
        " if(!el) throw new Error('找不到元素: '+%s);"
        " return (el.innerText || el.value || el.textContent || '').trim(); })()"
    ) % (json.dumps(selector), json.dumps(selector))
    return ev(code, tab)


def html(selector, tab=None):
    code = (
        "(() => { const el = document.querySelector(%s);"
        " if(!el) return null; return el.outerHTML; })()"
    ) % json.dumps(selector)
    return ev(code, tab)


def press(key, selector=None, tab=None):
    sel = json.dumps(selector) if selector else "document.activeElement"
    code = (
        "(() => { const el = (%s) || document.activeElement;"
        " if(!el) throw new Error('没有焦点元素');"
        " const o = {bubbles:true, cancelable:true, key:%s, code:%s};"
        " el.dispatchEvent(new KeyboardEvent('keydown', o));"
        " el.dispatchEvent(new KeyboardEvent('keypress', o));"
        " el.dispatchEvent(new KeyboardEvent('keyup', o));"
        " if (o.key === 'Enter' && el.form) { try { el.form.requestSubmit(); } catch(e) {} }"
        " return 'pressed ' + o.key; })()"
    ) % (sel, json.dumps(key), json.dumps(key))
    return ev(code, tab)


def wait_for(selector, timeout=10.0, tab=None, interval=0.25):
    code = "!!document.querySelector(%s)" % json.dumps(selector)
    end = time.time() + timeout
    while time.time() < end:
        try:
            if ev(code, tab):
                return True
        except RuntimeError:
            pass
        time.sleep(interval)
    raise TimeoutError(f"{timeout:.0f}s 内没等到 {selector}")


def wait_text(needle, timeout=10.0, tab=None, interval=0.3):
    code = "document.body ? document.body.innerText.includes(%s) : false" % json.dumps(needle)
    end = time.time() + timeout
    while time.time() < end:
        try:
            if ev(code, tab):
                return True
        except RuntimeError:
            pass
        time.sleep(interval)
    raise TimeoutError(f"{timeout:.0f}s 内页面没出现文字：{needle}")


def tabs(all_tabs=False):
    return call("tabs.list", {"all": all_tabs})


def active_tab():
    return call("tabs.active")


def goto(url, tab=None, new=False):
    if new or tab is None:
        t = call("tabs.create", {"url": url, "active": True})
        # 等加载完
        end = time.time() + 20
        while time.time() < end:
            try:
                cur = call("tabs.active")
                if cur["status"] == "complete" and cur["id"] == t["id"]:
                    return cur
            except RuntimeError:
                pass
            time.sleep(0.3)
        return t
    return call("tabs.update", {"tabId": tab, "url": url})


def shot(path, tab=None):
    r = call("screenshot", {"tabId": tab} if tab is not None else {}, timeout=30)
    data = r["dataUrl"].split(",", 1)[1]
    path = os.path.abspath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(base64.b64decode(data))
    return path


# -------------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="Chrome 桥接客户端")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="扩展是否已连上")
    p = sub.add_parser("tabs", help="列出标签页")
    p.add_argument("--all", action="store_true")
    sub.add_parser("active", help="当前标签页")

    p = sub.add_parser("goto", help="打开网址")
    p.add_argument("url")
    p.add_argument("--tab", type=int)
    p.add_argument("--new", action="store_true")

    p = sub.add_parser("js", help="在页面里执行表达式并返回结果")
    p.add_argument("code")
    p.add_argument("--tab", type=int)
    p.add_argument("--cdp", action="store_true")
    p.add_argument("--timeout", type=float, default=30)

    p = sub.add_parser("click", help="点击元素")
    p.add_argument("selector")
    p.add_argument("--tab", type=int)

    p = sub.add_parser("fill", help="填写一个字段")
    p.add_argument("selector")
    p.add_argument("value")
    p.add_argument("--tab", type=int)

    p = sub.add_parser("fillm", help="批量填写，参数是 JSON 对象")
    p.add_argument("mapping")
    p.add_argument("--tab", type=int)

    p = sub.add_parser("text", help="读取元素文字")
    p.add_argument("selector")
    p.add_argument("--tab", type=int)

    p = sub.add_parser("html", help="读取元素 HTML")
    p.add_argument("selector")
    p.add_argument("--tab", type=int)

    p = sub.add_parser("press", help="按键")
    p.add_argument("key")
    p.add_argument("--sel")
    p.add_argument("--tab", type=int)

    p = sub.add_parser("wait", help="等元素出现")
    p.add_argument("selector")
    p.add_argument("--timeout", type=float, default=10)
    p.add_argument("--tab", type=int)

    p = sub.add_parser("waittext", help="等页面出现文字")
    p.add_argument("needle")
    p.add_argument("--timeout", type=float, default=10)
    p.add_argument("--tab", type=int)

    p = sub.add_parser("shot", help="截图")
    p.add_argument("path")
    p.add_argument("--tab", type=int)

    a = ap.parse_args(argv)
    tab = getattr(a, "tab", None)
    try:
        if a.cmd == "status":
            out = status()
            if not out.get("server", True):
                out["hint"] = "桥接服务没运行"
            print(json.dumps(out, ensure_ascii=False, indent=2))
        elif a.cmd == "tabs":
            for t in tabs(a.all):
                mark = "*" if t["active"] else " "
                print(f'{mark} {t["id"]:>6}  {t["title"][:48]:<50} {t["url"][:80]}')
        elif a.cmd == "active":
            print(json.dumps(active_tab(), ensure_ascii=False, indent=2))
        elif a.cmd == "goto":
            print(json.dumps(goto(a.url, tab, a.new), ensure_ascii=False, indent=2))
        elif a.cmd == "js":
            v = ev(a.code, tab, a.cdp, a.timeout)
            print(json.dumps(v, ensure_ascii=False, indent=2) if not isinstance(v, str) else v)
        elif a.cmd == "click":
            print(json.dumps(click(a.selector, tab), ensure_ascii=False))
        elif a.cmd == "fill":
            print(json.dumps(fill(a.selector, a.value, tab), ensure_ascii=False))
        elif a.cmd == "fillm":
            print(json.dumps(fillm(json.loads(a.mapping), tab), ensure_ascii=False, indent=2))
        elif a.cmd == "text":
            print(text(a.selector, tab))
        elif a.cmd == "html":
            print(html(a.selector, tab))
        elif a.cmd == "press":
            print(press(a.key, a.sel, tab))
        elif a.cmd == "wait":
            wait_for(a.selector, a.timeout, tab)
            print("ok")
        elif a.cmd == "waittext":
            wait_text(a.needle, a.timeout, tab)
            print("ok")
        elif a.cmd == "shot":
            print(shot(a.path, tab))
    except (RuntimeError, TimeoutError) as e:
        print(f"失败：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
