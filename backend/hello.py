"""Phase 1 smoke test — confirms the new google-genai SDK is installed
and GEMINI_API_KEY works. Safe to delete after phase 1.

Run: uv run python -m backend.hello
"""
from google import genai

from backend.config import GEMINI_API_KEY


def main() -> None:
    client = genai.Client(api_key=GEMINI_API_KEY)
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents="Say hi in 5 words.",
    )
    print(resp.text)


if __name__ == "__main__":
    main()
