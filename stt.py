"""
stt.py — speech-to-text adapter.

We use Deepgram (NOUS/SignalWire don't give us a stand-alone public STT key we
can host). Deepgram handles telephony audio (mulaw/16k) natively.

Falls back to a DRY_RUN stub when no DEEPGRAM_API_KEY is set, so the loop can
be tested offline.
"""
import os
import requests

DEEPGRAM_KEY = os.getenv("DEEPGRAM_API_KEY", "")
DEEPGRAM_URL = (
    "https://api.deepgram.com/v1/listen"
    "?model=nova-2&smart_format=true&encoding=mulaw&sample_rate=8000"
)


def transcribe(mulaw_bytes: bytes) -> str:
    """mulaw_bytes: raw 8kHz mu-law audio. Returns transcript text."""
    if not DEEPGRAM_KEY or os.getenv("DRY_RUN", "false").lower() == "true":
        return "[DRY_RUN transcript]"
    r = requests.post(
        DEEPGRAM_URL,
        data=mulaw_bytes,
        headers={
            "Authorization": f"Token {DEEPGRAM_KEY}",
            "Content-Type": "audio/x-mulaw",
        },
        timeout=15,
    )
    r.raise_for_status()
    ch = r.json()["results"]["channels"][0]["alternatives"][0]
    return ch.get("transcript", "")
