"""Apply migrations in order. Idempotent (schema_migrations guard)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import psycopg

from core.config import load_config

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def apply_migrations(dsn: str):
    conn = psycopg.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, name TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        conn.commit()
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        for f in files:
            m = re.match(r"(\d+)_", f.name)
            if not m:
                continue
            version = int(m.group(1))
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM schema_migrations WHERE version=%s", (version,))
                if cur.fetchone():
                    continue
                cur.execute(f.read_text())
                cur.execute("INSERT INTO schema_migrations (version, name) VALUES (%s,%s)",
                            (version, f.name))
            conn.commit()
            print(f"applied {f.name}")
    finally:
        conn.close()


def main() -> int:
    cfg = load_config()
    apply_migrations(cfg["postgres_dsn"])
    print("migrations complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
