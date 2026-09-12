"""Small public CLI; all commands use the same REST API contract."""
from __future__ import annotations

import argparse
import json
import os
import urllib.request


def _base() -> str:
    return os.environ.get("MEGABRAIN_API_URL", f"http://{os.environ.get('MEGABRAIN_HOST','127.0.0.1')}:{os.environ.get('MEGABRAIN_PORT','4300')}")


def _get(path: str):
    req = urllib.request.Request(_base() + path)
    token = os.environ.get("MEGABRAIN_API_TOKEN")
    if token: req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=10) as r: return json.load(r)


def main(argv=None):
    p = argparse.ArgumentParser(prog="megabrain")
    sub = p.add_subparsers(dest="command", required=True)
    for name, path in (("health", "/health"), ("status", "/version"), ("doctor", "/health"), ("projects", "/v1/projects")):
        sub.add_parser(name).set_defaults(path=path)
    sub.add_parser("migrate").set_defaults(path=None)
    args = p.parse_args(argv)
    if args.command == "migrate":
        from scripts.migrate import main as migrate
        return migrate()
    print(json.dumps(_get(args.path), ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__": raise SystemExit(main())
