"""Web interface for the translation tool.

Run with:  python app.py
Then open http://127.0.0.1:5000

Flask serves one page and one JSON endpoint. All translation logic lives in
translator.py, so the same code is used by the tests with no web server
involved.
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request

import languages
from translator import MAX_INPUT_CHARS, TranslationError, looks_like_no_change, translate

app = Flask(__name__)


@app.route("/")
def index():
    # The dropdowns show the most likely languages first, then everything else
    # alphabetically. 133 entries in one flat alphabetical list would bury
    # Hindi and Kannada below a scroll.
    rest = [name for name in languages.LANGUAGES if name not in languages.COMMON]
    return render_template(
        "index.html",
        common=languages.COMMON,
        rest=rest,
        codes=languages.LANGUAGES,
        max_chars=MAX_INPUT_CHARS,
    )


@app.route("/api/translate", methods=["POST"])
def api_translate():
    payload = request.get_json(silent=True) or {}
    text = payload.get("text", "")
    source = payload.get("source", "auto")
    target = payload.get("target", "en")

    # Reject non-strings rather than coercing them. str() on a list or a bool
    # succeeds and produces a Python repr, so {"text": true} was being answered
    # with the Hindi for "True" and {"text": ["a","b"]} with a translated
    # "['a', 'b']". Nonsense in, confident nonsense out is worse than an error.
    for name, value in (("text", text), ("source", source), ("target", target)):
        if not isinstance(value, str):
            return jsonify(
                {"ok": False, "error": f"'{name}' must be a string, not {type(value).__name__}"}
            ), 400

    # Reject codes that are not in the table rather than passing them upstream.
    # A bad code would otherwise come back as an opaque failure from the service.
    valid = set(languages.LANGUAGES.values())
    if source != "auto" and source not in valid:
        return jsonify({"ok": False, "error": f"Unknown source language: {source}"}), 400
    if target not in valid:
        return jsonify({"ok": False, "error": f"Unknown target language: {target}"}), 400

    try:
        result = translate(text, source, target)
    except TranslationError as exc:
        # 200 with ok:false, not a 4xx. This is an expected outcome the page
        # renders as a message, not a protocol level error.
        return jsonify({"ok": False, "error": str(exc)})

    # Only report detection when detection was actually asked for. The service
    # returns the language it identified on every request, so echoing it back
    # when the user has explicitly picked English just tells them what they
    # already chose.
    detected_code = result.detected_code if source == "auto" else None
    detected_name = languages.name_for(detected_code) if detected_code else None
    return jsonify(
        {
            "ok": True,
            "text": result.text,
            "provider": result.provider,
            "detected_code": detected_code,
            "detected_name": detected_name,
            "unchanged": looks_like_no_change(text, result.text),
        }
    )


if __name__ == "__main__":
    print(f"Translation tool ready. {len(languages.LANGUAGES)} languages available.")
    print("Open http://127.0.0.1:5000")
    app.run(debug=False, port=5000)
