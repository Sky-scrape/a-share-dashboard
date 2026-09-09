"""Shared helpers for hithink-finance CLI fetchers."""
import json
import shutil
import subprocess
import time
import sys
from pathlib import Path

CLI = shutil.which("hithink-finance") or "hithink-finance"
PACE = 0.35          # seconds between remote calls
MAX_RETRY = 4

_last_call = [0.0]


def pace():
    elapsed = time.time() - _last_call[0]
    if elapsed < PACE:
        time.sleep(PACE - elapsed)


def run_cli(args, timeout=120):
    """Run CLI with pacing, retries and JSON envelope parsing.
    Returns dict envelope or raises RuntimeError."""
    last_err = None
    for attempt in range(MAX_RETRY):
        pace()
        _last_call[0] = time.time()
        try:
            proc = subprocess.run(
                [CLI] + args + ["--format", "json"],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            last_err = "timeout"
            time.sleep(2 * (attempt + 1))
            continue
        out = proc.stdout.strip()
        try:
            env = json.loads(out)
        except Exception:
            last_err = f"non-json out (rc={proc.returncode}): {out[:200]} {proc.stderr[:200]}"
            time.sleep(1.5 * (attempt + 1))
            continue
        if env.get("ok"):
            return env
        err = env.get("error", {}) or {}
        code = err.get("code", "")
        msg = err.get("message", "")
        retryable = err.get("retryable", False)
        last_err = f"{code}: {msg}"
        # validation errors: don't hammer
        if code in ("CLI_BAD_ARGUMENT", "VALIDATION") or not retryable and "argument" in msg.lower():
            raise RuntimeError(last_err)
        if code in ("AUTH_REQUIRED", "AUTH_INVALID", "FUYAO_1001"):
            raise RuntimeError("AUTH FAIL: " + last_err)
        # rate limit / upstream / network: backoff retry
        time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"failed after {MAX_RETRY} tries: {last_err}")


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ms_at(date_str, hour=0, minute=0):
    """Asia/Shanghai midnight (or given time) epoch ms for YYYY-MM-DD."""
    from datetime import datetime, timezone, timedelta
    tz = timezone(timedelta(hours=8))
    y, m, d = map(int, date_str.split("-"))
    return int(datetime(y, m, d, hour, minute, tzinfo=tz).timestamp() * 1000)


def trading_days(db_path, start, end):
    """Distinct trade dates from local kline DB between start and end (inclusive)."""
    import duckdb
    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT DISTINCT strftime(date,'%Y-%m-%d') d FROM raw_kline_daily "
        "WHERE date >= ? AND date <= ? ORDER BY 1", [start, end]).fetchall()
    con.close()
    return [r[0] for r in rows]
