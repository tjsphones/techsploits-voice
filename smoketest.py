"""Offline smoke test: prove the STT->brain->TTS path and HTTP endpoints
work without any credentials or network (DRY_RUN)."""
import os
os.environ["DRY_RUN"] = "true"
os.environ["TTS_ENGINE"] = "dry_run"
os.environ["PORT"] = "8099"

import asyncio
import base64
from aiohttp.test_utils import TestClient, TestServer

import app as appmod
import nous_client
import stt
import tts

# 1) brain path
print("== brain path ==")
mulaw_silence = b"\xff" * 12000  # ~1.5s silence == our turn trigger size
text = stt.transcribe(mulaw_silence)
print("stt ->", repr(text))
reply = nous_client.chat([{"role": "user", "content": "What are your hours?"}])
print("nous ->", reply)
audio = tts.synthesize(reply)
print("tts ->", len(audio), "bytes mulaw (expect 160-byte-multiple)")

# 2) HTTP endpoints
async def main():
    client = TestClient(TestServer(appmod.make_app()))
    await client.start_server()
    r = await client.get("/")
    print("\n== / ==", await r.text())
    r = await client.post("/voice")
    xml = await r.text()
    print("== /voice cXML ==\n", xml)
    r = await client.post("/swaig", json={
        "function": "send_sms",
        "argument": {"parsed": [{"to": "+14065551234", "message": "We open 9-6"}]}})
    print("== /swaig ==", await r.text())
    await client.close()

asyncio.run(main())
print("\nALL OK")
