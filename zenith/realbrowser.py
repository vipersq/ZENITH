"""Fetch pages that sit behind Cloudflare (NUFORC) by briefly driving the user's own Edge/Chrome.

The browser starts with a separate ZENITH profile, off-screen, and is closed when the batch is done.
Only plain DevTools calls are used (no automation flags), so the bot check sees a normal browser.
If the check ever insists on a click, the window is moved on-screen for the user and hides again after.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import websocket  # websocket-client

from .core import user_dir

CHALLENGE_JS = ("(function(){var t=(document.title||'').toLowerCase();"
                "return t.indexOf('just a moment')>=0||t.indexOf('attention required')>=0||"
                "t.indexOf('chwileczk')>=0||"
                "!!document.querySelector('#challenge-form,#cf-challenge-running,.cf-turnstile,"
                "iframe[src*=\"challenges.cloudflare.com\"]');})()")


LOCK = threading.Lock()   # one browser instance at a time (they share a profile)


class BrowserMissing(RuntimeError):
    pass


def find_browser() -> str | None:
    env = os.environ.get("ZENITH_BROWSER")
    if env and Path(env).exists():
        return env
    cands = []
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                     os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                      Path(base) / "Google/Chrome/Application/chrome.exe",
                      Path(base) / "BraveSoftware/Brave-Browser/Application/brave.exe"]
    for c in cands:
        if c.exists():
            return str(c)
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RealBrowser:
    """Context manager: with RealBrowser() as b: b.goto(url); b.wait_for(js); b.eval(js)"""

    def __init__(self, on_needs_human=None, extra_args=None):
        self.exe = find_browser()
        if not self.exe:
            raise BrowserMissing("neither Edge nor Chrome was found")
        self.profile = user_dir() / "browser-profile"
        self.profile.mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        self.proc = None
        self.ws = None
        self._id = 0
        self.on_needs_human = on_needs_human
        self.extra_args = extra_args or []
        self.shown = False
        self.window_id = None

    # ------------------------------------------------------------------ lifecycle
    def __enter__(self):
        LOCK.acquire()
        try:
            return self._start()
        except BaseException:
            LOCK.release()
            raise

    def _start(self):
        args = [
            self.exe,
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile}",
            "--no-first-run", "--no-default-browser-check", "--disable-sync",
            "--disable-features=Translate,msEdgeSidebarV2,msUndersideButton",
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--window-position=-32000,-32000", "--window-size=900,700",
            *self.extra_args,
            "about:blank",
        ]
        flags = 0
        if sys.platform == "win32":
            flags = 0x08000000  # CREATE_NO_WINDOW (no console flash)
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=flags)
        ws_url = None
        deadline = time.time() + 25
        while time.time() < deadline and ws_url is None:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=2) as r:
                    for t in json.loads(r.read().decode()):
                        if t.get("type") == "page":
                            ws_url = t["webSocketDebuggerUrl"]
                            break
            except OSError:
                time.sleep(0.4)
        if not ws_url:
            self._kill()
            raise RuntimeError("browser did not start (DevTools port)")
        self.ws = websocket.create_connection(ws_url, timeout=60, suppress_origin=True)
        try:
            self.window_id = self.call("Browser.getWindowForTarget").get("windowId")
            self._bounds(left=-32000, top=-32000, state="normal")
        except RuntimeError:
            pass
        return self

    def __exit__(self, *exc):
        try:
            self.close()
        finally:
            LOCK.release()

    def _kill(self):
        if self.proc:
            self.proc.kill()
            self.proc = None

    def close(self):
        try:
            if self.ws:
                try:
                    self.call("Browser.close", timeout=3)
                except Exception:  # noqa: BLE001
                    pass
                self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        if self.proc:
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.ws = self.proc = None

    # ------------------------------------------------------------------ DevTools
    def call(self, method, params=None, timeout=60):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            self.ws.settimeout(max(0.5, end - time.time()))
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})
        raise TimeoutError(method)

    def eval(self, expr, await_promise=False, timeout=60):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                           "awaitPromise": await_promise}, timeout=timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            desc = (d.get("exception") or {}).get("description") or d.get("text") or "JS error"
            raise RuntimeError(desc.splitlines()[0][:200])
        return r.get("result", {}).get("value")

    def goto(self, url):
        self.call("Page.navigate", {"url": url})
        time.sleep(0.8)

    def _bounds(self, left, top, state="normal"):
        if self.window_id is None:
            return
        self.call("Browser.setWindowBounds", {"windowId": self.window_id,
                                              "bounds": {"windowState": "normal"}})
        b = {"left": left, "top": top, "width": 900, "height": 700}
        if state != "normal":
            b = {"windowState": state}
        self.call("Browser.setWindowBounds", {"windowId": self.window_id, "bounds": b})

    def wait_ready(self, ready_js, timeout=90, human_after=25):
        """Wait until the bot check has passed and ready_js is true."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                challenged = self.eval(CHALLENGE_JS)
                if not challenged and self.eval(f"!!({ready_js})"):
                    if self.shown:
                        self._bounds(-32000, -32000)
                        self.shown = False
                        if self.on_needs_human:
                            self.on_needs_human(False)
                    return True
                if challenged and not self.shown and time.time() - start > human_after:
                    self._bounds(120, 120)
                    self.call("Page.bringToFront")
                    self.shown = True
                    if self.on_needs_human:
                        self.on_needs_human(True)
            except (RuntimeError, TimeoutError, websocket.WebSocketException):
                pass  # page navigating, try again
            time.sleep(1.0)
        raise TimeoutError(f"page did not pass the bot check within {timeout}s")


# ---------------------------------------------------------------------------- NUFORC helpers

def _form_js(url: str, form: dict, nonce_id: str) -> str:
    from urllib.parse import quote
    body = "&".join(f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in form.items())
    return (
        "(async()=>{try{"
        f"const n=document.getElementById({json.dumps(nonce_id)});"
        "if(!n) return 'ERR:no nonce on the page (title: '+document.title+')';"
        f"const r=await fetch({json.dumps(url)},{{method:'POST',credentials:'include',headers:{{"
        "'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8',"
        "'X-Requested-With':'XMLHttpRequest','Accept':'application/json, text/javascript, */*; q=0.01'},"
        f"body:{json.dumps(body)}+'&wdtNonce='+encodeURIComponent(n.value)}});"
        "const t=await r.text();"
        "if(!r.ok) return 'ERR:HTTP '+r.status+' '+t.slice(0,120).replace(/\\s+/g,' ');"
        "return t;}catch(e){return 'ERR:'+(e&&e.message?e.message:String(e));}})()"
    )


# Plan B: ask the page's own DataTable to show every row, then read its data (same arrays as AJAX)
TABLE_READY_JS = ("(function(){var $=window.jQuery;if(!$||!$.fn||!$.fn.dataTable)return false;"
                  "var t=$('table.wpDataTable, table.dataTable').first();"
                  "return t.length>0 && $.fn.dataTable.isDataTable(t[0]);})()")

TABLE_ALL_JS = r"""
(async()=>{try{
  const $=window.jQuery; const el=$('table.wpDataTable, table.dataTable').first()[0];
  const dt=$(el).DataTable();
  const done=new Promise(res=>{ $(el).one('draw.dt', ()=>res(true)); setTimeout(()=>res(false), 90000); });
  dt.page.len(-1).draw();
  await done;
  const rows=dt.rows().data().toArray().map(r=>Array.isArray(r)?r:Object.values(r));
  return JSON.stringify({data: rows});
}catch(e){return 'ERR:'+(e&&e.message?e.message:String(e));}})()
"""


def _month_text(b, ym, index_tpl, ajax_tpl, form) -> str:
    b.goto(index_tpl.format(ym=ym))
    b.wait_ready("document.getElementById('wdtNonceFrontendServerSide_1') || document.querySelector('table.wpDataTable')")
    errs = []
    t = b.eval(_form_js(ajax_tpl.format(ym=ym), form, "wdtNonceFrontendServerSide_1"),
               await_promise=True, timeout=120)
    if isinstance(t, str) and not t.startswith("ERR:") and t.lstrip().startswith("{"):
        return t
    errs.append(f"AJAX: {str(t)[4:] if isinstance(t, str) else t}")
    # plan B: the table on the page itself
    end = time.time() + 40
    while time.time() < end and not b.eval(TABLE_READY_JS):
        time.sleep(1)
    t = b.eval(TABLE_ALL_JS, await_promise=True, timeout=120)
    if isinstance(t, str) and not t.startswith("ERR:"):
        return t
    errs.append(f"tabela: {str(t)[4:] if isinstance(t, str) else t}")
    raise RuntimeError(" | ".join(errs))


def nuforc_month_texts(months, index_tpl, ajax_tpl, form, on_needs_human=None) -> list[str]:
    texts, errors = [], []
    with RealBrowser(on_needs_human) as b:
        for ym in months:
            try:
                texts.append(_month_text(b, ym, index_tpl, ajax_tpl, form))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{ym}: {e}")
    if not texts:
        raise RuntimeError("; ".join(errors) or "no data")
    return texts


def page_text(url, on_needs_human=None) -> str:
    with RealBrowser(on_needs_human) as b:
        b.goto(url)
        b.wait_ready("document.readyState==='complete' && document.body")
        time.sleep(0.5)
        return b.eval("document.body.innerText") or ""
