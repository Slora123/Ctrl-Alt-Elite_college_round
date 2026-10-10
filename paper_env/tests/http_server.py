"""Local HTTP server for dataset tests: Range/If-Range, ETag, failure injection, bearer auth, HF-like API."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FileServer:
    def __init__(self):
        self.files: dict[str, bytes] = {}           # url path -> content
        self.support_range = True
        self.cut_after: dict[str, int] = {}          # path -> bytes to send before dropping connection (once)
        self.fail_status: dict[str, list[int]] = {}  # path -> queue of status codes to return first
        self.token: str | None = None                # require "Authorization: Bearer <token>"
        self.requests: list[dict] = []
        self.hf: dict[str, dict] = {}                # "org/name" -> {"sha":..., "files": {path: bytes}, "lfs": bool}
        self._lock = threading.Lock()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # quiet
                pass

            def _record(self):
                with outer._lock:
                    outer.requests.append({"method": self.command, "path": self.path.split("?")[0],
                                           "range": self.headers.get("Range"), "auth": self.headers.get("Authorization"),
                                           "if_range": self.headers.get("If-Range")})

            def _send(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _auth_ok(self):
                return outer.token is None or self.headers.get("Authorization") == f"Bearer {outer.token}"

            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                self._record()
                path = self.path.split("?")[0]
                if not self._auth_ok():
                    return self._send(401, b"unauthorized")
                m = re.match(r"^/api/datasets/(.+)/revision/(.+)$", path)
                if m:
                    repo = outer.hf.get(m.group(1))
                    if not repo:
                        return self._send(404)
                    sib = []
                    for n, c in repo["files"].items():
                        e = {"rfilename": n, "size": len(c)}
                        if repo.get("lfs"):
                            e["lfs"] = {"sha256": repo.get("lfs_override", {}).get(n, hashlib.sha256(c).hexdigest())}
                        sib.append(e)
                    return self._send(200, json.dumps({"sha": repo["sha"], "siblings": sib}).encode(),
                                      {"Content-Type": "application/json"})
                m = re.match(r"^/datasets/(.+?)/resolve/[^/]+/(.+)$", path)
                if m:
                    repo = outer.hf.get(m.group(1))
                    content = repo["files"].get(m.group(2)) if repo else None
                    return self._serve(path, content)
                self._serve(path, outer.files.get(path))

            def _serve(self, path, content):
                q = outer.fail_status.get(path)
                if q:
                    return self._send(q.pop(0), b"fail")
                if content is None:
                    return self._send(404, b"nope")
                etag = '"' + hashlib.md5(content).hexdigest() + '"'
                rng = self.headers.get("Range")
                ifr = self.headers.get("If-Range")
                start = 0
                if rng and outer.support_range and (ifr is None or ifr == etag):
                    mm = re.match(r"bytes=(\d+)-", rng)
                    start = int(mm.group(1)) if mm else 0
                    if start >= len(content):
                        return self._send(416, b"", {"Content-Range": f"bytes */{len(content)}"})
                body = content[start:]
                headers = {"ETag": etag, "Accept-Ranges": "bytes" if outer.support_range else "none"}
                code = 200
                if start:
                    code = 206
                    headers["Content-Range"] = f"bytes {start}-{len(content) - 1}/{len(content)}"
                cut = outer.cut_after.pop(path, None)
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command == "HEAD":
                    return
                if cut is not None:
                    self.wfile.write(body[:cut])
                    self.wfile.flush()
                    self.close_connection = True
                    self.connection.close()
                    return
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def gets(self, path: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == "GET" and r["path"] == path]
