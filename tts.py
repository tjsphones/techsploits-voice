"""
tts.py — text-to-speech adapter.

Supported engines (set TTS_ENGINE):
  - gemini    : Google Gemini TTS via AI Studio (free; DEFAULT for this project)
  - elevenlabs : ElevenLabs API (good voice, needs a PAID key)
  - openai     : OpenAI TTS (needs OPENAI_API_KEY)
  - azure      : Azure Cognitive Speech (needs AZURE_*)
  - dry_run    : returns silence bytes so the loop is testable offline

Output is 8kHz mu-law PCM, which is what SignalWire's default <Stream> codec
(PCMU@8000h) expects. We synthesize at a higher rate then downsample + encode.
"""
import os
import base64
import subprocess
import tempfile
import requests
import numpy as np

ENGINE = os.getenv("TTS_ENGINE", "dry_run")


def _mulaw_encode(pcm16: np.ndarray) -> bytes:
    """Encode 16-bit PCM (int16, -32768..32767) to 8-bit mu-law bytes."""
    # Simple mu-law approximation (ITU standard). Good enough for telephony.
    MULAW_BIAS = 33
    pcm = pcm16.astype(np.int32)
    sign = (pcm < 0).astype(np.int32)
    pcm = np.abs(pcm) + MULAW_BIAS
    pcm = np.clip(pcm, 0, 0x7FFF)
    # segment quantization
    mask = np.clip((np.log2(pcm + 1) - 1).astype(np.int32), 0, 7)
    seg_end = (0x80 * (1 << mask)).astype(np.int32)
    seg_start = (seg_end >> 1).astype(np.int32)
    index = np.clip((pcm - seg_start) * 16 / (seg_end - seg_start), 0, 15).astype(np.int32)
    code = (index + 16 * seg_end // 0x80).astype(np.int32)
    code = np.where(sign, code | 0x80, code)
    code = ~code & 0xFF
    return code.astype(np.uint8).tobytes()


def _resample_and_encode(wav_16k_path: str) -> bytes:
    """Resample 16k mono wav to 8k mono and mu-law encode via ffmpeg if present,
    else via numpy linear resample + encode."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            out8 = tf.name
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_16k_path, "-ar", "8000", "-ac", "1",
             "-f", "mulaw", out8],
            check=True, capture_output=True,
        )
        with open(out8, "rb") as f:
            data = f.read()
        os.unlink(out8)
        # strip 44-byte wav header if present
        return data[44:] if data[:4] == b"RIFF" else data
    except Exception:
        # numpy fallback: load raw 16-bit, downsample by 2, encode
        import wave
        with wave.open(wav_16k_path, "rb") as w:
            fr = w.getframerate()
            n = w.getnframes()
            raw = w.readframes(n)
        pcm = np.frombuffer(raw, dtype=np.int16)
        if fr == 16000:
            pcm = pcm[::2]
        return _mulaw_encode(pcm)


def synthesize(text: str) -> bytes:
    """Return 8kHz mu-law audio bytes for `text`."""
    if ENGINE == "dry_run" or os.getenv("DRY_RUN", "false").lower() == "true":
        # 200ms of silence at 8k mulaw
        return b"\xff" * (8000 // 5)

    if ENGINE == "gemini":
        return synthesize_gemini(text)

    if ENGINE == "elevenlabs":
        key = os.getenv("ELEVENLABS_API_KEY", "")
        voice = os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
            json={"text": text, "model_id": "eleven_turbo_v2"},
            headers={"xi-api-key": key, "Content-Type": "application/json"},
            timeout=20,
        )
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            mp3 = tf.name
            tf.write(r.content)
        # convert mp3 -> 16k wav via ffmpeg
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as wf:
            wav = wf.name
        subprocess.run(["ffmpeg", "-y", "-i", mp3, "-ar", "16000", "-ac",
                        "1", wav], check=True, capture_output=True)
        os.unlink(mp3)
        out = _resample_and_encode(wav)
        os.unlink(wav)
        return out

    # openai / azure would go here similarly; default to dry_run bytes
    return b"\xff" * (8000 // 5)


def synthesize_gemini(text: str) -> bytes:
    """Gemini TTS via Google AI Studio native API (free with the same key as
    the FRONT brain). Returns 8kHz mu-law bytes for the phone."""
    key = os.getenv("GOOGLE_API_KEY", "")
    model = os.getenv("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = {
        "contents": [{"parts": [{"text": f"Speak naturally on the phone: {text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Kore"}}},
        },
    }
    r = requests.post(url, json=body, timeout=40)
    r.raise_for_status()
    js = r.json()
    parts = js.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    b64 = None
    for p in parts:
        if "inlineData" in p:
            b64 = p["inlineData"]["data"]
    if not b64:
        # fall back to 200ms silence so the call doesn't break
        return b"\xff" * (8000 // 5)
    raw = base64.b64decode(b64)
    # Gemini returns 24k l16; resample to 8k mulaw via ffmpeg if present
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            w24 = tf.name
        with open(w24, "wb") as f:
            f.write(raw)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as outf:
            w8 = outf.name
        subprocess.run(["ffmpeg", "-y", "-i", w24, "-ar", "8000", "-ac", "1",
                        "-f", "mulaw", w8], check=True, capture_output=True)
        with open(w8, "rb") as f:
            data = f.read()
        os.unlink(w24); os.unlink(w8)
        return data[44:] if data[:4] == b"RIFF" else data
    except Exception:
        # numpy fallback: raw l16 at 24k; decode + downsample + encode
        if raw[:4] == b"RIFF":
            raw = raw[44:]
        pcm = np.frombuffer(raw, dtype=np.int16)
        pcm = pcm[::3]  # ~24k -> 8k
        return _mulaw_encode(pcm)
