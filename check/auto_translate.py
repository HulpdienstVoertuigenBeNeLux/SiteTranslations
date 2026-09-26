#!/usr/bin/env python3
"""Auto-translate missing keys in locale JSON files using the nl_NL source."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests
from deep_translator import GoogleTranslator
from deep_translator.exceptions import TooManyRequests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
LANG_DIR = PROJECT_ROOT / "lang"
SOURCE_FILE = "nl_NL.json"
SOURCE_LANG = "nl"
PROTECTED_TERMS = [
    # Keep site/brand names unchanged across all locales (case-insensitive match).
    "HulpdienstvoertuigenBeNeLux",
    "Hulpdienstvoertuigen",
    "BeNeLux",
]
# Google allows ~5 req/s; stay well below that.
TRANSLATE_DELAY_SECONDS = 1.0
MAX_RETRIES = 5
RETRY_BASE_DELAY_SECONDS = 5.0


class RateLimitedError(Exception):
    pass


class LibreTranslateClient:
    def __init__(self, base_url: str, source: str, target: str) -> None:
        self.url = base_url.rstrip("/") + "/translate"
        self.source = source
        self.target = target

    def translate(self, text: str) -> str:
        response = requests.post(
            self.url,
            json={"q": text, "source": self.source, "target": self.target, "format": "text"},
            timeout=120,
        )
        response.raise_for_status()
        return response.json()["translatedText"]


def flatten_leaf_keys(value: Any, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            next_prefix = f"{prefix}.{k}" if prefix else k
            keys.update(flatten_leaf_keys(v, next_prefix))
    elif prefix:
        keys.add(prefix)
    return keys


def get_nested(data: dict, key_path: str) -> Any:
    parts = key_path.split(".")
    current = data
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def set_nested(data: dict, key_path: str, value: Any) -> None:
    parts = key_path.split(".")
    current = data
    for part in parts[:-1]:
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]
    current[parts[-1]] = value


def locale_to_lang(filename: str) -> str:
    # e.g. de_DE.json -> de, fr_FR.json -> fr, en_US.json -> en
    return Path(filename).stem.split("_")[0].lower()


def protect_terms(text: str, terms: list[str]) -> tuple[str, dict[str, str]]:
    if not terms:
        return text, {}

    # Prefer longest matches first to avoid partial overlaps.
    sorted_terms = sorted(terms, key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(term) for term in sorted_terms), re.IGNORECASE)
    replacements: dict[str, str] = {}
    counter = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal counter
        token = f"__PROTECTED_TERM_{counter}__"
        replacements[token] = match.group(0)
        counter += 1
        return token

    return pattern.sub(_replace, text), replacements


def restore_terms(text: str, replacements: dict[str, str]) -> str:
    restored = text
    for token, original in replacements.items():
        restored = restored.replace(token, original)
    return restored


def get_source_from_main() -> dict:
    """Fetch the latest source (nl_NL.json) from the main branch.
    
    This ensures we translate only keys still missing after any manual edits on main.
    """
    try:
        result = subprocess.run(
            ["git", "show", "origin/main:lang/nl_NL.json"],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError:
        print(
            "Warning: Could not fetch nl_NL.json from origin/main, using current branch.",
            file=sys.stderr,
        )
        source_path = LANG_DIR / SOURCE_FILE
        return json.loads(source_path.read_text(encoding="utf-8"))


def translate_value(
    value: Any, translator: GoogleTranslator | LibreTranslateClient
) -> tuple[Any, bool]:
    if not isinstance(value, str) or not value.strip():
        return value, True

    protected_value, replacements = protect_terms(value, PROTECTED_TERMS)

    for attempt in range(MAX_RETRIES):
        try:
            result = translator.translate(protected_value)
            translated = result if result else protected_value
            return restore_terms(translated, replacements), True
        except TooManyRequests:
            wait = RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
            print(
                f"  Rate limited, retrying in {wait:.0f}s ({attempt + 1}/{MAX_RETRIES})...",
                file=sys.stderr,
            )
            time.sleep(wait)
        except Exception as exc:
            print(f"  Warning: translation failed ({exc}), skipping this key.", file=sys.stderr)
            return None, False

    raise RateLimitedError("Still rate limited after retries.")


def main() -> int:
    # Fetch the latest source from main to account for manual edits
    source_data = get_source_from_main()
    source_keys = flatten_leaf_keys(source_data)

    locale_files = sorted(LANG_DIR.glob("*.json"))
    any_translated = False

    libre_url = os.environ.get("LIBRETRANSLATE_URL")
    delay = 0.0 if libre_url else TRANSLATE_DELAY_SECONDS
    print(f"Using {'LibreTranslate at ' + libre_url if libre_url else 'Google Translate'}.")

    for file_path in locale_files:
        if file_path.name == SOURCE_FILE:
            continue

        target_lang = locale_to_lang(file_path.name)
        target_data = json.loads(file_path.read_text(encoding="utf-8"))
        target_keys = flatten_leaf_keys(target_data)

        missing_keys = sorted(source_keys - target_keys)
        if not missing_keys:
            print(f"{file_path.name}: no missing keys, skipping.")
            continue

        print(f"{file_path.name}: translating {len(missing_keys)} missing keys to '{target_lang}'...")
        if libre_url:
            translator = LibreTranslateClient(libre_url, SOURCE_LANG, target_lang)
        else:
            translator = GoogleTranslator(source=SOURCE_LANG, target=target_lang)
        translated_count = 0
        skipped_count = 0
        rate_limited = False

        for key in missing_keys:
            source_value = get_nested(source_data, key)
            try:
                translated, ok = translate_value(source_value, translator)
            except RateLimitedError as exc:
                print(f"  {exc} Stopping; progress so far will be saved.", file=sys.stderr)
                rate_limited = True
                break
            if not ok:
                skipped_count += 1
                print(f"  {key}: skipped (translation failed)")
                time.sleep(delay)
                continue

            set_nested(target_data, key, translated)
            translated_count += 1
            print(f"  {key}: {repr(translated)}")
            time.sleep(delay)

        if translated_count > 0:
            file_path.write_text(
                json.dumps(target_data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            print(f"{file_path.name}: updated ({translated_count} added, {skipped_count} skipped).")
            any_translated = True
        else:
            print(f"{file_path.name}: no keys added ({skipped_count} skipped).")

        if rate_limited:
            print("Aborting remaining locales due to rate limiting. Try again later.", file=sys.stderr)
            return 1

    if not any_translated:
        print("Nothing to translate.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
