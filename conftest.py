import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# Tests never hit the real Gemini/Meta APIs (all network calls are mocked),
# but assistant.py and webhook_server.py read these env vars at import time.
os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("WHATSAPP_TOKEN", "test-whatsapp-token")
os.environ.setdefault("PHONE_NUMBER_ID", "test-phone-number-id")


# Webhook payloads across many test files share the same Meta message id
# ("wamid.test"); with delivery dedupe in place, one file's message would eat
# another's. Every test gets a fresh dedupe memory.
import pytest


@pytest.fixture(autouse=True)
def _fresh_dedupe_state():
    import webhook_server
    webhook_server._SEEN_MESSAGE_IDS.clear()
    yield
    webhook_server._SEEN_MESSAGE_IDS.clear()
