#!/usr/bin/env python3
"""GreenData AI, hosted mode.

Runs the same dashboard and the same backend as `python greendata.py serve`, but on a public server,
so that Apply and Rollback really work on a live link.

Why this is safe to expose:
  * A visitor never types a server path. The server only works inside a private sandbox folder per visitor
    (<sandbox>/<visitor id>/demo or /upload). Folder names are checked against a strict pattern.
  * Uploads are small (see the limits below), file names are sanitised, and nothing leaves the sandbox.
  * Sandboxes are deleted automatically after GD_TTL_MINUTES (default 60) and at most MAX_SANDBOXES exist.
  * Every POST needs the page token and a same-origin request. Only 3 heavy jobs run at once.

Run:  python hosted_server.py            (uses $PORT, default 8000, listens on 0.0.0.0)
"""
import argparse
import base64
import http.server
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
from http import cookies
from urllib.parse import urlparse

import greendata as gd

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, "greendata_prototype.html")
UI_PATH = os.path.join(HERE, "hosted_ui.html")

SANDBOX = os.environ.get("GD_SANDBOX") or os.path.join(tempfile.gettempdir(), "greendata_sandbox")
TTL_SECONDS = int(os.environ.get("GD_TTL_MINUTES", "60")) * 60
MAX_SANDBOXES = int(os.environ.get("GD_MAX_SANDBOXES", "40"))
MAX_FILES = 60
MAX_FILE_BYTES = 3_000_000
MAX_TOTAL_BYTES = 5_000_000
MAX_UPLOAD_BODY = 8_000_000      # base64 of 5 MB is about 6.7 MB
MAX_JSON_BODY = 200_000
MAX_SESSIONS = 300
MAX_BUSY = 3

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
SID_RE = re.compile(r"^[0-9a-f]{32}$")
SAFE_PART = re.compile(r"[^\w .,()+@=-]", re.UNICODE)


class HostedApi(gd.Api):
    """The normal Api, but every folder is resolved inside the visitor's own sandbox."""

    def __init__(self):
        super().__init__()
        self.ctx = threading.local()
        self.busy = threading.BoundedSemaphore(MAX_BUSY)

    # -- helpers -------------------------------------------------------------------------
    def _sid(self):
        sid = getattr(self.ctx, "sid", None)
        if not sid or not SID_RE.match(sid):
            raise ValueError("Session missing. Reload the page.")
        return sid

    def _base(self):
        base = os.path.join(SANDBOX, self._sid())
        os.makedirs(base, exist_ok=True)
        return base

    def _dir(self, d):  # overrides the base class: names only, never paths
        name = str(d or "").strip()
        if not NAME_RE.match(name):
            raise ValueError("Create the demo folder or upload files first.")
        path = os.path.join(self._base(), name)
        if not os.path.isdir(path):
            raise ValueError("That folder is gone from the server (sandboxes are temporary). Create it again.")
        return path

    def _session(self, b):
        s = super()._session(b)
        if s.get("sid") != self._sid():
            raise ValueError("Scan session expired. Please scan again.")
        return s

    @staticmethod
    def _clean_rel(raw):
        parts = []
        for p in str(raw or "").replace("\\", "/").split("/"):
            p = p.strip()
            if p in ("", "."):
                continue
            if p == "..":
                raise ValueError("Bad file path.")
            p = SAFE_PART.sub("_", p)[:100].strip()
            if not p or p.lower() == ".greendata":
                raise ValueError("Bad file name.")
            parts.append(p)
        if not parts or len(parts) > 6:
            raise ValueError("Bad file path.")
        return os.path.join(*parts)

    def _run_limited(self, fn, b):
        if not self.busy.acquire(blocking=False):
            raise ValueError("The server is busy with other visitors. Try again in a few seconds.")
        try:
            return fn(b)
        finally:
            self.busy.release()

    # -- actions -------------------------------------------------------------------------
    def demo(self, b):
        path = os.path.join(self._base(), "demo")
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        gd.make_demo(path, 1)
        return dict(dir="demo")

    def upload(self, b):
        files = b.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("No files received.")
        if len(files) > MAX_FILES:
            raise ValueError(f"Too many files. The demo server accepts up to {MAX_FILES}.")
        staged, total = {}, 0
        for f in files:
            if not isinstance(f, dict):
                raise ValueError("Bad upload.")
            rel = self._clean_rel(f.get("path") or f.get("name"))
            data = base64.b64decode(str(f.get("b64", "")), validate=True)
            if len(data) > MAX_FILE_BYTES:
                raise ValueError(f"{rel} is larger than {MAX_FILE_BYTES // 1_000_000} MB.")
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise ValueError(f"Upload is larger than {MAX_TOTAL_BYTES // 1_000_000} MB in total.")
            try:
                mtime = min(float(f.get("mtime") or 0) / 1000.0, time.time())
            except (TypeError, ValueError):
                mtime = 0
            staged[rel] = (data, mtime)
        base = self._base()
        path = os.path.join(base, "upload")
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        for rel, (data, mtime) in staged.items():
            dst = os.path.join(path, rel)
            if os.path.commonpath([os.path.abspath(dst), os.path.abspath(path)]) != os.path.abspath(path):
                raise ValueError("Bad file path.")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as fh:
                fh.write(data)
            if mtime > 0:
                os.utime(dst, (mtime, mtime))
        return dict(dir="upload", count=len(staged))

    def scan(self, b):
        name = str(b.get("dir") or "").strip()
        res = self._run_limited(super().scan, b)
        res["dir"] = name  # show the folder name, not the server path
        with self.lock:
            self.sessions[res["session"]]["sid"] = self._sid()
            while len(self.sessions) > MAX_SESSIONS:
                self.sessions.pop(next(iter(self.sessions)))
        return res

    def apply(self, b):
        b = dict(b, measure=False)
        return self._run_limited(super().apply, b)


def sweep_sandboxes():
    """Delete sandboxes that were idle longer than the TTL, and the oldest ones beyond MAX_SANDBOXES."""
    try:
        entries = []
        for n in os.listdir(SANDBOX):
            p = os.path.join(SANDBOX, n)
            if SID_RE.match(n) and os.path.isdir(p):
                entries.append((os.path.getmtime(p), p))
    except OSError:
        return
    now = time.time()
    entries.sort()
    keep = []
    for mt, p in entries:
        if now - mt > TTL_SECONDS:
            shutil.rmtree(p, ignore_errors=True)
        else:
            keep.append(p)
    for p in keep[: max(0, len(keep) - MAX_SANDBOXES)]:
        shutil.rmtree(p, ignore_errors=True)


def sweeper_loop():
    while True:
        time.sleep(300)
        sweep_sandboxes()


def make_hosted_server(host="0.0.0.0", port=8000):
    token, api = secrets.token_urlsafe(24), HostedApi()
    os.makedirs(SANDBOX, exist_ok=True)

    with open(UI_PATH, encoding="utf-8") as fh:
        ui_snippet = fh.read()

    class H(http.server.BaseHTTPRequestHandler):
        server_version = "GreenData"

        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or []):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _cookie_sid(self):
            try:
                c = cookies.SimpleCookie(self.headers.get("Cookie", ""))
            except cookies.CookieError:
                return None
            m = c.get("gd_sid")
            return m.value if m and SID_RE.match(m.value) else None

        def _same_origin(self):
            origin = self.headers.get("Origin")
            if not origin:
                return True
            return urlparse(origin).netloc == self.headers.get("Host", "")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(200, "ok", "text/plain")
            if path in ("/", "/index.html"):
                sid = self._cookie_sid() or secrets.token_hex(16)
                os.makedirs(os.path.join(SANDBOX, sid), exist_ok=True)
                os.utime(os.path.join(SANDBOX, sid))
                with open(HTML_PATH, encoding="utf-8") as fh:
                    page = fh.read()
                page = page.replace('window.GD_TOKEN="__GD_TOKEN__";', f'window.GD_TOKEN="{token}";window.GD_HOSTED=true;')
                page = page.replace("</body>", ui_snippet + "</body>", 1)
                flags = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                ck = f"gd_sid={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age={TTL_SECONDS}{flags}"
                return self._send(200, page, "text/html", [("Set-Cookie", ck)])
            if path == "/api/ping":
                if self.headers.get("X-GD-Token") != token:
                    return self._send(403, json.dumps(dict(error="Forbidden")))
                return self._send(200, json.dumps(dict(ok=True, hosted=True, version=gd.VERSION, codec=gd.CODEC_EXT, codecarbon=False)))
            self._send(404, json.dumps(dict(error="Not found")))

        def do_POST(self):
            route = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
            limit = MAX_UPLOAD_BODY if route == "upload" else MAX_JSON_BODY
            try:
                n = int(self.headers.get("Content-Length", 0))
            except ValueError:
                n = -1
            if n < 0 or n > limit:
                self.close_connection = True
                return self._send(413, json.dumps(dict(error="Request too large.")))
            raw = self.rfile.read(n) if n else b""
            if self.headers.get("X-GD-Token") != token or not self._same_origin():
                return self._send(403, json.dumps(dict(error="Forbidden")))
            sid = self._cookie_sid()
            if not sid:
                return self._send(400, json.dumps(dict(error="Session missing. Reload the page.")))
            if not self.path.startswith("/api/") or route not in ("scan", "replan", "apply", "rollback", "cpu", "demo", "upload"):
                return self._send(404, json.dumps(dict(error="Not found")))
            try:
                os.makedirs(os.path.join(SANDBOX, sid), exist_ok=True)
                os.utime(os.path.join(SANDBOX, sid))
                api.ctx.sid = sid
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Bad request.")
                return self._send(200, json.dumps(getattr(api, route)(body)))
            except (ValueError, KeyError, TypeError, OSError) as e:
                return self._send(400, json.dumps(dict(error=str(e))))
            finally:
                api.ctx.sid = None

    httpd = http.server.ThreadingHTTPServer((host, port), H)
    httpd.daemon_threads = True
    return httpd, token


def main(argv=None):
    ap = argparse.ArgumentParser(description="GreenData AI hosted mode (public demo server with sandboxed real Apply).")
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    a = ap.parse_args(argv)
    for p in (HTML_PATH, UI_PATH):
        if not os.path.exists(p):
            sys.exit(f"{os.path.basename(p)} must be in the same folder as hosted_server.py")
    sweep_sandboxes()
    threading.Thread(target=sweeper_loop, daemon=True).start()
    httpd, _ = make_hosted_server(a.host, a.port)
    print(f"GreenData AI (hosted mode) on http://{a.host}:{httpd.server_address[1]}/  sandbox: {SANDBOX}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
