# main.py
import asyncio
import base64
import datetime as dt
import json
import logging
import os
import pathlib
import re
from typing import Optional

import websockets
from aiohttp import web
from dotenv import load_dotenv

# -------------------- Setup --------------------
load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)

RUN_MODE = os.getenv("RUN_MODE", "http")  # "http", "ws", or "both" (both = local dev only)
PORT = int(os.getenv("PORT", "8080"))     # Render injects this automatically

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://your-api.onrender.com")
PUBLIC_WS_URL   = os.getenv("PUBLIC_WS_URL",   "wss://your-ws.onrender.com/twilio")

# Fixed AEST for the assignment (in prod use zoneinfo("Australia/Melbourne"))
AEST_TZ = dt.timezone(dt.timedelta(hours=10))

# -------------------- Data & paths --------------------
CALL_META: dict[str, dict] = {}   # callSid -> {from, recording_url, summary, qualification, booking, compliance_flags}
TRANSCRIPTS_DIR = pathlib.Path("transcripts"); TRANSCRIPTS_DIR.mkdir(exist_ok=True)
LOGS_DIR = pathlib.Path("logs"); LOGS_DIR.mkdir(exist_ok=True)
LOGFILE = LOGS_DIR / "calls.jsonl"
LATEST_LOG: Optional[dict] = None

# -------------------- Deepgram Agent WS --------------------
def load_agent_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

def deepgram_connect():
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPGRAM_API_KEY not set")
    return websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key],
    )

# -------------------- Helpers --------------------
def transcript_url(call_sid: Optional[str]) -> Optional[str]:
    if not call_sid: return None
    return f"{PUBLIC_BASE_URL}/transcripts/{call_sid}.txt"

def append_transcript(call_sid: str, line: str):
    if not call_sid or not line: return
    with (TRANSCRIPTS_DIR / f"{call_sid}.txt").open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")

# Booking window validators (per spec)
SLOT_RX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
ALLOWED_HOURS = {10, 13, 16}  # Mon–Fri
SAT_ALLOWED   = {10, 12}      # Sat only

def slot_ok(d: dt.datetime) -> bool:
    wd = d.weekday()  # Mon=0..Sun=6
    return (wd <= 4 and d.hour in ALLOWED_HOURS) or (wd == 5 and d.hour in SAT_ALLOWED)

# -------------------- Twilio <-> Deepgram bridge --------------------
async def handle_text(decoded: dict, twilio_ws, call_sid: Optional[str]):
    text = decoded.get("text") or decoded.get("content") or decoded.get("message")
    if text and call_sid:
        append_transcript(call_sid, text)

async def sts_sender(sts_ws, audio_q: asyncio.Queue):
    while True:
        chunk = await audio_q.get()
        try:
            await sts_ws.send(chunk)  # binary μ-law@8k
        except Exception:
            logging.exception("sts_sender error")
            break

async def sts_receiver(sts_ws, twilio_ws, streamsid_q: asyncio.Queue, call_sid_q: asyncio.Queue):
    streamsid = await streamsid_q.get()
    call_sid  = await call_sid_q.get()
    async for message in sts_ws:
        try:
            if isinstance(message, str):
                await handle_text(json.loads(message), twilio_ws, call_sid)
                continue
            # binary audio from agent -> Twilio media message
            payload = base64.b64encode(message).decode("ascii")
            await twilio_ws.send(json.dumps({
                "event": "media",
                "streamSid": streamsid,
                "media": {"payload": payload},
            }))
        except Exception:
            logging.exception("sts_receiver error")
            break

async def twilio_receiver(twilio_ws, sts_ws, audio_q: asyncio.Queue, streamsid_q: asyncio.Queue, call_sid_q: asyncio.Queue):
    BUFFER = 160  # 20ms @ 8kHz μ-law
    inbuf = bytearray()
    call_sid: Optional[str] = None

    async for raw in twilio_ws:
        try:
            data = json.loads(raw)
            event = data.get("event")

            if event == "start":
                start = data.get("start", {})
                streamsid = start.get("streamSid")
                if streamsid: streamsid_q.put_nowait(streamsid)
                call_sid = start.get("callSid") or start.get("customParameters", {}).get("callSid")
                if call_sid: call_sid_q.put_nowait(call_sid)
                from_num = start.get("customParameters", {}).get("from")
                if from_num: CALL_META.setdefault(call_sid or "", {})["from"] = from_num
                logging.info("Twilio stream start: streamSid=%s callSid=%s", streamsid, call_sid)

            elif event == "media":
                media = data.get("media", {})
                b64 = media.get("payload")
                if not b64: continue
                chunk = base64.b64decode(b64)
                # Accept if track missing or inbound
                track = media.get("track")
                if track is None or track in ("inbound", "inbound_audio"):
                    inbuf.extend(chunk)

            elif event == "stop":
                meta = CALL_META.get(call_sid or "", {})
                snapshot = {
                    "timestamp": dt.datetime.now(AEST_TZ).isoformat(),
                    "caller_cli": meta.get("from"),
                    "summary": meta.get("summary", ""),
                    "qualification": meta.get("qualification", {}),
                    "booking": meta.get("booking", {}),
                    "compliance_flags": meta.get("compliance_flags", []),
                    "transcript_url": transcript_url(call_sid),
                    "recording_url": meta.get("recording_url"),
                }
                with LOGFILE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(snapshot) + "\n")
                global LATEST_LOG; LATEST_LOG = snapshot
                break

            # drain ~20ms frames to Deepgram
            while len(inbuf) >= BUFFER:
                frame = inbuf[:BUFFER]
                audio_q.put_nowait(frame)
                del inbuf[:BUFFER]

        except Exception:
            logging.exception("twilio_receiver error")
            break

async def twilio_ws_handler(ws, path=None):
    logging.info("Twilio WS connected")
    config = load_agent_config()
    async with deepgram_connect() as dg_ws:
        await dg_ws.send(json.dumps(config))  # send agent settings

        audio_q = asyncio.Queue()
        streamsid_q = asyncio.Queue(maxsize=1)
        call_sid_q = asyncio.Queue(maxsize=1)

        tasks = {
            asyncio.create_task(sts_receiver(dg_ws, ws, streamsid_q, call_sid_q)),
            asyncio.create_task(sts_sender(dg_ws, audio_q)),
            asyncio.create_task(twilio_receiver(ws, dg_ws, audio_q, streamsid_q, call_sid_q)),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending: t.cancel()
    logging.info("Twilio WS closed")

# -------------------- HTTP Handlers --------------------
async def root(_):
    return web.Response(text="AI Voice Sales Agent: OK")

async def health(_):
    return web.json_response({"ok": True, "mode": RUN_MODE, "time": dt.datetime.now(AEST_TZ).isoformat()})

async def twiml(request: web.Request):
    # read From/CallSid if present (GET or POST)
    from_num = request.query.get("From")
    call_sid = request.query.get("CallSid")
    if request.can_read_body:
        try:
            data = await request.post()
            from_num = from_num or data.get("From")
            call_sid = call_sid or data.get("CallSid")
        except Exception:
            pass
    if call_sid:
        CALL_META.setdefault(call_sid, {})["from"] = from_num

    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="alice" language="en-AU">This call may be monitored or recorded.</Say>
  <Connect>
    <Stream url="{PUBLIC_WS_URL}"/>
  </Connect>
</Response>'''
    return web.Response(text=xml, content_type="text/xml; charset=utf-8")

async def recording_webhook(request: web.Request):
    data = await request.post()
    call_sid = data.get("CallSid")
    rec = data.get("RecordingUrl")
    if call_sid and rec:
        CALL_META.setdefault(call_sid, {})["recording_url"] = rec + ".mp3"
        logging.info("Recording saved for %s", call_sid)
    return web.Response(text="ok")

async def book_appointment(request: web.Request):
    try:
        body = await request.json()
        for k in ("name", "phone", "email", "slot_iso", "mode"):
            if not body.get(k):
                return web.json_response({"ok": False, "message": f"missing {k}"}, status=400)
        slot_iso = body["slot_iso"]
        if not SLOT_RX.match(slot_iso):
            return web.json_response({"ok": False, "message": "bad slot_iso"}, status=400)
        d = dt.datetime.fromisoformat(slot_iso)
        now = dt.datetime.now(dt.timezone.utc).astimezone(d.tzinfo)
        if d <= now:
            return web.json_response({"ok": False, "message": "slot in past"}, status=400)
        if not slot_ok(d):
            return web.json_response({"ok": False, "message": "slot not in allowed windows"}, status=400)

        booking_id = f"RS-{d.strftime('%Y%m%d-%H%M')}"
        msg = f"Booked {d.strftime('%a %d %b %H:%M %Z')}"
        return web.json_response({
            "ok": True,
            "booking_id": booking_id,
            "message": msg,
            "status": "confirmed",
            "slot_iso": slot_iso,
            "mode": body["mode"]
        })
    except Exception as e:
        logging.exception("book_appointment error")
        return web.json_response({"ok": False, "message": str(e)}, status=500)

async def ingest_log(request: web.Request):
    try:
        body = await request.json()
        call_sid = body.get("call_sid")
        if not call_sid:
            return web.json_response({"ok": False, "message": "missing call_sid"}, status=400)

        meta = CALL_META.setdefault(call_sid, {})
        for k in ("summary", "qualification", "booking", "compliance_flags"):
            if k in body: meta[k] = body[k]

        snapshot = {
            "timestamp": dt.datetime.now(AEST_TZ).isoformat(),
            "caller_cli": meta.get("from"),
            "summary": meta.get("summary", ""),
            "qualification": meta.get("qualification", {}),
            "booking": meta.get("booking", {}),
            "compliance_flags": meta.get("compliance_flags", []),
            "transcript_url": transcript_url(call_sid),
            "recording_url": meta.get("recording_url"),
        }
        with LOGFILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot) + "\n")
        global LATEST_LOG; LATEST_LOG = snapshot
        return web.json_response({"ok": True})
    except Exception as e:
        logging.exception("ingest_log error")
        return web.json_response({"ok": False, "message": str(e)}, status=500)

async def latest_log(_):
    return web.json_response(LATEST_LOG or {})

def make_app():
    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.router.add_get("/twiml", twiml)
    app.router.add_post("/twiml", twiml)  # Twilio usually POSTs
    app.router.add_post("/webhooks/recording", recording_webhook)
    app.router.add_post("/book_appointment", book_appointment)
    app.router.add_post("/logs/ingest", ingest_log)
    app.router.add_get("/logs/latest", latest_log)
    app.router.add_static("/transcripts/", str(TRANSCRIPTS_DIR) + "/")
    return app

# -------------------- Entrypoints --------------------
async def run_http():
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("HTTP started on 0.0.0.0:%s", PORT)
    while True:
        await asyncio.sleep(3600)

async def run_ws():
    server = await websockets.serve(twilio_ws_handler, host="0.0.0.0", port=PORT)
    logging.info("WS started on 0.0.0.0:%s (path: /twilio)", PORT)
    await server.wait_closed()

async def main():
    if RUN_MODE == "http":
        await run_http()
    elif RUN_MODE == "ws":
        await run_ws()
    else:
        # local dev convenience (two ports)
        async def http_task():
            os.environ["PORT"] = "8080"
            await run_http()
        async def ws_task():
            os.environ["PORT"] = "5000"
            await run_ws()
        await asyncio.gather(http_task(), ws_task())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pas


