import asyncio
import base64
import json
import logging
import os
import pathlib
import websockets
from aiohttp import web
from dotenv import load_dotenv
import datetime as dt
import re

load_dotenv()
logging.basicConfig(level=logging.INFO)

# -------------------- Globals --------------------
CALL_META = {}
TRANSCRIPTS_DIR = pathlib.Path("transcripts"); TRANSCRIPTS_DIR.mkdir(exist_ok=True)
LOGS_DIR = pathlib.Path("logs"); LOGS_DIR.mkdir(exist_ok=True)

# -------------------- Deepgram connection --------------------
def sts_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPGRAM_API_KEY not found")
    return websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key],
    )

def load_config():
    with open("config.json", "r") as f:
        return json.load(f)

# -------------------- Helpers --------------------
def append_transcript(call_sid: str, line: str):
    if not call_sid or not line:
        return
    with (TRANSCRIPTS_DIR / f"{call_sid}.txt").open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")

SLOT_RX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
ALLOWED_HOURS = {10, 13, 16}
SAT_ALLOWED = {10, 12}
AEST_TZ = dt.timezone(dt.timedelta(hours=10))

def slot_ok(d: dt.datetime) -> bool:
    wd = d.weekday()
    return (wd <= 4 and d.hour in ALLOWED_HOURS) or (wd == 5 and d.hour in SAT_ALLOWED)

# -------------------- Bridge handlers --------------------
async def handle_barge_in(decoded, twilio_ws, streamsid):
    try:
        if decoded.get("type") == "UserStartedSpeaking":
            await twilio_ws.send(json.dumps({"event": "clear", "streamSid": streamsid}))
    except Exception:
        logging.exception("handle_barge_in failed")

async def handle_text_message(decoded, twilio_ws, sts_ws, streamsid, call_sid=None):
    await handle_barge_in(decoded, twilio_ws, streamsid)
    txt = decoded.get("text") or decoded.get("content")
    if txt and call_sid:
        append_transcript(call_sid, txt)

async def sts_sender(sts_ws, audio_queue: asyncio.Queue):
    while True:
        chunk = await audio_queue.get()
        try:
            await sts_ws.send(chunk)
        except Exception:
            logging.exception("sts_sender send failed")
            break

async def sts_receiver(sts_ws, twilio_ws, streamsid_queue: asyncio.Queue, call_sid: str):
    streamsid = await streamsid_queue.get()
    async for message in sts_ws:
        try:
            if isinstance(message, str):
                decoded = json.loads(message)
                await handle_text_message(decoded, twilio_ws, sts_ws, streamsid, call_sid)
                continue
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

async def twilio_receiver(twilio_ws, sts_ws, audio_queue: asyncio.Queue, streamsid_queue: asyncio.Queue, call_sid: str):
    BUFFER_SIZE = 160  # 20ms of 8k mulaw
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

            elif event == "media":
                media = data.get("media", {})
                b64 = media.get("payload")
                if not b64:
                    continue
                chunk = base64.b64decode(b64)
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

# -------------------- Twilio <-> Deepgram handler --------------------
async def twilio_handler(twilio_ws, path=None, *args):
    call_sid = None
    audio_queue = asyncio.Queue()
    streamsid_queue = asyncio.Queue()
    try:
        async with sts_connect() as sts_ws:
            await sts_ws.send(json.dumps(load_config()))
            t1 = asyncio.create_task(sts_sender(sts_ws, audio_queue))
            t2 = asyncio.create_task(sts_receiver(sts_ws, twilio_ws, streamsid_queue, call_sid))
            t3 = asyncio.create_task(twilio_receiver(twilio_ws, sts_ws, audio_queue, streamsid_queue, call_sid))
            await asyncio.wait({t1, t2, t3}, return_when=asyncio.FIRST_EXCEPTION)
    except Exception:
        logging.exception("twilio_handler failure")
    finally:
        try:
            await twilio_ws.close()
        except Exception:
            pass

# -------------------- HTTP Endpoints --------------------
async def book_appointment(request: web.Request):
    try:
        body = await request.json()
        for k in ("name","phone","email","slot_iso","mode"):
            if not body.get(k):
                return web.json_response({"ok":False,"message":f"missing {k}"}, status=400)
        slot_iso = body["slot_iso"]
        if not SLOT_RX.match(slot_iso):
            return web.json_response({"ok":False,"message":"bad slot_iso"}, status=400)
        d = dt.datetime.fromisoformat(slot_iso)
        if d <= dt.datetime.now(dt.timezone.utc).astimezone(d.tzinfo):
            return web.json_response({"ok":False,"message":"slot in past"}, status=400)
        if not slot_ok(d):
            return web.json_response({"ok":False,"message":"slot not in allowed windows"}, status=400)
        booking_id = f"RS-{d.strftime('%Y%m%d-%H%M')}"
        return web.json_response({
            "ok": True,
            "booking_id": booking_id,
            "status": "confirmed",
            "slot_iso": slot_iso,
            "mode": body["mode"]
        })
    except Exception as e:
        logging.exception("book_appointment error")
        return web.json_response({"ok":False,"message":str(e)}, status=500)

async def recording_webhook(request: web.Request):
    data = await request.post()
    call_sid = data.get("CallSid")
    rec = data.get("RecordingUrl")
    if call_sid and rec:
        CALL_META.setdefault(call_sid, {})["recording_url"] = rec + ".mp3"
        logging.info("Recording saved for %s", call_sid)
    return web.Response(text="ok")

async def latest_transcript(request: web.Request):
    call_sid = request.query.get("call_sid")
    if not call_sid:
        return web.json_response({"error":"missing call_sid"}, status=400)
    path = TRANSCRIPTS_DIR / f"{call_sid}.txt"
    if not path.exists():
        return web.json_response({"error":"no transcript"}, status=404)
    return web.FileResponse(path)

def make_app():
    app = web.Application()
    app.router.add_post("/book_appointment", book_appointment)
    app.router.add_post("/webhooks/recording", recording_webhook)
    app.router.add_get("/transcript", latest_transcript)
    return app

# -------------------- Entrypoint --------------------
async def main():
    # Start HTTP (for booking + recording)
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logging.info("HTTP server on :8080")

    # Start WebSocket bridge
    server = await websockets.serve(twilio_handler, host="0.0.0.0", port=5000)
    logging.info("WS server on :5000 (/twilio)")
    await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())





