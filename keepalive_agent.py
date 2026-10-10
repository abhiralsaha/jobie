"""
keepalive_agent.py
==================
Autonomous keep-alive agent for https://jobie-gu7t.onrender.com/

Behaviour
---------
* Sends 2 HTTP GET requests to the target URL every 14 minutes.
* Logs every ping (success / failure) with a timestamp to keepalive.log.
* Runs forever until you press Ctrl+C or stop the process.
* Retries a failed ping once after 5 seconds before moving on.

Usage
-----
    python keepalive_agent.py

To run silently in the background on Windows:
    pythonw keepalive_agent.py        (uses pythonw.exe - no console window)

Or use the supplied run_keepalive.bat for a one-click start.
"""

import time
import datetime
import urllib.request
import urllib.error

# ── Configuration ──────────────────────────────────────────────────────────────
TARGET_URL      = "https://jobie-gu7t.onrender.com/"
PINGS_PER_CYCLE = 2          # how many requests to send each cycle
CYCLE_MINUTES   = 14         # how often to run a cycle (minutes)
RETRY_DELAY_SEC = 5          # wait before retrying a failed ping
LOG_FILE        = "keepalive.log"
REQUEST_TIMEOUT = 30         # seconds to wait for a response
# ──────────────────────────────────────────────────────────────────────────────


def _now() -> str:
    """Return current local time as a readable string."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(message: str) -> None:
    """Print to console AND append to log file."""
    line = f"[{_now()}] {message}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _ping(url: str, attempt: int) -> bool:
    """
    Send a single GET request to *url*.
    Returns True on success (any HTTP response), False on network error.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Jobie-KeepAlive-Agent/1.0"},
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            status = resp.status
        _log(f"  Ping #{attempt} -> HTTP {status}  OK")
        return True
    except urllib.error.HTTPError as exc:
        _log(f"  Ping #{attempt} -> HTTP {exc.code} (server replied, still alive)")
        return True
    except Exception as exc:
        _log(f"  Ping #{attempt} -> FAILED: {exc}")
        return False


def run_cycle(cycle_number: int) -> None:
    """Send PINGS_PER_CYCLE pings, retrying once on failure."""
    _log(f"-- Cycle #{cycle_number} starting ({PINGS_PER_CYCLE} pings) --")
    for i in range(1, PINGS_PER_CYCLE + 1):
        ok = _ping(TARGET_URL, i)
        if not ok:
            _log(f"  Retrying ping #{i} in {RETRY_DELAY_SEC}s ...")
            time.sleep(RETRY_DELAY_SEC)
            _ping(TARGET_URL, i)
        if i < PINGS_PER_CYCLE:
            time.sleep(2)
    _log(f"-- Cycle #{cycle_number} done. Next cycle in {CYCLE_MINUTES} min --\n")


def main() -> None:
    _log("=" * 60)
    _log("Jobie Keep-Alive Agent started")
    _log(f"  Target  : {TARGET_URL}")
    _log(f"  Schedule: {PINGS_PER_CYCLE} pings every {CYCLE_MINUTES} minutes")
    _log(f"  Log file: {LOG_FILE}")
    _log("=" * 60 + "\n")

    cycle = 0
    while True:
        cycle += 1
        run_cycle(cycle)
        time.sleep(CYCLE_MINUTES * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("\nAgent stopped by user (Ctrl+C). Goodbye!")
