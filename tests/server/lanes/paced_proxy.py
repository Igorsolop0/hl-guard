from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

ALLOWED = "api.hyperliquid-testnet.xyz:443"


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


class Proxy:
    def __init__(self, gap: float, log: Path) -> None:
        self.gap = gap
        self.log = log
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        method, target, _ = head.split(b"\r\n", 1)[0].decode().split(" ", 2)
        if method != "CONNECT" or target != ALLOWED:
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        async with self.lock:
            wait = self.next_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.next_at = time.monotonic() + self.gap
        with self.log.open("a") as f:
            f.write(f"{time.time():.3f} CONNECT {target}\n")
        host, port = target.rsplit(":", 1)
        up_reader, up_writer = await asyncio.open_connection(host, int(port))
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="HTTPS CONNECT proxy for live lanes. Tunnels only to the testnet API, "
        "at most one tunnel per --gap seconds, and logs '<unix time> CONNECT <host>' lines."
    )
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--gap", type=float, default=10.0)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    proxy = Proxy(args.gap, args.log)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", args.port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
