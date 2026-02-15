#!/usr/bin/env python3
"""Dashboard server for autonomous AI agent system.

Stdlib-only (no pip installs). Serves a live dashboard over HTTP with SSE
push updates whenever watched project files change on disk.

Endpoints:
    GET /            → serves dashboard/index.html
    GET /api/state   → full state snapshot
    GET /api/git     → commits, goal distribution, daily activity
    GET /api/summary → plain-English system summary (cycle, goals, log, metrics,
                       predictions, token tracking, health scorecard)
    GET /api/glossary→ definitions for key system terms
    GET /api/daydream→ D3-compatible node-link graph
    GET /api/security→ security events, wrapper events, backup status
    GET /api/architecture → goals, assets, document metadata, synapse history
    GET /api/predictions → prediction data from PREDICTIONS.md and domain files
    GET /api/tokens  → token tracking data from TOKEN-TRACKING.md
    GET /api/health  → health scorecard (goal health, metrics, system grade)
    GET /api/stream  → SSE event stream (persistent connection)
"""

import glob
import json
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from queue import Queue, Empty
from socketserver import ThreadingMixIn
from typing import Dict, List

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
    os.path.join(PROJECT_DIR, ".planning", "GOALS.md"):         "goals_full",
    os.path.join(PROJECT_DIR, ".planning", "ASSETS.md"):        "assets_full",
    os.path.join(PROJECT_DIR, ".planning", "PREDICTIONS.md"):  "predictions",
    os.path.join(PROJECT_DIR, ".planning", "TOKEN-TRACKING.md"): "token_tracking",
}

# Additional files to watch for document-change events (architecture view)
DOC_WATCH_FILES = {
    os.path.join(PROJECT_DIR, ".claude", "rules", f): f.replace(".md", "")
    for f in [
        "reality-model-architecture.md", "goal-advancement.md",
        "task-lifecycle.md", "review-gate.md", "approval-rules.md",
        "security.md", "usage-tracking.md", "self-correction.md",
        "model-strategy.md",
    ]
}
DOC_WATCH_FILES[os.path.join(PROJECT_DIR, "CLAUDE.md")] = "CLAUDE"
DOC_WATCH_FILES[os.path.join(PROJECT_DIR, ".planning", "FAILURE-CATALOG.md")] = "FAILURE-CATALOG"

# Commit prefixes that indicate self-modification / learning events
SYNAPSE_PATTERNS = re.compile(
    r"^(rules|review|identity|skill|strategy|research|opps|metrics|self-mod):",
    re.IGNORECASE,
)

# Map commit prefix to synapse type for visualization
SYNAPSE_TYPE_MAP = {
    "rules": "rules", "review": "review", "identity": "identity",
    "skill": "skill", "strategy": "strategy", "research": "research",
    "opps": "research", "metrics": "metrics", "self-mod": "rules",
}

GOAL_COLORS = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
]

# Model context window sizes (tokens)
MODEL_CONTEXT_LIMITS = {
    "claude-opus-4-6": 200000,
    "claude-sonnet-4-5": 200000,
    "claude-haiku-4-5": 200000,
}
MODEL_CONTEXT_DEFAULT = 200000

GIT_REFRESH_INTERVAL = 60
FILE_POLL_INTERVAL = 2
SSE_KEEPALIVE_INTERVAL = 15

# Claude Code session scanning — scan all users that might run sessions
CLAUDE_PROJECTS_DIRS = [
    "/home/claude-agent/.claude/projects",
    "/root/.claude/projects",
]
SESSION_ACTIVE_THRESHOLD = 900  # seconds (15 min) — sessions idle between turns are still active
SESSION_POLL_INTERVAL = 3

# ── Shared state ─────────────────────────────────────────────────────────────

file_cache = {}        # path → {"mtime": float, "data": Any}
git_cache = {}         # populated by git poller
cache_lock = threading.Lock()


# ── SSE Broadcaster ─────────────────────────────────────────────────────────

class SSEBroadcaster:
    """Thread-safe broadcaster that fans out SSE events to connected clients."""

    def __init__(self):
        self._clients: List[Queue] = []
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
    except OSError as e:
        print(f"Warning: Could not read {path}: {e}")
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
    sections: Dict[str, Dict[str, str]] = {}
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


def build_summary() -> dict:
    """Build a plain-English system summary from STATE.md, LOG.md, and METRICS.md."""
    planning_dir = os.path.join(PROJECT_DIR, ".planning")

    # --- STATE.md ---
    state_text = _read_file(os.path.join(planning_dir, "STATE.md")) or ""
    cycle_number = None
    active_goals = []
    current_status = "Unknown"
    for line in state_text.splitlines():
        lower = line.lower()
        # Cycle number: look for "Cycle: 42" or "cycle 42" patterns
        if cycle_number is None:
            m = re.search(r"cycle[:\s]+(\d+)", lower)
            if m:
                cycle_number = int(m.group(1))
        # Current status: look for "Status:" line
        if "status:" in lower and not line.strip().startswith("#"):
            current_status = line.split(":", 1)[1].strip()
        # Active goals from lines like "- **Goal Name** (active)"
        # or "## Goal Name" sections
        if line.startswith("## ") and "goal" in lower:
            active_goals.append(line[3:].strip())

    # Also try to pick up goals from GOALS.md
    goals_text = _read_file(os.path.join(planning_dir, "GOALS.md")) or ""
    if not active_goals:
        for line in goals_text.splitlines():
            if line.startswith("## "):
                active_goals.append(line[3:].strip())

    # --- LOG.md (latest entry) ---
    log_text = _read_file(os.path.join(planning_dir, "LOG.md")) or ""
    accomplishments = []
    blockers = []
    next_steps = []
    # Split into dated entries (## YYYY-MM-DD or ## Cycle N)
    log_sections = re.split(r"(?=^## )", log_text, flags=re.MULTILINE)
    latest_entry = ""
    for section in reversed(log_sections):
        if section.strip():
            latest_entry = section.strip()
            break

    # Parse accomplishments, blockers, next steps from latest entry
    current_list = None
    for line in latest_entry.splitlines():
        lower = line.lower().strip()
        if any(kw in lower for kw in ["accomplish", "completed", "done", "achieved", "delivered"]):
            current_list = accomplishments
        elif any(kw in lower for kw in ["blocker", "blocked", "issue", "problem", "stuck"]):
            current_list = blockers
        elif any(kw in lower for kw in ["next", "plan", "todo", "upcoming"]):
            current_list = next_steps
        elif line.strip().startswith("- ") and current_list is not None:
            current_list.append(line.strip().lstrip("- ").strip())

    # --- METRICS.md ---
    metrics_data = parse_metrics(os.path.join(planning_dir, "METRICS.md"))
    sections = metrics_data.get("sections", {})
    prediction_count = 0
    accuracy_rate = "N/A"
    total_cycles = cycle_number or 0
    for sec_name, sec_vals in sections.items():
        for key, val in sec_vals.items():
            key_lower = key.lower()
            if "prediction" in key_lower and "count" in key_lower:
                try:
                    prediction_count = int(re.sub(r"[^\d]", "", val))
                except ValueError:
                    pass
            elif "accuracy" in key_lower or "calibration" in key_lower:
                accuracy_rate = val
            elif "cycle" in key_lower and "count" in key_lower:
                try:
                    total_cycles = int(re.sub(r"[^\d]", "", val))
                except ValueError:
                    pass

    # Enrich with prediction data from the prediction pipeline
    pred_data = parse_predictions()
    pred_stats = pred_data.get("stats", {})
    if pred_stats.get("total", 0) > 0:
        prediction_count = pred_stats["total"]
    if pred_stats.get("accuracy", "N/A") != "N/A":
        accuracy_rate = pred_stats["accuracy"]

    # Build prediction list for frontend (combined upcoming + resolved)
    predictions_for_frontend = []
    for p in pred_data.get("resolved", []):
        predictions_for_frontend.append({
            "text": p.get("text", ""),
            "outcome": p.get("outcome", "unknown"),
            "date": p.get("date", ""),
            "confidence": p.get("confidence", "medium"),
            "confidenceValue": p.get("confidenceValue", 50),
            "id": p.get("id", ""),
            "domain": p.get("domain", ""),
        })
    for p in pred_data.get("upcoming", [])[:30]:
        predictions_for_frontend.append({
            "text": p.get("text", ""),
            "outcome": "pending",
            "date": p.get("date", ""),
            "confidence": p.get("confidence", "medium"),
            "confidenceValue": p.get("confidenceValue", 50),
            "id": p.get("id", ""),
            "domain": p.get("domain", ""),
        })

    # Token tracking data for frontend chart
    token_data = parse_token_tracking()

    # Health scorecard data
    health_data = parse_health_scorecard()

    return {
        "currentCycle": cycle_number,
        "activeGoals": active_goals,
        "currentStatus": current_status,
        "accomplishments": accomplishments[:10],
        "blockers": blockers[:5],
        "nextSteps": next_steps[:5],
        "metrics": {
            "predictionCount": prediction_count,
            "accuracyRate": accuracy_rate,
            "cycleCount": total_cycles,
        },
        "predictions": predictions_for_frontend,
        "predictionStats": pred_stats,
        "predictionCalibration": pred_data.get("calibration", []),
        "tokenTracking": token_data.get("daily", []),
        "tokenAlerts": token_data.get("alerts", []),
        "tokenGoalDistribution": token_data.get("goalDistribution", {}),
        "healthScorecard": health_data,
    }


# ── Prediction Data Pipeline ─────────────────────────────────────────────────

def parse_predictions() -> dict:
    """Parse PREDICTIONS.md index and all domain files in predictions/ directory.

    Returns structured prediction data:
    - stats: total, resolved, accuracy, domains
    - calibration: per-band breakdown
    - upcoming: list of pending predictions sorted by check date
    - resolved: list of resolved predictions with outcomes
    """
    planning_dir = os.path.join(PROJECT_DIR, ".planning")
    index_path = os.path.join(planning_dir, "PREDICTIONS.md")
    predictions_dir = os.path.join(planning_dir, "predictions")

    # --- Parse the index file for system stats ---
    index_text = _read_file(index_path) or ""

    stats = {
        "total": 0,
        "active": 0,
        "resolved": 0,
        "correct": 0,
        "incorrect": 0,
        "accuracy": "N/A",
        "domains": 0,
    }

    # Extract system stats from the table
    for line in index_text.splitlines():
        lower = line.lower()
        if "total predictions" in lower:
            m = re.search(r"(\d+)\s*\(", line)
            if m:
                stats["total"] = int(m.group(1))
            # Extract active count
            m_active = re.search(r"(\d+)\s*active", line)
            if m_active:
                stats["active"] = int(m_active.group(1))
            # Extract resolved count
            m_resolved = re.search(r"(\d+)\s*resolved", line)
            if m_resolved:
                stats["resolved"] = int(m_resolved.group(1))
        elif line.startswith("| Resolved") or ("resolved" in lower and "correct" in lower and "incorrect" not in lower):
            # Parse "| Resolved | 9 (8 CORRECT, 1 INCORRECT) |"
            m_correct = re.search(r"(\d+)\s*CORRECT", line)
            m_incorrect = re.search(r"(\d+)\s*INCORRECT", line)
            if m_correct:
                stats["correct"] = int(m_correct.group(1))
            if m_incorrect:
                stats["incorrect"] = int(m_incorrect.group(1))
        elif "domains" in lower and "|" in line and not line.strip().startswith("#"):
            m_dom = re.search(r"(\d+)\s*\(", line)
            if m_dom:
                stats["domains"] = int(m_dom.group(1))

    # Compute accuracy
    if stats["resolved"] > 0:
        stats["accuracy"] = f"{round(stats['correct'] / stats['resolved'] * 100)}%"

    # Extract calibration bands from index
    calibration = []
    in_calibration = False
    for line in index_text.splitlines():
        if "calibration stats" in line.lower():
            in_calibration = True
            continue
        if in_calibration and line.startswith("| ") and "%" in line:
            cols = [c.strip() for c in line.split("|")]
            cols = [c for c in cols if c]
            if len(cols) >= 4 and not cols[0].startswith("-") and "band" not in cols[0].lower():
                calibration.append({
                    "band": cols[0],
                    "resolved": cols[1] if len(cols) > 1 else "0",
                    "correct": cols[2] if len(cols) > 2 else "0",
                    "calibration": cols[3] if len(cols) > 3 else "N/A",
                })
        elif in_calibration and line.startswith("## ") and "calibration" not in line.lower():
            in_calibration = False

    # --- Parse domain files for individual predictions ---
    upcoming = []
    resolved_list = []

    domain_files = []
    if os.path.isdir(predictions_dir):
        for fname in sorted(os.listdir(predictions_dir)):
            if fname.endswith(".md"):
                domain_files.append(os.path.join(predictions_dir, fname))

    for fpath in domain_files:
        domain_name = os.path.splitext(os.path.basename(fpath))[0]
        text = _read_file(fpath)
        if text is None:
            continue

        # Parse tables: | ID | Conf | Check/Result | ... |
        in_resolved_section = False
        in_active_section = False

        for line in text.splitlines():
            lower = line.lower().strip()

            # Section detection
            if lower.startswith("## resolved"):
                in_resolved_section = True
                in_active_section = False
                continue
            elif lower.startswith("## active"):
                in_active_section = True
                in_resolved_section = False
                continue
            elif lower.startswith("## ") and ("data update" in lower or "note" in lower):
                in_resolved_section = False
                in_active_section = False
                continue

            if not line.strip().startswith("| "):
                continue

            cols = [c.strip() for c in line.split("|")]
            cols = [c for c in cols if c]

            # Skip header/separator rows
            if len(cols) < 3:
                continue
            if cols[0].lower() in ("id", "---", "----") or cols[0].startswith("-"):
                continue
            if all(c.startswith("-") for c in cols):
                continue

            pred_id = cols[0].strip()
            if not pred_id or not pred_id[0].isalpha():
                continue

            # Parse confidence
            conf_str = cols[1].strip().rstrip("%")
            try:
                confidence = int(conf_str)
            except ValueError:
                confidence = 50

            # Determine if resolved or active based on section or content
            check_col = cols[2].strip() if len(cols) > 2 else ""
            description = cols[3].strip() if len(cols) > 3 else ""
            falsification = cols[4].strip() if len(cols) > 4 else ""

            is_resolved = False
            outcome = "pending"

            if in_resolved_section:
                is_resolved = True
                # In resolved sections: | ID | Conf | Result | Summary |
                result_lower = check_col.lower()
                if "correct" in result_lower:
                    outcome = "correct"
                elif "incorrect" in result_lower:
                    outcome = "incorrect"
                else:
                    outcome = result_lower or "unknown"
            elif "RESOLVED" in check_col.upper() or "RESOLVED" in line.upper():
                is_resolved = True
                if "CORRECT" in line.upper() and "INCORRECT" not in line.upper():
                    outcome = "correct"
                elif "INCORRECT" in line.upper():
                    outcome = "incorrect"
            else:
                # Active prediction
                is_resolved = False
                outcome = "pending"

            # Confidence classification for frontend
            if confidence >= 75:
                conf_class = "high"
            elif confidence >= 45:
                conf_class = "medium"
            else:
                conf_class = "low"

            entry = {
                "id": pred_id,
                "text": description or check_col,
                "confidence": conf_class,
                "confidenceValue": confidence,
                "domain": domain_name,
                "outcome": outcome,
                "date": check_col if not is_resolved else "",
            }

            if is_resolved:
                resolved_list.append(entry)
            else:
                upcoming.append(entry)

    # Sort upcoming by check date (earliest first)
    def _sort_key(p):
        date_str = p.get("date", "")
        # Extract date from formats like "03-20" or "2026-03-20"
        m = re.search(r"(\d{2})-(\d{2})", date_str)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            return (month, day)
        return (99, 99)

    upcoming.sort(key=_sort_key)

    # Sort resolved by ID
    resolved_list.sort(key=lambda p: p.get("id", ""))

    return {
        "stats": stats,
        "calibration": calibration,
        "upcoming": upcoming[:50],  # Cap at 50 for API response size
        "resolved": resolved_list,
    }


# ── Token Tracking Data Pipeline ────────────────────────────────────────────

def parse_token_tracking() -> dict:
    """Parse TOKEN-TRACKING.md for per-cycle token data and weekly summaries.

    Returns structured token data for the frontend token chart.
    """
    tracking_path = os.path.join(PROJECT_DIR, ".planning", "TOKEN-TRACKING.md")
    text = _read_file(tracking_path)
    if text is None:
        return {"cycles": [], "weekly": [], "daily": [], "alerts": []}

    cycles = []
    weekly = []
    alerts = []
    goal_distribution = {}

    # Parse the backfilled cycle ledger table
    # Format: | Cycle | Date | Type | TM | Total | Primary Goal (%) | Secondary (%) | OH+META (%) |
    in_cycle_table = False
    in_weekly_table = False
    in_goal_dist = False
    in_alerts = False

    for line in text.splitlines():
        lower = line.lower().strip()

        # Section detection
        if "cycle ledger" in lower and lower.startswith("#"):
            in_cycle_table = True
            in_weekly_table = False
            in_goal_dist = False
            in_alerts = False
            continue
        elif "weekly summar" in lower and lower.startswith("#"):
            in_weekly_table = True
            in_cycle_table = False
            in_goal_dist = False
            continue
        elif "backfill goal distribution" in lower or ("goal distribution" in lower and lower.startswith("#")):
            in_goal_dist = True
            in_cycle_table = False
            continue
        elif "alert" in lower and "backfill" in lower:
            in_alerts = True
            in_goal_dist = False
            continue
        elif lower.startswith("---"):
            in_cycle_table = False
            in_weekly_table = False
            in_goal_dist = False
            in_alerts = False
            continue
        elif lower.startswith("## ") or lower.startswith("### "):
            # New section header — check what it is
            if "backfill summary" in lower or "backfill goal" in lower:
                in_cycle_table = False
                in_goal_dist = "goal distribution" in lower
            elif "live tracking" in lower:
                in_cycle_table = True  # Resume for live data
            continue

        if not line.strip().startswith("| "):
            # Check for alert lines
            if in_alerts and line.strip().startswith("- "):
                alert_text = line.strip().lstrip("- ").strip()
                if alert_text:
                    alerts.append(alert_text)
            continue

        cols = [c.strip() for c in line.split("|")]
        cols = [c for c in cols if c]

        if len(cols) < 2:
            continue
        if cols[0].lower() in ("cycle", "week", "goal", "metric", "---") or cols[0].startswith("-"):
            continue
        if all(c.startswith("-") for c in cols):
            continue

        if in_cycle_table:
            # Parse: | Cycle | Date | Type | TM | Total | Primary (%) | Secondary (%) | OH+META (%) |
            cycle_id = cols[0].strip()
            if not cycle_id or cycle_id.lower() in ("cycle", "---"):
                continue
            # Skip summary rows (e.g., "18 entries (16 numbered)", "~126K")
            if "entries" in cycle_id.lower() or "total" in cycle_id.lower():
                continue
            # Cycle ID should start with a digit or be like "202a", "195b"
            if not cycle_id[0].isdigit():
                continue

            date_str = cols[1].strip() if len(cols) > 1 else ""
            cycle_type = cols[2].strip() if len(cols) > 2 else ""
            teammates = cols[3].strip() if len(cols) > 3 else "0"
            total_str = cols[4].strip() if len(cols) > 4 else "0"

            # Parse total tokens (e.g., "200K", "60K")
            total_tokens = _parse_token_value(total_str)

            # Parse teammates count
            try:
                tm_count = int(teammates)
            except ValueError:
                tm_count = 0

            # Parse goal allocations from remaining columns
            primary = cols[5].strip() if len(cols) > 5 else ""
            secondary = cols[6].strip() if len(cols) > 6 else ""
            overhead = cols[7].strip() if len(cols) > 7 else ""

            cycles.append({
                "cycle": cycle_id,
                "date": date_str,
                "type": cycle_type,
                "teammates": tm_count,
                "total": total_tokens,
                "primary": primary,
                "secondary": secondary,
                "overhead": overhead,
            })

        elif in_weekly_table:
            # Parse: | Goal | Tokens | % | Target % | Delta | Alert |
            goal_name = cols[0].strip()
            tokens_str = cols[1].strip() if len(cols) > 1 else "0"
            pct = cols[2].strip() if len(cols) > 2 else "0%"
            target = cols[3].strip() if len(cols) > 3 else ""
            delta = cols[4].strip() if len(cols) > 4 else ""
            alert = cols[5].strip() if len(cols) > 5 else ""

            weekly.append({
                "goal": goal_name,
                "tokens": _parse_token_value(tokens_str),
                "pct": pct,
                "target": target,
                "delta": delta,
                "alert": alert,
            })

        elif in_goal_dist:
            # Parse: | Goal | Est. Tokens | % of Total |
            goal_name = cols[0].strip()
            tokens_str = cols[1].strip() if len(cols) > 1 else "0"
            pct = cols[2].strip() if len(cols) > 2 else "0%"
            goal_distribution[goal_name] = {
                "tokens": _parse_token_value(tokens_str),
                "pct": pct,
            }

    # Build daily aggregation from cycles (group by date)
    daily: Dict[str, int] = defaultdict(int)
    for c in cycles:
        date_key = c["date"]
        if date_key:
            daily[date_key] += c["total"]

    # Convert to sorted list for frontend chart
    daily_list = []
    for date_key in sorted(daily.keys()):
        daily_list.append({
            "date": _normalize_date(date_key),
            "total": daily[date_key],
            "input": int(daily[date_key] * 0.8),  # estimate 80/20 split
            "output": int(daily[date_key] * 0.2),
        })

    return {
        "cycles": cycles,
        "weekly": weekly,
        "daily": daily_list,
        "goalDistribution": goal_distribution,
        "alerts": alerts,
    }


def _parse_token_value(s: str) -> int:
    """Parse token count strings like '200K', '1,537K', '~1,130K', '2,260K'."""
    s = s.strip().lstrip("~").replace(",", "").replace("*", "")
    if not s or s == "0":
        return 0
    m = re.match(r"(\d+(?:\.\d+)?)\s*([KkMm])?", s)
    if m:
        num = float(m.group(1))
        unit = (m.group(2) or "").upper()
        if unit == "K":
            return int(num * 1000)
        elif unit == "M":
            return int(num * 1000000)
        return int(num)
    return 0


def _normalize_date(date_str: str) -> str:
    """Normalize date strings like 'Feb 13' to '2026-02-13' format."""
    month_map = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }
    # Already in ISO format
    if re.match(r"\d{4}-\d{2}-\d{2}", date_str):
        return date_str
    # "Feb 13" format
    m = re.match(r"(\w{3})\s+(\d{1,2})", date_str)
    if m:
        month = month_map.get(m.group(1).lower(), "01")
        day = m.group(2).zfill(2)
        return f"2026-{month}-{day}"
    return date_str


# ── Health Scorecard Data Pipeline ──────────────────────────────────────────

def parse_health_scorecard() -> dict:
    """Build structured health scorecard data from STATE.md and METRICS.md.

    Returns goal health, system metrics, and improvement indicators
    that the frontend can use instead of demo-mode algorithms.
    """
    planning_dir = os.path.join(PROJECT_DIR, ".planning")
    state_text = _read_file(os.path.join(planning_dir, "STATE.md")) or ""
    metrics_text = _read_file(os.path.join(planning_dir, "METRICS.md")) or ""

    # --- Parse goal health from STATE.md ---
    # Only parse goals within the "Reality Model Status" section
    goal_health = []
    current_goal = None
    current_status = "active"
    current_confidence = "N/A"
    current_mode = ""
    in_reality_model = False
    seen_goals = set()  # Track seen goal names to avoid duplicates

    for line in state_text.splitlines():
        # Detect Reality Model Status section
        if line.startswith("## ") and "reality model" in line.lower():
            in_reality_model = True
            continue
        # Stop at next ## section that isn't a subsection of reality model
        if line.startswith("## ") and in_reality_model and "reality model" not in line.lower():
            # Save last goal before exiting section
            if current_goal and current_goal not in seen_goals:
                goal_health.append(_build_goal_health(
                    current_goal, current_status, current_confidence, current_mode
                ))
                seen_goals.add(current_goal)
            in_reality_model = False
            current_goal = None
            continue

        if not in_reality_model:
            continue

        # Goal sections: ### Goal Name: STATUS
        if line.startswith("### ") and "goal" in line.lower():
            # Save previous goal
            if current_goal and current_goal not in seen_goals:
                goal_health.append(_build_goal_health(
                    current_goal, current_status, current_confidence, current_mode
                ))
                seen_goals.add(current_goal)

            header = line[4:].strip()
            # Parse "Product Goal: PAUSED (with capability watch)"
            # or "Self-Improvement Goal: ACTIVE — Understanding Mode"
            parts = header.split(":", 1)
            current_goal = parts[0].strip()
            rest = parts[1].strip() if len(parts) > 1 else ""

            current_status = "active"
            current_confidence = "N/A"
            current_mode = ""

            rest_lower = rest.lower()
            if "paused" in rest_lower:
                current_status = "paused"
            elif "active" in rest_lower:
                current_status = "active"

            # Extract mode if present
            if "understanding mode" in rest_lower:
                current_mode = "understanding"
            elif "action mode" in rest_lower:
                current_mode = "action"
            elif "low-intensity" in rest_lower:
                current_mode = "background"

        elif current_goal:
            lower = line.lower().strip()
            # Direction confidence
            if "direction confidence" in lower:
                m = re.search(r"(\d+)%", line)
                if m:
                    current_confidence = f"{m.group(1)}%"
                elif "n/a" in lower:
                    current_confidence = "N/A"

    # Don't forget the last goal if we're still in the reality model section
    if current_goal and current_goal not in seen_goals:
        goal_health.append(_build_goal_health(
            current_goal, current_status, current_confidence, current_mode
        ))

    # --- Parse metrics from METRICS.md ---
    metrics_summary = {}

    # Prediction calibration
    pred_section = False
    for line in metrics_text.splitlines():
        lower = line.lower().strip()
        if "prediction calibration" in lower:
            pred_section = True
            continue
        if pred_section and lower.startswith("## "):
            pred_section = False
            continue

        if pred_section:
            if "total predictions" in lower:
                m = re.search(r"(\d+)", line)
                if m:
                    metrics_summary["predictionTotal"] = int(m.group(1))
            elif "overall accuracy" in lower:
                m = re.search(r"(\d+)%", line)
                if m:
                    metrics_summary["predictionAccuracy"] = f"{m.group(1)}%"
            elif "premature" in lower:
                metrics_summary["predictionReliable"] = False
            elif "resolved" in lower and "correct" in lower:
                m_correct = re.search(r"(\d+)\s*CORRECT", line, re.IGNORECASE)
                m_incorrect = re.search(r"(\d+)\s*INCORRECT", line, re.IGNORECASE)
                if m_correct:
                    metrics_summary["predictionCorrect"] = int(m_correct.group(1))
                if m_incorrect:
                    metrics_summary["predictionIncorrect"] = int(m_incorrect.group(1))

    # Fix validation rate — look for the specific "X/Y validated" pattern
    for line in metrics_text.splitlines():
        lower = line.lower().strip()
        if "fix validation rate" in lower and "validated" in lower:
            # Match "0/17 validated" specifically (not confidence ratings like "1/5")
            m = re.search(r"(\d+)/(\d+)\s*validated", line)
            if m:
                metrics_summary["fixValidated"] = int(m.group(1))
                metrics_summary["fixTotal"] = int(m.group(2))
                break  # Use first specific match
        elif "fixes installed" in lower:
            m = re.search(r"installed[^:]*:\s*(\d+)", line, re.IGNORECASE)
            m_held = re.search(r"validated[^:]*:\s*(\d+)", line, re.IGNORECASE)
            if m:
                metrics_summary["fixTotal"] = int(m.group(1))
            if m_held:
                metrics_summary["fixValidated"] = int(m_held.group(1))
        elif "decision accuracy" in lower or "decision quality" in lower:
            m = re.search(r"(\d+)/(\d+)", line)
            if m:
                metrics_summary["decisionCorrect"] = int(m.group(1))
                metrics_summary["decisionTotal"] = int(m.group(2))
        elif "self-caught" in lower and "owner-caught" in lower and "ratio" not in lower:
            # Both on same line: "**Self-caught**: ~8 | **Owner-caught**: ~14"
            # Avoid the "Detection ratio" line which has percentages
            m_self = re.search(r"self-caught[^|]*?~?(\d+)", line, re.IGNORECASE)
            m_owner = re.search(r"owner-caught[^|]*?~?(\d+)", line, re.IGNORECASE)
            if m_self:
                metrics_summary["selfCaught"] = int(m_self.group(1))
            if m_owner:
                metrics_summary["ownerCaught"] = int(m_owner.group(1))
        elif "recurrence rate" in lower:
            m = re.search(r"~?(\d+)%", line)
            if m:
                metrics_summary["failureRecurrence"] = f"{m.group(1)}%"

    # Parse cycle count, skill count from System Performance section
    for line in metrics_text.splitlines():
        lower = line.lower().strip()
        if "cycles completed" in lower:
            # Table format: | Metric | Baseline | Current | Target |
            # We want the "Current" column (index 3 in split-by-pipe)
            cols = [c.strip() for c in line.split("|")]
            cols = [c for c in cols if c]
            if len(cols) >= 3:
                # "Current" is the 3rd column (index 2)
                m = re.search(r"(\d+)", cols[2])
                if m:
                    metrics_summary["cycleCount"] = int(m.group(1))
        elif "failure catalog" in lower:
            m = re.search(r"(\d+)\s*failure", line)
            if m:
                metrics_summary["failureModes"] = int(m.group(1))

    # --- Parse conviction/goal status table from METRICS.md ---
    conviction_data = []
    in_conviction = False
    for line in metrics_text.splitlines():
        if "conviction status" in line.lower():
            in_conviction = True
            continue
        if in_conviction and line.startswith("## "):
            in_conviction = False
            continue
        if in_conviction and line.startswith("| ") and "|" in line:
            cols = [c.strip() for c in line.split("|")]
            cols = [c for c in cols if c]
            if len(cols) >= 3 and cols[0].lower() not in ("goal", "---"):
                if not cols[0].startswith("-"):
                    conviction_data.append({
                        "goal": cols[0],
                        "phase": cols[1] if len(cols) > 1 else "",
                        "conviction": cols[2] if len(cols) > 2 else "",
                        "notes": cols[3] if len(cols) > 3 else "",
                    })

    # --- Compute overall system grade ---
    grade, grade_desc = _compute_system_grade(goal_health, metrics_summary)

    # --- Compute improvement indicators ---
    improving, speed, growth = _compute_improvement(metrics_summary)

    # --- Determine owner attention needed ---
    attention, attention_text, attention_urgency = _compute_attention(
        goal_health, metrics_summary, grade
    )

    return {
        "grade": grade,
        "gradeDesc": grade_desc,
        "goalHealth": goal_health,
        "conviction": conviction_data,
        "metrics": metrics_summary,
        "improving": improving,
        "speed": speed,
        "growth": growth,
        "attention": attention,
        "attentionText": attention_text,
        "attentionUrgency": attention_urgency,
    }


def _build_goal_health(name: str, status: str, confidence: str, mode: str) -> dict:
    """Build a single goal health entry."""
    # Determine health based on status and confidence
    health = "ok"
    trend = "flat"

    if status == "paused":
        health = "paused"
        trend = "flat"
    elif confidence != "N/A":
        try:
            conf_val = int(confidence.rstrip("%"))
            if conf_val >= 70:
                health = "ok"
                trend = "up"
            elif conf_val >= 40:
                health = "ok"
                trend = "flat"
            elif conf_val >= 20:
                health = "warn"
                trend = "flat"
            else:
                health = "warn"
                trend = "down"
        except ValueError:
            pass

    return {
        "name": name,
        "status": status,
        "health": health,
        "trend": trend,
        "confidence": confidence,
        "mode": mode,
    }


def _compute_system_grade(goal_health: list, metrics: dict) -> tuple:
    """Compute overall system grade (A-F) from goal health and metrics."""
    paused = [g for g in goal_health if g["status"] == "paused"]
    active = [g for g in goal_health if g["status"] == "active"]
    bad_goals = [g for g in active if g["health"] == "bad"]
    warn_goals = [g for g in active if g["health"] == "warn"]

    # Prediction accuracy factor
    accuracy_str = metrics.get("predictionAccuracy", "N/A")
    accuracy = None
    if accuracy_str != "N/A":
        try:
            accuracy = int(accuracy_str.rstrip("%"))
        except ValueError:
            pass

    # Fix validation factor
    fix_validated = metrics.get("fixValidated", 0)
    fix_total = metrics.get("fixTotal", 0)

    if len(bad_goals) >= 2:
        return ("F", "Critical issues detected. Multiple goals are failing.")
    elif len(bad_goals) >= 1:
        return ("D", "Significant issues. At least one goal needs attention.")
    elif len(warn_goals) >= 2 or (accuracy is not None and accuracy < 50):
        return ("C", "Some concerns. Worth checking on these areas.")
    elif len(warn_goals) <= 1 and len(active) > 0:
        if accuracy is None or accuracy >= 70:
            if fix_total > 0 and fix_validated / fix_total < 0.1:
                return ("B", "Healthy overall. Fix validation rate needs improvement.")
            return ("A", "System running well. Goals progressing with good direction.")
        else:
            return ("B", "Mostly good. Prediction accuracy needs more data.")
    else:
        return ("B", "Mostly healthy. Monitor for changes.")


def _compute_improvement(metrics: dict) -> tuple:
    """Determine improvement trajectory from metrics."""
    cycle_count = metrics.get("cycleCount", 0)
    prediction_total = metrics.get("predictionTotal", 0)

    # If system has high cycle count and growing predictions, it's improving
    if cycle_count > 150 and prediction_total > 100:
        return ("yes", "accelerating", "active")
    elif cycle_count > 100:
        return ("yes", "steady", "active")
    elif cycle_count > 50:
        return ("maybe", "building", "growing")
    else:
        return ("maybe", "early", "starting")


def _compute_attention(goal_health: list, metrics: dict, grade: str) -> tuple:
    """Determine if owner attention is needed."""
    if grade in ("F", "D"):
        bad_count = len([g for g in goal_health if g["health"] == "bad"])
        text = f"{bad_count} goal(s) need attention. System health degraded."
        urgency = "NOW" if grade == "F" else "THIS WEEK"
        return ("yes", text, urgency)
    elif grade == "C":
        return ("optional", "Some areas could use a look. Not urgent.", "WHENEVER CONVENIENT")
    else:
        # Check for pending owner actions from metrics
        pending = []
        fix_total = metrics.get("fixTotal", 0)
        fix_validated = metrics.get("fixValidated", 0)
        if fix_total > 10 and fix_validated == 0:
            pending.append("No fixes validated yet")

        if pending:
            return ("optional", " | ".join(pending), "WHENEVER CONVENIENT")
        return ("no", "System running well on its own. No action needed.", "")


GLOSSARY_TERMS = [
    {
        "name": "Cycle",
        "short": "One complete loop of the agent's work process.",
        "long": "A cycle is the fundamental unit of agent activity. Each cycle follows "
                "the pattern READ, DECIDE, EXECUTE, VERIFY and then repeats. The agent "
                "reads its current state, picks what to work on, does the work, and checks "
                "the results before starting the next cycle.",
    },
    {
        "name": "Phase (READ / DECIDE / EXEC / VERIFY)",
        "short": "The four steps the agent goes through in every work cycle.",
        "long": "READ: the agent reviews its goals, state, and recent history to "
                "understand the current situation. DECIDE: it picks which goal and task "
                "to work on next. EXECUTE: it carries out the chosen task. VERIFY: it "
                "checks whether the task succeeded and records what happened.",
    },
    {
        "name": "Reality Model",
        "short": "The agent's internal understanding of how the world works.",
        "long": "The reality model is a structured framework the agent uses to track "
                "what it knows, how confident it is in that knowledge, and where gaps "
                "exist. It helps the agent make better decisions by grounding actions "
                "in evidence rather than assumptions.",
    },
    {
        "name": "Direction Confidence",
        "short": "How sure the agent is that it is working on the right thing.",
        "long": "Direction confidence measures whether the agent believes its current "
                "strategy is correct. High confidence means the agent has evidence that "
                "its approach is working. Low confidence triggers more research and "
                "exploration before committing to action.",
    },
    {
        "name": "Conviction",
        "short": "The strength of the agent's belief in a particular decision.",
        "long": "Conviction reflects how strongly the agent stands behind a choice it "
                "has made. High conviction decisions are acted on quickly; low conviction "
                "decisions may be deferred or investigated further. Conviction is built "
                "through evidence gathering and testing.",
    },
    {
        "name": "Prediction",
        "short": "A testable forecast the agent makes about what will happen.",
        "long": "The agent makes explicit predictions about outcomes before taking "
                "action. These predictions are later checked against reality to improve "
                "the agent's decision-making accuracy over time. Tracking predictions "
                "is how the agent learns from its mistakes.",
    },
    {
        "name": "Calibration",
        "short": "How accurate the agent's predictions are compared to what actually happened.",
        "long": "Calibration measures the gap between what the agent expected and what "
                "actually occurred. Well-calibrated agents make reliable forecasts. Poor "
                "calibration triggers the agent to adjust its models and become more "
                "careful in its estimates.",
    },
    {
        "name": "Teammate",
        "short": "A sub-agent spawned to handle a specific task in parallel.",
        "long": "Teammates are temporary worker agents that the main Strategist agent "
                "creates to handle specific tasks. They run alongside the main agent, "
                "each focused on a single job, and report back when done. This allows "
                "multiple tasks to progress simultaneously.",
    },
    {
        "name": "HIT",
        "short": "A successful connection between two different research areas.",
        "long": "HIT stands for a cross-domain discovery where research in one area "
                "unexpectedly helps another. During ideation, the agent looks for these "
                "connections between goals. A high HIT rate means the agent is finding "
                "valuable synergies across its work.",
    },
    {
        "name": "SEED",
        "short": "A small exploratory investigation into an unknown area.",
        "long": "SEEDs are lightweight research tasks where the agent explores a topic "
                "it does not yet understand well. They are low-commitment investigations "
                "designed to build knowledge before making bigger decisions. Seeds may "
                "grow into full tasks if they reveal something valuable.",
    },
    {
        "name": "Ideation",
        "short": "The creative process where the agent brainstorms new ideas and connections.",
        "long": "During ideation, the agent deliberately explores new possibilities by "
                "combining what it knows across different goals. It looks for unexpected "
                "connections (HITs) and generates opportunities. Ideation is scheduled "
                "regularly to prevent the agent from getting stuck in routine work.",
    },
    {
        "name": "Failure Mode",
        "short": "A known pattern of mistakes the agent has cataloged.",
        "long": "Failure modes are documented patterns where the agent tends to make "
                "errors. By cataloging these, the agent can recognize when it is about "
                "to repeat a mistake and take corrective action. Each failure mode "
                "includes the root cause and a specific fix.",
    },
    {
        "name": "Self-Correction",
        "short": "The agent's ability to recognize and fix its own mistakes.",
        "long": "Self-correction is the process by which the agent detects errors in "
                "its reasoning or output, documents what went wrong, and adjusts its "
                "behavior to avoid the same mistake. It is tracked in the failure "
                "catalog and feeds back into improved decision-making.",
    },
    {
        "name": "Emergent Capability",
        "short": "A new skill or behavior the agent developed on its own.",
        "long": "Emergent capabilities are abilities the agent was not explicitly "
                "programmed to have but developed through its work. They arise from "
                "combining existing skills in novel ways. These are flagged for owner "
                "review since they represent the agent evolving beyond its original design.",
    },
    {
        "name": "Wake-up Trigger",
        "short": "An external event that starts a new agent work cycle.",
        "long": "Wake-up triggers are signals that prompt the agent to begin a new "
                "cycle. These include scheduled timers, incoming Telegram messages from "
                "the owner, or system events. The wrapper script manages these triggers "
                "and launches the agent when one fires.",
    },
    {
        "name": "Rate Budget",
        "short": "The limit on how many API calls the agent can make per time period.",
        "long": "The rate budget controls how quickly the agent consumes its allocated "
                "API resources. The agent monitors its usage and slows down or switches "
                "to cheaper models when approaching the limit. This prevents runaway "
                "costs and ensures the agent can run continuously.",
    },
    {
        "name": "Understanding Mode",
        "short": "When the agent focuses on learning and research instead of taking action.",
        "long": "In understanding mode, the agent prioritizes building knowledge over "
                "producing output. It reads, researches, and asks questions rather than "
                "making changes. This mode is used when the agent has low confidence "
                "in a new domain and needs to learn before acting.",
    },
    {
        "name": "Action Mode",
        "short": "When the agent focuses on executing tasks and producing results.",
        "long": "In action mode, the agent prioritizes getting work done. It writes "
                "code, creates content, makes commits, and advances its goals. This "
                "mode is used when the agent has high confidence and clear direction. "
                "It is the counterpart to understanding mode.",
    },
]


def get_glossary() -> List[dict]:
    """Return the glossary terms sorted alphabetically."""
    return sorted(GLOSSARY_TERMS, key=lambda t: t["name"].lower())


def parse_security_log(path: str) -> List[dict]:
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


def parse_goals(path: str) -> List[dict]:
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


def parse_goals_full(path: str) -> List[dict]:
    """Parse GOALS.md returning goal names with full text blocks."""
    text = _read_file(path)
    if text is None:
        return []
    goals = []
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            if current:
                current["text"] = current["text"].strip()
                goals.append(current)
            name = line[3:].strip()
            color = GOAL_COLORS[len(goals) % len(GOAL_COLORS)]
            current = {"name": name, "color": color, "text": ""}
        elif current is not None:
            current["text"] += line + "\n"
    if current:
        current["text"] = current["text"].strip()
        goals.append(current)
    return goals


def parse_assets(path: str) -> Dict[str, List[str]]:
    """Parse ASSETS.md returning categorized asset items."""
    text = _read_file(path)
    if text is None:
        return {}
    categories: Dict[str, List[str]] = {}
    current_cat = "_general"
    for line in text.splitlines():
        if line.startswith("## "):
            current_cat = line[3:].strip()
            categories.setdefault(current_cat, [])
        elif line.strip().startswith("- ") or line.strip().startswith("* "):
            item = line.strip().lstrip("-* ").strip()
            if item:
                categories.setdefault(current_cat, []).append(item)
    # Remove empty _general if it has no items
    if "_general" in categories and not categories["_general"]:
        del categories["_general"]
    return categories


def get_document_metadata() -> Dict[str, dict]:
    """Stat all planning files and return metadata."""
    planning_dir = os.path.join(PROJECT_DIR, ".planning")
    files_to_check = [
        "GOALS.md", "ASSETS.md", "STATE.md",
        "OPPORTUNITIES.md", "METRICS.md", "LOG.md",
    ]
    docs = {}
    for fname in files_to_check:
        fpath = os.path.join(planning_dir, fname)
        try:
            st = os.stat(fpath)
            docs[fname] = {
                "exists": True,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                "size": st.st_size,
            }
        except OSError:
            docs[fname] = {"exists": False, "mtime": None, "size": 0}
    return docs


def get_full_document_map() -> dict:
    """Build full document map with content, metadata, relationships, and tokens."""
    planning_dir = os.path.join(PROJECT_DIR, ".planning")
    rules_dir = os.path.join(PROJECT_DIR, ".claude", "rules")

    # Document definitions: (id, path, category, description)
    doc_defs = [
        # Rules
        ("reality-model-architecture", os.path.join(rules_dir, "reality-model-architecture.md"),
         "rules", "Research & understanding framework"),
        ("goal-advancement", os.path.join(rules_dir, "goal-advancement.md"),
         "rules", "Goal execution & cycle management"),
        ("task-lifecycle", os.path.join(rules_dir, "task-lifecycle.md"),
         "rules", "How work gets done"),
        ("review-gate", os.path.join(rules_dir, "review-gate.md"),
         "rules", "Owner interaction standards"),
        ("approval-rules", os.path.join(rules_dir, "approval-rules.md"),
         "rules", "What needs human approval"),
        ("security", os.path.join(rules_dir, "security.md"),
         "rules", "Security policies"),
        ("usage-tracking", os.path.join(rules_dir, "usage-tracking.md"),
         "rules", "Session/weekly usage reviews"),
        ("self-correction", os.path.join(rules_dir, "self-correction.md"),
         "rules", "Learn from mistakes protocol"),
        ("model-strategy", os.path.join(rules_dir, "model-strategy.md"),
         "rules", "Model & rate budget strategy"),
        # Planning / state
        ("GOALS", os.path.join(planning_dir, "GOALS.md"),
         "input", "Top-level goals"),
        ("ASSETS", os.path.join(planning_dir, "ASSETS.md"),
         "input", "Available resources"),
        ("STATE", os.path.join(planning_dir, "STATE.md"),
         "state", "System state & progress"),
        ("METRICS", os.path.join(planning_dir, "METRICS.md"),
         "state", "Performance metrics"),
        ("LOG", os.path.join(planning_dir, "LOG.md"),
         "state", "Daily activity log"),
        ("OPPORTUNITIES", os.path.join(planning_dir, "OPPORTUNITIES.md"),
         "state", "Discovered opportunities"),
        ("FAILURE-CATALOG", os.path.join(planning_dir, "FAILURE-CATALOG.md"),
         "state", "Mistake root causes & fixes"),
        # System hub
        ("CLAUDE", os.path.join(PROJECT_DIR, "CLAUDE.md"),
         "hub", "System identity & instructions"),
    ]

    # Relationship definitions: (source_id, target_id, relation_type, label)
    relationships = [
        # CLAUDE.md hub connections
        ("CLAUDE", "GOALS", "reads", "reads at cycle start"),
        ("CLAUDE", "ASSETS", "reads", "reads at cycle start"),
        ("CLAUDE", "reality-model-architecture", "defines", "core framework"),
        ("CLAUDE", "goal-advancement", "defines", "goal rules"),
        ("CLAUDE", "task-lifecycle", "defines", "work rules"),
        ("CLAUDE", "STATE", "reads", "current state"),
        # Reality model architecture connections
        ("reality-model-architecture", "STATE", "writes", "confidence levels"),
        ("reality-model-architecture", "goal-advancement", "extends", "research methods"),
        ("reality-model-architecture", "self-correction", "integrates", "learning feedback"),
        # Goal advancement connections
        ("goal-advancement", "GOALS", "reads", "cycle start"),
        ("goal-advancement", "ASSETS", "reads", "cycle start"),
        ("goal-advancement", "STATE", "writes", "rate tracking"),
        ("goal-advancement", "LOG", "writes", "usage tier"),
        ("goal-advancement", "OPPORTUNITIES", "writes", "ideation HITs"),
        ("goal-advancement", "METRICS", "writes", "ideation stats"),
        ("goal-advancement", "usage-tracking", "delegates", "session-end"),
        # Task lifecycle connections
        ("task-lifecycle", "STATE", "writes", "rest window"),
        # Review gate connections
        ("review-gate", "approval-rules", "enforces", "approval boundary"),
        ("review-gate", "reality-model-architecture", "applies", "emergence criteria"),
        # Self-correction connections
        ("self-correction", "FAILURE-CATALOG", "writes", "error entries"),
        ("self-correction", "METRICS", "writes", "decision quality"),
        ("self-correction", "reality-model-architecture", "informs", "refinements"),
        # Usage tracking connections
        ("usage-tracking", "STATE", "writes", "rate tracking"),
        ("usage-tracking", "LOG", "writes", "usage reviews"),
        ("usage-tracking", "OPPORTUNITIES", "reads", "seed investigations"),
        # Security connections
        ("security", "STATE", "writes", "installed versions"),
        ("security", "approval-rules", "enforces", "install blocking"),
        # Model strategy connections
        ("model-strategy", "STATE", "writes", "token usage"),
    ]

    docs = []
    for doc_id, path, category, description in doc_defs:
        text = _read_file(path)
        if text is None:
            continue
        word_count = len(text.split())
        token_estimate = int(word_count * 1.3)
        try:
            st = os.stat(path)
            mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
            size = st.st_size
        except OSError:
            mtime = None
            size = 0
        docs.append({
            "id": doc_id,
            "name": os.path.basename(path),
            "path": path.replace(PROJECT_DIR + "/", ""),
            "category": category,
            "description": description,
            "content": text[:50000],  # Cap content to prevent huge responses
            "words": word_count,
            "tokens": token_estimate,
            "size": size,
            "mtime": mtime,
        })

    return {"documents": docs, "relationships": relationships}


def get_synapse_history() -> List[dict]:
    """Extract self-modification commits from git log (learning events)."""
    try:
        result = subprocess.run(
            ["git", "log", "--pretty=format:%H|%aI|%s", "-500"],
            capture_output=True, text=True, timeout=15,
            cwd=PROJECT_DIR,
        )
        raw = result.stdout.strip()
    except subprocess.TimeoutExpired:
        print("Warning: git log timed out after 15s in get_synapse_history")
        return []
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"Warning: git command failed in get_synapse_history: {e}")
        return []

    synapses = []
    for line in raw.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, date_str, message = parts
        m = SYNAPSE_PATTERNS.match(message)
        if m:
            prefix = m.group(1).lower()
            syn_type = SYNAPSE_TYPE_MAP.get(prefix, "other")
            synapses.append({
                "hash": sha[:8],
                "date": date_str,
                "message": message,
                "type": syn_type,
            })
    return synapses


def get_review_status() -> dict:
    """Read the review counter file to determine review cycle status."""
    counter_path = os.path.join(STATUS_DIR, "review-counter")
    try:
        with open(counter_path, "r") as f:
            count = int(f.read().strip())
    except (OSError, ValueError):
        count = 0
    return {
        "sessionsSinceReview": count,
        "interval": 5,
    }


# ── Claude Code session scanning ────────────────────────────────────────────

def _decode_project_path(encoded_name):
    """Decode '-home-claude-agent-project' to a readable project name.

    Claude Code encodes paths by replacing / with -.
    /home/claude-agent/project -> -home-claude-agent-project
    """
    prefixes = [
        "-home-claude-agent-project-",
        "-home-claude-agent-project",
        "-home-claude-agent-",
        "-root-",
        "-root",
    ]
    name = encoded_name
    for prefix in prefixes:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if not name:
        return "Main Project"
    return name.replace("-", " ").strip().title()


def _tail_read(filepath, num_bytes=16384):
    """Read the last num_bytes of a file and return as lines.

    If the initial read contains no assistant messages (e.g. the tail is all
    progress events), automatically retries with up to 256KB to find actual
    content.
    """
    try:
        size = os.path.getsize(filepath)
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            if size > num_bytes:
                f.seek(size - num_bytes)
                f.readline()  # skip partial first line
            lines = f.readlines()

        # Check if we found any assistant messages; if not, read more
        has_assistant = any('"type":"assistant"' in l or '"type": "assistant"' in l for l in lines)
        if not has_assistant and size > num_bytes:
            # Retry with larger reads (64KB, 256KB)
            for bigger in (65536, 262144):
                if bigger <= num_bytes or size <= bigger:
                    continue
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(size - bigger)
                    f.readline()
                    lines = f.readlines()
                if any('"type":"assistant"' in l or '"type": "assistant"' in l for l in lines):
                    break

        return lines
    except OSError:
        return []


def _describe_tool(tool_name, tool_input):
    """Build a human-readable description for a tool call."""
    if tool_name in ("Read", "Glob", "Grep"):
        target = tool_input.get("file_path") or tool_input.get("pattern") or tool_input.get("path", "")
        return f"{tool_name}: {os.path.basename(str(target))}" if target else f"{tool_name}()"
    elif tool_name == "Edit":
        fp = tool_input.get("file_path", "")
        return f"Editing {os.path.basename(fp)}" if fp else "Editing file"
    elif tool_name == "Write":
        fp = tool_input.get("file_path", "")
        return f"Writing {os.path.basename(fp)}" if fp else "Writing file"
    elif tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return f"Running: {cmd[:60]}" if cmd else "Running command"
    elif tool_name == "Task":
        desc = tool_input.get("description", "")
        return f"Agent: {desc[:50]}" if desc else "Spawning agent"
    elif tool_name == "WebSearch":
        q = tool_input.get("query", "")
        return f"Searching: {q[:50]}" if q else "Web search"
    elif tool_name == "TodoWrite":
        return "Updating task list"
    elif tool_name == "Skill":
        skill = tool_input.get("skill", "")
        return f"Skill: {skill}" if skill else "Running skill"
    return f"{tool_name}()"


def _parse_session_tail(lines):
    """Parse tail of a session JSONL to extract current state."""
    result = {
        "currentTool": None,
        "currentAction": None,
        "lastUserMessage": None,
        "recentTools": [],
        "reasoning": [],
        "tokens": {"input": 0, "output": 0, "total": 0},
        "model": None,
        "contextWindow": {"used": 0, "max": MODEL_CONTEXT_DEFAULT, "ratio": 0, "compacting": False},
    }

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        entry_type = entry.get("type", "")

        if entry_type == "user":
            msg = entry.get("message", {})
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block["text"]
                    result["lastUserMessage"] = text[:120] + ("..." if len(text) > 120 else "")
                    # Check for /compact command
                    if "/compact" in text.lower():
                        result["contextWindow"]["compacting"] = True
                    break
                elif isinstance(block, str):
                    result["lastUserMessage"] = block[:120]
                    # Check for /compact command
                    if "/compact" in block.lower():
                        result["contextWindow"]["compacting"] = True
                    break

        if entry_type == "assistant":
            msg = entry.get("message", {})
            usage = msg.get("usage", {})
            if usage:
                inp = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                out = usage.get("output_tokens", 0)
                result["tokens"] = {"input": inp, "output": out, "total": inp + out}
                # Track context window: input tokens ≈ context window usage
                prev_ctx = result["contextWindow"]["used"]
                result["contextWindow"]["used"] = inp
                # Detect compaction: 30%+ drop in input tokens between consecutive turns
                if prev_ctx > 10000 and inp < prev_ctx * 0.7:
                    result["contextWindow"]["compacting"] = True
                else:
                    # Reset compacting flag if no drop detected (unless /compact was called)
                    if not result["contextWindow"]["compacting"]:
                        result["contextWindow"]["compacting"] = False
            if msg.get("model"):
                result["model"] = msg["model"]

            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    action = _describe_tool(tool_name, tool_input)
                    result["currentTool"] = tool_name
                    result["currentAction"] = action
                    result["recentTools"].append(action)
                elif isinstance(block, dict) and block.get("type") == "thinking":
                    # Extract thinking blocks (extended thinking / chain of thought)
                    text = block.get("thinking", "").strip()
                    if text:
                        # Take first ~200 chars of each thinking block
                        snippet = text[:200] + ("..." if len(text) > 200 else "")
                        result["reasoning"].append(snippet)
                elif isinstance(block, dict) and block.get("type") == "text":
                    # Extract assistant text as reasoning context
                    text = block.get("text", "").strip()
                    if text and len(text) > 10:
                        snippet = text[:200] + ("..." if len(text) > 200 else "")
                        result["reasoning"].append(snippet)
                elif isinstance(block, str) and len(block.strip()) > 10:
                    snippet = block.strip()[:200] + ("..." if len(block.strip()) > 200 else "")
                    result["reasoning"].append(snippet)

    # Keep only last 8 tool calls and last 5 reasoning entries
    result["recentTools"] = result["recentTools"][-8:]
    result["reasoning"] = result["reasoning"][-5:]

    return result


def _aggregate_daily_tokens():
    """Sum token usage from all JSONL files modified today across all project dirs."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total_input = 0
    total_output = 0
    session_count = 0

    for projects_dir in CLAUDE_PROJECTS_DIRS:
        if not os.path.isdir(projects_dir):
            continue
        for project_dir in os.listdir(projects_dir):
            project_path = os.path.join(projects_dir, project_dir)
            if not os.path.isdir(project_path):
                continue
            for jsonl_path in glob.glob(os.path.join(project_path, "*.jsonl")):
                try:
                    mtime = os.path.getmtime(jsonl_path)
                except OSError:
                    continue
                # Only count files modified today
                file_date = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")
                if file_date != today:
                    continue
                session_count += 1
                # Read entire file and sum all usage blocks
                try:
                    with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            line = line.strip()
                            if not line or '"usage"' not in line:
                                continue
                            try:
                                entry = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if entry.get("type") != "assistant":
                                continue
                            usage = entry.get("message", {}).get("usage", {})
                            if usage:
                                total_input += usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
                                total_output += usage.get("output_tokens", 0)
                except OSError:
                    continue

    return {
        "date": today,
        "input": total_input,
        "output": total_output,
        "total": total_input + total_output,
        "sessions": session_count,
    }


def _extract_task_descriptions(jsonl_path):
    """Extract Task tool call descriptions from a session JSONL.

    Returns dict mapping subagent short IDs to their task descriptions.
    Scans for Task tool_use blocks and their results to match agent IDs.
    """
    descriptions = {}
    pending_tasks = {}  # tool_use_id → description

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Quick filter: only parse lines that might contain Task calls or agent IDs
                if '"Task"' not in line and '"agent-' not in line and '"agentId"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if entry.get("type") == "assistant":
                    for block in entry.get("message", {}).get("content", []):
                        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Task":
                            inp = block.get("input", {})
                            desc = inp.get("description", "")
                            sa_type = inp.get("subagent_type", "")
                            tool_id = block.get("id", "")
                            label = desc or sa_type or "Subagent"
                            if tool_id:
                                pending_tasks[tool_id] = label

                elif entry.get("type") == "result":
                    # Results reference their tool_use_id and may contain the agent ID
                    tool_id = entry.get("tool_use_id", "")
                    if tool_id in pending_tasks:
                        # Look for agentId in the result content
                        content = entry.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                b.get("text", "") if isinstance(b, dict) else str(b)
                                for b in content
                            )
                        if isinstance(content, str):
                            # Match patterns like "agentId: a11c662" or "agent-a11c662"
                            for pattern in [r"agentId:\s*([a-f0-9]+)", r"agent-([a-f0-9]+)"]:
                                m = re.search(pattern, content)
                                if m:
                                    agent_short = m.group(1)
                                    descriptions[agent_short] = pending_tasks[tool_id]
                                    break
    except OSError:
        pass

    return descriptions


def scan_sessions():
    """Find all active Claude Code sessions on this machine."""
    now = time.time()
    agents = []

    for projects_dir in CLAUDE_PROJECTS_DIRS:
        if not os.path.isdir(projects_dir):
            continue

        for project_dir in os.listdir(projects_dir):
            project_path = os.path.join(projects_dir, project_dir)
            if not os.path.isdir(project_path):
                continue

            project_label = _decode_project_path(project_dir)

            for jsonl_path in glob.glob(os.path.join(project_path, "*.jsonl")):
                try:
                    mtime = os.path.getmtime(jsonl_path)
                except OSError:
                    continue

                if now - mtime > SESSION_ACTIVE_THRESHOLD:
                    continue

                session_id = os.path.splitext(os.path.basename(jsonl_path))[0]
                state = _parse_session_tail(_tail_read(jsonl_path))
                idle_secs = now - mtime
                session_status = "active" if idle_secs < 60 else "idle"

                # Map model to context limit and compute ratio
                ctx = state["contextWindow"]
                model_name = state["model"] or ""
                for model_prefix, limit in MODEL_CONTEXT_LIMITS.items():
                    if model_prefix in model_name:
                        ctx["max"] = limit
                        break
                ctx["ratio"] = ctx["used"] / ctx["max"] if ctx["max"] > 0 else 0

                agents.append({
                    "id": session_id,
                    "label": project_label,
                    "parent": None,
                    "status": session_status,
                    "currentTool": state["currentTool"],
                    "currentAction": state["currentAction"] or state.get("lastUserMessage") or ("Waiting for input..." if session_status == "idle" else "Working..."),
                    "recentTools": state["recentTools"],
                    "reasoning": state["reasoning"],
                    "goalId": None,
                    "tokens": state["tokens"],
                    "model": state["model"],
                    "contextWindow": ctx,
                    "x": 0, "y": 0, "nodeRadius": 28,
                })

                # Check for active subagents
                sa_dir = os.path.join(project_path, session_id, "subagents")
                if os.path.isdir(sa_dir):
                    # Extract task descriptions from parent session
                    task_descs = _extract_task_descriptions(jsonl_path)

                    SA_ACTIVE_THRESHOLD = 120  # 2 min — subagents finish quickly
                    for sa_file in glob.glob(os.path.join(sa_dir, "agent-*.jsonl")):
                        try:
                            sa_mtime = os.path.getmtime(sa_file)
                        except OSError:
                            continue
                        if now - sa_mtime > SA_ACTIVE_THRESHOLD:
                            continue

                        sa_name = os.path.splitext(os.path.basename(sa_file))[0]
                        sa_short_id = sa_name.replace("agent-", "")
                        sa_state = _parse_session_tail(_tail_read(sa_file, 32768))

                        # Build label: task description > current tool > agent type > short ID
                        sa_label = (
                            task_descs.get(sa_short_id)
                            or sa_state["currentTool"]
                            or sa_short_id
                        )

                        idle_secs_sa = now - sa_mtime
                        sa_status = "active" if idle_secs_sa < 60 else "idle"

                        # Map model to context limit and compute ratio for subagent
                        sa_ctx = sa_state["contextWindow"]
                        sa_model_name = sa_state["model"] or ""
                        for model_prefix, limit in MODEL_CONTEXT_LIMITS.items():
                            if model_prefix in sa_model_name:
                                sa_ctx["max"] = limit
                                break
                        sa_ctx["ratio"] = sa_ctx["used"] / sa_ctx["max"] if sa_ctx["max"] > 0 else 0

                        agents.append({
                            "id": sa_name,
                            "label": sa_label,
                            "parent": session_id,
                            "status": sa_status,
                            "currentTool": sa_state["currentTool"],
                            "currentAction": sa_state["currentAction"] or "Working...",
                            "recentTools": sa_state.get("recentTools", []),
                            "reasoning": sa_state["reasoning"],
                            "goalId": None,
                            "tokens": sa_state["tokens"],
                            "model": sa_state["model"],
                            "contextWindow": sa_ctx,
                            "x": 0, "y": 0, "nodeRadius": 16,
                        })

    return agents


# Mapping from file basenames / keywords to architecture document IDs.
# Used to detect which architecture documents agents are accessing.
_DOC_ID_MAP = {
    "STATE": "STATE", "state.md": "STATE",
    "GOALS": "GOALS", "goals.md": "GOALS",
    "ASSETS": "ASSETS", "assets.md": "ASSETS",
    "METRICS": "METRICS", "metrics.md": "METRICS",
    "LOG": "LOG", "log.md": "LOG",
    "OPPORTUNITIES": "OPPORTUNITIES", "opportunities.md": "OPPORTUNITIES",
    "FAILURE-CATALOG": "FAILURE-CATALOG", "failure-catalog.md": "FAILURE-CATALOG",
    "CLAUDE": "CLAUDE", "claude.md": "CLAUDE",
    "reality-model-architecture": "reality-model-architecture",
    "goal-advancement": "goal-advancement",
    "task-lifecycle": "task-lifecycle",
    "review-gate": "review-gate",
    "approval-rules": "approval-rules",
    "security": "security",
    "usage-tracking": "usage-tracking",
    "self-correction": "self-correction",
    "model-strategy": "model-strategy",
}


def _extract_doc_activity(agents):
    """Extract architecture document IDs being accessed by active agents.

    Scans recentTools from each agent for file references that map to
    architecture document IDs. Returns list of activity dicts.
    """
    activities = []
    seen = set()

    for agent in agents:
        if agent.get("status") != "active":
            continue
        tools = agent.get("recentTools", [])
        action = agent.get("currentAction", "")
        # Include currentAction in the search
        entries = tools[-5:] + ([action] if action else [])

        for entry in entries:
            if not entry:
                continue
            entry_lower = entry.lower()
            # Determine activity type from the tool description
            if any(kw in entry_lower for kw in ("read:", "reading", "grep:", "glob:")):
                activity_type = "read"
            elif any(kw in entry_lower for kw in ("edit", "writing", "write:")):
                activity_type = "write"
            else:
                continue

            # Try to match a document ID
            for keyword, doc_id in _DOC_ID_MAP.items():
                if keyword.lower() in entry_lower:
                    key = (doc_id, activity_type)
                    if key not in seen:
                        seen.add(key)
                        activities.append({
                            "docId": doc_id,
                            "type": activity_type,
                            "agent": agent.get("label", ""),
                        })
                    break

    return activities


def _read_wrapper_heartbeat() -> dict:
    """Read the wrapper heartbeat file to determine if the wrapper process is alive."""
    hb_path = os.path.join(STATUS_DIR, "heartbeat")
    try:
        with open(hb_path) as f:
            ts = f.read().strip()
        mtime = os.path.getmtime(hb_path)
        age = time.time() - mtime
        return {"alive": True, "last": ts, "age": age}
    except (OSError, ValueError):
        return {"alive": False, "last": None, "age": 9999}


def session_poller():
    """Continuously scan for active sessions and broadcast updates."""
    prev_ids = set()
    prev_activities = set()
    usage_tick = 0
    heartbeat_tick = 0
    USAGE_EVERY = 10  # aggregate daily tokens every 10 polls (~30s)
    HEARTBEAT_EVERY = 10  # broadcast heartbeat every ~30s even without sessions
    while True:
        agents = scan_sessions()
        cur_ids = {a["id"] for a in agents}

        if agents or cur_ids != prev_ids:
            broadcaster.broadcast("agent_tree", json.dumps({
                "agents": agents, "phase": "execute",
            }))
            n = len([a for a in agents if a["parent"] is None])
            wrapper_hb = _read_wrapper_heartbeat()
            broadcaster.broadcast("heartbeat", json.dumps({
                "alive": True,
                "last": datetime.now(timezone.utc).isoformat(),
                "activeSessions": n,
                "wrapperAlive": wrapper_hb["alive"],
                "wrapperAge": wrapper_hb["age"],
            }))
            prev_ids = cur_ids
            heartbeat_tick = 0

        # Even with no sessions, periodically broadcast heartbeat so client
        # knows the dashboard server (and wrapper) are alive
        heartbeat_tick += 1
        if heartbeat_tick >= HEARTBEAT_EVERY and not agents:
            heartbeat_tick = 0
            wrapper_hb = _read_wrapper_heartbeat()
            broadcaster.broadcast("agent_tree", json.dumps({
                "agents": [], "phase": "standby",
            }))
            broadcaster.broadcast("heartbeat", json.dumps({
                "alive": wrapper_hb["alive"],
                "last": datetime.now(timezone.utc).isoformat(),
                "activeSessions": 0,
                "wrapperAlive": wrapper_hb["alive"],
                "wrapperAge": wrapper_hb["age"],
            }))

        # Broadcast document activity for architecture view live flow
        if agents:
            activities = _extract_doc_activity(agents)
            cur_activity_keys = {(a["docId"], a["type"]) for a in activities}
            if cur_activity_keys != prev_activities:
                broadcaster.broadcast("doc_activity", json.dumps(activities))
                prev_activities = cur_activity_keys

        # Broadcast daily usage every USAGE_EVERY polls
        usage_tick += 1
        if usage_tick >= USAGE_EVERY:
            usage_tick = 0
            usage = _aggregate_daily_tokens()
            broadcaster.broadcast("daily_usage", json.dumps(usage))

        time.sleep(SESSION_POLL_INTERVAL)


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
    if event_type == "goals_full":
        return parse_goals_full(path)
    if event_type == "assets_full":
        return parse_assets(path)
    if event_type == "predictions":
        return parse_predictions()
    if event_type == "token_tracking":
        return parse_token_tracking()
    return parse_markdown_raw(path)


# ── File watcher thread ─────────────────────────────────────────────────────

def file_watcher():
    """Poll watched files every FILE_POLL_INTERVAL seconds, broadcast on change."""
    mtimes: Dict[str, float] = {}
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
        # Also watch architecture documents for change events
        for path, doc_id in DOC_WATCH_FILES.items():
            try:
                st = os.stat(path)
                mtime = st.st_mtime
                size = st.st_size
            except OSError:
                continue
            prev = mtimes.get(path)
            if prev != mtime:
                old_size = file_cache.get(path, {}).get("size", size)
                delta = size - old_size
                mtimes[path] = mtime
                with cache_lock:
                    file_cache[path] = {"mtime": mtime, "size": size}
                word_count = 0
                text = _read_file(path)
                if text:
                    word_count = len(text.split())
                broadcaster.broadcast("doc_change", json.dumps({
                    "id": doc_id,
                    "name": os.path.basename(path),
                    "mtime": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                    "size": size,
                    "delta": delta,
                    "tokens": int(word_count * 1.3),
                }))
        time.sleep(FILE_POLL_INTERVAL)


# ── Git data extraction ─────────────────────────────────────────────────────

COMMIT_TYPE_RE = re.compile(r"^(\w+)(?:\(.+?\))?:\s*(.+)")

def _build_goal_keywords(goal_names):
    """Build keyword matchers for goal names.

    Each goal gets: its full name, the name without 'goal' suffix,
    and individual significant words (>3 chars).
    Returns dict mapping keyword → goal name (lowercase).
    """
    # Additional keyword hints for common commit patterns
    EXTRA_KEYWORDS = {
        "product": ["revenue", "film", "festival", "competition", "monetiz",
                     "paying", "profit", "pricing", "customer"],
        "content": ["newsletter", "blog", "publish", "audience", "content",
                     "writing", "article", "post", "media"],
        "gui": ["dashboard", "gui", "visualization", "interface", "frontend",
                "ui", "display"],
        "self-improvement": ["self-review", "health check", "governance",
                             "ideation", "skill", "tool", "rule", "self-mod",
                             "capability", "v2 reality", "architecture",
                             "self-correction", "failure catalog"],
    }
    kw_map = {}
    for gn in goal_names:
        # Full name match
        kw_map[gn] = gn
        # Strip "goal" suffix
        stripped = gn.replace(" goal", "").strip()
        if stripped:
            kw_map[stripped] = gn
        # Individual words (>3 chars, not "goal")
        for word in stripped.split():
            if len(word) > 3 and word != "goal":
                kw_map[word] = gn
        # Extra keyword hints
        for hint_key, hints in EXTRA_KEYWORDS.items():
            if hint_key in gn:
                for h in hints:
                    kw_map[h] = gn
    return kw_map


def refresh_git_data():
    """Run git log and build structured commit data, including synapse history."""
    goals = parse_goals(os.path.join(PROJECT_DIR, ".planning", "GOALS.md"))
    goal_names = [g["name"].lower() for g in goals]
    goal_kw = _build_goal_keywords(goal_names)
    # Sort keywords longest first so specific matches win over short ones
    sorted_keywords = sorted(goal_kw.keys(), key=len, reverse=True)

    try:
        result = subprocess.run(
            ["git", "log", "--pretty=format:%H|%aI|%s", "-500"],
            capture_output=True, text=True, timeout=15,
            cwd=PROJECT_DIR,
        )
        raw = result.stdout.strip()
    except subprocess.TimeoutExpired:
        print("Warning: git log timed out after 15s in git_poller")
        raw = ""
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"Warning: git command failed in git_poller: {e}")
        raw = ""

    commits = []
    synapses = []
    goal_dist: Dict[str, int] = defaultdict(int)
    daily: Dict[str, int] = defaultdict(int)

    for line in raw.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        sha, date_str, message = parts
        m = COMMIT_TYPE_RE.match(message)
        ctype = m.group(1) if m else "other"

        matched_goal = None
        msg_lower = message.lower()
        for kw in sorted_keywords:
            if kw in msg_lower:
                matched_goal = goal_kw[kw]
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

        # Check if this commit is a synapse (learning/self-modification event)
        sm = SYNAPSE_PATTERNS.match(message)
        if sm:
            prefix = sm.group(1).lower()
            synapses.append({
                "hash": sha[:8],
                "date": date_str,
                "message": message,
                "type": SYNAPSE_TYPE_MAP.get(prefix, "other"),
            })

    data = {
        "commits": commits,
        "goals": goals,
        "goal_distribution": dict(goal_dist),
        "daily_activity": dict(daily),
        "synapses": synapses,
    }
    with cache_lock:
        git_cache.update(data)


def get_commit_detail(short_hash: str) -> dict:
    """Get detailed info for a single commit: full message, diff stats, changed files."""
    import re as _re
    # Sanitize hash input (only allow hex chars)
    clean = _re.sub(r'[^0-9a-fA-F]', '', short_hash)[:40]
    if not clean:
        return {"error": "invalid hash"}
    try:
        # Get full commit info + diff stat
        result = subprocess.run(
            ["git", "show", "--stat", "--format=%H%n%aI%n%an%n%B", clean],
            capture_output=True, text=True, timeout=10,
            cwd=PROJECT_DIR,
        )
        if result.returncode != 0:
            return {"error": "commit not found"}
        lines = result.stdout.strip().split("\n")
        if len(lines) < 4:
            return {"error": "unexpected format"}

        full_hash = lines[0]
        date = lines[1]
        author = lines[2]
        # Body is everything until the diff stat section
        # Diff stat starts with lines like " file | N ++" and ends with "N files changed..."
        body_lines = []
        stat_lines = []
        in_stat = False
        for line in lines[3:]:
            if not in_stat and ("|" in line and ("+" in line or "-" in line or "Bin" in line)):
                in_stat = True
            if in_stat:
                stat_lines.append(line)
            else:
                body_lines.append(line)
        body = "\n".join(body_lines).strip()

        # Parse changed files from stat
        files = []
        summary = ""
        for sl in stat_lines:
            sl = sl.strip()
            if "|" in sl:
                parts = sl.split("|", 1)
                fname = parts[0].strip()
                changes = parts[1].strip() if len(parts) > 1 else ""
                files.append({"name": fname, "changes": changes})
            elif "changed" in sl:
                summary = sl

        return {
            "hash": full_hash[:8],
            "fullHash": full_hash,
            "date": date,
            "author": author,
            "subject": body.split("\n")[0] if body else "",
            "body": body,
            "files": files,
            "summary": summary,
        }
    except subprocess.TimeoutExpired:
        print(f"Warning: git show timed out after 15s for commit {short_hash}")
        return {"error": "git timeout"}
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        print(f"Warning: git show failed for {short_hash}: {e}")
        return {"error": "git error"}


def git_poller():
    """Refresh git data every GIT_REFRESH_INTERVAL seconds."""
    while True:
        refresh_git_data()
        time.sleep(GIT_REFRESH_INTERVAL)


# ── Daydream graph builder ───────────────────────────────────────────────────

def build_daydream_graph() -> dict:
    nodes: List[dict] = []
    links: List[dict] = []
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

        elif path == "/api/architecture":
            goals_path = os.path.join(PROJECT_DIR, ".planning", "GOALS.md")
            assets_path = os.path.join(PROJECT_DIR, ".planning", "ASSETS.md")
            with cache_lock:
                synapses = git_cache.get("synapses", [])
            self._send_json({
                "goals": parse_goals_full(goals_path),
                "assets": parse_assets(assets_path),
                "documents": get_document_metadata(),
                "synapses": synapses,
                "reviewCycles": get_review_status(),
            })

        elif path == "/api/security":
            sec_path = os.path.join(PROJECT_DIR, ".planning", "LOG.md")
            wrapper_path = os.path.join(STATUS_DIR, "wrapper.log")
            backup_path = os.path.join(STATUS_DIR, "backup.json")
            self._send_json({
                "security": parse_security_log(sec_path),
                "wrapper": parse_security_log(wrapper_path),
                "backup": parse_json_file(backup_path),
            })

        elif path == "/api/summary":
            self._send_json(build_summary())

        elif path == "/api/glossary":
            self._send_json(get_glossary())

        elif path == "/api/documents":
            self._send_json(get_full_document_map())

        elif path == "/api/predictions":
            self._send_json(parse_predictions())

        elif path == "/api/tokens":
            self._send_json(parse_token_tracking())

        elif path == "/api/health":
            self._send_json(parse_health_scorecard())

        elif path.startswith("/api/commit/"):
            commit_hash = path.split("/api/commit/")[1]
            self._send_json(get_commit_detail(commit_hash))

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
    for target in (file_watcher, git_poller, sse_keepalive, session_poller):
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
