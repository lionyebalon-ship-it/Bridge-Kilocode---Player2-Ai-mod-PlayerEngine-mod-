"""Live terminal dashboard for the p2k-bridge operational endpoints.

Polls ``GET {url}/bridge/stats`` (and ``GET {url}/bridge/healthz`` for the
banner) over plain HTTP every ``--interval`` seconds and redraws a
human-readable dashboard in the terminal. No API key is required and only the
standard library is used.

    python3 bridge/monitor.py [--url http://127.0.0.1:9800] [--interval 1.0] [--once]
"""

import argparse
import json
import signal
import sys
import time
import urllib.error
import urllib.request

_CLEAR = "\033[2J\033[H"


def _get_json(url: str, timeout: float):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class _BridgeUnreachable(Exception):
    """Raised when /bridge/stats cannot be fetched."""


def _collect(base_url: str, timeout: float = 5.0):
    """Return (stats, healthz). Stats is required; healthz is best-effort."""
    try:
        stats = _get_json(f"{base_url}/bridge/stats", timeout)
        if not isinstance(stats, dict):
            raise _BridgeUnreachable()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise _BridgeUnreachable() from exc
    healthz: dict = {}
    try:
        payload = _get_json(f"{base_url}/bridge/healthz", timeout)
        if isinstance(payload, dict):
            healthz = payload
    except (urllib.error.URLError, OSError, ValueError):
        healthz = {}
    return stats, healthz


def _human_bytes(value) -> str:
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def _format_uptime(seconds) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "?"
    total = max(0, total)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _refill_label(refill_per_sec) -> str:
    try:
        rate = float(refill_per_sec)
    except (TypeError, ValueError):
        rate = 0.0
    if rate > 0:
        return f"1/{round(1.0 / rate, 1)}s"
    return "n/a"


def _recent_lines(recent) -> list:
    headers = ("TIME", "STATUS", "IN", "OUT", "TOT", "MS", "PREVIEW")
    rows = []
    if isinstance(recent, list):
        for entry in recent[:10]:
            if not isinstance(entry, dict):
                continue
            try:
                time_cell = time.strftime("%H:%M:%S", time.localtime(float(entry.get("ts"))))
            except (TypeError, ValueError):
                time_cell = "--:--:--"
            rows.append(
                (
                    time_cell,
                    str(entry.get("status", "?")),
                    str(entry.get("prompt_tokens", 0)),
                    str(entry.get("completion_tokens", 0)),
                    str(entry.get("total_tokens", 0)),
                    str(entry.get("duration_ms", 0)),
                    str(entry.get("preview", ""))[:40],
                )
            )
    if not rows:
        return ["  (no requests yet)"]
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]

    def render(cells) -> str:
        parts = [
            cells[i].rjust(widths[i]) if i in (2, 3, 4, 5) else cells[i].ljust(widths[i])
            for i in range(len(headers))
        ]
        return "  " + "  ".join(parts).rstrip()

    return [render(headers)] + [render(row) for row in rows]


def _format_dashboard(base_url: str, stats: dict, healthz: dict) -> str:
    requests = stats.get("requests") or {}
    budget = stats.get("token_budget") or {}
    recent = stats.get("recent_requests") or []

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    uptime = _format_uptime(stats.get("uptime_seconds"))
    model = healthz.get("model") or "?"

    lines = [
        f"p2k-bridge monitor   {base_url}",
        f"{now}   uptime {uptime}   model {model}",
        "",
        "REQUESTS: {} remaining this hour (limit {}/hr, refills {})".format(
            requests.get("available", 0),
            requests.get("limit_per_hour", 0),
            _refill_label(requests.get("refill_per_sec")),
        ),
        "",
        "COUNTERS",
        "  chat_requests {}   chat_ok {}   chat_failed {}".format(
            stats.get("chat_requests", 0),
            stats.get("chat_ok", 0),
            stats.get("chat_failed", 0),
        ),
        "  rate_limited {}   kilo_errors {}".format(
            stats.get("rate_limited", 0),
            stats.get("kilo_errors", 0),
        ),
        "  bytes  up {}   down {}   combined {}".format(
            _human_bytes(stats.get("bytes_up", 0)),
            _human_bytes(stats.get("bytes_down", 0)),
            _human_bytes((stats.get("bytes_up", 0) or 0) + (stats.get("bytes_down", 0) or 0)),
        ),
        "",
        "TOKENS",
    ]

    tokens_per_window = budget.get("tokens_per_window", 0)
    if tokens_per_window == 0:
        note = "(budget disabled \u2014 tracking only)"
    else:
        note = f"(enforced, remaining {budget.get('remaining', 0)})"
    lines.append(
        "  window usage {}   lifetime {} (prompt {} / completion {})   {}".format(
            budget.get("window_used", 0),
            budget.get("lifetime_total", 0),
            budget.get("lifetime_prompt", 0),
            budget.get("lifetime_completion", 0),
            note,
        )
    )
    lines.append("")
    lines.append("RECENT REQUESTS")
    lines.extend(_recent_lines(recent))
    return "\n".join(lines)


def _run_once(base_url: str) -> int:
    try:
        stats, healthz = _collect(base_url)
    except _BridgeUnreachable:
        print(f"bridge unreachable at {base_url} \u2014 is it running?")
        return 1
    print(_format_dashboard(base_url, stats, healthz))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="monitor.py",
        description="Live terminal dashboard for p2k-bridge (/bridge/stats, /bridge/healthz).",
    )
    parser.add_argument("--url", default="http://127.0.0.1:9800", help="bridge base URL (default: http://127.0.0.1:9800)")
    parser.add_argument("--interval", type=float, default=1.0, help="poll interval in seconds (default: 1.0)")
    parser.add_argument("--once", action="store_true", help="fetch and print a single snapshot, then exit")
    args = parser.parse_args(argv)

    base_url = args.url.rstrip("/")
    interval = args.interval if args.interval and args.interval > 0 else 1.0

    if args.once or not sys.stdout.isatty():
        return _run_once(base_url)

    stop = {"now": False}

    def _on_signal(signum, frame):
        stop["now"] = True

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    try:
        while not stop["now"]:
            try:
                stats, healthz = _collect(base_url)
                body = _format_dashboard(base_url, stats, healthz)
            except _BridgeUnreachable:
                body = f"bridge unreachable at {base_url} \u2014 is it running?"
            sys.stdout.write(_CLEAR)
            sys.stdout.write(body + "\n")
            sys.stdout.flush()
            slept = 0.0
            while slept < interval and not stop["now"]:
                slice_ = min(0.1, interval - slept)
                time.sleep(slice_)
                slept += slice_
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
