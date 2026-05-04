"""
ohos_htrace.cli — Command-line interface for htrace analysis.

Usage:
    ohos-htrace <htrace_file> <package> [--keywords kw1,kw2,...] [--json]
"""
import argparse
import json
import sys
import time
import os


def main():
    parser = argparse.ArgumentParser(
        prog="ohos-htrace",
        description="OpenHarmony htrace parser & cold-start analyzer",
    )
    parser.add_argument("htrace_file", help="Path to .htrace file")
    parser.add_argument("package", help="Package name keyword (e.g. 'aweme')")
    parser.add_argument(
        "-k", "--keywords",
        default="",
        help="Comma-separated function-name keywords for duration analysis",
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Output results as JSON",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show verbose output including top functions",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.htrace_file):
        print(f"Error: file not found: {args.htrace_file}", file=sys.stderr)
        sys.exit(1)

    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]

    from . import parse_and_analyze

    file_mb = os.path.getsize(args.htrace_file) / 1024 / 1024

    if not args.as_json:
        print(f"Parsing {args.htrace_file} ({file_mb:.1f} MB) ...")

    t0 = time.time()
    result = parse_and_analyze(args.htrace_file, args.package, keywords or None)
    elapsed = time.time() - t0

    if result is None:
        if args.as_json:
            print(json.dumps({"error": "No cold start detected"}, indent=2))
        else:
            print(f"No cold start detected for '{args.package}'")
        sys.exit(1)

    if args.as_json:
        # Serialize to JSON
        output = {
            "file": args.htrace_file,
            "package": args.package,
            "parse_time_s": round(elapsed, 2),
            **result,
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return

    # Pretty print
    print(f"Done in {elapsed:.1f}s\n")
    print(f"  Process:       {result['process_name']} (pid={result['pid']})")
    print(f"  Cold Start:    {result['cold_start_ms']:.2f} ms")
    print(f"  Callstack:     {result['node_count']:,} nodes")
    print(f"  Depth-0:       {result['depth0_count']} blocks")
    print(f"  Max Gap:       {result['max_gap_ms']:.1f} ms")
    print(f"  Frames:        {result['frame_count']}")

    if result.get("gaps"):
        print(f"  Gaps (>5ms):")
        for g in result["gaps"][:5]:
            print(f"    {g['dur_ms']:.1f}ms @ "
                  f"[{g['start_ms']:.1f}ms ~ {g['end_ms']:.1f}ms]")

    kw_dur = result.get("keyword_durations", {})
    if kw_dur and kw_dur.get("by_keyword"):
        print(f"\n  Keyword Durations:")
        for kw, info in kw_dur["by_keyword"].items():
            if info["count"] > 0:
                print(f"    {kw}: {info['total_ms']:.2f}ms "
                      f"({info['count']} hits)")

    if args.verbose and result.get("top_functions"):
        print(f"\n  Top-10 Functions:")
        for i, fn in enumerate(result["top_functions"], 1):
            print(f"    {i:2d}. {fn['dur_ms']:>8.2f}ms  "
                  f"d={fn['depth']}  {fn['name']}")

    print()


if __name__ == "__main__":
    main()
