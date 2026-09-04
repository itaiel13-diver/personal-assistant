import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# Tests never hit the real Gemini/Twilio APIs (all network calls are mocked),
# but assistant.py and webhook_server.py both require these env vars to be
# set just to import successfully.
os.environ.setdefault("GEMINI_API_KEY", "test-dummy-key")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-dummy-token")
