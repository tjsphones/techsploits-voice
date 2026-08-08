# Techsploits Voice Agent (your Nous brain on the phone)

A real-time phone agent for Brent's repair shop. SignalWire carries the call
and the audio; **your Nous model (the same brain behind Hermes) is the brain**,
with Brent's shop/context injected so it isn't a blank slate. A small server on
Render runs the live loop:

```
caller -> SignalWire <Connect><Stream> (wss) -> Render server
   -> STT (Deepgram) -> Nous LLM (+ shop_context.md) -> TTS -> audio back
```

Mid-call, the agent can call `send_sms` / `send_email` via the /swaig webhook.

## Honest notes
- The "brain" is Nous's **public** inference API (inference-api.nousresearch.com),
  reached with a Nous API key — NOT through Hermes's internal gateway (that
  token can't be exported to Render). Same model, public endpoint.
- Free Render tier **sleeps**; a sleeping server can't answer a ringing phone.
  For calls that must never miss, use the `starter` plan (~$7/mo, always on) in
  render.yaml, or host on an always-on machine.
- Speech (STT/TTS) is a separate service (Deepgram + ElevenLabs here). Cost is
  per-minute-ish, not the $0.16 flat SignalWire rate.

## 1. Get keys (free)
- Nous key: https://portal.nousresearch.com  ->  NOUS_API_KEY
- Deepgram key: https://deepgram.com  ->  DEEPGRAM_API_KEY
- ElevenLabs key (optional): https://elevenlabs.io  ->  ELEVENLABS_API_KEY
- SignalWire creds are already in C:\Users\tl\AppData\Local\hermes\.env
  (Project ID, Signing Key, Space URL, Token, Phone 406-416-6665).

## 2. Deploy to Render
1. `git init` this folder, push to a GitHub repo.
2. render.com -> New -> Web Service -> connect repo.
3. render.yaml is auto-detected. For always-on: change `plan: free` to
   `plan: starter`.
4. In Render dashboard -> Environment, set the secrets (paste from your .env):
   SIGNALWIRE_SIGNING_KEY, NOUS_API_KEY, DEEPGRAM_API_KEY, ELEVENLABS_API_KEY,
   SIGNALWIRE_PROJECT_ID, SIGNALWIRE_SPACE_URL, SIGNALWIRE_PHONE.
5. Deploy. Copy the deployed URL, e.g. https://techsploits-voice.onrender.com
6. Set env PUBLIC_WS_URL = wss://techsploits-voice.onrender.com/ws  (or it
   derives from the request Host, which works for wss on Render).

## 3. Point the phone number at it
In SignalWire dashboard -> Phone Numbers -> 406-416-6665 -> Set the
"Voice URL" / request handler to:  https://<your-render-app>/voice
(Method POST). SignalWire will fetch that cXML when the number is called, which
opens the bidirectional <Stream> to your server.

## 4. Test
- Call 406-416-6665 from another phone. You should hear the agent greet you.
- Watch Render logs for "caller:" / "agent :" lines.
- DRY_RUN=true in env disables all network calls (STT/LLM/TTS return stubs) so
  you can verify wiring safely.

## 5. Mid-call SMS / email
The /swaig endpoint receives `send_sms` / `send_email` actions. To make them
real, implement the TODO blocks in app.py:
- SMS: SignalWire REST "Send Message" with SIGNALWIRE_TOKEN.
- Email: SMTP via EMAIL_SMTP_* (your Gmail app password) or a provider.

## Files
- app.py          - WebSocket voice loop + /voice cXML + /swaig webhook
- nous_client.py  - the brain (OpenAI-compat relay to Nous + shop context)
- stt.py / tts.py - speech adapters (Deepgram / ElevenLabs, dry_run fallback)
- shop_context.md - what the agent knows about the shop (edit freely)
- smoketest.py    - offline test (DRY_RUN, no creds): `python smoketest.py`
- render.yaml     - Render deploy config
