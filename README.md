# Riverstone Place – Voice Sales Agent

This project implements a professional **voice sales agent** for **Riverstone Place**, a fictional apartment development in Abbotsford, VIC (Melbourne, Australia).
It demonstrates a **Twilio + Deepgram Agent + Python** pipeline with booking, compliance, transcript, and logging features.

---

## ✨ Features

* 📞 **Inbound Calls via Twilio** – callers are greeted and connected to the AI sales agent.
* 🎙 **Low-latency, barge-in conversation** – agent allows interruptions and handles mishears naturally.
* 📑 **Qualification workflow** – collects and confirms: budget, bedrooms, parking, timeframe, owner-occupier vs investor, finance status, suburb (Abbostford only), and contact details.
* ❓ **Knowledge-based answers** – agent answers only from the Riverstone Place knowledge pack, with objections handled and recommendations given.
* 📅 **Booking support** – detects booking confirmations and returns structured JSON with slot, mode, and reference ID.
* 📝 **Transcript storage** – per-call transcript saved and served as plain text at `/transcripts/{StreamSid}.txt`.
* 🎧 **Recording URL support** – accepts recording link from Twilio custom parameters and includes it in logs.
* 📊 **Logging** – per-call JSON log with timestamp, caller number, summary, qualification, booking, compliance flags, transcript URL, and recording URL.
* 🛡 **Compliance** – recognises STOP/unsubscribe phrases; declines FIRB/financial/tax/rental yield queries and offers referral.

---

## 🛠 Architecture

The system runs as a lightweight Python WebSocket server on Railway:

* **Twilio**

  * Handles inbound calls
  * Streams audio via `<Connect><Stream>` or `<Start><Stream>` to the WS service
  * Passes caller number as `customer_phone` parameter

* **WS Service (Python)**

  * Endpoint: `wss://<your-app>.up.railway.app/twilio`
  * Bridges Twilio audio ⇄ Deepgram Agent
  * Manages per-call state: transcript, booking, compliance
  * On call end: logs JSON, saves transcript, sends SMS (if configured)

* **Deepgram Agent**

  * STT: `nova-3`
  * LLM: `gpt-4o-mini`
  * TTS: `aura-2-thalia-en`
  * Configured with prompt + knowledge pack for Riverstone Place

* **Optional APIs**

  * `/health` → Health check
  * `/transcripts/{StreamSid}.txt` → Transcript per call

### Call Flow

1. Caller dials Twilio number.
2. Twilio TwiML `<Stream>` connects to Railway WS endpoint with `customer_phone`.
3. Server bridges audio with Deepgram Agent.
4. Agent:

   * Greets caller, collects qualification info
   * Answers questions from knowledge pack
   * Offers booking → tool response triggers booking log + SMS
5. On call end, server:

   * Finalises transcript
   * Logs JSON payload
   * Posts to webhook (if configured)
   * Sends SMS with booking confirmation + transcript (or transcript only)

---

## ⚙️ Tech Stack

* **Telephony:** [Twilio Media Streams](https://www.twilio.com/docs/voice/twiml/stream)
* **STT/TTS/Dialog:** [Deepgram Agent Converse](https://developers.deepgram.com)
* **Runtime:** Python 3.10+, `websockets`, `python-dotenv`, `twilio`, `requests`
* **Hosting:** [Railway](https://railway.app)

---

## 📦 Setup

### Requirements

```
websockets
python-dotenv
twilio
requests
```

### Environment Variables

```
DEEPGRAM_API_KEY=dg_xxx
PUBLIC_BASE_URL=https://<your-app>.up.railway.app
LOG_WEBHOOK_URL=https://example.com/logs   # optional
TWILIO_ACCOUNT_SID=ACxxx
TWILIO_AUTH_TOKEN=xxx
TWILIO_SMS_FROM=+61XXXXXXXXX
```

### Local Run

```bash
python main.py
```

Server listens on `0.0.0.0:$PORT` (default 5000).

### Railway Deploy

`railway.json`:

```json
{
  "build": { "builder": "NIXPACKS" },
  "deploy": { "startCommand": "python main.py" }
}
```

Steps:

1. Connect repo to Railway.
2. Add environment variables.
3. Deploy → copy public URL for Twilio.

---

## ☎️ Twilio Setup

TwiML Bin (Option A):

```xml
<Response>
  <Connect>
    <Stream url="wss://<YOUR_PUBLIC_HOST>/twilio" track="both_tracks">
      <Parameter name="customer_phone" value="{{From}}"/>
    </Stream>
  </Connect>
</Response>
```

Expected server log:

```
WS connected path=/twilio negotiated_subprotocol=audio
```

---

## ✅ Testing Checklist

1. Call Twilio number.
2. Logs show `negotiated_subprotocol=audio`.
3. Talk → hang up.
4. Check logs for `TRANSCRIPT_URL` and `CALL_LOG`.
5. Open transcript URL → transcript visible.
6. If Twilio vars set → SMS received with transcript (and booking info if booked).

---

## 🔧 Troubleshooting

* **Stream stops immediately:** TwiML misconfigured; ensure `<Stream>` URL uses `wss://<your-app>/twilio`.
* **No SMS:** Check Twilio vars and confirm number is in E.164 (`+61…`). See Twilio Messaging Logs.
* **Transcript 404:** Call didn’t reach `stop` event; check streamSid.
* **Booking not detected:** Ensure agent calls `book_appointment` tool.

---

## 🔒 Compliance & Security

* STOP/unsubscribe suppresses SMS.
* No financial/tax/FIRB advice given.
* Transcript URLs are public if base URL is set; restrict if needed.

