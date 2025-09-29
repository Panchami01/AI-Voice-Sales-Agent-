import asyncio
import base64
import json
import logging
import os
from urllib.parse import urlparse

import websockets
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

# -------------------- Deepgram connection --------------------
def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPGRAM_API_KEY not found")
    # Deepgram Agent expects subprotocols ["token", <API_KEY>]
    return websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key],
    )

def load_config():
    with open("config.json", "r") as f:
        return json.load(f)

# -------------------- Twilio <-> Agent helpers --------------------
async def handle_barge_in(decoded, twilio_ws, streamsid):
    try:
        if decoded.get("type") == "UserStartedSpeaking":
            await twilio_ws.send(json.dumps({"event": "clear", "streamSid": streamsid}))
    except Exception:
        logging.exception("handle_barge_in failed")

async def handle_text_message(decoded, twilio_ws, sts_ws, streamsid):
    await handle_barge_in(decoded, twilio_ws, streamsid)
    # place function-calling or message handling here if needed

async def sts_sender(sts_ws, audio_queue: asyncio.Queue):
    logging.info("sts_sender started")
    while True:
        chunk = await audio_queue.get()
        try:
            await sts_ws.send(chunk)
        except Exception:
            logging.exception("sts_sender send failed")
            break

async def sts_receiver(sts_ws, twilio_ws, streamsid_queue: asyncio.Queue):
    logging.info("sts_receiver started")
    streamsid = await streamsid_queue.get()  # wait for Twilio streamSid
    async for message in sts_ws:
        try:
            if isinstance(message, str):
                decoded = json.loads(message)
                await handle_text_message(decoded, twilio_ws, sts_ws, streamsid)
                continue
            # Binary presumed to be mulaw @ 8k (per your config)
            raw_mulaw = message
            media_message = {
                "event": "media",
                "streamSid": streamsid,
                "media": {"payload": base64.b64encode(raw_mulaw).decode("ascii")},
            }
            await twilio_ws.send(json.dumps(media_message))
        except Exception:
            logging.exception("sts_receiver failed")
            break

async def twilio_receiver(twilio_ws, sts_ws, audio_queue: asyncio.Queue, streamsid_queue: asyncio.Queue):
    BUFFER_SIZE = 20 * 160  # 20ms frames @ 8k mulaw (160 bytes)
    inbuffer = bytearray()
    async for message in twilio_ws:
        try:
            data = json.loads(message)
            event = data.get("event")

            if event == "start":
                streamsid = data.get("start", {}).get("streamSid")
                if streamsid:
                    streamsid_queue.put_nowait(streamsid)
                logging.info("Twilio stream started: %s", streamsid)

            elif event == "connected":
                continue

            elif event == "media":
                media = data.get("media", {})
                b64 = media.get("payload")
                if not b64:
                    continue
                chunk = base64.b64decode(b64)
                # Twilio track may be "inbound" or "inbound_audio"
                if media.get("track") in ("inbound", "inbound_audio"):
                    inbuffer.extend(chunk)

            elif event == "stop":
                logging.info("Twilio stream stopped")
                break

            while len(inbuffer) >= BUFFER_SIZE:
                frame = inbuffer[:BUFFER_SIZE]
                audio_queue.put_nowait(frame)
                del inbuffer[:BUFFER_SIZE]

        except Exception:
            logging.exception("twilio_receiver crashed")
            break

# -------------------- Render health check & WS upgrade separation --------------------
async def _http_response(status: int, body: str, content_type="text/plain; charset=utf-8"):
    return (status, [("Content-Type", content_type)], body.encode("utf-8"))

# Only serve /health to plain-HTTP; let WS upgrades proceed untouched
async def process_request(path, request_headers):
    upgrade = request_headers.get("Upgrade", "").lower()
    if upgrade == "websocket":
        logging.info(
            "WS upgrade attempt path=%s subprotocols=%s",
            path,
            request_headers.get("Sec-WebSocket-Protocol"),
        )
        return None  # proceed with WS handshake
    if path == "/health":
        return await _http_response(200, "OK")
    return await _http_response(404, "Not Found")

# Allowed *path components* (ignore querystrings)
ALLOWED_WS_PATHS = {"/twilio", "/ws", "/"}

def _is_allowed_ws_path(request_path: str) -> bool:
    # urlparse to strip querystrings like /twilio?foo=bar
    pure_path = urlparse(request_path).path or "/"
    return pure_path in ALLOWED_WS_PATHS

# ✅ Handler compatible with both (websocket) and (websocket, path)
async def twilio_handler(twilio_ws, path=None, *args):
    if path is None:
        # Newer websockets passes only (websocket); path is on the object
        path = getattr(twilio_ws, "path", "/")

    if not _is_allowed_ws_path(path):
        logging.warning("Rejecting unexpected WS path: %s", path)
        try:
            await twilio_ws.close(code=1008, reason="Invalid path")
        finally:
            return

    logging.info("WS connected path=%s negotiated_subprotocol=%s", path, twilio_ws.subprotocol)

    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()

    try:
        async with sts_connect() as sts_ws:
            await sts_ws.send(json.dumps(load_config()))
            t1 = asyncio.create_task(sts_sender(sts_ws, audio_queue))
            t2 = asyncio.create_task(sts_receiver(sts_ws, twilio_ws, streamsid_queue))
            t3 = asyncio.create_task(twilio_receiver(twilio_ws, sts_ws, audio_queue, streamsid_queue))
            await asyncio.wait({t1, t2, t3}, return_when=asyncio.FIRST_EXCEPTION)
    except Exception:
        logging.exception("twilio_handler failure")
    finally:
        try:
            await twilio_ws.close()
        except Exception:
            pass

# -------------------- Server bootstrap (Render-ready) --------------------
async def main():
    port = int(os.environ.get("PORT", 5000))

    # Subprotocols: Twilio requires "audio".
    # Keep the list minimal to guarantee selection when offered.
    advertised_subprotocols = ["audio"]

    # Keepalive pings to avoid idle disconnects on PaaS
    ping_interval = 20  # seconds
    ping_timeout = 20   # seconds

    server = await websockets.serve(
        twilio_handler,
        host="0.0.0.0",
        port=port,
        process_request=process_request,      # /health for HTTP; None for upgrades
        subprotocols=advertised_subprotocols, # negotiate Twilio cleanly
        max_size=8 * 1024 * 1024,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
    )
    logging.info("Started server on 0.0.0.0:%s", port)
    await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())






















