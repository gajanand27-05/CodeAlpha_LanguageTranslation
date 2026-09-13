"""Translation back ends, with automatic fallback between them.

Two services are used, tried in order. Both are free and neither needs an API
key, which is the point: the tool should run for anyone who clones it without
first setting up billing on a cloud account.

Why two rather than one. While building this, the `deep-translator` library's
Google back end stopped working: these free endpoints are undocumented and they
change. A tool with a single hard-coded service is one silent upstream change
away from being broken, and that change will happen while it is being demoed
rather than while it is being built. If the first provider fails for any reason,
the second is tried before an error is reported.

The trade is honest and worth stating: free endpoints can rate-limit and can
disappear. An official Google Cloud Translation or Azure Translator key would be
steadier, and swapping one in means adding a class with a `translate` method to
the PROVIDERS list. Nothing else in the project needs to change.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

# Longest text accepted in one request. Chosen to keep the interface responsive
# and to stay clear of the URL length limits these GET based endpoints impose.
MAX_INPUT_CHARS = 5000

USER_AGENT = "Mozilla/5.0 (compatible; CodeAlpha-LanguageTranslation/1.0)"
TIMEOUT_SECONDS = 20


class TranslationError(Exception):
    """Raised when every provider has failed. The message is shown to the user."""


@dataclass
class Translation:
    text: str
    detected_code: str | None   # set when the source language was auto-detected
    provider: str               # which service actually answered


def _get_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def split_into_chunks(text: str, limit: int) -> list[str]:
    """Split text into pieces no longer than `limit`, preferring clean breaks.

    Both endpoints take the text in the query string, so a long input has to be
    sent in pieces. Splitting mid-word would be visible in the output, so the
    break points are tried in order of preference: paragraph, then line, then
    sentence, then space. A single word longer than the limit is cut, because at
    that point there is nowhere sensible left to break.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = -1
        for separator in ("\n\n", "\n", ". ", "। ", "? ", "! ", " "):
            found = window.rfind(separator)
            if found > 0:
                cut = found + len(separator)
                break
        if cut <= 0:
            cut = limit  # one very long word: hard cut
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


def restore_edges(chunk: str, translated: str) -> str:
    """Put back the whitespace the service trimmed from a chunk's edges.

    Chunks are cut *after* their separator, so each one ends with the "\\n\\n",
    "\\n" or " " it was split on. Both services strip that before answering:
    send "The river is wide. " and the reply comes back with no trailing space.
    Joining those replies end to end then welds the last word of one chunk onto
    the first word of the next, and a paragraph break disappears entirely.

    The seam is the one thing the careful choice of break points exists to hide,
    and it only shows up on text long enough to be split, which is why it
    survived a test suite built on short strings.
    """
    if not chunk.strip():
        return chunk
    lead = chunk[: len(chunk) - len(chunk.lstrip())]
    trail = chunk[len(chunk.rstrip()):]
    return lead + translated.strip() + trail


class GoogleProvider:
    """Google's public translate endpoint. Also reports the detected language."""

    name = "Google"
    chunk_limit = 1200  # keeps the encoded URL well inside practical limits

    def translate(self, text: str, source: str, target: str) -> tuple[str, str | None]:
        pieces, detected = [], None
        for chunk in split_into_chunks(text, self.chunk_limit):
            query = urllib.parse.urlencode(
                {"client": "gtx", "sl": source, "tl": target, "dt": "t", "q": chunk}
            )
            payload = _get_json(f"https://translate.googleapis.com/translate_a/single?{query}")

            if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
                raise TranslationError("Unexpected response from Google")

            # payload[0] is a list of [translated, original, ...] segments, and
            # payload[2] is the detected source language when sl was "auto".
            piece = "".join(seg[0] for seg in payload[0] if seg and seg[0])
            pieces.append(restore_edges(chunk, piece))
            if detected is None and len(payload) > 2 and isinstance(payload[2], str):
                detected = payload[2]
        return "".join(pieces), detected


class MyMemoryProvider:
    """MyMemory's public API. Used when Google fails.

    It caps a single query at 500 characters and reports that as a 403 in the
    response body rather than as an HTTP error, so the chunk limit is set below
    the cap and the body is checked explicitly.
    """

    name = "MyMemory"
    chunk_limit = 450

    def translate(self, text: str, source: str, target: str) -> tuple[str, str | None]:
        # This service has no auto-detect, so "auto" cannot be honoured here.
        # Saying so plainly is better than silently translating from the wrong
        # language and returning confident nonsense.
        if source == "auto":
            raise TranslationError("MyMemory cannot detect the source language")

        pieces = []
        for chunk in split_into_chunks(text, self.chunk_limit):
            query = urllib.parse.urlencode({"q": chunk, "langpair": f"{source}|{target}"})
            payload = _get_json(f"https://api.mymemory.translated.net/get?{query}")

            if not isinstance(payload, dict):
                raise TranslationError("Unexpected response from MyMemory")
            status = payload.get("responseStatus")
            if status not in (200, "200"):
                raise TranslationError(str(payload.get("responseDetails") or "MyMemory refused"))
            translated = (payload.get("responseData") or {}).get("translatedText")
            if not translated:
                raise TranslationError("MyMemory returned nothing")
            pieces.append(restore_edges(chunk, translated))
        return "".join(pieces), None


PROVIDERS = [GoogleProvider(), MyMemoryProvider()]


def translate(text: str, source: str, target: str, providers=None) -> Translation:
    """Translate `text`, trying each provider until one succeeds.

    `source` is an ISO code or "auto". Raises TranslationError only when every
    provider has failed, with the first provider's reason, since that is the one
    the user would have expected to answer.
    """
    if not text.strip():
        raise TranslationError("There is nothing to translate")
    if len(text) > MAX_INPUT_CHARS:
        raise TranslationError(
            f"Text is {len(text)} characters. The limit is {MAX_INPUT_CHARS}"
        )
    if source == target:
        raise TranslationError("The source and target languages are the same")

    providers = PROVIDERS if providers is None else providers
    first_error: str | None = None

    for provider in providers:
        try:
            translated, detected = provider.translate(text, source, target)
        except (TranslationError, urllib.error.URLError, OSError, ValueError, KeyError,
                IndexError, TypeError, json.JSONDecodeError) as exc:
            if first_error is None:
                first_error = f"{provider.name}: {exc}"
            continue

        if translated and translated.strip():
            return Translation(text=translated, detected_code=detected, provider=provider.name)
        if first_error is None:
            first_error = f"{provider.name}: empty translation"

    # Only MyMemory's Google-less path can honour an explicit source, so when
    # "auto" was asked for and everything failed, the fallback never really had
    # a chance. Say that, because picking a source language is a fix the user
    # can apply and "HTTP Error 429" on its own does not suggest it.
    if source == "auto":
        raise TranslationError(
            f"{first_error or 'No translation provider is available'}. "
            "Auto-detect needs Google. Choose the source language to use the fallback"
        )

    raise TranslationError(first_error or "No translation provider is available")


def looks_like_no_change(original: str, translated: str) -> bool:
    """True when the output is effectively the input.

    Worth flagging in the interface. It usually means the text was already in
    the target language, or the service had nothing for this pair, and a user
    staring at unchanged text deserves to be told which.
    """
    normalise = lambda s: re.sub(r"\s+", " ", s).strip().casefold()
    return normalise(original) == normalise(translated)
