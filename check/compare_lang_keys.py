#!/usr/bin/env python3
"""Compare locale JSON files and report missing translation keys."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_LANG_DIR = PROJECT_ROOT / "lang"
DEFAULT_OUTPUT_FILE = SCRIPT_DIR / "missing-keys-report.txt"
DISCORD_CONTENT_LIMIT = 2000
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_FIELD_VALUE_LIMIT = 1024
DISCORD_DELAY_BETWEEN_MESSAGES_SECONDS = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare locale JSON files and report missing keys across files."
    )
    parser.add_argument(
        "--lang-dir",
        default=str(DEFAULT_LANG_DIR),
        help="Directory that contains locale JSON files (default: project_root/lang)",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_FILE),
        help="Output report file path (default: check/missing-keys-report.txt).",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    parser.add_argument(
        "--source",
        default="nl_NL.json",
        help="Source locale filename used as the baseline (default: nl_NL.json)",
    )
    parser.add_argument(
        "--discord-webhook",
        help="Discord webhook URL for missing-translation notifications.",
    )
    parser.add_argument(
        "--notify-timeout",
        type=float,
        default=10.0,
        help="Webhook request timeout in seconds (default: 10)",
    )
    return parser.parse_args()


def flatten_leaf_keys(value: Any, prefix: str = "") -> Set[str]:
    keys: Set[str] = set()

    if isinstance(value, dict):
        for child_key, child_value in value.items():
            next_prefix = f"{prefix}.{child_key}" if prefix else child_key
            keys.update(flatten_leaf_keys(child_value, next_prefix))
        return keys

    if isinstance(value, list):
        for index, child_value in enumerate(value):
            next_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
            keys.update(flatten_leaf_keys(child_value, next_prefix))
        return keys

    if prefix:
        keys.add(prefix)

    return keys


def read_locale_files(lang_dir: Path) -> Dict[str, Set[str]]:
    if not lang_dir.exists() or not lang_dir.is_dir():
        raise FileNotFoundError(f"Language directory not found: {lang_dir}")

    locale_files = sorted(lang_dir.glob("*.json"))
    if len(locale_files) < 2:
        raise ValueError(
            f"Need at least 2 JSON files in {lang_dir} to compare, found {len(locale_files)}."
        )

    keys_by_file: Dict[str, Set[str]] = {}

    for file_path in locale_files:
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {file_path}: {exc}") from exc

        if not isinstance(data, (dict, list)):
            raise ValueError(
                f"Top-level JSON in {file_path} must be an object or list for key comparison."
            )

        keys_by_file[file_path.name] = flatten_leaf_keys(data)

    return keys_by_file


def build_source_report(
    keys_by_file: Dict[str, Set[str]], source_file: str
) -> Tuple[Set[str], Dict[str, List[str]], Dict[str, List[str]], Dict[str, List[str]]]:
    if source_file not in keys_by_file:
        available = ", ".join(sorted(keys_by_file.keys()))
        raise ValueError(f"Source file '{source_file}' not found. Available files: {available}")

    source_keys = keys_by_file[source_file]
    file_names = sorted(keys_by_file.keys())

    missing_by_file: Dict[str, List[str]] = {}
    extra_by_file: Dict[str, List[str]] = {}

    for file_name in file_names:
        if file_name == source_file:
            continue

        file_keys = keys_by_file[file_name]
        missing_keys = sorted(source_keys - file_keys)
        extra_keys = sorted(file_keys - source_keys)
        missing_by_file[file_name] = missing_keys
        extra_by_file[file_name] = extra_keys

    missing_by_key: Dict[str, List[str]] = {}
    for key in sorted(source_keys):
        missing_files = [
            file_name
            for file_name in file_names
            if file_name != source_file and key not in keys_by_file[file_name]
        ]
        if missing_files:
            missing_by_key[key] = missing_files

    return source_keys, missing_by_key, missing_by_file, extra_by_file


def format_text_report(
    source_file: str,
    source_keys: Set[str],
    missing_by_key: Dict[str, List[str]],
    missing_by_file: Dict[str, List[str]],
    extra_by_file: Dict[str, List[str]],
) -> str:
    lines: List[str] = []

    lines.append("Locale key comparison report (source-based)")
    lines.append("=" * 43)
    lines.append(f"Source file: {source_file}")
    lines.append(f"Total source keys: {len(source_keys)}")
    lines.append(f"Source keys missing in at least one file: {len(missing_by_key)}")
    lines.append("")

    lines.append("Missing source keys by file")
    lines.append("-" * 27)
    for file_name, missing_keys in missing_by_file.items():
        lines.append(f"{file_name}: {len(missing_keys)} missing")
        for key in missing_keys:
            lines.append(f"  - {key}")
        lines.append("")

    lines.append("Extra keys not in source")
    lines.append("-" * 24)
    for file_name, extra_keys in extra_by_file.items():
        lines.append(f"{file_name}: {len(extra_keys)} extra")
        for key in extra_keys:
            lines.append(f"  - {key}")
        lines.append("")

    lines.append("Missing files by key")
    lines.append("-" * 20)
    for key, missing_files in missing_by_key.items():
        file_list = ", ".join(missing_files)
        lines.append(f"{key}: {file_list}")

    if not missing_by_key:
        lines.append("No missing keys detected. All files contain the same leaf keys.")

    return "\n".join(lines).rstrip() + "\n"


def format_json_report(
    source_file: str,
    source_keys: Set[str],
    missing_by_key: Dict[str, List[str]],
    missing_by_file: Dict[str, List[str]],
    extra_by_file: Dict[str, List[str]],
) -> str:
    report = {
        "summary": {
            "sourceFile": source_file,
            "totalSourceKeys": len(source_keys),
            "sourceKeysMissingInAtLeastOneFile": len(missing_by_key),
        },
        "missingByFile": missing_by_file,
        "missingByKey": missing_by_key,
        "extraByFile": extra_by_file,
    }
    return json.dumps(report, indent=2, ensure_ascii=True) + "\n"


def truncate_for_discord(content: str, limit: int = DISCORD_CONTENT_LIMIT) -> str:
    if len(content) <= limit:
        return content

    suffix = "\n... message truncated"
    max_prefix_len = max(0, limit - len(suffix))
    return content[:max_prefix_len] + suffix


def split_lines_by_limit(lines: List[str], limit: int) -> List[str]:
    chunks: List[str] = []
    current_lines: List[str] = []
    current_length = 0

    for line in lines:
        safe_line = truncate_for_discord(line, limit)
        line_length = len(safe_line)

        if not current_lines:
            current_lines.append(safe_line)
            current_length = line_length
            continue

        projected_length = current_length + 1 + line_length
        if projected_length <= limit:
            current_lines.append(safe_line)
            current_length = projected_length
        else:
            chunks.append("\n".join(current_lines))
            current_lines = [safe_line]
            current_length = line_length

    if current_lines:
        chunks.append("\n".join(current_lines))

    return chunks


def post_discord_payload(
    webhook_url: str, payload: Dict[str, Any], timeout_seconds: float
) -> None:
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=timeout_seconds):
        pass


def notify_discord_on_missing(
    webhook_url: str,
    source_file: str,
    source_key_count: int,
    missing_by_key: Dict[str, List[str]],
    missing_by_file: Dict[str, List[str]],
    timeout_seconds: float,
) -> None:
    missing_count = len(missing_by_key)
    if missing_count == 0:
        return

    affected_files = [
        f"{file_name}: {len(keys)}"
        for file_name, keys in sorted(missing_by_file.items())
        if keys
    ]
    affected_files_text = ", ".join(affected_files) if affected_files else "none"

    description_lines = [
        f"Source: **{source_file}**",
        f"Source key count: **{source_key_count}**",
        f"Missing keys: **{missing_count}**",
        f"Affected files: {affected_files_text}",
    ]

    description = truncate_for_discord(
        "\n".join(description_lines), DISCORD_EMBED_DESCRIPTION_LIMIT
    )

    title = truncate_for_discord(
        "Missing translation keys detected", DISCORD_EMBED_TITLE_LIMIT
    )
    affected_files_field = truncate_for_discord(
        affected_files_text, DISCORD_EMBED_FIELD_VALUE_LIMIT
    )

    detail_lines = [
        f"- {key}: {', '.join(missing_files)}"
        for key, missing_files in sorted(missing_by_key.items())
    ]
    detail_chunks = split_lines_by_limit(detail_lines, DISCORD_EMBED_DESCRIPTION_LIMIT)

    embeds: List[Dict[str, Any]] = [
        {
            "title": title,
            "description": description,
            "color": 15158332,
            "fields": [
                {
                    "name": "Affected files (missing key count)",
                    "value": affected_files_field,
                    "inline": False,
                }
            ],
        }
    ]

    for index, chunk in enumerate(detail_chunks, start=1):
        embeds.append(
            {
                "title": truncate_for_discord(
                    f"Missing key details (part {index}/{len(detail_chunks)})",
                    DISCORD_EMBED_TITLE_LIMIT,
                ),
                "description": chunk,
                "color": 15158332,
            }
        )

    payloads = [
        {
            "username": "TranslationsChecker",
            "embeds": [embed],
        }
        for embed in embeds
    ]

    try:
        for index, payload in enumerate(payloads):
            post_discord_payload(webhook_url, payload, timeout_seconds)
            if index < len(payloads) - 1:
                time.sleep(DISCORD_DELAY_BETWEEN_MESSAGES_SECONDS)
    except urllib.error.URLError as exc:
        raise ValueError(f"Failed to send Discord notification: {exc}") from exc


def main() -> int:
    args = parse_args()
    lang_dir = Path(args.lang_dir)

    try:
        keys_by_file = read_locale_files(lang_dir)
        source_keys, missing_by_key, missing_by_file, extra_by_file = build_source_report(
            keys_by_file, args.source
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        report = format_json_report(
            args.source, source_keys, missing_by_key, missing_by_file, extra_by_file
        )
    else:
        report = format_text_report(
            args.source, source_keys, missing_by_key, missing_by_file, extra_by_file
        )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"Report written to {output_path}")

    if args.discord_webhook:
        try:
            notify_discord_on_missing(
                webhook_url=args.discord_webhook,
                source_file=args.source,
                source_key_count=len(source_keys),
                missing_by_key=missing_by_key,
                missing_by_file=missing_by_file,
                timeout_seconds=args.notify_timeout,
            )
            if missing_by_key:
                print("Discord notification sent.")
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
