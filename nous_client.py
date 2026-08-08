"""
nous_client.py — relay call conversation to the Nous inference API.

This is the "brain". It speaks the OpenAI chat-completions protocol against
Nous's OpenAI-compatible endpoint, and injects Brent's shop/context so the
phone agent actually KNOWS what's pertinent (the whole reason we didn't just
let SignalWire's generic brain take the call).

Secret: NOUS_API_KEY is read from the environment; never hard-coded.
"""
import os
import requests

BASE_URL = os.getenv("NOUS_BASE_URL", "https://inference-api.nousresearch.com/v1")
MODEL = os.getenv("NOUS_MODEL", "tencent/hy3:free")
API_KEY = os.getenv("NOUS_API_KEY", "")

# Brent's background context. Loaded from shop_context.md so it can be edited
# without touching code. This is what makes the agent "know the shop".
try:
    _CTX_PATH = os.path.join(os.path.dirname(__file__), "shop_context.md")
    with open(_CTX_PATH, "r", encoding="utf-8") as _f:
        SHOP_CONTEXT = _f.read().strip()
except FileNotFoundError:
    SHOP_CONTEXT = ""


def build_system_prompt() -> str:
    return (
        "You are the voice assistant for Techsploits, a computer, electronics, "
        "and phone repair shop owned by Brent. You speak naturally on the phone "
        "with customers. Keep replies short and conversational — one or two "
        "sentences when possible, because you are talking, not texting.\n\n"
        "Your knowledge of the shop and owner (so you are not a blank slate):\n"
        f"{SHOP_CONTEXT}\n\n"
        "Rules:\n"
        "- Be friendly, plain-spoken, and helpful.\n"
        "- If you need to book a repair or take a message, you may call the "
        "send_sms or send_email function.\n"
        "- Do not invent prices; say you'll confirm and can text/email details.\n"
        "- Never read out full credit-card or SSN numbers.\n"
    )


def chat(messages: list[dict], temperature: float = 0.4) -> str:
    """messages: list of {role, content}. Returns the assistant's text reply."""
    payload = {
        "model": MODEL,
        "messages": [{"role": "system", "content": build_system_prompt()}, *messages],
        "temperature": temperature,
        "max_tokens": 300,
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    if os.getenv("DRY_RUN", "false").lower() == "true":
        # No network call in dry-run; echo a stub so the loop is testable.
        last = messages[-1]["content"] if messages else ""
        return f"[DRY_RUN] I heard: {last[:80]}"
    r = requests.post(
        f"{BASE_URL}/chat/completions",
        json=payload,
        headers=headers,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


if __name__ == "__main__":
    print(chat([{"role": "user", "content": "What are your hours?"}]))
