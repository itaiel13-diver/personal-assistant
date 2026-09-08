"""Downloading the media Itai sends, from Meta's servers.

A WhatsApp message with a photo or a voice note does not carry the media
itself. The webhook payload carries a media id, which is a pointer: the bytes
live behind two Graph API calls. The first turns the id into a download URL,
the second fetches that URL. Both need the same WHATSAPP_TOKEN the outgoing
messages use, so no new credential is introduced here.

This module is deliberately small and knows nothing about Gemini. It fetches
bytes and reports failure with (None, None); what the bytes mean is the
caller's business. The split matters because the webhook handler must be able
to tell "the download failed" apart from "the model failed" - they are
different problems with different Hebrew answers to Itai.

Failures return, never raise. The webhook is Meta's endpoint: an exception
there reads as a dead service and invites retries, while a returned failure
becomes a sentence Itai can act on.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")

# Gemini's inline-media ceiling is 20MB. WhatsApp photos are a few hundred KB
# and voice notes rarely pass one MB, so anything near the cap is not a voice
# note - it is a video or a document that arrived through the wrong handler.
# Refusing it here is cheaper than downloading it to find out.
MAX_MEDIA_BYTES = 20 * 1024 * 1024

TIMEOUT_SECONDS = 30


def base_mime(mime_type: str) -> str:
    """Drops the parameters from a MIME type. WhatsApp voice notes arrive as
    'audio/ogg; codecs=opus', and the parameter is legal HTTP but not a legal
    Gemini mime_type - sending it whole gets the request rejected."""
    return (mime_type or "").split(";")[0].strip().lower()


def download_media(media_id: str) -> tuple:
    """Fetches the bytes behind a WhatsApp media id.

    Returns (content_bytes, mime_type) on success and (None, None) on any
    failure. The mime type comes from Meta's own metadata, not from guessing
    by extension.
    """
    token = os.environ.get("WHATSAPP_TOKEN")
    if not token:
        logger.error("WHATSAPP_TOKEN is not set - cannot download media.")
        return None, None

    headers = {"Authorization": f"Bearer {token}"}
    try:
        # Step one: the id is not a URL. It resolves to one, and that URL is
        # short-lived, so it is fetched immediately rather than stored.
        meta = requests.get(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}",
            headers=headers,
            timeout=TIMEOUT_SECONDS,
        )
        if meta.status_code >= 400:
            logger.error(f"Media lookup failed: {meta.status_code} {meta.text[:200]}")
            return None, None
        info = meta.json()
        url = info.get("url")
        if not url:
            logger.error(f"Media lookup returned no URL: {info}")
            return None, None
        mime_type = base_mime(info.get("mime_type", ""))

        declared_size = info.get("file_size")
        if declared_size and int(declared_size) > MAX_MEDIA_BYTES:
            logger.warning(f"Media {media_id} is {declared_size} bytes - over the inline limit, not downloading.")
            return None, None

        # Step two: the download URL sits on Meta's CDN and still requires the
        # same bearer token. Streaming with a cap, so a mislabeled file cannot
        # eat the instance's memory.
        response = requests.get(url, headers=headers, timeout=TIMEOUT_SECONDS, stream=True)
        if response.status_code >= 400:
            logger.error(f"Media download failed: {response.status_code}")
            return None, None
        content = b""
        for chunk in response.iter_content(chunk_size=65536):
            content += chunk
            if len(content) > MAX_MEDIA_BYTES:
                logger.warning(f"Media {media_id} exceeded the inline limit mid-download - dropping it.")
                return None, None
        if not content:
            logger.error(f"Media {media_id} downloaded empty.")
            return None, None
        return content, mime_type or None
    except requests.RequestException as e:
        logger.error(f"Media download raised: {e}")
        return None, None
    except (ValueError, KeyError) as e:
        logger.error(f"Media lookup returned unexpected JSON: {e}")
        return None, None
