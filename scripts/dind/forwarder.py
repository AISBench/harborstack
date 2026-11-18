#!/usr/bin/env python3
"""Simple TCP port forwarder used by the dind path.

Usage:
    forwarder.py <listen_host> <target_host> <port> [port ...]

Listens on listen_host:<port> and forwards each connection to
target_host:<same port>.  Two instances are used:

  * inside the dind network namespace:
        forwarder.py <dind-ip> 127.0.0.1 18930 18900 ...
  * on the host loopback:
        forwarder.py 127.0.0.1 <dind-ip> 18930 18900 ...
"""

import asyncio
import sys


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle(local_reader, local_writer, target_host, port):
    try:
        remote_reader, remote_writer = await asyncio.open_connection(target_host, port)
    except Exception:
        try:
            local_writer.close()
        except Exception:
            pass
        return
    await asyncio.gather(
        pipe(local_reader, remote_writer),
        pipe(remote_reader, local_writer),
    )


async def main():
    if len(sys.argv) < 4:
        print(__doc__.strip(), file=sys.stderr)
        raise SystemExit(2)
    listen_host = sys.argv[1]
    target_host = sys.argv[2]
    ports = [int(x) for x in sys.argv[3:]]
    servers = []
    for port in ports:
        server = await asyncio.start_server(
            lambda r, w, p=port: handle(r, w, target_host, p),
            listen_host,
            port,
        )
        servers.append(server)
    print(f"forwarding {listen_host} -> {target_host}: {ports}", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
