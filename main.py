import asyncio
import base64
import json
import logging
import os
import websockets
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPGRAM_API_KEY not found")
    # Fixed URL
    return websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key],
    )

def load_config():
    with open("config.json", "r") as f:
        return json.load(f)

async def handle_barge_in(decoded, twilio_ws, streamsid):
    try:
        if decoded.get("type") == "UserStartedSpeaking":
            await twilio_ws.send(json.dumps({"event": "clear", "streamSid": streamsid}))
    except Exception:
        logging.exception("handle_barge_in failed")

async def handle_text_message(decoded, twilio_ws, sts_ws, streamsid):
    await handle_barge_in(decoded, twilio_ws, streamsid)
    # TODO: function calling if needed

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
            # Binary assumed to be mulaw @ 8k (per your config)
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
    BUFFER_SIZE = 20 * 160  # 20ms of 8k mulaw (160 bytes)
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
                # Twilio may send track as "inbound" or "inbound_audio"
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

# ✅ Handler compatible with both (websocket) and (websocket, path)
async def twilio_handler(twilio_ws, path=None, *args):
    if path is None:
        # Newer websockets passes only (websocket); path is on the object
        path = getattr(twilio_ws, "path", "/")
    if path != "/twilio":
        logging.warning("Unexpected WS path: %s", path)

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


# Add this helper anywhere above main()
async def _http_response(status: int, body: str, content_type="text/plain; charset=utf-8"):
    return (status, [("Content-Type", content_type)], body.encode("utf-8"))

# Only serve plain HTTP for non-upgrade requests to /health
async def process_request(path, request_headers):
    # If client is attempting a WebSocket upgrade, let websockets do the handshake
    upgrade = request_headers.get("Upgrade", "").lower()
    if upgrade == "websocket":
        return None  # proceed with WS handshake

    # Plain HTTP health-check only
    if path == "/health":
        return await _http_response(200, "OK")

    # For any other plain-HTTP path, send 404 (optional: 204)
    return await _http_response(404, "Not Found")

# OPTIONAL: if you want to require a specific WS path like "/twilio"
ALLOWED_WS_PATHS = {"/", "/twilio", "/ws"}  # include the one your client actually uses

async def main():
    port = int(os.getenv("PORT", "5000"))  # fallback to 5000 for local dev
    common_subprotocols = ["audio", "json", "twilio", "deepgram"]
    server = await websockets.serve(
        twilio_handler,
        host="0.0.0.0",
        port=port,
        process_request=process_request,  # /health for HTTP, None for upgrades
        subprotocols=common_subprotocols, # negotiate Twilio/others cleanly
        max_size=8 * 1024 * 1024          # optional: raise if you stream big frames
    )
    logging.info(f"Started server on 0.0.0.0:{port}")
    await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())



















