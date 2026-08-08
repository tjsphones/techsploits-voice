"""
nous_client.py — two-tier "brain" router for the Techsploits voice agent.

FRONT model (fast, non-reasoning, e.g. Google Gemma via OpenRouter) handles
light conversational turns: greetings, hours, booking, simple FAQs — ~1-2s.
If a question needs real reasoning (diagnosis, tradeoffs, anything it's unsure
about) it replies with the single word ESCALATE and we hand off to the REASON
model (Nous tencent/hy3:free) — your "heavy" brain — which answers with full
shop context. Most calls never escalate; the slow model only runs when needed.

Secrets from env (Hermes .env): OPENROUTER_API_KEY (front), NOUS_API_KEY (reason).
"""
import os
import requests

# ---- FRONT (fast, light conversation) ----
FRONT_BASE_URL = os.getenv("FRONT_BASE_URL", "https://openrouter.ai/api/v1")
FRONT_API_KEY  = os.getenv("FRONT_API_KEY", os.getenv("OPENROUTER_API_KEY", ""))
FRONT_MODEL    = os.getenv("FRONT_MODEL", "google/gemma-4-26b-a4b-it:free")

# ---- REASON (heavy, your Nous brain) ----
REASON_BASE_URL = os.getenv("REASON_BASE_URL", "https://inference-api.nousresearch.com/v1")
REASON_API_KEY  = os.getenv("REASON_API_KEY", os.getenv("NOUS_API_KEY", ""))
REASON_MODEL    = os.getenv("REASON_MODEL", "tencent/hy3:free")

ESCALATE_PREFIX = os.getenv("ESCALATE_PREFIX", "ESCALATE").upper()

try:
    _CTX = os.path.join(os.path.dirname(__file__), "shop_context.md")
    with open(_CTX, "r", encoding="utf-8") as _f:
        SHOP_CONTEXT = _f.read().strip()
except FileNotFoundError:
    SHOP_CONTEXT = ""


def build_front_prompt() -> str:
    return (
        "You are the first-line voice assistant for Techsploits, Brent's repair shop. "
        "You handle greetings, store hours, booking repairs, and simple FAQs quickly "
        "and warmly. Keep replies to ONE or TWO short spoken sentences.\n\n"
        "Shop facts (so you are not a blank slate):\n"
        f"{SHOP_CONTEXT}\n\n"
        "RULE: If the question needs technical diagnosis, comparing repair options, "
        "pricing decisions, or anything you are unsure about, respond with the single "
        "word ESCALATE on the FIRST line and nothing else. Otherwise just answer the "
        "customer naturally."
    )


def build_reason_prompt() -> str:
    return (
        "You are the senior voice assistant for Techsploits, a computer, electronics, "
        "and phone repair shop owned by Brent. A question was escalated to you because "
        "it needs real reasoning. Answer concisely in ONE or TWO spoken sentences — you "
        "are talking, not texting.\n\n"
        "Your knowledge of the shop and owner:\n"
        f"{SHOP_CONTEXT}\n\n"
        "Rules:\n"
        "- Be friendly, plain-spoken, helpful.\n"
        "- Do not invent prices; say you'll confirm and can text/email details.\n"
        "- Never read out full credit-card or SSN numbers."
    )


# backward-compat alias
build_system_prompt = build_reason_prompt


def _call(base, key, model, messages, max_tokens, temperature=0.4):
    """One chat-completion. DRY_RUN returns a stub (optionally ESCALATE)."""
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = m["content"]
            break
    if os.getenv("DRY_RUN", "false").lower() == "true":
        if "<<ESCALATE>>" in last_user:
            return "ESCALATE"
        return f"[DRY_RUN] I heard: {last_user[:60]}"
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    r = requests.post(
        f"{base}/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"] or ""


def chat(messages: list[dict], temperature: float = 0.4, debug: bool = False):
    """messages: list of {role, content}. Returns final spoken text.

    If debug=True, returns (text, meta) where meta describes the routing.
    Caller (app.py) uses the plain str form.
    """
    # No fast model configured -> use reason only (max privacy / single brain)
    if not FRONT_API_KEY:
        reason_msgs = [{"role": "system", "content": build_reason_prompt()}, *messages]
        final = _call(REASON_BASE_URL, REASON_API_KEY, REASON_MODEL, reason_msgs, 600)
        if debug:
            return final, {"escalated": False, "front_model": None,
                           "reason_model": REASON_MODEL}
        return final

    # FRONT pass
    front_msgs = [{"role": "system", "content": build_front_prompt()}, *messages]
    front_reply = _call(FRONT_BASE_URL, FRONT_API_KEY, FRONT_MODEL, front_msgs, 200)
    escalated = front_reply.strip().upper().startswith(ESCALATE_PREFIX)

    if escalated:
        reason_msgs = [{"role": "system", "content": build_reason_prompt()}, *messages]
        final = _call(REASON_BASE_URL, REASON_API_KEY, REASON_MODEL, reason_msgs, 600)
        result = final
    else:
        result = front_reply

    if debug:
        return result, {"escalated": escalated, "front_model": FRONT_MODEL,
                        "reason_model": (REASON_MODEL if escalated else None)}
    return result


if __name__ == "__main__":
    print(chat([{"role": "user", "content": "What are your hours?"}]))
