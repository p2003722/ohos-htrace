# ohos-htrace

> Pure-Python parser & cold-start analyzer for OpenHarmony `.htrace` trace files.

**No external binaries needed** — replaces the C++ `trace_streamer` tool with a native Python implementation that parses `.htrace` protobuf data directly into an in-memory SQLite database.

---

## ✨ Features

- 🚀 **Pure Python** — no `streamer.exe` dependency, works on any platform
- ⚡ **Fast** — parses 120MB htrace in ~3 seconds
- 🎯 **Cold-start analysis** — automatic detection of app launch timing
- 📊 **Keyword breakdown** — measure time spent in specific functions
- 🔧 **CLI & API** — use from command line or import in your Python code
- 📦 **pip installable** — `pip install ohos-htrace`

## 📦 Installation

```bash
pip install ohos-htrace
```

Or install from source:

```bash
git clone https://github.com/p2003722/ohos-htrace.git
cd ohos-htrace
pip install -e .
```

## 🚀 Quick Start

### Command Line

```bash
# Basic cold-start analysis
ohos-htrace trace.htrace com.example.app

# With keyword duration breakdown
ohos-htrace trace.htrace aweme -k AppSpawn,LaunchAbility,LoadModule

# JSON output (for scripting)
ohos-htrace trace.htrace aweme --json

# Verbose output with top-10 functions
ohos-htrace trace.htrace aweme -k AppSpawn,LaunchAbility -v
```

**Example output:**

```
Parsing trace.htrace (118.7 MB) ...
Done in 3.6s

  Process:       ss.hm.ugc.aweme (pid=4575)
  Cold Start:    2566.08 ms
  Callstack:     69,933 nodes
  Depth-0:       16,916 blocks
  Max Gap:       27.9 ms
  Frames:        0

  Keyword Durations:
    AppSpawn: 19.12ms (5 hits)
    LaunchAbility: 3175.94ms (2 hits)
    LoadModule: 1164.25ms (3 hits)
```

### Python API

```python
import ohos_htrace

# One-shot analysis
result = ohos_htrace.parse_and_analyze(
    "trace.htrace",
    "aweme",
    keywords=["AppSpawn", "LaunchAbility", "LoadModule"],
)

print(f"Cold start: {result['cold_start_ms']:.2f}ms")
print(f"Max gap: {result['max_gap_ms']:.1f}ms")

for kw, info in result["keyword_durations"]["by_keyword"].items():
    print(f"  {kw}: {info['total_ms']:.2f}ms ({info['count']} hits)")
```

### Low-Level API

```python
import ohos_htrace

# Step 1: Parse htrace → in-memory SQLite
conn = ohos_htrace.parse_to_memory_db("trace.htrace")

# Step 2: Query like a normal database
cur = conn.cursor()
cur.execute("SELECT name, pid FROM process")
for name, pid in cur.fetchall():
    print(f"  {name} (pid={pid})")

# Step 3: Use analysis functions
procs = ohos_htrace.find_matching_processes(conn, "aweme")
for proc in procs:
    tree = ohos_htrace.get_process_callstack(conn, proc["id"])
    cold_ms, gaps = ohos_htrace.compute_cold_start_time(tree)
    print(f"  {proc['name']}: {cold_ms:.2f}ms")

conn.close()
```

## 📐 Database Schema

The in-memory database mirrors the C++ `trace_streamer` output:

| Table | Columns | Description |
|-------|---------|-------------|
| `process` | id, name, pid | Process list |
| `thread` | id, ipid, name, tid | Thread list (ipid → process.id) |
| `callstack` | id, callid, parent_id, name, depth, ts, dur | Function call stack (callid → thread.id) |
| `frame_slice` | id, vsync, ts, dur, type_desc, flag, ipid | Frame rendering data |

## 🔍 How It Works

```
.htrace file
    │
    ├─ 1024-byte Header (magic: OHOSPROF)
    │
    └─ Segments: [4-byte length][protobuf payload]
         │
         └─ ProfilerPluginData
              │
              └─ ftrace-plugin → TracePluginResult
                   │
                   ├─ ftrace_cpu_detail[].event[]
                   │    ├─ print_format (B|E|S|F events → callstack)
                   │    ├─ task_rename_format (→ process names)
                   │    └─ sched_switch_format (→ thread names)
                   │
                   └─ comm_dict[] (→ thread name resolution)
```

**Key implementation details:**

- **tgid extraction from buf** — Uses the tgid from `B|<tgid>|<name>` (not `event.tgid`), matching C++ streamer behavior. This is critical for correct process attribution after `fork()`.
- **Event sorting** — Events from different CPU cores are sorted by `(tgid, pid, ts)` before B/E stack matching, preventing cross-core timing artifacts.
- **Async events** — Supports `S|F` (async start/finish) events with cookie-based pairing.

## 🆚 Comparison with trace_streamer (C++)

Tested on a 118.7MB htrace file:

| Metric | trace_streamer | ohos-htrace | Match |
|--------|---------------|-------------|-------|
| Cold Start | 2566.08 ms | 2566.08 ms | ✅ |
| Max Gap | 27.9 ms | 27.9 ms | ✅ |
| AppSpawn | 19.12ms (5) | 19.12ms (5) | ✅ |
| LaunchAbility | 3175.94ms (2) | 3175.94ms (2) | ✅ |
| JsAbilityStage | 1575.63ms (5) | 1575.63ms (5) | ✅ |
| LoadModule | 1164.25ms (3) | 1164.25ms (3) | ✅ |
| Parse Time | 4.6s | 3.6s | ⚡ faster |
| Callstack | 71,114 | 69,933 | ~98% (binder events) |

**Remaining differences:** binder transaction events (~1,181 entries) and frame_slice data (from `hidump-plugin`, not yet parsed). These do not affect cold-start analysis accuracy.

## 📋 Requirements

- Python ≥ 3.10
- protobuf ≥ 4.21.0

## 📄 License

Apache License 2.0
