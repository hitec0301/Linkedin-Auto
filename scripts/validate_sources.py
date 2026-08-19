#!/usr/bin/env python3
"""Check every configured feed and report what it returned.

Run this before you trust a curate run, and again any time you edit
config/sources.yaml. Exits non-zero if any enabled RSS feed returned nothing,
because a silently empty feed is indistinguishable from a quiet news week.

    python scripts/validate_sources.py
    python scripts/validate_sources.py --tier 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lnp.config import load_config, load_sources
from lnp.ingest import fetch_feed

OK = "OK   "
FAIL = "FAIL "
SKIP = "SKIP "


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", type=int, help="check only this tier")
    parser.add_argument(
        "--include-disabled", action="store_true", help="also check disabled feeds"
    )
    args = parser.parse_args()

    config = load_config()
    feeds = load_sources()
    timeout = int(config.get("ingest.request_timeout_seconds", 20))
    user_agent = config.get("drafting.user_agent", "lnp-pipeline/1.0")

    failures = []
    unverified = []
    print(f"{'STATUS':6} {'TIER':4} {'ENTRIES':>7}  SOURCE")
    print("-" * 78)

    for feed in feeds:
        if args.tier and feed["tier"] != args.tier:
            continue
        name = feed.get("name", feed.get("url"))
        if feed.get("ingest") != "rss":
            print(f"{SKIP} {feed['tier']:<4} {'-':>7}  {name}  (email-only source; route via Gmail label)")
            continue
        if not feed.get("enabled", True) and not args.include_disabled:
            print(f"{SKIP} {feed['tier']:<4} {'-':>7}  {name}  (disabled)")
            continue

        result = fetch_feed(feed, timeout, user_agent)
        flag = " [verify]" if feed.get("verify") else ""
        if result.ok:
            print(f"{OK} {feed['tier']:<4} {result.count:>7}  {name}{flag}")
            if feed.get("verify"):
                unverified.append(name)
        else:
            print(f"{FAIL} {feed['tier']:<4} {0:>7}  {name}{flag}\n{'':21}{result.error}")
            failures.append((name, feed.get("url", ""), result.error))

    print("-" * 78)
    if unverified:
        print(
            f"{len(unverified)} feed(s) marked `verify: true` responded successfully. "
            "Remove the flag in config/sources.yaml to confirm them:"
        )
        for name in unverified:
            print(f"  - {name}")
    if failures:
        print(f"\n{len(failures)} feed(s) returned nothing:")
        for name, url, error in failures:
            print(f"  - {name}: {url}\n      {error}")
        print(
            "\nFix the URL or set `enabled: false` in config/sources.yaml. "
            "Do not leave a dead feed configured — it hides a shrinking source list."
        )
        return 1
    print("All enabled feeds returned entries.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
