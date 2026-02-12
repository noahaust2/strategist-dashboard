#!/usr/bin/env python3
"""Dashboard server for autonomous AI agent system.

Stdlib-only (no pip installs). Serves a live dashboard over HTTP with SSE
push updates whenever watched project files change on disk.

Endpoints:
    GET /            → serves dashboard/index.html
    GET /api/state   → full state snapshot
    GET /api/git     → commits, goal distribution, daily activity
    GET /api/daydream→ D3-compatible node-link graph
    GET /api/security→ security events, wrapper events, backup status
    GET /api/stream  → SSE event stream (persistent connection)
"""

import json
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from queue import Queue, Empty
from socketserver import ThreadingMixIn

# ── Constants ────────────────────────────────────────────────────────────────

PROJECT_DIR = "/home/claude-agent/project"
STATUS_DIR = "/home/claude-agent/status"
DASHBOARD_DIR = "/home/claude-agent/dashboard"
PORT = 3000

WATCHED_FILES = {
    os.path.join(STATUS_DIR, "agent.json"):      "agent_status",
    os.path.join(STATUS_DIR, "stagnation.json"):  "stagnation",
    os.path.join(STATUS_DIR, "heartbeat"):         "heartbeat",
    os.path.join(PROJECT_DIR, ".planning", "STATE.md"):         "state",
    os.path.join(PROJECT_DIR, ".planning", "OPPORTUNITIES.md"): "opportunities",
    os.path.join(PROJECT_DIR, ".planning", "METRICS.md"):       "metrics",
    os.path.join(PROJECT_DIR, ".planning", "LOG.md"):           "log",
    os.path.join(PROJECT_DIR, ".planning", "STRATEGY.md"):      "strategy",
}

GOAL_COLORS = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]

GIT_REFRESH_INTERVAL = 60
FILE_POLL_INTERVAL = 2
SSE_KEEPALIVE_INTERVAL = 15

# ── Shared state ─────────────────────────────────────────────────────────────

file_cache = {}        # path → {"mtime": float, "data": Any}
git_cache = {}         # populated by git poller
cache_lock = threading.Lock()


# ── SSE Broadcaster ─────────────────────────────────────────────────────────

class SSEBroadcaster:
    """Thread-safe broadcaster that fans out SSE events to connected clients."""

    def __init__(self):
        self._clients: list[Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> Queue:
        q: Queue = Queue()
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: Queue):
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass

    def broadcast(self, event_type: str, data: str):
        msg = f"event: {event_type}\ndata: {data}\n\n"
        with self._lock:
            for q in self._clients:
                q.put(msg)

    def keepalive(self):
        with self._lock:
            for q in self._clients:
                q.put(": keepalive\n\n")


broadcaster = SSEBroadcaster()


# ── Parsers ──────────────────────────────────────────────────────────────────

def _read_file(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def parse_json_file(path: str) -> dict | list | None:
    text = _read_file(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text.strip()}


def parse_heartbeat(path: str) -> dict:
    text = _read_file(path)
    if text is None:
        return {"alive": False}
    return {"alive": True, "last": text.strip()}


def parse_markdown_raw(path: str) -> dict:
    text = _read_file(path)
    if text is None:
        return {"content": ""}
    return {"content": text}


def parse_opportunities(path: str) -> dict:
    text = _read_file(path)
    if text is None:
        return {"sections": []}
    sections = []
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            if current:
                sections.append(current)
            title = line[3:].strip()
            current = {
                "title": title,
                "status": "open",
                "approved": False,
                "lines": [],
            }
        elif current is not None:
            lower = line.lower()
            if "status:" in lower:
                current["status"] = line.split(":", 1)[1].strip().lower()
            if "[approved]" in lower or "approved:" in lower:
                current["approved"] = True
            current["lines"].append(line)
    if current:
        sections.append(current)
    return {"sections": sections}


def parse_metrics(path: str) -> dict:
    text = _read_file(path)
    if text is None:
        return {"sections": {}}
    sections: dict[str, dict[str, str]] = {}
    current_section = "_default"
    for line in text.splitlines():
        if line.startswith("## "):
            current_section = line[3:].strip()
            sections.setdefault(current_section, {})
        elif ":" in line and not line.startswith("#"):
            key, _, val = line.partition(":")
            key, val = key.strip().strip("-* "), val.strip()
            if key:
                sections.setdefault(current_section, {})[key] = val
    return {"sections": sections}


def parse_security_log(path: str) -> list[dict]:
    text = _read_file(path)
    if text is None:
        return []
    entries = []
    ts_re = re.compile(r"^\[(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})\]\s*(.*)")
    for line in text.splitlines():
        m = ts_re.match(line)
        if m:
            entries.append({"timestamp": m.group(1), "message": m.group(2)})
    return entries


def parse_goals(path: str) -> list[dict]:
    text = _read_file(path)
    if text is None:
        return []
    goals = []
    for line in text.splitlines():
        if line.startswith("## "):
            name = line[3:].strip()
            color = GOAL_COLORS[len(goals) % len(GOAL_COLORS)]
            goals.append({"name": name, "color": color})
    return goals


# ── File read helpers per event type ─────────────────────────────────────────

def _read_watched_file(path: str, event_type: str):
    if event_type in ("agent_status", "stagnation"):
        return parse_json_file(path)
    if event_type == "heartbeat":
        return parse_heartbeat(path)
    if event_type == "opportunities":
        return parse_opportunities(path)
    if event_type == "metrics":
        return parse_metrics(path)
    return parse_markdown_raw(path)


# ── File watcher thread ─────────────────────────────────────────────────────

def file_watcher():
    """Poll watched files every FILE_POLL_INTERVAL seconds, broadcast on change."""
    mtimes: dict[str, float] = {}
    while True:
        for path, event_type in WATCHED_FILES.items():
            try:
                st = os.stat(path)
                mtime = st.st_mtime
            except OSError:
                continue
            if mtimes.get(path) != mtime:
                mtimes[path] = mtime
                data = _read_watched_file(path, event_type)
                with cache_lock:
                    file_cache[path] = {"mtime": mtime, "data": data}
                broadcaster.broadcast(event_type, json.dumps(data))
        time.sleep(FILE_POLL_INTERVAL)


# ── Git data extraction ─────────────────────────────────────────────────────

COMMIT_TYPE_RE = re.compile(r"^(\w+)(?:\(.+?\))?:\s*(.+)")

def refresh_git_data():
    """Run git log and build structured commit data."""
    goals = parse_goals(os.path.join(PROJECT_DIR, "GOALS.md"))
    goal_names = [g["name"].lower() for g in goals]

    try:
        result = subprocess.run(
            ["git", "log", "--pretty=format:%H|%aI|%s", "-200"],
            capture_output=True, text=True, timeout=15,
            cwd=PROJECT_DIR,
        )
        raw = result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        raw = ""

    commits = []
    goal_dist: dict[str, int] = defaultdict(int)
    daily: dict[str, int] = defaultdict(int)

    for line in raw.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, date_str, message = parts
        m = COMMIT_TYPE_RE.match(message)
        ctype = m.group(1) if m else "other"

        matched_goal = None
        msg_lower = message.lower()
        for gn in goal_names:
            if gn in msg_lower:
                matched_goal = gn
                break

        if matched_goal:
            goal_dist[matched_goal] += 1

        day = date_str[:10]
        daily[day] += 1

        commits.append({
            "hash": sha[:8],
            "date": date_str,
            "message": message,
            "type": ctype,
            "goal": matched_goal,
        })

    data = {
        "commits": commits,
        "goals": goals,
        "goal_distribution": dict(goal_dist),
        "daily_activity": dict(daily),
    }
    with cache_lock:
        git_cache.update(data)


def git_poller():
    """Refresh git data every GIT_REFRESH_INTERVAL seconds."""
    while True:
        refresh_git_data()
        time.sleep(GIT_REFRESH_INTERVAL)


# ── Daydream graph builder ───────────────────────────────────────────────────

def build_daydream_graph() -> dict:
    nodes: list[dict] = []
    links: list[dict] = []
    node_ids: set[str] = set()

    # Parse research/INDEX.md → research item nodes
    index_path = os.path.join(PROJECT_DIR, "research", "INDEX.md")
    text = _read_file(index_path) or ""
    # Expected row format: | ID | goal | date | title |
    for line in text.splitlines():
        cols = [c.strip() for c in line.split("|")]
        cols = [c for c in cols if c]
        if len(cols) >= 4 and not cols[0].startswith("-") and cols[0].lower() != "id":
            rid, goal, date, title = cols[0], cols[1], cols[2], cols[3]
            if rid and rid not in node_ids:
                nodes.append({
                    "id": rid, "type": "research",
                    "goal": goal, "date": date, "title": title,
                })
                node_ids.add(rid)

    # Parse research/PAIRS.md → links between research items
    pairs_path = os.path.join(PROJECT_DIR, "research", "PAIRS.md")
    text = _read_file(pairs_path) or ""
    for line in text.splitlines():
        cols = [c.strip() for c in line.split("|")]
        cols = [c for c in cols if c]
        if len(cols) >= 3 and cols[0].lower() != "source":
            source, target = cols[0], cols[1]
            result = cols[2].lower() if len(cols) > 2 else "unknown"
            if source in node_ids and target in node_ids:
                links.append({
                    "source": source, "target": target,
                    "result": result,
                })

    # Parse OPPORTUNITIES.md → [DAYDREAM]-tagged entries as special nodes
    opps_path = os.path.join(PROJECT_DIR, ".planning", "OPPORTUNITIES.md")
    text = _read_file(opps_path) or ""
    dd_re = re.compile(r"\[DAYDREAM\]\s*(.*)", re.IGNORECASE)
    dd_idx = 0
    for line in text.splitlines():
        m = dd_re.search(line)
        if m:
            did = f"daydream-{dd_idx}"
            nodes.append({
                "id": did, "type": "daydream",
                "title": m.group(1).strip(),
            })
            node_ids.add(did)
            dd_idx += 1

    return {"nodes": nodes, "links": links}


# ── SSE keepalive thread ────────────────────────────────────────────────────

def sse_keepalive():
    while True:
        time.sleep(SSE_KEEPALIVE_INTERVAL)
        broadcaster.keepalive()


# ── HTTP handler ─────────────────────────────────────────────────────────────

class DashboardHandler(BaseHTTPRequestHandler):
    """Read-only HTTP request handler."""

    def log_message(self, fmt, *args):
        # Quieter logging: single line
        pass

    def _send_json(self, data: dict | list, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, filepath: str, content_type: str):
        try:
            with open(filepath, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/":
            self._send_file(
                os.path.join(DASHBOARD_DIR, "index.html"), "text/html"
            )

        elif path == "/api/state":
            with cache_lock:
                snapshot = {
                    evt: file_cache.get(fp, {}).get("data")
                    for fp, evt in WATCHED_FILES.items()
                }
            self._send_json(snapshot)

        elif path == "/api/git":
            with cache_lock:
                self._send_json(dict(git_cache))

        elif path == "/api/daydream":
            self._send_json(build_daydream_graph())

        elif path == "/api/security":
            sec_path = os.path.join(PROJECT_DIR, ".planning", "LOG.md")
            wrapper_path = os.path.join(STATUS_DIR, "wrapper.log")
            backup_path = os.path.join(STATUS_DIR, "backup.json")
            self._send_json({
                "security": parse_security_log(sec_path),
                "wrapper": parse_security_log(wrapper_path),
                "backup": parse_json_file(backup_path),
            })

        elif path == "/api/stream":
            self._handle_sse()

        else:
            self.send_error(404)

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = broadcaster.subscribe()
        try:
            while True:
                try:
                    msg = q.get(timeout=30)
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                except Empty:
                    continue
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            broadcaster.unsubscribe(q)


# ── Server ───────────────────────────────────────────────────────────────────

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    # Start background threads
    for target in (file_watcher, git_poller, sse_keepalive):
        t = threading.Thread(target=target, daemon=True)
        t.start()

    server = ThreadedHTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"Dashboard server listening on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
