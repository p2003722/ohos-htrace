"""
ohos_htrace — OpenHarmony htrace file parser & cold-start analyzer
"""
__version__ = "0.1.0"

from .parser import HtraceParser
from .db_builder import build_memory_db
from .analyzer import (
    analyze_cold_start,
    find_matching_processes,
    get_process_callstack,
    compute_cold_start_time,
    compute_keyword_durations,
    get_frame_slices,
)

import os
import sqlite3


def parse_to_memory_db(filepath: str) -> sqlite3.Connection:
    """
    Parse a binary .htrace file into an in-memory SQLite database.

    The database schema mirrors the output of the C++ trace_streamer tool,
    containing tables: process, thread, callstack, frame_slice.

    Args:
        filepath: Path to the .htrace file.

    Returns:
        An in-memory sqlite3.Connection with the parsed data.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file has an invalid header.
    """
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"htrace file not found: {filepath}")

    parser = HtraceParser(filepath)
    events = parser.parse()
    conn = build_memory_db(events)
    return conn


def parse_and_analyze(filepath: str, package_name: str,
                      keywords: list[str] | None = None) -> dict:
    """
    One-shot API: parse an htrace file and analyze cold start for a package.

    Args:
        filepath: Path to .htrace file.
        package_name: Package name keyword (e.g. 'aweme', 'com.example.app').
        keywords: Optional list of function name keywords to measure durations.

    Returns:
        Dict with keys: cold_start_ms, process_name, pid, gaps, keyword_durations,
        top_functions, node_count, depth0_count.
        Returns None if no cold start is detected.
    """
    conn = parse_to_memory_db(filepath)
    try:
        return analyze_cold_start(conn, package_name, keywords=keywords)
    finally:
        conn.close()
