"""
ohos_htrace.analyzer — Cold-start analysis for OpenHarmony htrace data.

Provides functions to query the in-memory SQLite database produced by the
parser, detect cold-start timing, compute keyword durations, and extract
frame-slice data.
"""
import re
import sqlite3
import logging
from typing import Optional

_log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  Process / Thread / Callstack Queries
# ═══════════════════════════════════════════════════════════════════

def find_matching_processes(conn: sqlite3.Connection,
                            package_name: str) -> list[dict]:
    """
    Find processes whose name contains *package_name* (last 15 chars).

    Returns:
        List of dicts with keys: id, name, pid.
    """
    suffix = package_name[-15:] if len(package_name) > 15 else package_name
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, pid FROM process WHERE name LIKE ? ORDER BY id",
        (f"%{suffix}%",),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_process_callstack(conn: sqlite3.Connection,
                           ipid: int) -> list[dict]:
    """
    Retrieve the full callstack tree for a given internal process id.

    Returns:
        Flat list of dicts with keys: id, parent_id, name, depth, ts, dur, tid.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.parent_id, c.name, c.depth, c.ts, c.dur,
               c.callid AS tid
        FROM callstack c
        WHERE c.callid IN (SELECT id FROM thread WHERE ipid = ?)
        ORDER BY c.ts
    """, (ipid,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_frame_slices(conn: sqlite3.Connection,
                     ipid: int | None = None) -> list[dict]:
    """
    Query ``frame_slice`` table for Actual Timeline frame data.

    Returns:
        List of dicts with keys: vsync, ts, dur, type_desc, flag.
    """
    try:
        cur = conn.cursor()
        if ipid is not None:
            cur.execute("""
                SELECT vsync, ts, dur, type_desc, flag
                FROM frame_slice
                WHERE type_desc = 'actural' AND ipid = ?
                ORDER BY ts
            """, (ipid,))
        else:
            cur.execute("""
                SELECT vsync, ts, dur, type_desc, flag
                FROM frame_slice
                WHERE type_desc = 'actural'
                ORDER BY ts
            """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════════
#  Cold-Start Detection
# ═══════════════════════════════════════════════════════════════════

# First-frame markers — any of these names signals the end of cold start.
_FIRST_FRAME_MARKERS = (
    "firstDraw",
    "DrawFrame",
    "RSMainThread::DoWindowRender",
    "H:RSModifier::Draw",
    "H:RenderFrame",
)

_GAP_THRESHOLD_NS = 5_000_000  # 5 ms


def compute_cold_start_time(
    nodes: list[dict],
) -> tuple[float | None, list[dict]]:
    """
    Compute cold-start duration (ms) and detect idle gaps in the timeline.

    The cold-start end is determined by the earlier of:
      1. A first-frame marker (firstDraw / DrawFrame / DoWindowRender …)
      2. The first ``UIVsyncCallback`` completion

    Args:
        nodes: Flat callstack nodes for a single thread.

    Returns:
        ``(cold_start_ms, gaps)`` where *gaps* is a list of
        ``{'start_ms': float, 'end_ms': float, 'dur_ms': float}`` dicts.
    """
    valid = [n for n in nodes
             if n.get("ts") is not None and n.get("dur") is not None]
    if not valid:
        return None, []

    min_ts = min(n["ts"] for n in valid)

    # Method 1: first-frame marker
    frame_end = None
    for n in sorted(valid, key=lambda x: x["ts"]):
        name = n.get("name", "")
        if any(m in name for m in _FIRST_FRAME_MARKERS):
            frame_end = n["ts"] + n["dur"]
            break

    # Method 2: first UIVsyncCallback
    vsync_end = None
    for n in sorted(valid, key=lambda x: x["ts"]):
        if "UIVsyncCallback" in (n.get("name", "") or ""):
            vsync_end = n["ts"] + n["dur"]
            break

    candidates = [t for t in (frame_end, vsync_end) if t is not None]
    end_ts = min(candidates) if candidates else None

    gaps = _detect_gaps(valid, min_ts, end_ts)

    if end_ts is not None and end_ts > min_ts:
        return (end_ts - min_ts) / 1_000_000, gaps

    return None, gaps


def _detect_gaps(nodes, min_ts, end_ts=None):
    """Detect idle gaps (>5 ms) between depth-0 blocks."""
    top = sorted(
        [n for n in nodes
         if (n.get("depth", 0) or 0) == 0 and (n.get("dur", 0) or 0) > 0],
        key=lambda n: n["ts"],
    )
    if end_ts is not None:
        top = [b for b in top if b["ts"] < end_ts]
    if len(top) < 2:
        return []

    gaps = []
    for i in range(len(top) - 1):
        cur_end = top[i]["ts"] + top[i]["dur"]
        next_start = top[i + 1]["ts"]
        gap_ns = next_start - cur_end
        if gap_ns >= _GAP_THRESHOLD_NS:
            gaps.append({
                "start_ms": (cur_end - min_ts) / 1e6,
                "end_ms": (next_start - min_ts) / 1e6,
                "dur_ms": gap_ns / 1e6,
            })
    return gaps


# ═══════════════════════════════════════════════════════════════════
#  Keyword Duration Analysis
# ═══════════════════════════════════════════════════════════════════

def compute_keyword_durations(
    nodes: list[dict],
    keywords: list[str],
    cold_start_ms: float | None = None,
) -> dict:
    """
    Measure total time spent in functions matching each *keyword*.

    Only nodes within the cold-start window are considered.

    Args:
        nodes: Flat callstack nodes for a single thread.
        keywords: Function-name substrings to search for.
        cold_start_ms: If provided, restricts search to the cold-start window.

    Returns:
        ``{'total_ms': float, 'by_keyword': {kw: {'total_ms', 'count', 'details'}}}``
    """
    result: dict = {"total_ms": 0.0, "by_keyword": {}}
    if not nodes or not keywords:
        return result

    valid = [n for n in nodes if n.get("dur") and n["dur"] > 0]
    if not valid:
        return result

    min_ts = min(n["ts"] for n in valid)
    end_ts = (min_ts + cold_start_ms * 1e6) if cold_start_ms else None

    overall = 0.0
    for kw in keywords:
        kw_lower = kw.lower()
        matches = []
        for n in valid:
            if end_ts and n["ts"] >= end_ts:
                continue
            name = (n.get("name", "") or "").lower()
            if kw_lower in name:
                dur_ms = n["dur"] / 1e6
                matches.append({
                    "name": n.get("name", "")[:80],
                    "dur_ms": round(dur_ms, 3),
                    "depth": n.get("depth", 0),
                })
        total = sum(m["dur_ms"] for m in matches)
        result["by_keyword"][kw] = {
            "total_ms": round(total, 2),
            "count": len(matches),
            "details": matches,
        }
        overall += total

    result["total_ms"] = round(overall, 2)
    return result


# ═══════════════════════════════════════════════════════════════════
#  High-Level Analysis
# ═══════════════════════════════════════════════════════════════════

def _is_system_tag(name: str) -> bool:
    """Filter out system wrapper events (all-uppercase tags)."""
    s = (name or "").strip()
    if s.startswith("H:"):
        s = s[2:]
    return bool(re.match(r"^[A-Z0-9_]{3,}(?:$|[^a-zA-Z0-9_])", s))


def analyze_cold_start(
    conn: sqlite3.Connection,
    package_name: str,
    keywords: list[str] | None = None,
) -> dict | None:
    """
    Full cold-start analysis for a package.

    Args:
        conn: SQLite connection (from ``parse_to_memory_db``).
        package_name: Package name keyword.
        keywords: Optional function-name keywords for duration breakdown.

    Returns:
        Dict with cold_start_ms, process_name, pid, gaps,
        keyword_durations, top_functions, node_count, etc.
        Returns ``None`` if no cold start detected.
    """
    procs = find_matching_processes(conn, package_name)
    if not procs:
        _log.warning("No matching process for '%s'", package_name)
        return None

    best = None
    for proc in procs:
        ipid = proc["id"]
        tree = get_process_callstack(conn, ipid)
        filtered = [n for n in tree if not _is_system_tag(n.get("name", ""))]
        if not filtered:
            filtered = tree

        # Group by tid
        by_tid: dict[int, list] = {}
        for n in filtered:
            by_tid.setdefault(n.get("tid", 0), []).append(n)

        for tid, nodes in by_tid.items():
            cold_ms, gaps = compute_cold_start_time(nodes)
            if cold_ms is None or cold_ms <= 0:
                continue

            kw_dur = (compute_keyword_durations(nodes, keywords, cold_ms)
                      if keywords else {})
            frames = get_frame_slices(conn, ipid)

            # Top-10 functions
            top = sorted(nodes, key=lambda n: n.get("dur", 0) or 0,
                         reverse=True)[:10]

            depth0 = [n for n in nodes
                      if (n.get("depth", 0) or 0) == 0
                      and (n.get("dur", 0) or 0) > 0]

            result = {
                "cold_start_ms": round(cold_ms, 2),
                "process_name": proc["name"],
                "pid": proc["pid"],
                "ipid": ipid,
                "tid": tid,
                "node_count": len(nodes),
                "depth0_count": len(depth0),
                "gaps": gaps,
                "max_gap_ms": round(max((g["dur_ms"] for g in gaps),
                                        default=0), 1),
                "frame_count": len(frames),
                "keyword_durations": kw_dur,
                "top_functions": [
                    {
                        "name": n.get("name", "")[:80],
                        "dur_ms": round((n.get("dur", 0) or 0) / 1e6, 2),
                        "depth": n.get("depth", 0),
                    }
                    for n in top
                ],
            }

            if best is None or cold_ms > best["cold_start_ms"]:
                best = result

    return best
