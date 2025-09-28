# main.py
import asyncio
import base64
import datetime
import json
import logging
import os
import pathlib
import re
from typing import Optional

import websockets
from aiohttp import web
from dotenv import load_dotenv

# ===================== Setup =====================
load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)

MODE = os.getenv("RUN_MODE", "both")  # "ws", "http", or "both" (both = local dev)
PORT = int(os.getenv("PORT", "8080"))  # Render sets this automatically

# Public URLs (used in TwiML and links inside logs)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://YOUR_API_BASE")
PUBLIC_WS_URL = os.getenv("PUBLIC_WS_URL", "wss://YOUR_WS_BASE/twilio")

# NOTE: This is a fixed AEST offset per the assignment spec (UTC+10:00).
# In production you’d use zoneinfo("Australia/Melbourne") to handle DST.
AEST_TZ = datetime.timezone(datetime.timedelta(hours=10))

# ===================== In-memory state & paths =====================
CALL_META: dict[str, dict] = {}  # callSid -> {from, recording_url, summary, qualification, booking, compliance_flags}
TRANSCRIPTS_DIR = pathlib.Path("transcripts")
TRANSCRIPTS_DIR.mkdir(exist_ok=True)
LOGS_DIR = pathlib.Path("logs")
LOGS_DIR.mkdir(exist_ok=True)
LOGFILE = LOGS_DIR / "calls.jsonl"
LATEST_LOG: Optional[dict] = None

# ===================== Deepgram Agent (WS) =====================
def sts_connect():
    """Create a Deepgram Agent Converse websocket connection."""
    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPGRAM_API_KEY not found in environment")
    # Agent Converse endpoint
    return websockets.connect(
        "wss://agent.deepgram.com/v1/agent/converse",
        subprotocols=["token", api_key],
    )

def load_config():
    """Load Deepgram agent config (mulaw@8000 for Twilio)."""
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

# ===================== Helpers =====================
def append_transcript(call_sid: str, line: str):
    if not call_sid or not line:
        return
    (TRANSCRIPTS_DIR / f"{call_sid}.txt").open("a", encoding="utf-8").write(line.rstrip() + "\n")

def transcript_url(call_sid: Optional[str]) -> Optional[str]:
    if not call_sid or not PUBLIC_BASE_URL:
        return None
    return f"{PUBLIC_BASE_URL}/transcripts/{call_sid}.txt"

# Booking validators (per spec)
SLOT_RX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
ALLOWED_HOURS = {10, 13, 16}  # Mon–Fri
SAT_ALLOWED = {10, 12}        # Sat

def slot_ok(dt: datetime.datetime) -> bool:
    wd = dt.weekday()  # Mon=0 .. Sun=6
    if wd <= 4 and dt.hour in ALLOWED_HOURS:
        return True
    if wd == 5 and dt.hour in SAT_ALLOWED:
        return True
    return False

# ===================== Twilio <-> Deepgram pipeline =====================
async def handle_barge_in(decoded: dict, twilio_ws, streamsid: Optional[str]):
    """
    (Optional) If you’re using barge-in control messages from the agent,
    implement them here (e.g., stop current TTS on user speech).
    """
    # Placeholder for future logic
    return

async def handle_text_message(decoded: dict, twilio_ws, sts_ws, streamsid: Optional[str], call_sid: Optional[str]):
    """Capture text events for transcripts; keep barge-in snappy."""
    await handle_barge_in(decoded, twilio_ws, streamsid)
    txt = decoded.get("text") or decoded.get("content") or decoded.get("message")
    if txt and call_sid:
        append_transcript(call_sid, txt)

async def sts_sender(sts_ws, audio_q: asyncio.Queue):
    """Send μ-law 8k chunks from Twilio to Deepgram."""
    logging.info("sts_sender started")
    while True:
        chunk = await audio_q.get()
        try:
            await sts_ws.send(chunk)  # binary
        except Exception:
            logging.exception("sts_sender failed")
            break

async def sts_receiver(sts_ws, twilio_ws, streamsid_q: asyncio.Queue, call_sid_q: asyncio.Queue):
    """Receive agent (binary μ-law @8k or control JSON) and forward to Twilio."""
    logging.info("sts_receiver started")
    streamsid = await streamsid_q.get()
    call_sid = await call_sid_q.get()
    async for message in sts_ws:
        try:
            if isinstance(message, str):
                decoded = json.loads(message)
                await handle_text_message(decoded, twilio_ws, sts_ws, streamsid, call_sid)
                continue
            # Binary: assume μ-law 8k (per config.json)
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

async def twilio_receiver(twilio_ws, sts_ws, audio_q: asyncio.Queue, streamsid_q: asyncio.Queue, call_sid_q: asyncio.Queue):
    """Receive Twilio frames; forward μ-law 8k to Deepgram, write final log on stop."""
    BUFFER_SIZE = 160  # 20ms of 8k μ-law = 160 bytes
    inbuffer = bytearray()
    call_sid: Optional[str] = None

    async for message in twilio_ws:
        try:
            data = json.loads(message)
            event = data.get("event")

            if event == "start":
                stream = data.get("start", {})
                streamsid = stream.get("streamSid")
                if streamsid:
                    streamsid_q.put_nowait(streamsid)
                call_sid = (stream.get("callSid") or
                            stream.get("customParameters", {}).get("callSid"))
                if call_sid:
                    call_sid_q.put_nowait(call_sid)
                from_number = stream.get("customParameters", {}).get("from")
                if from_number:
                    CALL_META.setdefault(call_sid or "", {})["from"] = from_number
                logging.info("Twilio stream started: streamSid=%s callSid=%s", streamsid, call_sid)

            elif event == "media":
                media = data.get("media", {})
                b64 = media.get("payload")
                if not b64:
                    continue
                chunk = base64.b64decode(b64)
                # Accept if track missing OR is inbound/inbound_audio (Twilio can omit or vary this)
                track = media.get("track")
                if (track is None) or (track in ("inbound", "inbound_audio")):
                    inbuffer.extend(chunk)

            elif event == "stop":
                logging.info("Twilio stream stopped (callSid=%s)", call_sid)
                now = datetime.datetime.now(AEST_TZ)
                meta = CALL_META.get(call_sid or "", {})
                log = {
                    "timestamp": now.isoformat(),
                    "caller_cli": meta.get("from"),
                    "summary": meta.get("summary", ""),
                    "qualification": meta.get("qualification", {}),
                    "booking": meta.get("booking", {}),
                    "compliance_flags": meta.get("compliance_flags", []),
                    "transcript_url": transcript_url(call_sid),
                    "recording_url": meta.get("recording_url"),
                }
                with LOGFILE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(log) + "\n")
                global LATEST_LOG
                LATEST_LOG = log
                break

            # Drain to Deepgram in ~20ms frames
            while len(inbuffer) >= BUFFER_SIZE:
                frame = inbuffer[:BUFFER_SIZE]
                audio_q.put_nowait(frame)
                del inbuffer[:BUFFER_SIZE]

        except Exception:
            logging.exception("twilio_receiver crashed")
            break

# websockets.serve handler
async def twilio_ws_handler(websocket, path=None):
    """
    Twilio <Stream url="wss://.../twilio"> connects here.
    We bridge Twilio <-> Deepgram Agent.
    """
    logging.info("Twilio WS connected")
    config = load_config()
    async with sts_connect() as sts_ws:
        # Send initial config to agent
        await sts_ws.send(json.dumps(config))

        audio_q: asyncio.Queue = asyncio.Queue()
        streamsid_q: asyncio.Queue = asyncio.Queue(maxsize=1)
        call_sid_q: asyncio.Queue = asyncio.Queue(maxsize=1)

        # Fan-out tasks
        recv_task = asyncio.create_task(sts_receiver(sts_ws, websocket, streamsid_q, call_sid_q))
        send_task = asyncio.create_task(sts_sender(sts_ws, audio_q))
        tw_task   = asyncio.create_task(twilio_receiver(websocket, sts_ws, audio_q, streamsid_q, call_sid_q))

        done, pending = await asyncio.wait(
            {recv_task, send_task, tw_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
    logging.info("Twilio WS closed")

# ===================== HTTP routes =====================
async def health(request: web.Request):
    return web.json_response({"ok": True, "mode": MODE, "time": datetime.datetime.now(AEST_TZ).isoformat()})

async def twiml(request: web.Request):
    """
    Twilio Voice webhook (GET). Returns TwiML that starts recording and streams media to our WS.
    """
    from_number = request.query.get("From")
    call_sid = request.query.get("CallSid")
    if call_sid:
        CALL_META.setdefault(call_sid, {})["from"] = from_number

    recording_cb = f"{PUBLIC_BASE_URL}/webhooks/recording"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="en-AU" voice="Polly.Nicole">I am your AI Sales Agent</Say>
  <Start>
    <Recording recordingStatusCallback="{recording_cb}" />
  </Start>
  <Connect>
    <Stream url="{PUBLIC_WS_URL}"/>
  </Connect>
</Response>"""
    return web.Response(text=xml, content_type="text/xml")

async def recording_webhook(request: web.Request):
    """
    Twilio posts recording status here; we save RecordingUrl (append .mp3) against the CallSid.
    """
    data = await request.post()
    call_sid = data.get("CallSid")
    rec_url = data.get("RecordingUrl")
    if call_sid and rec_url:
        CALL_META.setdefault(call_sid, {})["recording_url"] = rec_url + ".mp3"
        logging.info("Recording URL saved for %s: %s", call_sid, rec_url)
    return web.Response(text="ok")

# ---------- Booking API (mock) ----------
async def book_appointment(request: web.Request):
    try:
        body = await request.json()
        # Required fields
        for k in ("name", "phone", "email", "slot_iso", "mode"):
            if not body.get(k):
                return web.json_response({"ok": False, "message": f"missing {k}"}, status=400)
        slot_iso = body["slot_iso"]
        if not SLOT_RX.match(slot_iso):
            return web.json_response({"ok": False, "message": "bad slot_iso"}, status=400)

        dt = datetime.datetime.fromisoformat(slot_iso)  # must include timezone, e.g., +10:00
        # Simple "future" check
        if dt <= datetime.datetime.now(datetime.timezone.utc).astimezone(dt.tzinfo):
            return web.json_response({"ok": False, "message": "slot in past"}, status=400)
        if not slot_ok(dt):
            return web.json_response({"ok": False, "message": "slot not in allowed windows"}, status=400)

        booking_id = f"RS-{dt.strftime('%Y%m%d-%H%M')}"
        msg = f"Booked {dt.strftime('%a %d %b %H:%M %Z')}"
        return web.json_response({
            "ok": True,
            "booking_id": booking_id,
            "message": msg,
            "status": "confirmed",
            "slot_iso": slot_iso,
            "mode": body["mode"]
        })
    except Exception as e:
        logging.exception("book_appointment failed")
        return web.json_response({"ok": False, "message": str(e)}, status=500)

# ---------- Log ingest for agent ----------
async def ingest_log(request: web.Request):
    """
    Agent posts call summary/compliance/qualification/booking here via http tool.
    Body must include: call_sid (or caller_cli), and any of summary/qualification/booking/compliance_flags.
    """
    try:
        body = await request.json()
        call_sid = body.get("call_sid")
        if not call_sid:
            return web.json_response({"ok": False, "message": "missing call_sid"}, status=400)

        meta = CALL_META.setdefault(call_sid, {})
        for k in ("summary", "qualification", "booking", "compliance_flags"):
            if k in body:
                meta[k] = body[k]

        now = datetime.datetime.now(AEST_TZ)
        log = {
            "timestamp": now.isoformat(),
            "caller_cli": meta.get("from"),
            "summary": meta.get("summary", ""),
            "qualification": meta.get("qualification", {}),
            "booking": meta.get("booking", {}),
            "compliance_flags": meta.get("compliance_flags", []),
            "transcript_url": transcript_url(call_sid),
            "recording_url": meta.get("recording_url"),
        }
        with LOGFILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(log) + "\n")
        global LATEST_LOG
        LATEST_LOG = log

        return web.json_response({"ok": True})
    except Exception as e:
        logging.exception("ingest_log failed")
        return web.json_response({"ok": False, "message": str(e)}, status=500)

async def latest_log(request: web.Request):
    return web.json_response(LATEST_LOG or {})

def make_app():
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/twiml", twiml)
    app.router.add_post("/webhooks/recording", recording_webhook)
    app.router.add_post("/book_appointment", book_appointment)
    app.router.add_post("/logs/ingest", ingest_log)
    app.router.add_get("/logs/latest", latest_log)
    app.router.add_static("/transcripts/", str(TRANSCRIPTS_DIR) + "/")
    return app

# ===================== Entry points for Render/local =====================
async def start_ws_only():
    server = await websockets.serve(twilio_ws_handler, host="0.0.0.0", port=PORT)
    logging.info(f"WS started on 0.0.0.0:{PORT} (path: /twilio)")
    await server.wait_closed()

async def start_http_only():
    app = make_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"HTTP started on 0.0.0.0:{PORT}")
    while True:
        await asyncio.sleep(3600)

async def main():
    if MODE == "ws":
        await start_ws_only()
    elif MODE == "http":
        await start_http_only()
    else:
        # Local dev "both": run HTTP on 8080 and WS on 5000
        async def run_http():
            app = make_app()
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", 8080)
            await site.start()
            logging.info("HTTP started on 0.0.0.0:8080")
            while True:
                await asyncio.sleep(3600)

        async def run_ws():
            server = await websockets.serve(twilio_ws_handler, host="0.0.0.0", port=5000)
            logging.info("WS started on 0.0.0.0:5000 (path: /twilio)")
            await server.wait_closed()

        await asyncio.gather(run_http(), run_ws())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
