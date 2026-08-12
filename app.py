"""
app.py — SignalWire <Connect><Stream> voice-loop server.

Flow per call (one WebSocket connection per call):
  WebSocket connect -> "connected" -> "start" (mediaFormat)
  inbound "media" frames (base64 mu-law 8k) accumulate -> when silence/VAD, STT
  -> Nous brain (with Brent's context) -> TTS -> send media frames back
  -> "mark" -> wait -> repeat -> "stop"

Also exposes:
  GET  /              health check
  POST /voice         returns cXML that opens the bidirectional <Stream> to /ws
  POST /swaig         SWAIG-style webhook: send_sms / send_email mid-call
"""
import os
import re
import base64
import json
import asyncio
import logging
import urllib.request
import urllib.error
from aiohttp import web, WSMsgType

import stt
import tts
import nous_client

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voice")

PORT = int(os.getenv("PORT", "8080"))
AUTH_TOKEN = os.getenv("SIGNALWIRE_SIGNING_KEY", "")  # optional bearer check
WS_PATH = "/ws"
STREAM_URL = os.getenv("PUBLIC_WS_URL", "")  # e.g. wss://techsploits-voice.onrender.com/ws

# SignalWire creds for outbound SMS (deliver messages to Brent)
SW_PROJECT = os.getenv("SIGNALWIRE_PROJECT_ID", "")
SW_TOKEN = os.getenv("SIGNALWIRE_TOKEN", "")
SW_SPACE = os.getenv("SIGNALWIRE_SPACE_URL", "").replace("https://", "").strip("/")
SW_FROM = os.getenv("SIGNALWIRE_PHONE", "406-416-6665")
BRENT_CELL = os.getenv("BRENT_CELL", "406-590-8432")

# Matches the hidden [[SMS:...]] tag Chris appends (caller never hears it)
_SMS_RE = re.compile(r"\[\[SMS:(.*?)\]\]", re.DOTALL)


def deliver_to_brent(reply: str):
    """Strip the [[SMS:...]] tag from the spoken reply and text Brent its
    contents via SignalWire. Returns (spoken_text, sms_text_or_None)."""
    m = _SMS_RE.search(reply)
    if not m:
        return reply, None
    sms_text = m.group(1).strip()
    spoken = _SMS_RE.sub("", reply).strip()
    if not SW_PROJECT or not SW_TOKEN or not SW_SPACE:
        log.warning("[sms] not configured (missing SignalWire env); skipping send")
        return spoken, sms_text  # still return sms_text so it's logged
    url = f"https://{SW_SPACE}/api/laml/2010-04-01/Accounts/{SW_PROJECT}/Messages.json"
    data = urllib.parse.urlencode({
        "From": SW_FROM, "To": BRENT_CELL, "Body": f"Techsploits call: {sms_text}"
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization",
                   "Basic " + base64.b64encode(f"{SW_PROJECT}:{SW_TOKEN}".encode()).decode())
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            log.info("[sms] sent to Brent (%d): %s", r.status, sms_text[:80])
    except Exception as e:
        log.error("[sms] FAILED to send to Brent: %s | text=%s", e, sms_text[:80])
    return spoken, sms_text


# ---------------- cXML endpoint ----------------
async def voice_cxml(request: web.Request) -> web.Response:
    """SignalWire calls this when the number is dialed. Returns cXML that
    opens a BIDIRECTIONAL stream to our WebSocket, so we both hear and speak."""
    ws = STREAM_URL or f"wss://{request.headers.get('Host','localhost')}{WS_PATH}"
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{ws}" />
  </Connect>
</Response>"""
    return web.Response(text=xml, content_type="application/xml")


# ---------------- SWAIG mid-call actions ----------------
async def swaig(request: web.Request) -> web.Response:
    """Minimal SWAIG-style endpoint. The agent (Nous) can be given these as
    tool calls; here we accept a JSON action and perform send_sms / send_email.
    In production wire this to your SMS/email provider. For now: log + DRY_RUN."""
    body = await request.json()
    fn = body.get("function")
    args = body.get("argument", {}).get("parsed", [{}])
    args = args[0] if isinstance(args, list) and args else {}
    if os.getenv("DRY_RUN", "false").lower() == "true":
        log.info("[DRY_RUN] swaig %s args=%s", fn, args)
        return web.json_response({"response": f"[dry_run] would {fn}: {args}"})
    if fn == "send_sms":
        # TODO: call SignalWire REST Send Message with SIGNALWIRE_TOKEN
        log.info("send_sms -> %s : %s", args.get("to"), args.get("message"))
        return web.json_response({"response": "Text sent."})
    if fn == "send_email":
        # TODO: send via SMTP/provider using EMAIL_SMTP_* env
        log.info("send_email -> %s : %s", args.get("to"), args.get("subject"))
        return web.json_response({"response": "Email sent."})
    return web.json_response({"response": "unknown function"})


# ---------------- WebSocket voice loop ----------------
async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Optional bearer auth. NOTE: SignalWire's native <Stream> does NOT send a
    # bearer token, so we must NOT close the connection on a missing/!matching
    # header — that would reject every real inbound call. Log only.
    auth = request.headers.get("Authorization", "")
    if AUTH_TOKEN and not auth.endswith(AUTH_TOKEN):
        log.warning("ws auth token absent/mismatched (allowing: SignalWire "
                     "stream sends no bearer)")

    stream_sid = None
    audio_buf = bytearray()
    convo = []  # rolling {role, content}
    last_audio_ts = 0.0   # monotonic time of last media frame
    processing = False     # true while a turn is in flight (avoid overlap)

    async def send_audio(mulaw_bytes: bytes):
        # chunk into ~20ms frames (160 bytes @ 8k) and stream
        chunk = 160
        if ws.closed:
            log.warning("send_audio: ws already closed, skipping")
            return
        try:
            for i in range(0, len(mulaw_bytes), chunk):
                frame = mulaw_bytes[i:i + chunk]
                if not frame:
                    break
                if ws.closed:
                    log.warning("send_audio: ws closed mid-send (caller hung up)")
                    return
                msg = {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": base64.b64encode(frame).decode()},
                }
                await ws.send_json(msg)
                # Pace frames at real-time (~20ms each) so SignalWire plays
                # them at the correct rate. Blasting them instantly garbles
                # the agent's voice on the caller's end.
                await asyncio.sleep(0.02)
            # mark so we know playback flushed
            if not ws.closed:
                await ws.send_json({"event": "mark", "streamSid": stream_sid,
                                    "mark": {"name": "done"}})
        except Exception as e:
            log.warning("send_audio aborted (caller likely hung up): %s", e)

    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            data = json.loads(msg.data)
            event = data.get("event")
            if event == "connected":
                log.info("ws connected")
            elif event == "start":
                stream_sid = data["start"]["streamSid"]
                fmt = data["start"].get("mediaFormat", {})
                log.info("stream start sid=%s fmt=%s", stream_sid, fmt)
                # Play a SHORT instant greeting so the caller hears a live
                # human-ish voice immediately (no dead air -> no premature hangup).
                try:
                    greet = tts.synthesize(
                        "Techsploits, this is Chris — how can I help you today?")
                    await send_audio(greet)
                except Exception as e:
                    log.warning("greeting failed: %s", e)
            elif event == "media":
                payload = data["media"]["payload"]
                audio_buf.extend(base64.b64decode(payload))
                last_audio_ts = asyncio.get_event_loop().time()
            elif event == "mark":
                pass
            elif event == "stop":
                log.info("stream stop")
                break
            elif event == "dtmf":
                log.info("dtmf %s", data.get("dtmf", {}).get("digit"))
        elif msg.type == WSMsgType.ERROR:
            log.error("ws error %s", ws.exception())
            break

        # Turn trigger: process when we have ~0.7s of buffered audio, OR when
        # ~0.5s of silence has elapsed since the last frame AND we have >=0.2s
        # buffered (so Chris answers after the caller STOPS talking, not only
        # on long utterances). Skip if a turn is already in flight.
        now = asyncio.get_event_loop().time()
        have_audio = len(audio_buf) >= 1600  # ~0.2s @ 8k mulaw
        silent_gap = (now - last_audio_ts) >= 0.5 if last_audio_ts else False
        if (not processing) and have_audio and (len(audio_buf) >= 5600 or (silent_gap and last_audio_ts)):
            buf = bytes(audio_buf)
            audio_buf = bytearray()
            last_audio_ts = 0.0
            processing = True
            try:
                text = stt.transcribe(buf)
                if text and text.strip():
                    log.info("caller: %s", text)
                    convo.append({"role": "user", "content": text})
                    try:
                        reply = nous_client.chat(convo)
                    except Exception as e:
                        log.error("brain error: %s", e)
                        reply = "I'm sorry, I didn't catch that — could you repeat?"
                    convo.append({"role": "assistant", "content": reply})
                    # keep last 10 turns
                    convo = convo[-10:]
                    log.info("agent : %s", reply)
                    # Chris may have embedded a [[SMS:...]] tag — strip it from
                    # what the caller hears, and deliver the contents to Brent.
                    spoken, sms = deliver_to_brent(reply)
                    if sms:
                        log.info("agent delivered to Brent via SMS: %s", sms)
                    try:
                        audio = tts.synthesize(spoken)
                    except Exception as e:
                        log.error("tts error: %s", e)
                        audio = b""
                    if audio:
                        await send_audio(audio)
                    else:
                        log.warning("no audio to send (empty reply/tts)")
                else:
                    log.info("caller audio but empty transcript (VAD/STT silent)")
            except Exception as e:
                log.error("turn processing crashed: %s", e)
            finally:
                processing = False

    await ws.close()
    return ws


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    app.router.add_post("/voice", voice_cxml)
    app.router.add_post("/swaig", swaig)
    app.router.add_get(WS_PATH, ws_handler)
    app.router.add_post(WS_PATH, ws_handler)
    return app


if __name__ == "__main__":
    app = make_app()
    log.info("Starting voice loop on :%d (DRY_RUN=%s)", PORT,
             os.getenv("DRY_RUN", "false"))
    web.run_app(app, host="0.0.0.0", port=PORT)
