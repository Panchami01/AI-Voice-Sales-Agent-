import asyncio
import base64
import json
import logging
import os
from urllib.parse import urlparse
from collections import defaultdict
from datetime import datetime, timezone

import websockets
from dotenv import load_dotenv

# ---------- NEW (optional) Twilio SMS ----------
TWILIO_OK = False
try:
    from twilio.rest import Client as TwilioClient  # pip install twilio
    TWILIO_OK = True
except Exception:
    TWILIO_OK = False

load_dotenv()
logging.basicConfig(level=logging.INFO)

# -------------------- New globals (safe & additive) --------------------
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
LOG_WEBHOOK_URL = os.getenv("LOG_WEBHOOK_URL", "").strip()

# Twilio SMS config (only used if fully set)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_SMS_FROM    = os.getenv("TWILIO_SMS_FROM", "").strip()
twilio_client = None
if TWILIO_OK and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_SMS_FROM:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    logging.info("Twilio SMS enabled.")
else:
    if TWILIO_OK:
        logging.info("Twilio library present but env vars missing; SMS will be skipped.")
    else:
        logging.info("Twilio library not installed; SMS will be skipped.")

# In-memory transcript lines and per-call state keyed by streamSid
TRANSCRIPTS = defaultdict(list)   # {streamSid: ["[who] text", ...]}
CALL_STATE  = {}                  # {streamSid: {...}}

def _append_transcript(streamsid: str, who: str, text: str):
    if streamsid and text:
        # track unsubscribe intent for compliance
        if who.lower().startswith("user") or who.lower().startswith("caller"):
            if " stop" in f" {text.lower()} " or text.strip().upper() == "STOP":
                CALL_STATE.setdefault(streamsid, {}).setdefault("compliance_flags", []).append("unsubscribe")
        TRANSCRIPTS[streamsid].append(f"[{who}] {text}")

def _transcript_url(streamsid: str) -> str:
    if not streamsid:
        return ""
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/transcripts/{streamsid}.txt"
    return f"/transcripts/{streamsid}.txt"

def _post_log_if_configured(payload: dict):
    if not LOG_WEBHOOK_URL:
        return
    try:
        import requests  # optional dependency
        requests.post(LOG_WEBHOOK_URL, json=payload, timeout=5)
    except Exception:
        logging.exception("Failed to POST call log to LOG_WEBHOOK_URL")

def _send_sms(to_number: str, body: str):
    """Send SMS if Twilio is configured; otherwise log what would be sent."""
    if not to_number:
        logging.info("SMS skipped: no destination number.")
        return
    # suppress if caller unsubscribed
    st = CALL_STATE.get(_sid_from_number(to_number), None)  # best-effort; harmless if None
    # (We also check compliance flags at send-time below using call state.)

    if not twilio_client:
        logging.info("SMS not sent (Twilio not configured). Would send to %s: %s", to_number, body)
        return
    try:
        twilio_client.messages.create(to=to_number, from_=TWILIO_SMS_FROM, body=body)
        logging.info("SMS sent to %s", to_number)
    except Exception:
        logging.exception("Failed to send SMS")

def _sid_from_number(_):  # helper kept for potential future mapping
    return None

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

def _maybe_update_booking(decoded: dict, streamsid: str):
    """
    Detect booking confirmations coming from the agent.
    Supports shapes:
      1) {"type":"ToolResponse","name":"book_appointment","result":{...}}
      2) {"booking": {...}}
      3) {"ok":true,"booking_id":"...","slot_iso":"...","message":"..."}
    """
    if not streamsid:
        return
    state = CALL_STATE.get(streamsid)
    if state is None:
        return

    booking = None
    if decoded.get("type") == "ToolResponse" and decoded.get("name") == "book_appointment":
        booking = decoded.get("result") or {}
    if not booking and "booking" in decoded:
        booking = decoded.get("booking") or {}
    if not booking and decoded.get("ok") and decoded.get("booking_id"):
        booking = decoded

    if not booking:
        return

    state["booking"] = {
        "slot_iso":    booking.get("slot_iso"),
        "mode":        booking.get("mode"),
        "booking_id":  booking.get("booking_id"),
        "status":      "ok" if booking.get("ok", True) else "failed",
        "message":     booking.get("message"),
    }

def _maybe_update_summary(decoded: dict, streamsid: str):
    # If the agent outputs a final text summary, store it.
    if not streamsid:
        return
    text = decoded.get("summary") or ""
    if isinstance(text, str) and text.strip():
        CALL_STATE.setdefault(streamsid, {})["summary"] = text.strip()

async def handle_text_message(decoded, twilio_ws, sts_ws, streamsid):
    # Append any readable text to transcript
    text = decoded.get("text") or decoded.get("content") or decoded.get("message") or ""
    speaker = decoded.get("speaker") or decoded.get("role") or "assistant"
    if isinstance(text, str) and text.strip():
        _append_transcript(streamsid, speaker, text.strip())

    # Track booking & (optional) summary if present
    _maybe_update_booking(decoded, streamsid)
    _maybe_update_summary(decoded, streamsid)

    await handle_barge_in(decoded, twilio_ws, streamsid)
    # add function-calling or other message handling here if needed

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
            # Binary presumed mulaw @ 8k (per config.json)
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
    current_sid = None

    async for message in twilio_ws:
        try:
            data = json.loads(message)
            event = data.get("event")

            if event == "start":
                start = data.get("start", {})
                streamsid = start.get("streamSid")
                if streamsid:
                    current_sid = streamsid
                    streamsid_queue.put_nowait(streamsid)
                    # pull custom params (customer_phone, optional recording_url)
                    custom = (start.get("customParameters") or {}) if isinstance(start.get("customParameters"), dict) else {}
                    caller_cli = custom.get("customer_phone") or start.get("from")
                    recording_url = custom.get("recording_url")
                    call_sid = start.get("callSid")

                    # seed per-call state
                    CALL_STATE[streamsid] = {
                        "caller_cli": caller_cli,
                        "call_sid": call_sid,
                        "recording_url": recording_url,  # may be None; fine
                        "qualification": {},
                        "booking": {},
                        "compliance_flags": [],
                        "summary": ""
                    }
                    _append_transcript(streamsid, "system", "Call started.")
                logging.info("Twilio stream started: %s", streamsid)

            elif event == "connected":
                continue

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
                # Finalise transcript, emit logs, and SEND SMS
                if current_sid:
                    _append_transcript(current_sid, "system", "Call ended.")
                    transcript_link = _transcript_url(current_sid)

                    # Build call log payload
                    state = CALL_STATE.get(current_sid, {})
                    # Prefer local timezone (+10/+11) for human-reads in logs:
                    ts = datetime.now(timezone.utc).astimezone().isoformat()

                    payload = {
                        "timestamp": ts,
                        "caller_cli": state.get("caller_cli"),
                        "summary": state.get("summary", ""),
                        "qualification": state.get("qualification", {}),
                        "booking": state.get("booking", {}),
                        "compliance_flags": state.get("compliance_flags", []),
                        "transcript_url": transcript_link,
                        "recording_url": state.get("recording_url"),
                        "call_sid": state.get("call_sid"),
                        "stream_sid": current_sid
                    }

                    # 1) Print transcript link
                    logging.info("TRANSCRIPT_URL %s", transcript_link)

                    # 2) Emit call log JSON
                    logging.info("CALL_LOG %s", json.dumps(payload, ensure_ascii=False))
                    _post_log_if_configured(payload)

                    # 3) SMS (booking + transcript) unless unsubscribed
                    booking = state.get("booking", {})
                    customer_phone = state.get("caller_cli")
                    unsubscribed = "unsubscribe" in (state.get("compliance_flags") or [])
                    if not unsubscribed and customer_phone:
                        if booking.get("booking_id"):
                            booking_out = {
                                "type": "appointment",
                                "streamSid": current_sid,
                                "booking": booking,
                                "transcript_url": transcript_link
                            }
                            logging.info("APPOINTMENT %s", json.dumps(booking_out, ensure_ascii=False))
                            _post_log_if_configured(booking_out)

                            # Compose SMS with appointment + transcript
                            sms_lines = []
                            if booking.get("message"):
                                sms_lines.append(booking["message"])
                            else:
                                sms_lines.append(
                                    f"Your Riverstone Place appointment is booked. Ref {booking.get('booking_id')} at {booking.get('slot_iso')} (AEST)."
                                )
                            if transcript_link:
                                sms_lines.append(f"Transcript: {transcript_link}")
                            _send_sms(customer_phone, "\n".join(sms_lines))
                        else:
                            # No booking → still send transcript link so the caller has it
                            if transcript_link:
                                _send_sms(customer_phone, f"Thanks for calling Riverstone Place. Your transcript: {transcript_link}")

                break

            # Drain inbound buffer in 20ms frames
            while len(inbuffer) >= BUFFER_SIZE:
                frame = inbuffer[:BUFFER_SIZE]
                audio_queue.put_nowait(frame)
                del inbuffer[:BUFFER_SIZE]

        except Exception:
            logging.exception("twilio_receiver crashed")
            break

# -------------------- Health check & transcript serving --------------------
async def _http_response(status: int, body: str, content_type="text/plain; charset=utf-8"):
    return (status, [("Content-Type", content_type)], body.encode("utf-8"))

async def process_request(path, request_headers):
    upgrade = request_headers.get("Upgrade", "").lower()
    if upgrade == "websocket":
        logging.info(
            "WS upgrade attempt path=%s subprotocols=%s",
            path,
            request_headers.get("Sec-WebSocket-Protocol"),
        )
        return None  # let WS handshake proceed

    # Health
    if path == "/health":
        return await _http_response(200, "OK")

    # Serve transcript text: /transcripts/<streamSid>.txt
    if path.startswith("/transcripts/") and path.endswith(".txt"):
        streamsid = path.rsplit("/", 1)[-1].removesuffix(".txt")
        lines = TRANSCRIPTS.get(streamsid)
        if not lines:
            return await _http_response(404, f"Transcript not found for {streamsid}")
        body = "\n".join(lines)
        return await _http_response(200, body, content_type="text/plain; charset=utf-8")

    return await _http_response(404, "Not Found")

ALLOWED_WS_PATHS = {"/twilio", "/ws", "/"}

def _is_allowed_ws_path(request_path: str) -> bool:
    pure_path = urlparse(request_path).path or "/"
    return pure_path in ALLOWED_WS_PATHS

# Compatible with websockets API styles
async def twilio_handler(twilio_ws, path=None, *args):
    if path is None:
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

# -------------------- Server bootstrap --------------------
async def main():
    # $PORT for Render/Railway; keep 5000 fallback for local
    port = int(os.environ.get("PORT", 5000))

    # Twilio requires "audio" subprotocol
    advertised_subprotocols = ["audio"]

    # Keepalive pings to avoid idle disconnects
    ping_interval = 20
    ping_timeout = 20

    server = await websockets.serve(
        twilio_handler,
        host="0.0.0.0",
        port=port,
        process_request=process_request,      # /health & /transcripts over HTTP
        subprotocols=advertised_subprotocols, # negotiate Twilio
        max_size=8 * 1024 * 1024,
        ping_interval=ping_interval,
        ping_timeout=ping_timeout,
    )
    logging.info("Started server on 0.0.0.0:%s", port)
    await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(main())



























