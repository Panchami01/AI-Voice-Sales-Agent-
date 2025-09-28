# Riverstone Place – Voice Sales Agent

This project implements a professional **voice sales agent** for **Riverstone Place**, a fictional apartment development in Abbotsford, VIC (Melbourne, Australia).  
It was built as part of a take-home test and demonstrates a **Twilio + Deepgram Agent + Python** pipeline with booking, compliance, transcript, and logging features.

---

## ✨ Features

- 📞 **Inbound Calls via Twilio** – caller hears a natural Polly greeting and is connected to an AI sales agent.
- 🎙 **Low-latency, barge-in conversation** – agent allows interruptions and clarifies when mishearing.
- 📑 **Qualification workflow** – collects and confirms: name, phone, email, budget, bedrooms, parking, buyer type, move-in timeframe, finance status, preferred suburbs.
- ❓ **Knowledge-based answers** – agent answers only from the knowledge pack provided, with objection handling and recommendations.
- 📅 **Booking API** – `/book_appointment` validates allowed AEST time slots and returns spec JSON.
- 📝 **Transcript storage** – per-call transcript saved and served as plain text at `/transcripts/{CallSid}.txt`.
- 🎧 **Recording URL** – Twilio recording started at call connect; final `.mp3` link included in the log.
- 📊 **Logging** – per-call JSON log with timestamp, caller_cli, summary, qualification, booking, compliance flags, transcript & recording URLs at `/logs/latest`.
- 🛡 **Compliance** – supports STOP/unsubscribe phrases; declines FIRB/financial/tax/rental yield queries and offers referral.

---

## 🛠 Architecture

The system is split into two lightweight services hosted on Render:

- **API Service (HTTP, `riverstone-api`)**
  - `GET /twiml` → Returns TwiML (greeting, start recording, connect to media stream)
  - `POST /webhooks/recording` → Stores Twilio recording URL
  - `POST /book_appointment` → Validates AEST slots, returns booking JSON
  - `GET /logs/latest` → Last call log JSON
  - `GET /transcripts/{CallSid}.txt` → Transcript per call

- **WS Service (WSS, `riverstone-ws`)**
  - `wss://.../twilio` → Receives Twilio Media Streams, relays to Deepgram Agent, streams TTS back to Twilio

- **Deepgram Agent**
  - Listens (STT), thinks (LLM, GPT-4o-mini), and speaks (TTS, Aura-2 Thalia EN)
  - Configured for μ-law 8k audio (compatible with Twilio)
  - Implements sales agent prompt, qualification, objections, booking flow

- **Twilio**
  - Handles inbound calls
  - Plays initial greeting (Polly Nicole)
  - Streams audio to/from WS Service
  - Starts/stops recordings, posts RecordingUrl back to API

### Flow

1. Caller dials Twilio number.  
2. Twilio requests `/twiml` from API service.  
3. TwiML:
   - Greets caller
   - Starts recording → `/webhooks/recording`
   - Connects call to `wss://riverstone-ws.onrender.com/twilio`  
4. WS service bridges Twilio audio ⇄ Deepgram Agent in real time.  
5. Agent:
   - Collects qualification info
   - Answers questions from knowledge pack
   - Calls `/book_appointment` when user selects slot  
6. API service exposes:
   - Transcript at `/transcripts/{CallSid}.txt`
   - Recording URL (from Twilio)
   - JSON log at `/logs/latest`  



## ⚙️ Tech Stack

- **Telephony:** [Twilio Media Streams](https://www.twilio.com/docs/voice/twiml/stream)  
- **STT/TTS/Dialog:** [Deepgram Agent Converse](https://developers.deepgram.com) (Nova-3, Aura-2 Thalia EN)  
- **Runtime:** Python 3.12, `websockets`, `aiohttp`, `python-dotenv`  
- **Hosting:** [Render](https://render.com) – two services:  
  - `riverstone-api` → HTTP endpoints  
  - `riverstone-ws` → WSS media stream endpoint  

---

## 📦 Setup (Local)

1. Clone repo & install deps:
   ```bash
   pip install -r requirements.txt
