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
