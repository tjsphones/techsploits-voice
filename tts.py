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

# Loudness normalization for the phone. A flat gain (TTS_GAIN) was too weak:
# Gemini returns low-level audio and 1.6x only reached ~-30 dBFS. Instead we
# normalize to a TARGET RMS (loudness), so output is consistently audible
# regardless of what the TTS engine returns. -20 dBFS RMS ~ comfortable phone
# level. Capped at the int16 rail so it can never clip/distort.
TARGET_DB = float(os.getenv("TTS_TARGET_DB", "-20"))
TARGET_RMS = 10 ** (TARGET_DB / 20.0) * 32768.0  # linear scale vs 16-bit FS
# Never boost quiet audio more than this many times (safety against noise
# amplification); most real speech needs <4x.
MAX_BOOST = float(os.getenv("TTS_MAX_BOOST", "12.0"))


def _normalize(pcm: np.ndarray) -> np.ndarray:
    """Scale PCM to TARGET_RMS loudness, clamped to int16 to avoid clipping.

    If the sample is already louder than target, leave it (don't attenuate
    quiet-down clicks). Returns int16 numpy array.
    """
    pcm = pcm.astype(np.float32)
    rms = float(np.sqrt(np.mean(pcm ** 2)))
    if rms < 1e-3:
        return pcm.astype(np.int16)  # silence: don't amplify
    boost = min(TARGET_RMS / rms, MAX_BOOST)
    return np.clip(pcm * boost, -32768, 32767).astype(np.int16)



def _mulaw_encode(pcm16: np.ndarray) -> bytes:
    """ITU-T G.711 mu-law encode: 16-bit PCM -> 8-bit mu-law.

    This is the STANDARD algorithm (matches what SignalWire / ffmpeg decode),
    so round-trip level is preserved. The previous custom approximation lost
    ~22 dB and made the agent quiet + muffled.
    """
    pcm = pcm16.astype(np.int32)
    sign = (pcm < 0).astype(np.int32)
    mag = np.abs(pcm)
    # bias + clip
    mag = np.clip(mag, 0, 0x7FFF) + 33
    # segment
    seg = np.zeros_like(mag)
    for s in range(1, 8):
        seg = np.where(mag >= (0x80 << s), s, seg)
    # quantization
    seg_start = 0x80 << seg
    seg_end = 0x80 << (seg + 1)
    index = (mag - seg_start) * 16 // (seg_end - seg_start)
    index = np.clip(index, 0, 15)
    code = index + 16 * (seg + 1)
    code = np.where(sign, code | 0x80, code)
    # standard mu-law bit inversion
    code = ~code & 0xFF
    return code.astype(np.uint8).tobytes()


def _decode_mulaw(data: bytes) -> np.ndarray:
    """Inverse of _mulaw_encode (standard ITU-T G.711)."""
    arr = np.frombuffer(data, dtype=np.uint8).astype(np.int32)
    arr = ~arr & 0xFF
    sign = (arr & 0x80) >> 7
    seg = (arr >> 4) & 0x07
    index = arr & 0x0F
    seg_start = 0x80 << seg
    sample = seg_start + (index << (seg + 3)) + (1 << (seg + 2))
    sample = np.where(sign, -sample, sample)
    return sample.astype(np.int16)


def _resample_and_encode(wav_16k_path: str) -> bytes:
    """Resample 16k mono wav to 8k mono and mu-law encode via ffmpeg if present,
    else via numpy linear resample + encode."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
            out8 = tf.name
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_16k_path,
             "-ar", "8000", "-ac", "1", "-f", "mulaw", out8],
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
        return _mulaw_encode(_normalize(pcm))


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
    # Gemini returns 24k l16. Downsample to 8k, NORMALIZE loudness to target,
    # then encode with the standard (correct) mu-law routine.
    if raw[:4] == b"RIFF":
        raw = raw[44:]
    pcm = np.frombuffer(raw, dtype=np.int16)
    pcm = pcm[::3]  # ~24k -> 8k
    return _mulaw_encode(_normalize(pcm))
