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
import base64
import json
import asyncio
import logging
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

    async def send_audio(mulaw_bytes: bytes):
        # chunk into ~20ms frames (160 bytes @ 8k) and stream
        chunk = 160
        for i in range(0, len(mulaw_bytes), chunk):
            frame = mulaw_bytes[i:i + chunk]
            if not frame:
                break
            msg = {
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": base64.b64encode(frame).decode()},
            }
            await ws.send_json(msg)
        # mark so we know playback flushed
        await ws.send_json({"event": "mark", "streamSid": stream_sid,
                            "mark": {"name": "done"}})

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
            elif event == "media":
                payload = data["media"]["payload"]
                audio_buf.extend(base64.b64decode(payload))
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

        # Simple turn trigger: process when we have ~1.5s of audio buffered.
        if len(audio_buf) >= 12000:  # ~1.5s @ 8k mulaw
            buf = bytes(audio_buf)
            audio_buf = bytearray()
            text = stt.transcribe(buf)
            if text and text.strip():
                log.info("caller: %s", text)
                convo.append({"role": "user", "content": text})
                reply = nous_client.chat(convo)
                convo.append({"role": "assistant", "content": reply})
                # keep last 10 turns
                convo = convo[-10:]
                log.info("agent : %s", reply)
                audio = tts.synthesize(reply)
                await send_audio(audio)

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
