import asyncio
import base64
import datetime as dt
import json
import logging
import os
import pathlib
import re

from aiohttp import web, WSMsgType
import websockets
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
if not DEEPGRAM_API_KEY:
    raise RuntimeError("Missing DEEPGRAM_API_KEY")

PORT = int(os.getenv("PORT", "10000"))  # Render injects PORT

# Directories
TRANSCRIPTS_DIR = pathlib.Path("transcripts"); TRANSCRIPTS_DIR.mkdir(exist_ok=True)
CALL_META = {}

# Booking slot rules
SLOT_RX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
ALLOWED_HOURS = {10, 13, 16}
SAT_ALLOWED = {10, 12}
AEST_TZ = dt.timezone(dt.timedelta(hours=10))


def slot_ok(d: dt.datetime) -> bool:
    wd = d.weekday()
    return (wd <= 4 and d.hour in ALLOWED_HOURS) or (wd == 5 and d.hour in SAT_ALLOWED)


def append_transcript(call_sid: str, line: str):
    if not call_sid or not line:
        return
    with (TRANSCRIPTS_DIR / f"{call_sid}.txt").open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


# ============== Deepgram Agent Connection ==============
import websockets as ws_lib

def sts_connect():
    return ws_lib.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", DEEPGRAM_API_KEY],
    )

def load_config():
    with open("config.json", "r") as f:
        return json.load(f)


# ============== Media Bridge ==============
async def handle_barge_in(decoded, twilio_ws, streamsid):
    if decoded.get("type") == "UserStartedSpeaking":
        await twilio_ws.send_json({"event": "clear", "streamSid": streamsid})

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
            logging.exception("sts_sender failed")
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
            await twilio_ws.send_json({
                "event": "media",
                "streamSid": streamsid,
                "media": {"payload": base64.b64encode(raw_mulaw).decode("ascii")},
            })
        except Exception:
            logging.exception("sts_receiver failed")
            break

async def twilio_ws_handler(request: web.Request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    call_sid = request.query.get("CallSid")
    audio_q = asyncio.Queue()
    streamsid_q = asyncio.Queue()

    try:
        async with sts_connect() as sts_ws:
            await sts_ws.send(json.dumps(load_config()))
            t1 = asyncio.create_task(sts_sender(sts_ws, audio_q))
            t2 = asyncio.create_task(sts_receiver(sts_ws, ws, streamsid_q, call_sid))
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    ev = data.get("event")
                    if ev == "start":
                        streamsid = data.get("start", {}).get("streamSid")
                        if streamsid:
                            streamsid_q.put_nowait(streamsid)
                            logging.info("Twilio stream started: %s", streamsid)
                    elif ev == "media":
                        payload = base64.b64decode(data["media"]["payload"])
                        audio_q.put_nowait(payload)
                    elif ev == "stop":
                        logging.info("Twilio stream stopped")
                        break
                elif msg.type == WSMsgType.ERROR:
                    logging.error("ws connection closed with exception %s", ws.exception())
            t1.cancel(); t2.cancel()
    except Exception:
        logging.exception("twilio_ws_handler failure")

    return ws


# ============== HTTP API Endpoints ==============
async def root(_): return web.Response(text="AI Voice Sales Agent OK")

async def twiml(request: web.Request):
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="alice" language="en-AU">This call may be monitored or recorded.</Say>
  <Connect>
    <Stream url="wss://{request.host}/twilio"/>
  </Connect>
</Response>"""
    return web.Response(text=xml, content_type="text/xml")

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

async def transcript(request: web.Request):
    call_sid = request.query.get("call_sid")
    if not call_sid:
        return web.json_response({"error":"missing call_sid"}, status=400)
    path = TRANSCRIPTS_DIR / f"{call_sid}.txt"
    if not path.exists():
        return web.json_response({"error":"no transcript"}, status=404)
    return web.FileResponse(path)


# ============== Entrypoint ==============
def make_app():
    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/twiml", twiml)
    app.router.add_post("/twiml", twiml)  # Twilio POSTs
    app.router.add_get("/twilio", twilio_ws_handler)  # Twilio WS upgrades here
    app.router.add_post("/book_appointment", book_appointment)
    app.router.add_post("/webhooks/recording", recording_webhook)
    app.router.add_get("/transcript", transcript)
    return app

if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)









