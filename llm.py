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

import base64
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
        # The chat model is already multimodal, so vision needs no second id.
        "vision": True,
    },
    {
        "name": "groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model_env": "GROQ_MODEL_NAME",
        # llama-3.3-70b-versatile was decommissioned on 2026-08-16; every call to
        # it returns an error, which silently cut the cascade's biggest tier.
        # gpt-oss-120b is Groq's designated successor on the free tier. If Render
        # still pins GROQ_MODEL_NAME to the old id it wins over this default -
        # the variable must be cleared there, not just here.
        "model": "openai/gpt-oss-120b",
        # gpt-oss-120b is text-only; photos go to a vision model instead.
        # Groq's vision line churns as fast as its text line (llama-4-scout is
        # already gone), so the id is overridable from Render.
        "vision": True,
        "vision_model": "qwen/qwen3.6-27b",
        "vision_model_env": "GROQ_VISION_MODEL_NAME",
        # Qwen thinks in <think> tags before answering, and on the free tier
        # that thinking alone can eat the whole output budget (1,000 tokens a
        # minute) while the reply itself never arrives. "none" buys a direct
        # answer. Vision-only: the text tier's behaviour stays untouched.
        "vision_extra": {"reasoning_effort": "none"},
    },
    {
        "name": "openrouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL_NAME",
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        # The default free model is text-only; never show it a photo.
        "vision": False,
    },
)


def available() -> list:
    """The providers that actually have a key set, in order of preference."""
    return [p["name"] for p in PROVIDERS if os.environ.get(p["key_env"])]


def _model_for(provider: dict) -> str:
    return os.environ.get(provider["model_env"]) or provider["model"]


def _ask_one(provider: dict, messages: list, max_tokens: int, temperature: float,
             model: str = None, extra: dict = None) -> str:
    key = os.environ.get(provider["key_env"])
    body = {
        "model": model or _model_for(provider),
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if extra:
        body.update(extra)
    response = requests.post(
        provider["url"],
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    body = response.json()
    return (body["choices"][0]["message"]["content"] or "").strip()


def _shrink_for_tpm(messages: list) -> list:
    """Trims every oversized text content to its head and tail.

    Groq's free tier counts a request against an 8,000-token-per-minute budget
    and refuses the whole call with HTTP 413 when one request is too fat - a
    long conversation injected into the prompt does exactly that. Answering
    from the ends of the context (persona and instructions live at the head of
    the system message, the latest exchanges at the tail of the user turn) is
    strictly better than not answering at all.
    """
    shrunk = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str) and len(content) > 5000:
            content = (content[:2000]
                       + "\n...[cut to fit the token budget]...\n"
                       + content[-2000:])
        shrunk.append({**message, "content": content})
    return shrunk


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
        shrunk = False
        try:
            answer = _ask_one(provider, messages, max_tokens, temperature)
            if answer:
                logger.info(f"LLM answered by {provider['name']}")
                return answer
            logger.warning(f"{provider['name']} returned an empty answer; falling through")
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 413 and not shrunk:
                # Too many tokens for one minute's budget, not a refusal of the
                # request itself: retry once with a trimmed prompt before
                # falling through.
                logger.warning(f"{provider['name']} refused with HTTP 413; retrying with a trimmed prompt")
                shrunk = True
                messages = _shrink_for_tpm(messages)
                try:
                    answer = _ask_one(provider, messages, max_tokens, temperature)
                    if answer:
                        logger.info(f"LLM answered by {provider['name']} after trimming the prompt")
                        return answer
                    logger.warning(f"{provider['name']} returned an empty answer; falling through")
                except Exception as e2:
                    logger.warning(f"{provider['name']} failed even trimmed ({e2}); falling through")
                continue
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



def _vision_model_for(provider: dict) -> str | None:
    """The model to show a photo to, or None when the provider is text-only."""
    if not provider.get("vision"):
        return None
    env_name = provider.get("vision_model_env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    return provider.get("vision_model") or _model_for(provider)


_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def _strip_thinking(text: str) -> str:
    """Thinking models wrap their reasoning in <think> tags; the reply is what
    remains. A reply that is only thinking counts as empty, like any other."""
    return _THINK.sub("", text or "").strip()


def ask_image(image_bytes: bytes, mime_type: str, prompt: str, system: str = "",
              max_tokens: int = 2000, temperature: float = 0.2, skip: tuple = ()) -> str | None:
    """ask(), for a photo: the first vision-capable provider with quota answers.

    The image travels inline as a base64 data URI, the one shape every
    OpenAI-compatible vision endpoint agrees on. Providers whose configured
    model is text-only are skipped outright - sending them pixels is how a
    photo gets a blind description, the exact failure this function exists to
    prevent. None means no vision tier answered, and the caller must say so
    honestly rather than guess what the photo shows.

    max_tokens is generous on purpose: the current vision models think inside
    <think> tags before answering, and the budget must pay for both the
    thinking and the reply - a tight cap truncates inside the thinking and
    returns nothing at all.
    """
    data_uri = f"data:{mime_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": data_uri}},
        {"type": "text", "text": prompt},
    ]})

    tried = []
    for provider in PROVIDERS:
        if provider["name"] in skip:
            continue
        model = _vision_model_for(provider)
        if not model:
            continue
        if not os.environ.get(provider["key_env"]):
            continue
        tried.append(provider["name"])
        try:
            answer = _strip_thinking(
                _ask_one(provider, messages, max_tokens, temperature,
                         model=model, extra=provider.get("vision_extra")))
            if answer:
                logger.info(f"Image answered by {provider['name']} ({model})")
                return answer
            logger.warning(f"{provider['name']} returned an empty answer; falling through")
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            logger.warning(f"{provider['name']} refused the image with HTTP {status}; falling through")
        except Exception as e:
            logger.warning(f"{provider['name']} failed on the image ({e}); falling through")

    if tried:
        logger.error(f"Every vision provider failed: {', '.join(tried)}")
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
