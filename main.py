import asyncio
import base64
import datetime as dt
import json
import logging
import os
import pathlib
import re
from typing import Optional

from aiohttp import web, WSMsgType
import websockets
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
if not DEEPGRAM_API_KEY:
    raise RuntimeError("Missing DEEPGRAM_API_KEY")

PORT = int(os.getenv("PORT", "10000"))  # Render injects this automatically

# Storage
TRANSCRIPTS_DIR = pathlib.Path("transcripts"); TRANSCRIPTS_DIR.mkdir(exist_ok=True)
CALL_META: dict[str, dict] = {}

# Booking rules
SLOT_RX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
ALLOWED_HOURS = {10, 13, 16}
SAT_ALLOWED = {10, 12}
AEST_TZ = dt.timezone(dt.timedelta(hours=10))

def slot_ok(d: dt.datetime) -> bool:
    wd = d.weekday()
    return (wd <= 4 and d.hour in ALLOWED_HOURS) or (wd == 5 and d.hour in SAT_ALLOWED)

def append_transcript(call_sid: Optional[str], line: str):
    if not call_sid or not line:
        return
    with (TRANSCRIPTS_DIR / f"{call_sid}.txt").open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")

def load_config():
    with open("config.json", "r") as f:
        return json.load(f)

# -------- μ-law (G.711) -> PCM16 conversion --------
# 20ms from Twilio = 160 μ-law bytes -> 160 linear16 samples -> 320 output bytes
def mulaw_to_pcm16(mulaw: bytes) -> bytes:
    out = bytearray(len(mulaw) * 2)
    j = 0
    for b in mulaw:
        u = ~b & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        # Bias = 132 (0x84). Formula from ITU G.711 μ-law
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        if sign:
            sample = -sample
        # clamp to 16-bit
        if sample > 32767: sample = 32767
        if sample < -32768: sample = -32768
        # little-endian write
        out[j] = sample & 0xFF
        out[j+1] = (sample >> 8) & 0xFF
        j += 2
    return bytes(out)

# ==================== Twilio <-> Deepgram bridge ====================

async def handle_text_message(decoded, call_sid=None):
    txt = decoded.get("text") or decoded.get("content")
    if txt and call_sid:
        append_transcript(call_sid, txt)

async def dg_receiver(sts_ws, twilio_ws, streamsid_queue: asyncio.Queue, call_sid: str, ready_evt: asyncio.Event):
    """
    Receive Deepgram control + TTS. Don't block on streamSid; start reading immediately.
    """
    streamsid = None
    first_jsons_to_log = 5

    async for message in sts_ws:
        try:
            # pick up streamSid as soon as Twilio provides it
            if streamsid is None and not streamsid_queue.empty():
                streamsid = await streamsid_queue.get()

            if isinstance(message, str):
                decoded = json.loads(message)
                if first_jsons_to_log > 0:
                    logging.info("[DG←] %s", json.dumps(decoded)[:800])
                    first_jsons_to_log -= 1
                if not ready_evt.is_set():
                    ready_evt.set()
                await handle_text_message(decoded, call_sid)
                continue

            # Binary TTS from Deepgram -> Twilio (requires streamSid)
            if streamsid:
                payload = base64.b64encode(message).decode("ascii")
                await twilio_ws.send_json({
                    "event": "media",
                    "streamSid": streamsid,
                    "media": {"payload": payload},
                })
        except Exception:
            logging.exception("dg_receiver failed")
            break

async def twilio_ws_handler(request: web.Request):
    """
    Twilio connects here. We bridge Twilio <-> Deepgram Agent using JSON audio messages:
      - {"type":"input_audio_buffer.append","audio":"<base64 PCM16>"}
      - {"type":"input_audio_buffer.commit"}
      - {"type":"input_audio_buffer.flush"} at end
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    call_sid = request.query.get("CallSid")
    streamsid_q = asyncio.Queue()
    ready_evt = asyncio.Event()

    COMMIT_INTERVAL_MS = 100
    last_commit_ms = 0

    try:
        async with websockets.connect(
            "wss://agent.deepgram.com/v1/agent/converse",
            subprotocols=["token", DEEPGRAM_API_KEY],
        ) as sts_ws:
            logging.info("Connected to Deepgram Agent WS")
            cfg = load_config()
            await sts_ws.send(json.dumps(cfg))  # send Settings first
            logging.info("[DG→] config sent")

            # start receiver immediately
            dg_recv_task = asyncio.create_task(dg_receiver(sts_ws, ws, streamsid_q, call_sid, ready_evt))

            # wait for any DG JSON (Welcome/Started) to gate audio
            await ready_evt.wait()
            logging.info("Deepgram ready: will forward audio as input_audio_buffer.append (PCM16)")

            # Twilio media loop
            ulaw_buf = bytearray()
            UL_FRAME = 160  # bytes (20 ms @ 8kHz μ-law)

            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    ev = data.get("event")

                    if ev == "start":
                        streamsid = data.get("start", {}).get("streamSid")
                        if streamsid:
                            await streamsid_q.put(streamsid)
                            logging.info("Twilio stream started: %s", streamsid)

                    elif ev == "media":
                        # accumulate μ-law bytes to 20ms
                        payload = base64.b64decode(data["media"]["payload"])
                        ulaw_buf.extend(payload)
                        while len(ulaw_buf) >= UL_FRAME:
                            ulaw_frame = bytes(ulaw_buf[:UL_FRAME])
                            del ulaw_buf[:UL_FRAME]
                            pcm16 = mulaw_to_pcm16(ulaw_frame)
                            # send JSON append with base64 PCM16
                            await sts_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(pcm16).decode("ascii"),
                            }))
                            # periodic commit for low latency
                            now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
                            if now_ms - last_commit_ms >= COMMIT_INTERVAL_MS:
                                await sts_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                                last_commit_ms = now_ms

                    elif ev == "stop":
                        logging.info("Twilio stream stopped")
                        try:
                            await sts_ws.send(json.dumps({"type": "input_audio_buffer.flush"}))
                        except Exception:
                            pass
                        break

                elif msg.type == WSMsgType.ERROR:
                    logging.error("Twilio ws error: %s", ws.exception())

            dg_recv_task.cancel()

    except Exception:
        logging.exception("twilio_ws_handler failure")

    return ws

# ==================== HTTP Endpoints ====================

async def root(_): return web.Response(text="AI Voice Sales Agent OK")

async def twiml(request: web.Request):
    host = request.host
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="alice" language="en-AU">This call may be monitored or recorded.</Say>
  <Connect>
    <Stream url="wss://{host}/twilio"/>
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

# ==================== Entrypoint ====================

def make_app():
    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/twiml", twiml)
    app.router.add_post("/twiml", twiml)
    app.router.add_get("/twilio", twilio_ws_handler)  # WS upgrades here
    app.router.add_post("/book_appointment", book_appointment)
    app.router.add_post("/webhooks/recording", recording_webhook)
    app.router.add_get("/transcript", transcript)
    return app

if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)














