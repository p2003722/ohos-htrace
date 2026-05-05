# AGENTS.md — AI Agent Guide for ohos-htrace

> This file helps AI coding assistants (Cursor, Copilot, Gemini, Claude, etc.)
> understand and correctly use the `ohos-htrace` library.

## What This Library Does

`ohos-htrace` is a **pure-Python parser for OpenHarmony `.htrace` trace files**.
It replaces the C++ `trace_streamer` tool — no external binaries needed.

Primary use cases:
- Parse `.htrace` binary files into queryable SQLite databases
- Analyze application cold-start performance
- Measure function durations by keyword matching
- Automate performance testing pipelines

## Installation

```bash
pip install ohos-htrace
```

## API Quick Reference

### High-Level (recommended for most tasks)

```python
import ohos_htrace

# One-shot: parse + analyze cold start
result = ohos_htrace.parse_and_analyze(
    "trace.htrace",           # Path to .htrace file
    "com.example.app",        # Package name (or substring)
    keywords=["AppSpawn", "LoadModule"],  # Optional function keywords
)

# result dict keys:
#   cold_start_ms: float    — Total cold start duration in ms
#   process_name: str       — Matched process name
#   pid: int                — Process ID
#   max_gap_ms: float       — Largest idle gap in ms
#   node_count: int         — Total callstack nodes
#   depth0_count: int       — Top-level function blocks
#   frame_count: int        — Rendered frames count
#   gaps: list[dict]        — Idle gaps >5ms [{start_ms, end_ms, dur_ms}]
#   keyword_durations: dict — Per-keyword timing breakdown
#   top_functions: list     — Top-10 functions by duration
```

### Low-Level (for custom queries)

```python
import ohos_htrace

# Step 1: Parse .htrace → in-memory SQLite database
conn = ohos_htrace.parse_to_memory_db("trace.htrace")

# Step 2: Query with standard SQL
cur = conn.cursor()
cur.execute("SELECT name, pid FROM process")
for name, pid in cur.fetchall():
    print(f"{name} (pid={pid})")

# Step 3: Use analysis helpers
procs = ohos_htrace.find_matching_processes(conn, "aweme")
for proc in procs:
    tree = ohos_htrace.get_process_callstack(conn, proc["id"])
    cold_ms, gaps = ohos_htrace.compute_cold_start_time(tree)
    print(f"{proc['name']}: {cold_ms:.2f}ms cold start")

conn.close()
```

### CLI

```bash
# Basic cold-start analysis
ohos-htrace trace.htrace com.example.app

# With keyword breakdown
ohos-htrace trace.htrace aweme -k AppSpawn,LaunchAbility,LoadModule

# JSON output for scripting
ohos-htrace trace.htrace aweme --json

# Verbose with top-10 functions
ohos-htrace trace.htrace aweme -v
```

## Database Schema

After parsing, the in-memory SQLite database contains these tables:

| Table | Columns | Description |
|-------|---------|-------------|
| `process` | id, name, pid | Process list |
| `thread` | id, ipid, name, tid | Thread list (ipid → process.id) |
| `callstack` | id, callid, parent_id, name, depth, ts, dur | Function call tree (callid → thread.id, ts/dur in nanoseconds) |
| `frame_slice` | id, vsync, ts, dur, type_desc, flag, ipid | Frame rendering data |

**Important notes on the schema:**
- `ts` and `dur` are in **nanoseconds** — divide by 1,000,000 for milliseconds
- `callstack.callid` is the internal thread id (references `thread.id`)
- `callstack.depth` = 0 means top-level function, higher = nested deeper
- `callstack.parent_id` links to the parent node in the call tree

## Public API Surface

All public functions are exported from the top-level `ohos_htrace` module:

| Function | Signature | Returns |
|----------|-----------|---------|
| `parse_to_memory_db` | `(filepath: str) → sqlite3.Connection` | In-memory SQLite connection |
| `parse_and_analyze` | `(filepath, package_name, keywords=None) → dict \| None` | Analysis result dict or None |
| `find_matching_processes` | `(conn, package_name) → list[dict]` | `[{id, name, pid}]` |
| `get_process_callstack` | `(conn, ipid) → list[dict]` | `[{id, parent_id, name, depth, ts, dur, tid}]` |
| `compute_cold_start_time` | `(nodes) → (float \| None, list[dict])` | `(cold_start_ms, gaps)` |
| `compute_keyword_durations` | `(nodes, keywords, cold_start_ms=None) → dict` | `{total_ms, by_keyword: {kw: {total_ms, count, details}}}` |
| `get_frame_slices` | `(conn, ipid=None) → list[dict]` | `[{vsync, ts, dur, type_desc, flag}]` |

## Common Patterns

### Batch analysis of multiple traces
```python
import ohos_htrace
from pathlib import Path

results = []
for f in Path("traces/").glob("*.htrace"):
    r = ohos_htrace.parse_and_analyze(str(f), "com.example.app")
    if r:
        results.append({"file": f.name, "cold_start_ms": r["cold_start_ms"]})
```

### Custom SQL queries on parsed data
```python
conn = ohos_htrace.parse_to_memory_db("trace.htrace")
cur = conn.cursor()

# Find slowest functions in a specific process
cur.execute("""
    SELECT c.name, c.dur / 1000000.0 AS dur_ms, c.depth
    FROM callstack c
    JOIN thread t ON c.callid = t.id
    JOIN process p ON t.ipid = p.id
    WHERE p.name LIKE '%aweme%'
    ORDER BY c.dur DESC
    LIMIT 20
""")
for name, dur_ms, depth in cur.fetchall():
    print(f"  {dur_ms:8.2f}ms  d={depth}  {name}")
```

### Process name matching
The package name is matched against the **last 15 characters** of the process name
(matching Linux's 15-char `comm` limit). So `"aweme"` will match `"ss.hm.ugc.aweme"`.

## Gotchas & Edge Cases

1. **Package name matching uses suffix**: `find_matching_processes` matches on the last
   15 chars of the process name. Use a short, unique substring.

2. **Returns None when no cold start detected**: `parse_and_analyze()` returns `None`
   (not an empty dict) if no matching process or cold start is found. Always check.

3. **Timestamps are nanoseconds**: All `ts` and `dur` values in the database are in
   nanoseconds. The analysis functions return milliseconds.

4. **Memory usage**: The entire trace is parsed into an in-memory SQLite database.
   A 120MB htrace file produces ~70k callstack rows — this is fine for typical traces.

5. **Only ftrace-plugin data is parsed**: The parser handles `ftrace-plugin` segments
   (B/E/S/F trace events, sched_switch, task_rename). Other plugins like `hidump-plugin`
   are not yet supported.

## Dependencies

- Python ≥ 3.10
- protobuf ≥ 4.21.0
- No other dependencies
