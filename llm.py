"""One question in, one answer out - asked of whichever free model still has quota.

Why this module exists at all. The conversational assistant runs on Gemini's
free tier, which allows 20 generate_content requests per day per model. That is
a real ceiling, not a theoretical one: production has already returned 429 to
Itai mid-conversation. Twenty requests cannot also pay for a proactive layer
that reads mail, triages it and asks questions, so everything proactive was
built rule-based - correct, but unable to reason.

Groq's free tier allows 1,000 requests a day for llama-3.3-70b-versatile, and
OpenRouter another 50 across its free models. Both are free without a credit
card. Stacking them behind Gemini turns the 20/day ceiling into roughly 1,070 -
enough that a routine can afford to think about each email - and costs nothing.

The three speak the same OpenAI-compatible protocol, so there is one request
shape here and three configurations of it, not three clients. Gemini keeps its
own SDK in assistant.py, because the conversation needs tool calling and this
module deliberately does not: it is text in, text out, which is all a routine
ever needs and the only thing all three providers agree on.

A provider is skipped when its key is missing, and abandoned for the next one
the moment it says no. Every provider failing returns None rather than raising,
because the callers are heartbeat routines: an assistant that cannot think must
go quiet, never take the endpoint down with it.
"""

import json
import logging
import os
import re

import requests

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30

# Order is the whole design: cheapest ceiling first, so the big free tier is
# still untouched when the conversation needs it. Gemini leads because it is
# the model Itai is already talking to and its answers are the ones he knows.
PROVIDERS = (
    {
        "name": "gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL_NAME",
        "model": "gemini-3.6-flash",
    },
    {
        "name": "groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL_NAME",
        "model": "llama-3.3-70b-versatile",
    },
    {
        "name": "openrouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL_NAME",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
    },
)


def available() -> list:
    """The providers that actually have a key set, in order of preference."""
    return [p["name"] for p in PROVIDERS if os.environ.get(p["key_env"])]


def _model_for(provider: dict) -> str:
    return os.environ.get(provider["model_env"]) or provider["model"]


def _ask_one(provider: dict, messages: list, max_tokens: int, temperature: float) -> str:
    key = os.environ.get(provider["key_env"])
    response = requests.post(
        provider["url"],
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json={
            "model": _model_for(provider),
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    return (body["choices"][0]["message"]["content"] or "").strip()


def ask(prompt: str, system: str = "", max_tokens: int = 600,
        temperature: float = 0.2, skip: tuple = ()) -> str | None:
    """Asks the first provider that has quota, and returns None if none do.

    None is a real answer here and callers must handle it: it means the routine
    should fall back to whatever it would have done without a model, which for
    every current caller is a rule-based path that still works.

    skip names providers to pass over. The conversation uses it after Gemini has
    already answered 429 on its own SDK: asking it again over HTTP would spend a
    round trip to be told the same thing.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    tried = []
    for provider in PROVIDERS:
        if provider["name"] in skip:
            continue
        if not os.environ.get(provider["key_env"]):
            continue
        tried.append(provider["name"])
        try:
            answer = _ask_one(provider, messages, max_tokens, temperature)
            if answer:
                logger.info(f"LLM answered by {provider['name']}")
                return answer
            logger.warning(f"{provider['name']} returned an empty answer; falling through")
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            # 429 is the daily free-tier quota and will not clear on a retry;
            # every other error is equally a reason to ask somebody else. There
            # is no retry here on purpose - the next provider IS the retry.
            logger.warning(f"{provider['name']} refused with HTTP {status}; falling through")
        except Exception as e:
            logger.warning(f"{provider['name']} failed ({e}); falling through")

    if not tried:
        logger.info("No LLM provider is configured - set GROQ_API_KEY for a free 1,000/day tier")
    else:
        logger.error(f"Every LLM provider failed: {', '.join(tried)}")
    return None


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def ask_json(prompt: str, system: str = "", max_tokens: int = 600, skip: tuple = ()):
    """ask(), for the callers that need a structured answer rather than prose.

    Small models wrap JSON in a code fence and add a sentence of preamble however
    firmly they are told not to, so the fence is stripped and, failing that, the
    outermost braces are located. Returns None when nothing parses - a malformed
    answer and a dead provider mean the same thing to the caller.
    """
    raw = ask(prompt, system=system, max_tokens=max_tokens, temperature=0.0, skip=skip)
    if not raw:
        return None

    fenced = _FENCE.search(raw)
    if fenced:
        raw = fenced.group(1).strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    logger.warning(f"LLM answer was not JSON: {raw[:200]}")
    return None
