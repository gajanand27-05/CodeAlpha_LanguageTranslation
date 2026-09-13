"""Checks the translation layer. Run with:  python test_translator.py

Two groups, deliberately separated.

The offline group uses fake providers and never touches the network. It is the
group that must always pass, and it is where the fallback logic, the chunking
and the input validation are tested. A test that needs the internet to pass is
a test that fails on a train, and a suite that fails for reasons unrelated to
the code stops being trusted.

The live group actually calls the two services. It is reported separately and
does not count towards the pass or fail total, because those services being
down says nothing about whether this code is correct. Run with --live.
"""

from __future__ import annotations

import sys
import urllib.parse

import languages
from translator import (
    MAX_INPUT_CHARS,
    Translation,
    TranslationError,
    looks_like_no_change,
    restore_edges,
    split_into_chunks,
    translate,
)

# This suite prints translated text, which is the whole point of the live group
# and is rarely ASCII. A Windows console pipe defaults to cp1252 and cannot
# encode Devanagari, so printing a correct Hindi result raised UnicodeEncodeError.
# errors="replace" means an un-printable character degrades to a placeholder
# instead of taking the run down: the test is about the translation, not about
# what the terminal happens to support.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

failures = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global failures
    print(f"{'PASS' if condition else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not condition:
        failures += 1


class FakeProvider:
    """Stands in for a real service so the logic can be tested exactly."""

    chunk_limit = 100

    def __init__(self, name, result=None, error=None, detected=None):
        self.name = name
        self.result = result
        self.error = error
        self.detected = detected
        self.calls = []

    def translate(self, text, source, target):
        self.calls.append((text, source, target))
        if self.error is not None:
            raise self.error
        return (self.result if self.result is not None else f"[{self.name}]{text}"), self.detected


# ------------------------------------------------------------------ chunking

def test_chunking() -> None:
    print("\n--- splitting long text ---")
    check("short text stays whole", split_into_chunks("hello", 100) == ["hello"])
    check("empty text gives no chunks", split_into_chunks("", 100) == [])

    text = "First line.\nSecond line.\nThird line."
    chunks = split_into_chunks(text, 20)
    check("every chunk is within the limit", all(len(c) <= 20 for c in chunks),
          f"lengths={[len(c) for c in chunks]}")
    check("nothing is lost or duplicated", "".join(chunks) == text)

    sentences = "Alpha beta gamma. Delta epsilon zeta. Eta theta iota."
    chunks = split_into_chunks(sentences, 25)
    check("breaks after a sentence where it can",
          all(c.endswith((". ", ".")) or c == chunks[-1] for c in chunks),
          f"{chunks}")

    long_word = "x" * 250
    chunks = split_into_chunks(long_word, 100)
    check("one unbreakable word is hard cut", len(chunks) == 3 and "".join(chunks) == long_word,
          f"{[len(c) for c in chunks]}")

    paragraph = "Para one text here.\n\nPara two text here.\n\nPara three text."
    chunks = split_into_chunks(paragraph, 30)
    check("paragraph breaks are preferred", any(c.endswith("\n\n") for c in chunks), f"{chunks}")

    try:
        split_into_chunks("abc", 0)
        check("a zero limit is rejected", False)
    except ValueError:
        check("a zero limit is rejected", True)


def test_chunk_seams_survive_a_trimming_service() -> None:
    """Both real services strip whitespace off a chunk before answering.

    Chunks are cut after their separator, so each ends with the space or the
    newlines it was split on. Without putting those back the rejoined output
    welds one chunk's last word onto the next chunk's first word, and paragraph
    breaks vanish. Only visible on text long enough to be split.
    """
    print("\n--- chunk seams ---")

    check("a trimmed sentence gets its space back",
          restore_edges("The river is wide. ", "Le fleuve est large.")
          == "Le fleuve est large. ")
    check("a paragraph break is preserved",
          restore_edges("First para.\n\n", "Premier para.") == "Premier para.\n\n")
    check("leading whitespace is preserved",
          restore_edges("  indented", "indenté") == "  indenté")
    check("a chunk with no edge whitespace is untouched",
          restore_edges("plain", "simple") == "simple")
    check("an all-whitespace chunk is returned as is",
          restore_edges("\n\n", "anything") == "\n\n")

    # The chunking lives inside each real provider, so the fix has to be tested
    # through the real provider. Only the HTTP call is replaced: the stub trims
    # the text exactly as both live services do, and wraps it so every chunk
    # boundary is visible in the result.
    import translator as t

    def fake_get_json(url: str):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        sent = query["q"][0]
        trimmed = f"<{sent.strip()}>"
        if "mymemory" in url:
            return {"responseStatus": 200, "responseData": {"translatedText": trimmed}}
        return [[[trimmed, sent]], None, "en"]

    source_text = "First sentence here. Second sentence here. Third sentence here."
    # Three chunks at a 30 character limit, each wrapped, with the two seam
    # spaces intact. Before the fix this came back as "...here.><Second...".
    expected = "<First sentence here.> <Second sentence here.> <Third sentence here.>"

    real_get_json = t._get_json
    t._get_json = fake_get_json
    try:
        for provider in (t.GoogleProvider(), t.MyMemoryProvider()):
            provider.chunk_limit = 30
            out, _ = provider.translate(source_text, "en", "fr")
            check(f"{provider.name}: seams survive rejoining", out == expected, out)
    finally:
        t._get_json = real_get_json


# ------------------------------------------------------------------ fallback

def test_fallback() -> None:
    print("\n--- provider fallback ---")

    good = FakeProvider("Good", result="bonjour")
    never = FakeProvider("Never", result="should not be used")
    out = translate("hello", "en", "fr", providers=[good, never])
    check("the first working provider answers", out.text == "bonjour")
    check("and the second is not called", never.calls == [], f"{never.calls}")
    check("the provider is named in the result", out.provider == "Good", out.provider)

    broken = FakeProvider("Broken", error=TranslationError("service down"))
    backup = FakeProvider("Backup", result="hola")
    out = translate("hello", "en", "es", providers=[broken, backup])
    check("a failing provider falls through to the next", out.text == "hola")
    check("and the fallback is credited", out.provider == "Backup", out.provider)

    empty = FakeProvider("Empty", result="   ")
    out = translate("hello", "en", "es", providers=[empty, backup])
    check("an empty answer counts as a failure", out.provider == "Backup", out.provider)

    a = FakeProvider("A", error=TranslationError("first reason"))
    b = FakeProvider("B", error=TranslationError("second reason"))
    try:
        translate("hello", "en", "es", providers=[a, b])
        check("all providers failing raises", False)
    except TranslationError as exc:
        check("all providers failing raises", True)
        check("and reports the first provider's reason", "first reason" in str(exc), str(exc))

    # Only the primary service can auto-detect, so an "auto" request that fails
    # everywhere never really got a fallback. The error has to say so, since
    # choosing a source language is a fix the user can actually apply.
    try:
        translate("hello", "auto", "es", providers=[a, b])
        check("an auto request failing everywhere raises", False)
    except TranslationError as exc:
        check("an auto request failing everywhere raises", True)
        check("and still reports the first provider's reason", "first reason" in str(exc), str(exc))
        check("and tells the user to choose a source language",
              "source language" in str(exc), str(exc))

    try:
        translate("hello", "en", "es", providers=[a, b])
    except TranslationError as exc:
        check("an explicit source gets no such advice",
              "source language" not in str(exc), str(exc))

    crasher = FakeProvider("Crasher", error=OSError("connection reset"))
    out = translate("hello", "en", "es", providers=[crasher, backup])
    check("a network level error also falls through", out.provider == "Backup", out.provider)

    detector = FakeProvider("Detector", result="hello", detected="fr")
    out = translate("bonjour", "auto", "en", providers=[detector])
    check("a detected language is passed through", out.detected_code == "fr", str(out.detected_code))


# ---------------------------------------------------------------- validation

def test_validation() -> None:
    print("\n--- input validation ---")
    p = FakeProvider("P")

    for label, text in [("empty text", ""), ("only spaces", "     "), ("only newlines", "\n\n")]:
        try:
            translate(text, "en", "fr", providers=[p])
            check(f"{label} is rejected", False)
        except TranslationError:
            check(f"{label} is rejected", True)

    try:
        translate("x" * (MAX_INPUT_CHARS + 1), "en", "fr", providers=[p])
        check("over-long text is rejected", False)
    except TranslationError as exc:
        check("over-long text is rejected", True)
        check("and the message gives both numbers",
              str(MAX_INPUT_CHARS) in str(exc) and str(MAX_INPUT_CHARS + 1) in str(exc), str(exc))

    try:
        translate("hello", "en", "en", providers=[p])
        check("same source and target is rejected", False)
    except TranslationError:
        check("same source and target is rejected", True)

    check("nothing reached the provider", p.calls == [], f"{p.calls}")

    check("text exactly at the limit is allowed",
          translate("x" * MAX_INPUT_CHARS, "en", "fr", providers=[FakeProvider("P")]) is not None)


def test_api_rejects_non_string_fields() -> None:
    """The web layer must not coerce non-strings into text to translate.

    str() on a list or a bool succeeds, so without an explicit type check the
    endpoint answered {"text": true} with the Hindi for "True", and a JSON
    array with a translated "['a', 'b']".
    """
    print("\n--- api rejects non-string fields ---")
    import app as web

    client = web.app.test_client()
    for label, payload in [
        ("a number", {"text": 12345, "source": "en", "target": "hi"}),
        ("a list", {"text": ["a", "b"], "source": "en", "target": "hi"}),
        ("an object", {"text": {"k": "v"}, "source": "en", "target": "hi"}),
        ("a boolean", {"text": True, "source": "en", "target": "hi"}),
        ("a non-string source", {"text": "hi", "source": 1, "target": "hi"}),
        ("a non-string target", {"text": "hi", "source": "en", "target": 2}),
    ]:
        response = client.post("/api/translate", json=payload)
        body = response.get_json()
        ok = response.status_code == 400 and not body.get("ok") and "must be a string" in body.get("error", "")
        check(f"{label} is rejected", ok, f"{response.status_code} {body}")

    # A well-formed request must still be accepted by the same code path. This
    # uses a stubbed translate so the check does not depend on the network.
    original = web.translate
    web.translate = lambda text, source, target: type(
        "R", (), {"text": "ok", "detected_code": None, "provider": "Stub"}
    )()
    try:
        response = client.post(
            "/api/translate", json={"text": "hello", "source": "en", "target": "hi"}
        )
        check("a valid string request still succeeds",
              response.status_code == 200 and response.get_json().get("ok") is True,
              str(response.get_json()))
    finally:
        web.translate = original


def test_no_change_detection() -> None:
    print("\n--- unchanged output detection ---")
    check("identical text is flagged", looks_like_no_change("Hello", "Hello"))
    check("case and spacing are ignored", looks_like_no_change("Hello  world", "hello world"))
    check("a real translation is not flagged", not looks_like_no_change("Hello", "Bonjour"))


def test_language_table() -> None:
    print("\n--- language table ---")
    check("the table is populated", len(languages.LANGUAGES) > 100, str(len(languages.LANGUAGES)))
    check("codes are unique",
          len(set(languages.LANGUAGES.values())) == len(languages.LANGUAGES))
    check("every shortcut language exists",
          all(n in languages.LANGUAGES for n in languages.COMMON),
          str([n for n in languages.COMMON if n not in languages.LANGUAGES]))
    check("English maps to en", languages.code_for("English") == "en")
    check("an unknown name gives None", languages.code_for("Klingon") is None)
    check("a code resolves back to a name", languages.name_for("hi") == "Hindi",
          languages.name_for("hi"))
    check("an unknown code returns itself", languages.name_for("zz") == "zz")
    check("auto is not offered as a language", "auto" not in languages.LANGUAGES.values())


# --------------------------------------------------------------------- live

def live_checks() -> None:
    print("\n--- live services (not counted; needs the internet) ---")
    from translator import GoogleProvider, MyMemoryProvider

    # Only the network call is inside the try. The print used to be in there
    # too, and on a Windows console that defaults to cp1252 the Devanagari
    # result could not be encoded, so a successful translation raised
    # UnicodeEncodeError on the way to the screen and was reported as the
    # service being unavailable. Both providers were working perfectly.
    for provider, source in [(GoogleProvider(), "en"), (MyMemoryProvider(), "en")]:
        try:
            out, _ = provider.translate("Good morning", source, "hi")
        except Exception as exc:
            print(f"LIVE  {provider.name:10} unavailable: {type(exc).__name__} {str(exc)[:70]}")
            continue
        print(f"LIVE  {provider.name:10} en->hi  {out!r}")

    try:
        result = translate("Bonjour tout le monde", "auto", "en")
    except Exception as exc:
        print(f"LIVE  auto-detect unavailable: {exc}")
    else:
        print(f"LIVE  auto-detect  detected={result.detected_code!r} via {result.provider}: "
              f"{result.text!r}")


def main() -> int:
    test_chunking()
    test_chunk_seams_survive_a_trimming_service()
    test_fallback()
    test_validation()
    test_api_rejects_non_string_fields()
    test_no_change_detection()
    test_language_table()

    if "--live" in sys.argv:
        live_checks()
    else:
        print("\n(run with --live to also call the real services)")

    print(f"\n{'all offline checks passed' if not failures else str(failures) + ' FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
