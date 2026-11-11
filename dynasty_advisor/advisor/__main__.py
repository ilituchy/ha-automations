"""CLI: ``python3 -m advisor [--league ID] [--username NAME] [--strategy S]``.

League id and username come from flags or from a config file kept OUTSIDE the
repo (default ``~/.config/dynasty_advisor/config.json``):

    {"username": "...", "league_id": "...", "strategy": "balanced"}

This repo is public; league ids identify a real person's team and stay out of it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .run import RunError, run_once
from .trades import STRATEGIES

DEFAULT_CONFIG = os.path.expanduser("~/.config/dynasty_advisor/config.json")
DEFAULT_DATA = os.path.expanduser("~/.local/share/dynasty_advisor")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="advisor")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--username")
    ap.add_argument("--league")
    ap.add_argument("--strategy", choices=sorted(STRATEGIES) + ["auto"],
                    help="auto (default) follows your own team's mode: contender -> contend, seller -> rebuild")
    ap.add_argument("--data-dir", default=DEFAULT_DATA)
    ap.add_argument("--offline", action="store_true", help="use cached responses only")
    ap.add_argument("--trades", type=int, default=10)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = {}
    if os.path.exists(args.config):
        with open(args.config) as f:
            cfg = json.load(f)
    username = args.username or cfg.get("username")
    league_id = args.league or cfg.get("league_id")
    strategy = args.strategy or cfg.get("strategy") or "auto"
    if not username:
        ap.error("need --username or a config file")

    try:
        res = run_once(username, league_id, strategy, args.data_dir, offline=args.offline, n_trades=args.trades)
    except RunError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(res.markdown)
    print("report: %s\njournal: %s\n(%d HTTP calls, %.1fs, %d packages evaluated)" % (
        res.report_path, res.journal_path, res.calls, res.seconds, res.evaluated), file=sys.stderr)
    return 0

if __name__ == "__main__":
    sys.exit(main())
