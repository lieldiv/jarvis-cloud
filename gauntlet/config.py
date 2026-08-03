"""Shared configuration for the Gauntlet package.

Reuses the same GROQ_API_KEY / model choice as app.py, and the same
"fail loudly with a clear message" approach for a missing key rather than
falling back to a hardcoded default.
"""

import os

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

MODEL_NAME = os.environ.get("GAUNTLET_MODEL_NAME", "openai/gpt-oss-120b")

MAX_GAUNTLET_ROUNDS = int(os.environ.get("GAUNTLET_MAX_ROUNDS", "8"))

_client = None


def get_groq_client() -> Groq:
    global _client
    if _client is not None:
        return _client

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and add your "
            "key (see README.md) before running the Gauntlet GUI."
        )
    _client = Groq(api_key=api_key)
    return _client
