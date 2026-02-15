# Dashboard Improvement Roadmap

Last updated: 2026-02-14

## Overview

This roadmap prioritizes improvements to the Strategist Dashboard based on a
thorough analysis of `index.html` (~9200 lines) and `dashboard-server.py`
(~1750 lines). Each item is ranked by impact (how much it helps the user
understand system health and activity) and effort (development time).

The dashboard's three core questions:
1. **Is it healthy?**
2. **What's it doing?**
3. **How's it thinking?**

---

## Completed — Round 1 (2026-02-14)

### 1. Metrics & Predictions Panel
**Impact: HIGH | Effort: MEDIUM | Status: DONE**

Added a new sidebar tab (triangle icon) with:
- 2x2 KPI grid: Cycles completed, Tasks/week, Prediction accuracy, Total predictions
- Accuracy card highlights green when >= 70%, amber when < 50%
- Recent Predictions list with HIT/MISS/PENDING badges
- Health Summary combining system health, heartbeat age, context usage, agent count, and growth vitality
- Token Usage 7-day bar chart (see #3 below)

*Why it matters:* The backend already parsed METRICS.md for prediction count,
accuracy rate, and cycle count via `build_summary()`, but the frontend only
showed a single brief text line. This surfaces the Metrics goal data and
directly answers "Is it healthy?"

*Files changed:* `index.html` (CSS, HTML, JavaScript)

### 2. Data Staleness Indicators
**Impact: HIGH | Effort: LOW | Status: DONE**

Added:
- HUD bar freshness indicator (green/amber/red dot + "Xs ago" text, visible on hover)
- Process panel staleness banner at the top showing data age with color coding:
  - Green "Data is current" (< 10 minutes)
  - Amber "Data may be stale" (10-30 minutes)
  - Red "Data is outdated" (> 30 minutes)
- All SSE event handlers now call `markDataReceived()` to track last data timestamp
- Auto-refreshes every 10 seconds

*Why it matters:* The main canvas view had zero indication of when data was
last fetched. Users could not tell if the dashboard was showing stale data.
This immediately improves trust and answers "Is it healthy?"

*Files changed:* `index.html` (CSS, HTML, JavaScript)

### 3. Token Usage Trend Chart
**Impact: HIGH | Effort: MEDIUM | Status: DONE**

Added to the Metrics panel:
- 7-day bar chart showing daily token consumption
- Today's bar highlighted in cyan, previous days in blue
- Day-of-week labels below each bar
- Total for today shown in header
- Tracks token history from `daily_usage` SSE events
- Works with demo mode (seeded with realistic 7-day data)

*Why it matters:* Only "Tokens today" was shown as a single number in the HUD.
No trend visualization existed. This surfaces Token Distribution goal data and
helps monitor costs over time.

*Files changed:* `index.html` (CSS, HTML, JavaScript)

---

## Completed — Round 2 (2026-02-14)

### 4. Hidden Components Audit & Cleanup
**Impact: MEDIUM | Effort: LOW | Status: DONE**

Audited all components hidden with `display: none`:

**Removed (dead code):**
- Section Markers (#marker-status, #marker-agents, #marker-thinking,
  #marker-goals) — CSS + HTML removed entirely. These were numbered circles
  on the canvas intended as a reading-flow guide. They were always hidden and
  redundant with sidebar tab navigation. No JavaScript referenced them.
  ~50 lines of CSS + 16 lines of HTML removed.

**Kept hidden (documented why):**
- Mission Status panel — Redundant with HUD bar. Cannot remove: 15+
  `updateMissionStatus()` calls and extensive JS functions reference the DOM.
  Added explanatory CSS comment.
- Activity Feed — Redundant with sidebar Logs tab. Cannot remove: 20+
  `addFeedEntry()` calls throughout the codebase serve as a data accumulator.
  Added explanatory CSS comment.
- Thinking Ticker — Redundant with sidebar Thinking tab and agent node hover
  detail. Cannot remove: 10+ `updateThinkingTicker()` calls. Added
  explanatory CSS comment.

**Not touched (functional):**
- Synapse tree and document constellation (commented out in animation loop) —
  these are architecture-view features, not hidden components.

### 5. Connection Health with Reconnection Feedback
**Impact: LOW-MEDIUM | Effort: LOW | Status: DONE**

Enhanced the SSE connection status indicator:
- **Retry countdown**: Shows "retry 5s" next to the connection dot during
  reconnection attempts, counting down in real-time
- **Disconnection duration**: Shows "down Xm" when disconnected for >1 minute,
  with a red background highlight on the connection status area
- **State tracking**: New variables `connDisconnectedAt`, `connRetryTimer`,
  `connRetryTarget` track disconnection lifecycle
- **Automatic cleanup**: All disconnection UI clears on successful reconnect
- Status text now reads "disconnected" (was "reconnecting") for clarity

*Files changed:* `index.html` (CSS, HTML, JavaScript)

### 14. Health Scorecard Panel (NEW)
**Impact: HIGH | Effort: MEDIUM | Status: DONE**

Added a full Health Scorecard at the top of the Metrics panel, inspired by
the User Comprehension goal's research template. Shows:
- **Overall system grade** (A/B/C/D/F) with color-coded badge and plain
  English description
- **Per-goal health indicators**: colored dots (green/yellow/red) with trend
  arrows (up/flat/down) and ACTIVE/PAUSED status badges
- **Rate of improvement**: "Getting better?" (YES/MAYBE/NO), Speed
  (Accelerating/Steady/Slowing/Stalled), Growth vitality
- **Owner attention flag**: "No action needed" (green) or specific actionable
  request (amber/red) with urgency level

Grading algorithm considers:
- System health status (green/yellow/red)
- Per-goal commit activity (7-day window)
- Colony particle staleness (stalled goals)
- Prediction accuracy (when available)
- Context window pressure
- Growth vitality trend

Works in demo mode with realistic seed data showing grade B.

*Files changed:* `index.html` (CSS, HTML, JavaScript)

### 15. Enhanced Prediction Tracker (NEW)
**Impact: MEDIUM | Effort: MEDIUM | Status: DONE**

Replaced the basic prediction list with a structured tracker:
- **Upcoming section**: Pending predictions with confidence badges (HIGH/MED/LOW),
  color-coded by confidence level, "Due in Xd" countdown, confidence bar
- **Resolved section**: HIT/MISS badges with confidence bars, sorted most
  recent first
- **Confidence visualization**: Small bar chart per prediction showing
  confidence level (green=high, amber=medium, gray=low)
- Demo data updated with confidence levels and future resolution dates

*Files changed:* `index.html` (CSS, HTML, JavaScript)

---

## Priority 2 — Medium Impact, Medium Effort

### 6. Mobile Responsive Improvements
**Impact: MEDIUM | Effort: MEDIUM | Est: 8-12 hours**

Current mobile CSS is minimal (only sidebar goes full-width at 768px). The
agent tree, HUD bar, and canvas visualizations are unusable on mobile. Touch
zoom support is missing.

**Recommendation:**
- Collapse HUD bar to icon-only mode on mobile
- Stack sidebar below the visualization on small screens
- Add touch pinch-zoom for the agent tree
- Provide a simplified "list view" for mobile that shows key metrics without
  the force-directed graph

### 7. Accessibility (ARIA + Keyboard Navigation)
**Impact: MEDIUM | Effort: MEDIUM | Est: 6-10 hours**

No ARIA labels, roles, or keyboard navigation. Sidebar tabs are not keyboard-
accessible. Screen readers get nothing useful from the visualization.

**Recommendation:**
- Add `role="tablist"` / `role="tab"` / `role="tabpanel"` to sidebar
- Add ARIA labels to HUD elements
- Make sidebar tabs keyboard-navigable (arrow keys + Enter)
- Add `aria-live="polite"` to status text elements
- Provide a text-only "screen reader summary" that updates when state changes

### 8. Session Timeline / Cycle Duration History
**Impact: MEDIUM | Effort: MEDIUM | Est: 6-8 hours**

No visualization of historical session durations, cycle lengths, or uptime
patterns. Backend tracks session counts but doesn't expose historical trends.

**Recommendation:**
- Add a timeline view in the Metrics panel showing cycle start/end times
- Color-code cycles by goal worked on
- Show cycle duration distribution (average, min, max)
- Requires backend changes to persist cycle history to a file

### 9. Performance: Particle Collision Detection
**Impact: LOW-MEDIUM | Effort: LOW | Est: 2-3 hours**

`updatePhysics()` does O(n^2) pairwise collision checks on all particles.
With many particles (50+) this causes frame drops.

**Recommendation:**
- Skip collision detection for distant particles (early distance check)
- Or use spatial hashing (grid cells) for O(n) average-case
- Or simply skip collision for particles in background/dimmed colonies

---

## Priority 3 — Medium-High Impact, High Effort

### 10. Causal Analysis View
**Impact: MEDIUM | Effort: HIGH | Est: 20-30 hours**

No view exists showing cause-effect relationships between agent actions and
outcomes. This is a new goal with no existing data pipeline.

**Recommendation:**
- Design a new view (third option in view toggle) showing a Sankey or flow
  diagram: Action -> Outcome chains
- Requires backend instrumentation: track which actions led to which results
- Start simple: link commit types to goal progress changes
- Phase 2: link specific decisions (from thinking steps) to measured outcomes

### 11. Prediction Tracking Pipeline
**Impact: MEDIUM | Effort: HIGH | Est: 15-20 hours**

Currently predictions exist only in METRICS.md text. No structured pipeline
for creating, tracking, and resolving predictions. (Frontend tracker now
exists from item #15; backend pipeline still needed.)

**Recommendation:**
- Backend: Parse METRICS.md predictions into structured data
- Backend: Track prediction outcomes over time in a JSON file
- Frontend: Accuracy trend chart (rolling 30-day window)

### 12. Real-Time Token Budget Visualization
**Impact: MEDIUM | Effort: MEDIUM-HIGH | Est: 10-15 hours**

The token chart shows daily totals, but there's no budget tracking, cost
estimation, or burn-rate projection.

**Recommendation:**
- Add configurable daily/weekly token budget
- Show burn rate and projected overage/underage
- Color-code the token bar chart against budget thresholds
- Requires backend: store budget config, calculate projections

### 13. Notification System
**Impact: MEDIUM | Effort: MEDIUM | Est: 8-12 hours**

Currently alerts are buried in the Process panel sidebar. No push
notifications, no sound, no desktop notifications.

**Recommendation:**
- Add browser notification permission request
- Push desktop notifications for critical events (health red, stagnation,
  context > 90%, cycle failure)
- Add optional sound alerts
- Badge count on sidebar tabs when new data arrives

---

## Architecture Notes

- All improvements should maintain the single-file SPA architecture
- Backend changes should use stdlib-only Python (no pip dependencies)
- New SSE event types can be added to both `dashboard-server.py` and the
  frontend handlers
- Demo mode must be updated for any new data sources (always test offline)
- The architecture view and canvas view are independent — improvements to one
  don't need to affect the other

## File Reference

| File | Lines | Role |
|------|-------|------|
| `index.html` | ~9200 | Full SPA: CSS + HTML + JavaScript |
| `dashboard-server.py` | ~1750 | Backend: HTTP + SSE + file watchers |
