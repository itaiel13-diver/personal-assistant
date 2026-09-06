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
and Bing result pages are both refused by the fetcher. So search_web says
plainly that it needs the paid plan rather than inventing an answer, which is
the one failure mode that would actually mislead Itai.
"""
import logging
import os

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

    Ask a full question in the query, in whichever language Itai used. Hebrew
    works and is usually better for Israeli subjects.

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
    logger.info(f"Web tool: search_web(query={query!r})")
    try:
        return _ask(query, types.Tool(google_search=types.GoogleSearch()), f"'{query}'")
    except Exception as e:
        logger.error(f"search_web failed for {query!r}: {e}")
        if _is_quota_error(e):
            return (
                "חיפוש חי באינטרנט לא זמין כרגע: הוא דורש את התוכנית בתשלום של "
                "Gemini, והמפתח הנוכחי בתוכנית החינמית. אם יש לך קישור מסוים "
                "אני כן יכול לפתוח אותו ולקרוא אותו. תגיד לאיתי שזו החלטה של "
                "עלות, לא תקלה."
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
