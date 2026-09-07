"""Web access for the assistant.

Kept out of assistant.py because grounding cannot simply be switched on next to
the existing tools. Gemini treats google_search as a built-in tool, and a request
that carries built-in tools alongside function declarations is rejected for most
models - so turning it on in the main conversation would have taken email,
calendar and attachments down with it.

The way round it is a second, self-contained call: the assistant calls the tool
like any other function, and this module runs a fresh one-shot request whose
only tool is the built-in one. The main conversation keeps its function
declarations and never sees a built-in tool, and the answer comes back as an
ordinary function result. It costs one extra Gemini call per lookup and no new
credentials - the existing GEMINI_API_KEY covers it.

The two halves are not equally available, measured against the live API on
2026-09-06 with the production key:

  read_web_page  (url_context)   works on the free tier.
  search_web     (google_search) returns 429 RESOURCE_EXHAUSTED on the free
                                 tier, on both models tried, while a plain call
                                 on the same key succeeds. Search grounding is
                                 billed separately from ordinary generation, so
                                 this is a plan limit, not a spent daily budget
                                 and not a bug.

Routing a search engine through url_context does not get round it - DuckDuckGo
and Bing result pages are both refused by the fetcher.

So search_web now prefers a dedicated search API over Gemini grounding, and
Tavily is the one it takes. That choice came out of a survey on 2026-09-06 of
what a free tier actually still buys:

  Google Custom Search JSON API   closed to new customers, shuts down 2027-01-01.
  Bing Web Search API             deprecated 2025-08-11.
  Brave Search API                free tier withdrawn; a card is required to sign up.
  Serper                          2,500 credits once, not monthly - it runs out.
  ddgs (DuckDuckGo)               no key at all, but unofficial scraping: it has
                                  been fingerprint-blocked since 2026-08 and
                                  breaks whenever the site changes. Not a
                                  foundation for something Itai relies on.
  Exa                             20,000 requests/month free, no card, but it is
                                  semantic "find me similar pages" search, which
                                  is the wrong shape for "when is the game".
  Tavily                          1,000 credits/month free, indefinitely, no card,
                                  built to answer an agent's question directly.

Tavily wins on fit rather than on volume. It returns a written answer plus the
pages behind it, which is exactly the shape this module already hands back, and
1,000 lookups a month is far past what one person asks over WhatsApp.

Gemini grounding is kept as the path when no Tavily key is configured, so the
assistant degrades to the honest refusal instead of to silence.

Searching is rationed: two lookups per incoming WhatsApp message, enforced here
rather than asked for in the prompt. See MAX_SEARCHES_PER_MESSAGE for what
happened without it.
"""
import logging
import os
import threading

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Follows the conversation model unless deliberately overridden. Defaulting to a
# different model instead cost a whole test round: gemini-3.6-flash returned 429
# RESOURCE_EXHAUSTED on the free tier while the model actually in production had
# quota to spare, so the lookup would have failed for a reason that had nothing
# to do with the web.
WEB_MODEL_NAME = (
    os.environ.get("GEMINI_WEB_MODEL_NAME")
    or os.environ.get("GEMINI_MODEL_NAME")
    or "gemini-flash-lite-latest"
)

# WhatsApp is the destination, so a wall of text is a worse answer than a short
# one. This is a ceiling, not a target - the tool result is also carried in the
# prompt for the rest of the conversation.
MAX_ANSWER_CHARS = 4000
MAX_SOURCES = 5

# Set this and search_web works on the free tier. Leave it unset and search_web
# falls back to Gemini grounding, which answers with its own refusal.
TAVILY_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT_SECONDS = 20

# Live searches allowed per incoming WhatsApp message. This is not a cost
# control - 1,000 free credits a month is far past what one person spends over
# WhatsApp. It is here because a model that is allowed ten searches will take
# ten: on 2026-09-06 a single question about a football fixture produced ten
# search_web calls in one turn, drifting between basketball and football
# because it never established which sport was meant, and took nearly a minute
# to answer a question Itai ended up answering himself. Two is enough for one
# lookup plus one correction. Past that the move that actually helps is to ask
# him, not to rephrase the guess a third time.
#
# The cap is enforced here rather than in the system prompt because the prompt
# was not what failed. Gemini's automatic function calling will make up to ten
# tool calls per turn on its own, and a sentence asking it not to is a request;
# this is a refusal it cannot route around.
MAX_SEARCHES_PER_MESSAGE = 2

# Per-thread, because the budget belongs to one incoming message and the
# webhook handles each message start to finish inside its own request thread.
# A plain module-level counter would be reset by a second message arriving
# while the first was still searching.
_budget = threading.local()


def begin_message() -> None:
    """Starts a fresh search budget. Called once per incoming WhatsApp message.

    Without it the count carries over and the second question of the day finds
    the budget already spent.
    """
    _budget.spent = 0
    _budget.answers = {}


def _state():
    """The budget for the message in flight, created on demand.

    Anything that calls a tool without going through the webhook - a test, a
    script, the REPL - gets a budget of its own instead of an AttributeError.
    """
    if not hasattr(_budget, "spent"):
        begin_message()
    return _budget

_client = None


def _get_client():
    """Built on first use, not at import: a missing key must fail the one tool
    that needs it, not stop the whole assistant from starting."""
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set")
        _client = genai.Client(api_key=api_key)
    return _client


def _is_quota_error(error) -> bool:
    """A 429 on search grounding is a plan limit, not a transient rate limit, so
    it deserves an answer Itai can act on rather than a retry that cannot pass."""
    text = str(error)
    return "RESOURCE_EXHAUSTED" in text or "429" in text


def _sources(response) -> list:
    """Pulls the cited pages out of the grounding metadata.

    Every field here is optional and the shape has changed between SDK versions,
    so this walks defensively: a missing citation must degrade the answer by one
    line, never raise in the middle of a reply Itai is waiting for.
    """
    found = []
    try:
        for candidate in getattr(response, "candidates", None) or []:
            meta = getattr(candidate, "grounding_metadata", None)
            for chunk in getattr(meta, "grounding_chunks", None) or []:
                web = getattr(chunk, "web", None)
                if web is None:
                    continue
                title = (getattr(web, "title", "") or "").strip()
                uri = (getattr(web, "uri", "") or "").strip()
                label = title or uri
                if label and label not in found:
                    found.append(label)
    except Exception as e:
        logger.warning(f"Could not read grounding sources: {e}")
    return found[:MAX_SOURCES]


def _tavily_search(query: str) -> str:
    """Asks Tavily and returns the answer with the pages behind it.

    Tavily answers the question itself and hands back the sources it used, so
    there is no second Gemini call on this path - the result goes straight back
    as the function result. That makes it both cheaper and faster than grounding
    would have been even if grounding were available.

    The key is sent two ways on purpose. The Authorization header is the current
    documented form and api_key in the body is the older one, and this could not
    be checked live: api.tavily.com is unreachable from the sandbox this was
    written in, so a wrong auth shape would have surfaced as a 401 on the very
    first real question Itai asked. Sending both costs nothing and cannot be
    wrong in either direction.
    """
    import requests

    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    response = requests.post(
        TAVILY_URL,
        timeout=TAVILY_TIMEOUT_SECONDS,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "query": query,
            "api_key": api_key,
            "search_depth": "basic",  # 1 credit; "advanced" costs 2 and digs deeper.
            "include_answer": True,
            "max_results": MAX_SOURCES,
        },
    )

    if response.status_code == 401:
        return (
            "מפתח החיפוש (TAVILY_API_KEY) לא תקין. תגיד לאיתי לבדוק אותו "
            "בהגדרות של Render."
        )
    if response.status_code in (429, 432, 433):
        # Tavily signals an exhausted plan with its own codes as well as 429.
        return (
            "נגמרה מכסת החיפושים החודשית (1,000 חינם בחודש). היא מתאפסת בתחילת "
            "החודש הבא. אם יש לך קישור מסוים אני כן יכול לפתוח אותו."
        )
    response.raise_for_status()
    data = response.json()

    answer = (data.get("answer") or "").strip()
    results = data.get("results") or []

    if not answer:
        # include_answer is honoured on every plan, but a query with nothing
        # behind it comes back with results and no summary. The snippets are
        # still a real answer, so hand those over rather than reporting failure.
        snippets = []
        for item in results[:3]:
            text = (item.get("content") or "").strip()
            if text:
                snippets.append(text)
        answer = "\n\n".join(snippets)

    if not answer:
        return f"לא מצאתי תשובה על '{query}'. נסה לנסח אחרת."

    if len(answer) > MAX_ANSWER_CHARS:
        answer = answer[:MAX_ANSWER_CHARS].rsplit("\n", 1)[0] + "\n[קוצר]"

    sources = []
    for item in results:
        label = (item.get("title") or item.get("url") or "").strip()
        if label and label not in sources:
            sources.append(label)
    if sources:
        answer += "\n\nמקורות: " + " · ".join(sources[:MAX_SOURCES])
    return answer


def _ask(prompt: str, tool: types.Tool, label: str) -> str:
    client = _get_client()
    response = client.models.generate_content(
        model=WEB_MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[tool],
            # There are no function declarations here to call, and leaving the
            # automatic loop on only prints a warning about using it outside a chat.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )
    answer = (getattr(response, "text", "") or "").strip()
    if not answer:
        return f"לא הצלחתי להביא תשובה מהאינטרנט על {label}. נסה לנסח אחרת."

    if len(answer) > MAX_ANSWER_CHARS:
        answer = answer[:MAX_ANSWER_CHARS].rsplit("\n", 1)[0] + "\n[קוצר]"

    sources = _sources(response)
    if sources:
        answer += "\n\nמקורות: " + " · ".join(sources)
    return answer


def search_web(query: str) -> str:
    """Searches the live internet and returns an answer with its sources.

    Use this for anything you cannot know from your own training or from Itai's
    mail, calendar and files: today's news, prices, sports results, opening
    hours, a company or product you are unsure about, whether something is still
    true. Anything time-sensitive belongs here - your training data has a cutoff
    and Itai does not know where it falls, so guessing reads as a confident lie.

    You get two searches per message and no more. Make them count:

    - Ask one full, specific question rather than several rewordings of the same
      one. Put in what pins it down - the date, the city, the exact team, whether
      it is כדורגל or כדורסל. A vague query costs the same as a precise one and
      comes back vague.
    - If you cannot tell what Itai meant, ask him one short question instead of
      searching. Guessing the subject is the main way both searches get burned.
    - Read the result before you search again. It already carries its sources.
      Spend the second search only on a gap the first one left - never on the
      same question in other words.
    - When the budget is gone, answer with what you found and say what is
      missing. Do not apologise for the limit; just be useful with what you have.

    Ask the question in whichever language Itai used. Hebrew works and is
    usually better for Israeli subjects.

    This tool may report that live search is unavailable on the current plan. If
    it does, say so plainly and offer to open a specific link with read_web_page
    instead. Never fall back to answering from memory as though you had looked it
    up - on anything time-sensitive that produces a confident wrong answer, which
    is worse than admitting the limit.

    Args:
        query: The question to look up, e.g. 'מתי המשחק הבא של הפועל תל אביב'.

    Returns:
        The answer with the pages it came from, or an explanation if the
        lookup failed.
    """
    query = (query or "").strip()
    if not query:
        return "צריך שאלה לחיפוש."

    state = _state()
    key = " ".join(query.lower().split())

    # Asking the identical question twice inside one message is the cheap half
    # of the waste and the easy half to refuse: hand back what the first search
    # returned rather than buying the same answer again. A repeat costs no
    # credit and no budget - it was already paid for.
    if key in state.answers:
        logger.info(f"Web tool: search_web repeat, answered from this message: {query!r}")
        return state.answers[key]

    if state.spent >= MAX_SEARCHES_PER_MESSAGE:
        already = " · ".join(state.answers) or "—"
        logger.info(f"Web tool: search_web budget spent, refused {query!r}")
        return (
            f"[הערת מערכת] נגמרו {MAX_SEARCHES_PER_MESSAGE} החיפושים המותרים "
            f"להודעה הזאת. כבר חיפשת: {already}. אל תחפש שוב — ענה לאיתי עם מה "
            "שכבר מצאת וציין מה חסר, ואם לא ברור מה הוא התכוון תשאל אותו שאלת "
            "הבהרה אחת קצרה במקום לנחש."
        )

    state.spent += 1
    logger.info(
        f"Web tool: search_web(query={query!r}) "
        f"[{state.spent}/{MAX_SEARCHES_PER_MESSAGE}]"
    )
    answer = _run_search(query)
    # Cached even when it is an error message. A retry of the identical query
    # would fail the identical way, and a second failure is worth less than the
    # seconds it takes.
    state.answers[key] = answer
    return answer


def _run_search(query: str) -> str:
    """The lookup itself, once the budget has allowed it."""
    if os.environ.get("TAVILY_API_KEY", "").strip():
        try:
            return _tavily_search(query)
        except Exception as e:
            logger.error(f"Tavily search failed for {query!r}: {e}")
            # Deliberately not falling through to Gemini grounding here. That
            # path is known to 429 on this key, so it would add several seconds
            # of waiting before producing a worse answer than saying what broke.
            return f"החיפוש באינטרנט נכשל: {e}"

    try:
        return _ask(query, types.Tool(google_search=types.GoogleSearch()), f"'{query}'")
    except Exception as e:
        logger.error(f"search_web failed for {query!r}: {e}")
        if _is_quota_error(e):
            return (
                "חיפוש חי באינטרנט לא מוגדר. הדרך של Gemini דורשת תוכנית בתשלום, "
                "אבל יש חלופה חינמית: מפתח של Tavily ב-TAVILY_API_KEY. עד אז — אם "
                "יש לך קישור מסוים אני כן יכול לפתוח אותו ולקרוא אותו."
            )
        return f"החיפוש באינטרנט נכשל: {e}"


def read_web_page(url: str, question: str = "") -> str:
    """Opens a specific web page and reads it.

    Use this when Itai sends a link, or when a page came up in search_web and he
    wants what is actually on it rather than a summary of the result. Prefer
    search_web when there is no particular page in mind.

    Args:
        url: The full address, including https://.
        question: Optional - what to look for on the page. Without it the page
            is summarised.

    Returns:
        The content of the page, or an explanation if it could not be opened.
    """
    url = (url or "").strip()
    if not url:
        return "צריך כתובת של עמוד."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    logger.info(f"Web tool: read_web_page(url={url!r})")
    ask = question.strip() or "סכם את העמוד הזה"
    try:
        return _ask(f"{ask}\n\n{url}", types.Tool(url_context=types.UrlContext()), url)
    except Exception as e:
        logger.error(f"read_web_page failed for {url!r}: {e}")
        return f"לא הצלחתי לפתוח את העמוד: {e}"
