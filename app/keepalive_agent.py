"""
app/keepalive_agent.py
======================
Self-pinging keep-alive agent that runs as a daemon thread inside the
FastAPI lifespan.

Why it exists
-------------
Render free-tier web services spin down after ~15 minutes with no *inbound*
HTTP traffic.  The internal poll loop is invisible to Render, so the service
looks idle even while it is working.  Sending an inbound HTTP request every
14 minutes keeps Render convinced the service is active.

How it works
------------
* A daemon thread wakes up every CYCLE_MINUTES and sends PINGS_PER_CYCLE GET
  requests to BASE_URL (i.e. the service itself).
* A threading.Event is used to stop the thread cleanly when the FastAPI
  lifespan exits.
* All activity is written to the standard application logger.

Usage (wired into lifespan automatically)
-----------------------------------------
    from app.keepalive_agent import KeepAliveAgent

    agent = KeepAliveAgent(base_url="https://jobie-gu7t.onrender.com/")
    agent.start()   # call inside lifespan startup
    # ...
    agent.stop()    # call inside lifespan shutdown
"""

import logging
import threading
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────
CYCLE_MINUTES   = 14     # minutes between ping bursts
PINGS_PER_CYCLE = 2      # pings per burst
REQUEST_TIMEOUT = 30     # seconds per request
PING_GAP_SEC    = 2      # gap between pings in the same burst
RETRY_DELAY_SEC = 5      # wait before retrying a failed ping
# ─────────────────────────────────────────────────────────────────────────────


class KeepAliveAgent:
    """Sends periodic HTTP GET requests to *base_url* to prevent spin-down."""

    def __init__(self, base_url: str) -> None:
        self.base_url  = base_url.rstrip("/") + "/"
        self._stop_evt = threading.Event()
        self._thread   = threading.Thread(
            target=self._run,
            name="keepalive-agent",
            daemon=True,   # dies automatically when the main process exits
        )

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background pinger thread."""
        logger.info(
            "[KeepAliveAgent] started – pinging %s every %d min (%d pings/cycle)",
            self.base_url, CYCLE_MINUTES, PINGS_PER_CYCLE,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to stop and wait for it to exit."""
        logger.info("[KeepAliveAgent] stopping …")
        self._stop_evt.set()
        self._thread.join(timeout=10)
        logger.info("[KeepAliveAgent] stopped")

    # ── Internal ───────────────────────────────────────────────────────────

    def _ping(self, attempt: int) -> bool:
        """Send one GET request.  Returns True if the server replied at all."""
        try:
            req = urllib.request.Request(
                self.base_url,
                headers={"User-Agent": "Jobie-KeepAlive-Agent/1.0"},
            )
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                logger.info("[KeepAliveAgent] ping #%d -> HTTP %s OK", attempt, resp.status)
            return True
        except urllib.error.HTTPError as exc:
            # Any HTTP response (404, 503, …) means the server is running.
            logger.info(
                "[KeepAliveAgent] ping #%d -> HTTP %s (server alive)", attempt, exc.code
            )
            return True
        except Exception as exc:
            logger.warning("[KeepAliveAgent] ping #%d FAILED: %s", attempt, exc)
            return False

    def _run_cycle(self, cycle: int) -> None:
        logger.info("[KeepAliveAgent] cycle #%d – sending %d pings", cycle, PINGS_PER_CYCLE)
        for i in range(1, PINGS_PER_CYCLE + 1):
            if self._stop_evt.is_set():
                return
            ok = self._ping(i)
            if not ok:
                logger.info("[KeepAliveAgent] retrying ping #%d in %ds", i, RETRY_DELAY_SEC)
                self._stop_evt.wait(timeout=RETRY_DELAY_SEC)
                if not self._stop_evt.is_set():
                    self._ping(i)
            if i < PINGS_PER_CYCLE:
                self._stop_evt.wait(timeout=PING_GAP_SEC)

    def _run(self) -> None:
        cycle = 0
        while not self._stop_evt.is_set():
            cycle += 1
            try:
                self._run_cycle(cycle)
            except Exception as exc:          # never let the thread die silently
                logger.exception("[KeepAliveAgent] unexpected error in cycle #%d: %s", cycle, exc)
            # Sleep for CYCLE_MINUTES, but wake up immediately if stop() is called
            self._stop_evt.wait(timeout=CYCLE_MINUTES * 60)
        logger.info("[KeepAliveAgent] thread exiting")
