import os
import asyncio
import logging
from typing import Tuple, List, Optional

import websockets
from websockets.server import WebSocketServerProtocol

# your existing stable handler
from main import twilio_handler

logging.basicConfig(level=logging.INFO)

# --- HTTP handling for Render health checks ---
async def process_request(path: str, request_headers) -> Optional[Tuple[int, List[tuple], bytes]]:
    # Simple healthcheck
    if path == "/health":
        body = b"ok"
        return 200, [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))], body

    # Log non-WS HTTP probes for debugging; return None to allow WS upgrades elsewhere
    # (Returning None lets websockets perform a proper WS upgrade when present.)
    upgrade = request_headers.get("Upgrade", "")
    if not upgrade:
        # Plain HTTP request to a non-health path; return a small 404 instead of 400/426
        body = b"not found"
        return 404, [("Content-Type", "text/plain"), ("Content-Length", str(len(body)))], body

    return None  # proceed with websocket handshake

# --- Optional: log handshake details before your handler runs ---
async def on_connect(ws: WebSocketServerProtocol, path: str):
    logging.info(
        "WS handshake accepted: path=%s, peer=%s, subprotocol=%s",
        path, ws.remote_address, ws.subprotocol
    )
    await twilio_handler(ws, path)

async def run():
    port = int(os.getenv("PORT", "5000"))
    host = "0.0.0.0"
    logging.info(f"Binding WebSocket server on {host}:{port}")

    # Accept Twilio Media Streams subprotocol; add others you expect
    server = await websockets.serve(
        on_connect,
        host=host,
        port=port,
        process_request=process_request,
        subprotocols=["audio.twilio.com"],  # <<< important
        max_size=16 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
    )

    await server.wait_closed()

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass

