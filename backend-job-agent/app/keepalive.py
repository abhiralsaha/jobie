import asyncio
import logging
import time

import httpx

log = logging.getLogger("keepalive")
REQUESTS_PER_BATCH = 2
REQUEST_PAUSE_SECONDS = 2


class KeepAlive:
    def __init__(self, url: str, interval_seconds: int):
        self.url = url
        self.interval_seconds = interval_seconds
        self.client = httpx.AsyncClient(timeout=30, follow_redirects=True)

    async def _request(self, request_number: int) -> None:
        started = time.perf_counter()
        try:
            response = await self.client.get(
                self.url,
                headers={"User-Agent": "Jobie-Backend-Agent-KeepAlive/1.0"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning(
                "Keep-alive request failed | request=%d/%d url=%s duration=%.2fs error=%s",
                request_number,
                REQUESTS_PER_BATCH,
                self.url,
                time.perf_counter() - started,
                exc,
            )
        else:
            log.info(
                "Keep-alive request succeeded | request=%d/%d status=%d url=%s duration=%.2fs",
                request_number,
                REQUESTS_PER_BATCH,
                response.status_code,
                self.url,
                time.perf_counter() - started,
            )

    async def run(self, stop: asyncio.Event) -> None:
        log.info(
            "Keep-alive started | url=%s interval_seconds=%d requests_per_batch=%d",
            self.url,
            self.interval_seconds,
            REQUESTS_PER_BATCH,
        )
        try:
            while not stop.is_set():
                batch_started = time.perf_counter()
                log.info("Keep-alive batch started | url=%s", self.url)
                for request_number in range(1, REQUESTS_PER_BATCH + 1):
                    if stop.is_set():
                        break
                    await self._request(request_number)
                    if request_number < REQUESTS_PER_BATCH and not stop.is_set():
                        try:
                            await asyncio.wait_for(
                                stop.wait(), timeout=REQUEST_PAUSE_SECONDS
                            )
                        except asyncio.TimeoutError:
                            pass
                log.info(
                    "Keep-alive batch finished | duration=%.2fs next_in_seconds=%d",
                    time.perf_counter() - batch_started,
                    self.interval_seconds,
                )
                if not stop.is_set():
                    try:
                        await asyncio.wait_for(
                            stop.wait(), timeout=self.interval_seconds
                        )
                    except asyncio.TimeoutError:
                        pass
        finally:
            await self.client.aclose()
            log.info("Keep-alive stopped")
