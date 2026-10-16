"""Local dashboard: read-only status server (stdlib only).

`openmainty serve --port 8901` exposes run state, drift, and logs for a
human to watch. It never mutates state, never spends quota, and never
touches target checkouts. For login-persistent operation see
`openmainty app install` (launchd, macOS).
"""
from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import deps, doctor, paths, repos, store

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpenMainty</title>
<style>
:root { color-scheme: light dark; }
body { font-family: -apple-system, "Segoe UI", system-ui, sans-serif;
  margin: 0 auto; max-width: 1080px; padding: 24px 20px 60px; line-height: 1.45; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { opacity: .65; font-size: 13px; margin-bottom: 20px; }
h2 { font-size: 15px; margin: 26px 0 8px; border-bottom: 1px solid #8884; padding-bottom: 4px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #8883; vertical-align: top; }
th { font-weight: 600; opacity: .7; }
.pill { display: inline-block; min-width: 64px; text-align: center; font-size: 11px;
  padding: 2px 8px; margin: 1px 2px; border: 1px solid #8886; border-radius: 20px; }
.done { background: #1a7f3744; } .failed { background: #d1242f33; }
.blocked { background: #9a670033; } .pending { opacity: .55; }
.running { background: #0969da33; }
pre { font-size: 12px; overflow: auto; border: 1px solid #8884; padding: 10px; border-radius: 8px; max-height: 320px; }
a { color: inherit; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
.card { border: 1px solid #8884; padding: 12px 14px; border-radius: 10px; }
.card h3 { margin: 0 0 6px; font-size: 14px; }
.muted { opacity: .6; font-size: 12px; }
#meta { font-size: 12px; opacity: .6; }
</style></head>
<body>
<h1>OpenMainty</h1>
<div class="sub">Your AI maintainer for open source. Read-only view. <span id="meta"></span></div>
<h2>Repositories</h2><div class="grid" id="repos"></div>
<h2>Recent runs</h2><div id="runs"></div>
<h2>Run detail</h2><div id="detail" class="muted">Select a run above.</div>
<h2>Daily watch log</h2><pre id="watchlog">(loading)</pre>
<script>
const pill = s => `<span class="pill ${s}">${s}</span>`;
async function j(p) { const r = await fetch(p); return r.json(); }
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
async function load() {
  const o = await j("/api/overview");
  document.getElementById("meta").textContent =
    `home ${o.home} · ${o.pc} · updated ${o.at}`;
  document.getElementById("repos").innerHTML = o.repos.map(r =>
    `<div class="card"><h3>${esc(r.name)}</h3><div class="muted">${esc(r.github)} · ${esc(r.checkout || "not linked")}</div><div>${esc(r.drift)}</div></div>`).join("") || "(none)";
  document.getElementById("runs").innerHTML =
    `<table><tr><th>run</th><th>issue</th><th>steps</th><th>wait</th></tr>` +
    o.runs.map(s => `<tr><td><a href="#" data-run="${s.id}">${s.id}</a></td>` +
      `<td>${esc(s.repo)}#${esc(s.issue)} ${esc(s.title || "")}</td>` +
      `<td>${Object.entries(s.steps).map(([k, v]) => pill(v.status)).join("")}</td>` +
      `<td>${esc(s.waitReason || "")}</td></tr>`).join("") + `</table>`;
  document.querySelectorAll("[data-run]").forEach(a => a.onclick = e => { e.preventDefault(); detail(a.dataset.run); });
  document.getElementById("watchlog").textContent = o.watch_log_tail || "(empty)";
}
async function detail(id) {
  const s = await j("/api/run?id=" + encodeURIComponent(id));
  document.getElementById("detail").innerHTML =
    `<p><b>${esc(s.id)}</b> ${esc(s.repo)}#${esc(s.issue)} — ${esc(s.title || "")}<br>` +
    `<span class="muted">base ${esc((s.baseCommit || "").slice(0, 12))} · work ${esc((s.workCommit || "").slice(0, 12))} · artifacts ${(Object.keys(s.artifacts || {}).join(", ") || "none")}</span></p>` +
    `<table><tr><th>step</th><th>status</th><th>exit</th><th>note</th></tr>` +
    Object.entries(s.steps).map(([k, v]) =>
      `<tr><td>${k}</td><td>${pill(v.status)}</td><td>${v.exit_code ?? ""}</td><td>${esc(v.note || "")}</td></tr>`).join("") + `</table>`;
  window.scrollTo(0, document.getElementById("detail").offsetTop - 10);
}
load(); setInterval(load, 30000);
</script></body></html>
"""


def overview() -> dict:
    from .util import pc_id, utcnow
    from . import pcconfig

    pc = pcconfig.load()
    repo_rows = []
    for name in repos.list_all():
        profile = repos.load(name)
        checkout = (pc.get("checkouts", {}) or {}).get(name)
        drift = "no dependencies declared"
        if profile.get("dependencies"):
            if checkout and Path(checkout).is_dir():
                try:
                    rows = deps.check(profile, Path(checkout))
                    bad = [r for r in rows if r["drift"]]
                    err = [r for r in rows if r["error"]]
                    unpub = [r for r in rows if r.get("unpublished")]
                    parts = []
                    if bad:
                        parts.append(f"DRIFT: {', '.join(r['name'] for r in bad)}")
                    if unpub:
                        parts.append(f"unpublished: {', '.join(r['name'] for r in unpub)}")
                    if err:
                        parts.append(f"errors: {len(err)}")
                    drift = "; ".join(parts) if parts else f"ok ({len(rows)} tracked)"
                except Exception as exc:  # noqa: BLE001 - dashboard must not crash
                    drift = f"check failed: {exc}"
            else:
                drift = "checkout not linked"
        repo_rows.append({"name": name, "github": profile.get("github"),
                          "checkout": checkout, "drift": drift})
    log = paths.home() / "watch.log"
    tail = ""
    try:
        tail = log.read_text(encoding="utf-8")[-4000:]
    except OSError:
        pass
    return {"home": str(paths.home()), "pc": pc_id(), "at": utcnow(),
            "repos": repo_rows, "runs": store.list_runs(20), "watch_log_tail": tail}


def safe_run_file(run_id: str, name: str) -> Path | None:
    if not run_id or not name or "/" in name or name.startswith("."):
        return None
    dest = (store.run_dir(run_id) / name).resolve()
    try:
        dest.relative_to(store.run_dir(run_id).resolve())
    except ValueError:
        return None
    return dest if dest.is_file() else None


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenMaintyDashboard/0.1"

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send(PAGE.encode(), "text/html; charset=utf-8")
            elif parsed.path == "/api/overview":
                self._send(json.dumps(overview()).encode(), "application/json")
            elif parsed.path == "/api/run":
                run_id = parse_qs(parsed.query).get("id", [""])[0]
                self._send(json.dumps(store.load(run_id)).encode(), "application/json")
            elif parsed.path == "/api/file":
                q = parse_qs(parsed.query)
                dest = safe_run_file(q.get("run", [""])[0], q.get("name", [""])[0])
                if dest is None:
                    self.send_error(404, "no such file")
                    return
                self._send(dest.read_bytes(), "text/plain; charset=utf-8")
            elif parsed.path == "/api/doctor":
                self._send(json.dumps(doctor.quick()).encode(), "application/json")
            else:
                self.send_error(404, "unknown path")
        except Exception as exc:  # noqa: BLE001 - never drop the connection raw
            self.send_error(500, f"dashboard error: {html.escape(str(exc))[:200]}")

    def log_message(self, *args):  # keep cron logs quiet
        pass


def serve(port: int) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"openmainty dashboard on http://127.0.0.1:{httpd.server_port}/ (Ctrl-C to stop)")
    httpd.serve_forever()
