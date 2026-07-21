#!/usr/bin/env python3
"""Import Data Point Name descriptions from VW's historical-data PDF.

This is a development tool only.  The dashboard generator remains standard-
library-only and uses the generated dictionary embedded in build_dashboard.py.
"""

import argparse
import ast
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError as exc:  # pragma: no cover - exercised by developers only
    raise SystemExit(
        "This importer requires pypdf: python3 -m pip install pypdf"
    ) from exc


UUID_PATTERN = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
UUID_RE = re.compile(UUID_PATTERN)
ROW_RE = re.compile(r"^(" + UUID_PATTERN + r")")
NUMERIC_NAME_RE = re.compile(r"^\d+(?:-\d+){2}")

# The PDF table uses these fixed x coordinates.  pypdf's layout character
# scale varies between pages, so the latter three are expressed as ratios to
# the Data Point Name column and evaluated per page.
KEY_X = 53.2
NAME_X = 271.6
DESCRIPTION_X = 390.3
UNIT_X = 507.4
TYPE_X = 624.5
DESCRIPTION_RATIO = (DESCRIPTION_X - KEY_X) / (NAME_X - KEY_X)
UNIT_RATIO = (UNIT_X - KEY_X) / (NAME_X - KEY_X)
TYPE_RATIO = (TYPE_X - KEY_X) / (NAME_X - KEY_X)

TECH_TYPE_RE = re.compile(
    r"^(?:"
    r"bit|blob|bool(?:ean)?|bytea?|character\b|date\b|datetime\b|"
    r"decimal\b|double\b|enum\b|float\b|int(?:eger|\d*)?\b|"
    r"json\b|long\b|numeric\b|real\b|short\b|signed\b|str(?:ing|uct)?\b|"
    r"timestamp\b|u?int\d*\b|unsigned\b|varchar\b"
    r")",
    re.IGNORECASE,
)

# These descriptions are present in the source document but are attached to
# malformed rows, or use a service-specific prefix that the delivered export
# omits. Keep their verified text and export aliases when regenerating the map.
CURATED_DESCRIPTION_FALLBACKS = {
    # Aliases emitted by exports for dictionary names that include a longer
    # service-specific prefix in one source and omit it in another.
    "ChargingProfileStatus.[*].NextChargingTimer.[*].id":
        "Charging Timer information for vehicle wakeup trigger",
    "carCapturedUTCTimestamp": "Car captured UTC timestamp",
    "chargingStatus.chargingScenario":
        "The scenario of why the vehicle is charging or waiting to charge.",
    "cruise_range_primary_info.value":
        "Information regarding the cruise range primary of the vehicle with "
        "subcategory value",
    "envelope.[*].report.climatizationWithoutExternalPower":
        "A settings value that determines if the infrastructure is inactive or "
        "available. If the battery is low (less than 20%), climatization will "
        "not be started.",
    "trunk_lid_info.trunk_lid_status.value":
        "Information regarding the trunk lid of the vehicle with subcategory "
        "trunk lid status with subcategory value",
}


def normalize_text(value):
    """Collapse PDF wrapping and repair its common encoding artifacts."""
    value = value.replace("\u00c2\u00b0", "°")
    value = value.replace("\u00c2\u00b7\u00c2", "·")
    value = value.replace("\u00c2", "")
    value = value.replace("â\x80¦", "…")
    value = value.replace("â \x80¦", "…")
    value = value.replace("â\x80¢", "•")
    value = value.replace("â\x80 ¢", "•")
    value = value.replace("â\x80°", "‰")
    value = re.sub(r"(?<=\w)â\x80\x80s\b", "’s", value)
    value = re.sub(r"(?<=\d)\s*â\x80\x80\s*(?=\d)", "–", value)
    value = value.replace("â\x80 \x80", "—")
    value = value.replace("â\x80\x80", "—")
    value = value.replace("Î©", "Ω")
    value = value.replace("Ã¼", "ü").replace("Ã¤", "ä")
    value = value.replace("Ã¶", "ö").replace("Ã³", "ó")
    return re.sub(r"\s+", " ", value).strip()


def join_wrapped(parts, compact=False):
    """Join PDF cell lines, preserving hyphenated line breaks."""
    result = ""
    for raw_part in parts:
        part = normalize_text(raw_part)
        if not part:
            continue
        if compact or result.endswith("-"):
            result += part
        else:
            result += (" " if result else "") + part
    return result


def page_layout_rows(page, page_number):
    """Return normal rows and UUIDs whose PDF cells collapsed horizontally."""
    lines = (page.extract_text(extraction_mode="layout") or "").splitlines()
    first_row = next((line for line in lines if ROW_RE.match(line)), None)
    if first_row is None:
        return [], set()

    column_match = re.match(
        r"^" + UUID_PATTERN + r"(\s+)(?=\S)", first_row
    )
    if column_match is None:
        raise ValueError(f"Could not locate table columns on PDF page {page_number}")

    name_column = column_match.end()
    # Start two characters before the nominal boundary.  pypdf rounds the
    # layout position by up to one character, while the PDF leaves a wider
    # gutter between cells.
    description_column = round(name_column * DESCRIPTION_RATIO) - 2
    unit_column = round(name_column * UNIT_RATIO) - 2
    type_column = round(name_column * TYPE_RATIO) - 2

    rows = []
    current = None

    def finish_current():
        if current is None:
            return
        rows.append(
            {
                "uuid": current["uuid"],
                "name": join_wrapped(current["name_parts"], compact=True),
                "description": join_wrapped(current["description_parts"]),
                "page": page_number,
            }
        )

    for line in lines:
        row_match = ROW_RE.match(line)
        if row_match:
            finish_current()
            current = {
                "uuid": row_match.group(1),
                "name_parts": [],
                "description_parts": [],
            }
        if current is None:
            continue
        padded = line + " " * max(0, type_column - len(line))
        current["name_parts"].append(
            padded[name_column:description_column]
        )
        current["description_parts"].append(
            padded[description_column:unit_column]
        )
    finish_current()

    broken = {row["uuid"] for row in rows if not row["name"]}
    return rows, broken


def page_stream_fallbacks(page, broken_uuids):
    """Recover rows whose adjacent PDF cells were emitted as one text object."""
    if not broken_uuids:
        return {}

    recovered = {
        uuid: {"tail": "", "identity": [], "description": []}
        for uuid in broken_uuids
    }
    current_uuid = None

    def visitor(text, _cm, tm, _font, _size):
        nonlocal current_uuid
        uuid_match = UUID_RE.search(text)
        if uuid_match:
            current_uuid = uuid_match.group(0)
            if current_uuid in recovered:
                recovered[current_uuid]["tail"] = normalize_text(
                    text[uuid_match.end():]
                )
            return
        if current_uuid not in recovered or not text.strip():
            return
        x_position = float(tm[4])
        if abs(x_position) < 0.1:
            recovered[current_uuid]["identity"].append(normalize_text(text))
        elif abs(x_position - DESCRIPTION_X) < 2:
            recovered[current_uuid]["description"].append(normalize_text(text))

    page.extract_text(visitor_text=visitor)
    return recovered


def split_fallback_name(tail, known_names):
    numeric_match = NUMERIC_NAME_RE.match(tail)
    if numeric_match:
        name = numeric_match.group(0)
        return name, tail[numeric_match.end():].strip()

    matching_names = [
        name for name in known_names
        if tail == name or tail.startswith(name + " ")
    ]
    if matching_names:
        name = max(matching_names, key=len)
        return name, tail[len(name):].strip()

    first_word, separator, remainder = tail.partition(" ")
    if separator and (
        first_word.islower()
        or any(character in first_word for character in ".[]_-")
    ):
        return first_word, remainder.strip()

    # In the malformed rows left after the two rules above, the complete cell
    # is retained in the first text object (for example "Valet Alert Name").
    return tail, ""


def extract_dictionary(pdf_path, expected_rows=None):
    reader = PdfReader(str(pdf_path))
    rows = []
    fallbacks = {}

    # Pages 1-2 are the cover and instructions; the table starts on page 3.
    for page_number, page in enumerate(reader.pages[2:], start=3):
        page_rows, broken_uuids = page_layout_rows(page, page_number)
        rows.extend(page_rows)
        if broken_uuids:
            fallbacks.update(page_stream_fallbacks(page, broken_uuids))

    known_names = {row["name"] for row in rows if row["name"]}
    for uuid, fallback in fallbacks.items():
        name, first_description = split_fallback_name(
            fallback["tail"], known_names
        )
        description_parts = [first_description]
        description_parts.extend(
            part for part in fallback["identity"]
            if part and not TECH_TYPE_RE.match(part)
        )
        description_parts.extend(fallback["description"])
        for row in rows:
            if row["uuid"] == uuid:
                row["name"] = name
                row["description"] = join_wrapped(description_parts)
    if expected_rows is not None and len(rows) != expected_rows:
        raise ValueError(
            f"Expected {expected_rows:,} PDF rows, extracted {len(rows):,}"
        )
    unnamed = [row for row in rows if not row["name"]]
    if unnamed:
        raise ValueError(f"Could not extract {len(unnamed)} Data Point Names")

    descriptions_by_name = defaultdict(list)
    for row in rows:
        if row["description"]:
            descriptions_by_name[row["name"]].append(row["description"])

    descriptions = {}
    ambiguous_names = 0
    for name, candidates in descriptions_by_name.items():
        counts = Counter(candidates)
        if len(counts) > 1:
            ambiguous_names += 1
        # Repeated matching descriptions are stronger evidence; length breaks
        # ties in favor of the most informative entry.
        descriptions[name] = max(
            counts, key=lambda value: (counts[value], len(value), value)
        )

    stats = {
        "rows": len(rows),
        "uniqueUuids": len({row["uuid"] for row in rows}),
        "uniqueNames": len({row["name"] for row in rows}),
        "describedNames": len(descriptions),
        "ambiguousNames": ambiguous_names,
        "fallbackRows": len(fallbacks),
    }
    return descriptions, stats


def python_dictionary_literal(descriptions):
    lines = ["BUNDLED_FIELD_DESCRIPTIONS = {"]
    for name in sorted(descriptions):
        key = json.dumps(name, ensure_ascii=False)
        value = json.dumps(descriptions[name], ensure_ascii=False)
        lines.append(f"    {key}: {value},")
    lines.append("}")
    return "\n".join(lines)


def replace_python_dictionary(path, descriptions):
    source = path.read_text(encoding="utf-8")
    start = source.index("BUNDLED_FIELD_DESCRIPTIONS = {")
    end_marker = "\n}\n\n\ndef parse_ts"
    end = source.index(end_marker, start) + 2
    updated = (
        source[:start]
        + python_dictionary_literal(descriptions)
        + source[end:]
    )
    path.write_text(updated, encoding="utf-8")


def read_python_descriptions(path):
    source = path.read_text(encoding="utf-8")
    module = ast.parse(source)
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name)
            and target.id == "BUNDLED_FIELD_DESCRIPTIONS"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError(f"No BUNDLED_FIELD_DESCRIPTIONS found in {path}")


def read_python_dictionary_info(path):
    source = path.read_text(encoding="utf-8")
    start = source.index("BUNDLED_DICTIONARY_INFO = {")
    value_start = source.index("{", start)
    value_end = source.index("\n}\n\nBUNDLED_FIELD_DESCRIPTIONS", value_start) + 2
    return ast.literal_eval(source[value_start:value_end])


def write_web_dictionary(path, info, descriptions):
    ordered_descriptions = {
        name: descriptions[name] for name in sorted(descriptions)
    }
    payload = {"info": info, "descriptions": ordered_descriptions}
    text = (
        "/* VW Data Dictionary V4.0 — bundled field descriptions "
        "(generated from build_dashboard.py). */\n"
        "globalThis.VW_DICTIONARY = "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + ";\n"
    )
    path.write_text(text, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="VW historical Data Dictionary PDF")
    parser.add_argument(
        "--python-source",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "build_dashboard.py",
        help="canonical generator to update",
    )
    parser.add_argument(
        "--web-dictionary",
        type=Path,
        help="optional web port dictionary.js to regenerate",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="extract and validate without writing"
    )
    parser.add_argument(
        "--expected-rows",
        type=int,
        default=5_150,
        help="expected table row count (default: 5150 for V4.0)",
    )
    args = parser.parse_args()

    existing_descriptions = read_python_descriptions(args.python_source)
    descriptions, stats = extract_dictionary(args.pdf, args.expected_rows)
    preservation_candidates = {
        **CURATED_DESCRIPTION_FALLBACKS,
        **existing_descriptions,
    }
    preserved = {
        name: description
        for name, description in preservation_candidates.items()
        if name not in descriptions
    }
    descriptions = {**preserved, **descriptions}
    stats["preservedDescriptions"] = len(preserved)
    stats["embeddedDescriptions"] = len(descriptions)
    print(json.dumps(stats, indent=2, sort_keys=True))
    if args.dry_run:
        return

    replace_python_dictionary(args.python_source, descriptions)
    print(f"Updated {args.python_source}")
    if args.web_dictionary:
        info = read_python_dictionary_info(args.python_source)
        write_web_dictionary(args.web_dictionary, info, descriptions)
        print(f"Updated {args.web_dictionary}")


if __name__ == "__main__":
    main()
