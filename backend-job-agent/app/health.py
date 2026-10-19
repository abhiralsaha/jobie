import asyncio
import time

STATE = {"last_cycle": 0.0, "cycles": 0, "alerts": 0}


async def _handle(reader, writer):
    try:
        await reader.read(1024)
        age = int(time.time() - STATE["last_cycle"]) if STATE["last_cycle"] else -1
        body = f"ok cycles={STATE['cycles']} alerts={STATE['alerts']} last_cycle_age_s={age}\n"
        writer.write(("HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                      f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}").encode())
        await writer.drain()
    finally:
        writer.close()


async def start(port: int):
    return await asyncio.start_server(_handle, "0.0.0.0", port)
