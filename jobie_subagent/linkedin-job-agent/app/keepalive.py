import asyncio
import logging

import httpx

log = logging.getLogger("keepalive")
REQUESTS_PER_BATCH = 2
REQUEST_PAUSE_SECONDS = 2


class KeepAlive:
    def __init__(self, url: str, interval_seconds: int):
        self.url = url
        self.interval_seconds = interval_seconds
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=True)

    async def _request(self, request_number: int):
        try:
            response = await self.client.get(self.url)
            response.raise_for_status()
        except httpx.HTTPError:
            log.exception("Keep-alive request %d/%d failed | url=%s",
                          request_number, REQUESTS_PER_BATCH, self.url)
        else:
            log.info("Keep-alive request %d/%d succeeded | status=%d url=%s",
                     request_number, REQUESTS_PER_BATCH, response.status_code, self.url)

    async def run(self, stop: asyncio.Event):
        log.info("Keep-alive started | url=%s interval=%ds requests_per_batch=%d",
                 self.url, self.interval_seconds, REQUESTS_PER_BATCH)
        try:
            while not stop.is_set():
                for request_number in range(1, REQUESTS_PER_BATCH + 1):
                    if stop.is_set():
                        break
                    await self._request(request_number)
                    if request_number < REQUESTS_PER_BATCH and not stop.is_set():
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=REQUEST_PAUSE_SECONDS)
                        except asyncio.TimeoutError:
                            pass

                if not stop.is_set():
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
                    except asyncio.TimeoutError:
                        pass
        finally:
            await self.client.aclose()
