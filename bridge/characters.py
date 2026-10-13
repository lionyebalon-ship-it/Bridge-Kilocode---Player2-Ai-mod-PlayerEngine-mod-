"""CLI for managing the p2k-bridge NPC character roster.

The roster lives in ``characters.json`` (next to this script by default) and is
served to the game at ``GET /v1/selected_characters``. All characters share the
bridge's single pinned model configured globally in ``bridge/config.json``;
there is no per-character model field.

Every character carries an ``id`` — Player2NPC needs a non-empty id to persist
the companion's state. ``add`` generates one from the short name (or you pass
``--id``), and the bridge backfills any id that is missing when it serves.

Subcommands: list, add, show, remove. Stdlib only.
"""

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent / "characters.json"

_REQUIRED_TEXT_FIELDS = ("--name", "--short", "--greeting", "--description")

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_ID_MAX_LENGTH = 64


def _load(path: Path) -> dict:
    """Read the roster file as a dict, defaulting to an empty roster.

    Missing or unreadable/undecodable/invalid files yield ``{"characters": []}``
    so callers never crash. Any other top-level keys are preserved verbatim.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"characters": []}
    try:
        data = json.loads(text)
    except ValueError:
        return {"characters": []}
    if not isinstance(data, dict):
        return {"characters": []}
    if not isinstance(data.get("characters"), list):
        data["characters"] = []
    return data


def _save(path: Path, data: dict) -> None:
    """Read-modify-write helper: pretty-print the whole dict (indent=2).

    Preserves every non-roster key the caller leaves in ``data`` untouched.
    """
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _truncate(text: str, limit: int = 40) -> str:
    return text if len(text) <= limit else text[:limit]


def _slugify(text: str) -> str:
    """Lowercase slug: non-alphanumeric runs collapse to '-', edges stripped."""
    return "-".join(part for part in re.split(r"[^a-zA-Z0-9]+", text.lower()) if part)


def _existing_ids(characters: list) -> set[str]:
    """Lower-cased ids already present in the roster (entries without one skip)."""
    return {
        str(c.get("id")).strip().lower()
        for c in characters
        if isinstance(c, dict) and str(c.get("id", "")).strip()
    }


def _generate_id(existing: set[str], short_name: str, name: str) -> str:
    """Id for a new character: slug of short_name, else a unique char- id."""
    slug = _slugify(short_name)
    if slug and slug not in existing:
        return slug
    named = _slugify(name)
    for candidate in (f"char-{slug}" if slug else "", named, f"char-{named}" if named else ""):
        if candidate and candidate not in existing:
            return candidate
    counter = 2
    while f"char-{counter}" in existing:
        counter += 1
    return f"char-{counter}"


def _validated_id(raw: str) -> str | None:
    """Return raw when it is a usable id, else None (caller reports the error)."""
    candidate = raw.strip()
    if not candidate:
        return None
    if len(candidate) > _ID_MAX_LENGTH or not _ID_PATTERN.match(candidate):
        return None
    return candidate


def _find_index(characters: list, query: str) -> int:
    """Index of the character whose name or short_name equals query (or -1)."""
    target = query.strip().lower()
    for index, character in enumerate(characters):
        if not isinstance(character, dict):
            continue
        if str(character.get("name", "")).strip().lower() == target:
            return index
        if str(character.get("short_name", "")).strip().lower() == target:
            return index
    return -1


def _cmd_list(args: argparse.Namespace) -> int:
    characters = [c for c in _load(args.path)["characters"] if isinstance(c, dict)]
    if not characters:
        print("no characters configured")
        return 0
    headers = ("ID", "NAME", "SHORT", "GREETING", "DESCRIPTION")
    rows = [
        (
            str(c.get("id", "")),
            str(c.get("name", "")),
            str(c.get("short_name", "")),
            _truncate(str(c.get("greeting", ""))),
            _truncate(str(c.get("description", ""))),
        )
        for c in characters
    ]
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]

    def render(cells) -> str:
        return "  ".join(cells[i].ljust(widths[i]) for i in range(len(headers))).rstrip()

    print(render(headers))
    for row in rows:
        print(render(row))
    print(f"{len(characters)} character(s)")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    name = args.name or ""
    short_name = args.short_name or ""
    greeting = args.greeting or ""
    description = args.description or ""
    missing = [
        flag
        for flag, value in (
            ("--name", name),
            ("--short", short_name),
            ("--greeting", greeting),
            ("--description", description),
        )
        if not value.strip()
    ]
    if missing:
        print(f"error: missing required field(s): {', '.join(missing)}", file=sys.stderr)
        return 1

    requested_id = None
    if args.id is not None:
        requested_id = _validated_id(args.id)
        if requested_id is None:
            print(
                f"error: invalid --id {args.id!r}: use 1-{_ID_MAX_LENGTH} characters, "
                "letters/digits/'-'/'_' only, no spaces",
                file=sys.stderr,
            )
            return 1

    data = _load(args.path)
    characters = data["characters"]
    name_key = name.strip().lower()
    short_key = short_name.strip().lower()
    existing_ids = _existing_ids(characters)
    if not args.force:
        for existing in characters:
            if not isinstance(existing, dict):
                continue
            if str(existing.get("name", "")).strip().lower() == name_key:
                print(f'error: a character named "{name}" already exists (use --force to add anyway)', file=sys.stderr)
                return 1
            if str(existing.get("short_name", "")).strip().lower() == short_key:
                print(f'error: short name "{short_name}" already exists (use --force to add anyway)', file=sys.stderr)
                return 1
        # Legacy entries written before the id field keeps their file content
        # untouched; the bridge backfills ids when it serves the roster.
        if requested_id is not None and requested_id.lower() in existing_ids:
            print(
                f'error: id "{requested_id}" already exists (use --force to add anyway)',
                file=sys.stderr,
            )
            return 1

    character_id = requested_id if requested_id is not None else _generate_id(existing_ids, short_name, name)
    voice_ids = [v.strip() for v in (args.voice_ids or "").split(",") if v.strip()]
    characters.append(
        {
            "id": character_id,
            "name": name,
            "short_name": short_name,
            "greeting": greeting,
            "description": description,
            "voice_ids": voice_ids,
            "meta": {"skin_url": args.skin_url or ""},
        }
    )
    _save(args.path, data)
    print(f'Added "{name}" ({short_name}) as id {character_id}; {len(characters)} character(s) total')
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    name = (args.name or "").strip()
    if not name:
        print("error: --name is required", file=sys.stderr)
        return 1
    data = _load(args.path)
    characters = data["characters"]
    index = _find_index(characters, name)
    if index < 0:
        print(f'error: no character matching "{name}" in {args.path}', file=sys.stderr)
        return 1
    removed = characters.pop(index)
    _save(args.path, data)
    print(f'Removed "{removed.get("name", name)}"; {len(characters)} character(s) remaining')
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    name = (args.name or "").strip()
    if not name:
        print("error: --name is required", file=sys.stderr)
        return 1
    data = _load(args.path)
    characters = data["characters"]
    index = _find_index(characters, name)
    if index < 0:
        print(f'error: no character matching "{name}" in {args.path}', file=sys.stderr)
        return 1
    print(json.dumps(characters[index], indent=2))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="characters.py",
        description="Manage the p2k-bridge NPC character roster (characters.json).",
    )
    parser.add_argument(
        "--path",
        dest="global_path",
        default=None,
        metavar="P",
        help="path to characters.json (default: the file next to this script)",
    )
    sub = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--path", dest="cmd_path", default=None, metavar="P", help="path to characters.json")

    p_list = sub.add_parser("list", parents=[common], help="list the roster as an aligned table")
    p_list.set_defaults(func=_cmd_list)

    p_add = sub.add_parser("add", parents=[common], help="add a character")
    p_add.add_argument("--name", default=None, help="display name (required, unique)")
    p_add.add_argument("--short", dest="short_name", default=None, help="short name / tag (required, unique)")
    p_add.add_argument("--id", default=None, help="stable id (default: slug of --short); letters/digits/'-'/'_', max 64")
    p_add.add_argument("--greeting", default=None, help="opening line (required, non-empty)")
    p_add.add_argument("--description", default=None, help="personality text (required, non-empty)")
    p_add.add_argument("--skin-url", dest="skin_url", default="", help="optional NPC skin URL")
    p_add.add_argument("--voice-ids", dest="voice_ids", default="", help="comma-separated TTS voice ids")
    p_add.add_argument("--force", action="store_true", help="allow a duplicate name, short_name or id")
    p_add.set_defaults(func=_cmd_add)

    p_remove = sub.add_parser("remove", parents=[common], help="remove a character (by name or short name)")
    p_remove.add_argument("--name", default=None, help="name or short_name to remove")
    p_remove.set_defaults(func=_cmd_remove)

    p_show = sub.add_parser("show", parents=[common], help="print a character's full JSON")
    p_show.add_argument("--name", default=None, help="name or short_name to show")
    p_show.set_defaults(func=_cmd_show)

    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help(sys.stderr)
        return 2
    args.path = Path(args.cmd_path or args.global_path or str(DEFAULT_PATH))
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
