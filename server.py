import asyncio
import logging
import os
from typing import Tuple, List

import websockets

# Import your existing websocket handler from main.py _without_ changing it.
# Assumes main.py defines `twilio_handler(websocket, path)` or similar.
from main import twilio_handler

logging.basicConfig(level=logging.INFO)

# Handle plain HTTP (Render health checks) on the SAME socket used by websockets
# via websockets.serve(process_request=...) hook.
# Returning a tuple -> (status_code, headers, body_bytes) serves a normal HTTP response.
async def process_request(path: str, request_headers) -> Tuple[int, List[Tuple[str, str]], bytes] | None:
    if path == "/health":
        body = b"ok"
        headers = [
            ("Content-Type", "text/plain; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ]
        return 200, headers, body
    # For any other non-WS plain HTTP request, return a minimal 404 so Render doesn't keep probing it.
    # WS upgrade requests will skip this and go through the websocket handshake.
    return None  # None -> continue with websocket upgrade if present (or default HTTP handling)

async def run():
    port = int(os.getenv("PORT", "5000"))
    host = "0.0.0.0"
    logging.info(f"Starting WebSocket server on {host}:{port}")

    server = await websockets.serve(
        twilio_handler,          # your existing handler from main.py
        host=host,
        port=port,
        process_request=process_request,  # adds /health on same port
        max_size=16 * 1024 * 1024,        # safe defaults
        ping_interval=20,
        ping_timeout=20,
    )

    await server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
