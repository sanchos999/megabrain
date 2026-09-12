"""MegaBrain configuration. Environment > config file > defaults."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "megabrain.json"

DEFAULTS = {
    "bind_host": "127.0.0.1",
    "bind_port": 4300,
    "api_token": "",                      # empty = auth disabled (loopback dev); set for real use
    "postgres_dsn": "postgresql://megabrain@127.0.0.1:5432/megabrain",
    "redis_url": "redis://127.0.0.1:6390/0",
    "redis_prefix": "mb:",
    "blob_dir": str(ROOT / "state" / "blobs"),
    "blob_inline_limit": 8192,            # payloads larger than this go to blob store
    "token_estimate_chars_per_token": 4,
    "capsule_default_token_budget": 2000,
    "max_recent_events": 20,
    "resolver_recent_projects": 5,
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    # environment overrides (MB_ prefix, uppercase)

    aliases = {
        "MEGABRAIN_HOST": "bind_host", "MEGABRAIN_PORT": "bind_port",
        "MEGABRAIN_API_TOKEN": "api_token", "MEGABRAIN_DATABASE_URL": "postgres_dsn",
        "MEGABRAIN_REDIS_URL": "redis_url", "MEGABRAIN_DATA_DIR": "blob_dir",
    }
    for env, val in os.environ.items():
        if env in aliases:
            key = aliases[env]
            cfg[key] = int(val) if key == "bind_port" else val
            continue
        if not env.startswith("MB_"):
            continue
        key = env[3:].lower()
        if key in cfg:
            cur = cfg.get(key)
            if isinstance(cur, int) and not isinstance(cur, bool):
                cfg[key] = int(val)
            elif isinstance(cur, bool):
                cfg[key] = val.lower() in ("1", "true", "yes")
            else:
                cfg[key] = val
    # token file (systemd EnvironmentFile style) wins
    tokfile = os.environ.get("MB_API_TOKEN_FILE")
    if tokfile and Path(tokfile).exists():
        cfg["api_token"] = Path(tokfile).read_text().strip()
    return cfg
