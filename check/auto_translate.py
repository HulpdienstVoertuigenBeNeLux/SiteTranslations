#!/usr/bin/env python3
"""Auto-translate missing keys in locale JSON files using the nl_NL source."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from deep_translator import GoogleTranslator

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
LANG_DIR = PROJECT_ROOT / "lang"
SOURCE_FILE = "nl_NL.json"
SOURCE_LANG = "nl"
# Small delay between API calls to avoid rate limiting.
TRANSLATE_DELAY_SECONDS = 0.2


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


def translate_value(value: Any, translator: GoogleTranslator) -> Any:
    if not isinstance(value, str) or not value.strip():
        return value
    try:
        result = translator.translate(value)
        return result if result else value
    except Exception as exc:
        print(f"  Warning: translation failed ({exc}), keeping source value.", file=sys.stderr)
        return value


def main() -> int:
    # Fetch the latest source from main to account for manual edits
    source_data = get_source_from_main()
    source_keys = flatten_leaf_keys(source_data)

    locale_files = sorted(LANG_DIR.glob("*.json"))
    any_translated = False

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
        translator = GoogleTranslator(source=SOURCE_LANG, target=target_lang)

        for key in missing_keys:
            source_value = get_nested(source_data, key)
            translated = translate_value(source_value, translator)
            set_nested(target_data, key, translated)
            print(f"  {key}: {repr(translated)}")
            time.sleep(TRANSLATE_DELAY_SECONDS)

        file_path.write_text(
            json.dumps(target_data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"{file_path.name}: updated.")
        any_translated = True

    if not any_translated:
        print("Nothing to translate.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
