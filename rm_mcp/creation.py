"""CREATION.md -- the page template an authored document is laid out on.

The outbound half of the pair whose inbound half is ROUTING.md. ROUTING.md
answers "where does what I pulled off the device go"; this answers "what
should something I put ON the device look like". Both are written by
`--init`, both are the user's to edit, and neither is code.

The problem it solves is narrow and real. An agent asked to "make a PDF for
my reMarkable" has no way to know the page geometry, so it reaches for A4 --
which is 1:1.415 against a 3:4 screen, letterboxes on the device and throws
away margin on every side. Every pusher in this repo did exactly that until
2026-08-18. The numbers that fix it existed after that, but only as five
module constants inside rm_render_content.py: invisible to a caller,
unreachable by a user. This module makes them a named, printed, editable
object.

    rm_create(content=...)        -> reads CREATION.md, renders, pushes
    edit CREATION.md              -> the next rm_create looks different

No restart and no code edit between those two lines. That is the whole
design: the template ships basic on purpose, and changing it is editing one
markdown file.

Format
------
A template is a `## Template: <name>` heading followed by up to two fenced
blocks -- ```json for the geometry, ```css for the type. Either may be
omitted and DEFAULT_TEMPLATE fills the gap, so a section that only wants a
different typeface is a css block and nothing else. Everything outside the
fenced blocks is prose for the human, and is ignored.

A MALFORMED FILE IS AN ERROR, NEVER A FALLBACK
----------------------------------------------
If CREATION.md exists but cannot be parsed, this module raises. It does not
quietly use DEFAULT_TEMPLATE. The failure being avoided is the one this
workspace keeps paying for -- an operation that looks like it succeeded:
a user edits the CSS, the JSON beside it has a trailing comma, the render
comes back cheerfully with the OLD template, and nothing anywhere says the
edit was discarded. Better to refuse and name the line.

An ABSENT file is a different thing and is not an error: a fresh clone that
has never run `--init` gets DEFAULT_TEMPLATE, which is what it should get.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from . import config

# rm_render_content owns the numbers: it is the file that renders with them,
# it ships in the public manifest, and the CLI is a caller too. Importing it
# here rather than restating the geometry is what stops this module and the
# renderer disagreeing about what "default" means. Safe across the AGPL
# boundary -- rm_render_content never imports fitz at any scope (that is what
# tests/test_agpl_boundary.py asserts); it shells out to a child interpreter.
import rm_render_content  # noqa: E402  (config puts tools/ on sys.path)

CREATION_NAME = "CREATION.md"

DEFAULT_TEMPLATE_NAME = "default"

# `## Template: <name>` -- the only structural markup in the file.
_HEADING_RE = re.compile(r"^##[ \t]+Template:[ \t]*(?P<name>.+?)[ \t]*$", re.MULTILINE)

# A fenced block with an info string. Non-greedy body, so two blocks in one
# section do not swallow each other.
_FENCE_RE = re.compile(
    r"^```[ \t]*(?P<lang>[A-Za-z0-9_+-]*)[ \t]*\n(?P<body>.*?)^```[ \t]*$",
    re.MULTILINE | re.DOTALL)


class CreationError(RuntimeError):
    """CREATION.md exists but could not be used as written."""


def creation_path() -> Path:
    """Beside ROUTING.md, at the repo root in either layout."""
    return config.RM_MCP_DIR / CREATION_NAME


# -- reading ------------------------------------------------------------------

def parse_creation(text: str) -> dict[str, dict]:
    """Every `## Template:` section in `text`, as name -> partial template.

    Partial, deliberately: what is returned is what the FILE said, not the
    resolved template. Merging against DEFAULT_TEMPLATE happens in
    load_template, so a caller can tell "the user set this" from "this is
    the default" -- which is what `rm_create(dry_run=True)` reports.
    """
    headings = list(_HEADING_RE.finditer(text))
    if not headings:
        raise CreationError(
            f"no '## Template: <name>' heading found. {CREATION_NAME} needs at "
            f"least one; delete the file to fall back to the built-in default.")

    out: dict[str, dict] = {}
    for i, head in enumerate(headings):
        name = head.group("name").strip()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        section = text[head.end():end]

        partial: dict = {"name": name}
        seen: set[str] = set()
        for fence in _FENCE_RE.finditer(section):
            lang = fence.group("lang").lower()
            body = fence.group("body")
            if lang == "json":
                if "json" in seen:
                    raise CreationError(
                        f"template {name!r} has two json blocks; keep one.")
                seen.add("json")
                try:
                    geometry = json.loads(body)
                except ValueError as exc:
                    raise CreationError(
                        f"template {name!r}: the json block is not valid JSON "
                        f"({exc}). The render was NOT run, so the previous "
                        f"template is still what the device would get.") from exc
                if not isinstance(geometry, dict):
                    raise CreationError(
                        f"template {name!r}: the json block must be an object, "
                        f"got {type(geometry).__name__}")
                geometry.pop("css", None)   # css belongs in the css block
                geometry.pop("name", None)  # the heading is the name
                partial.update(geometry)
            elif lang == "css":
                if "css" in seen:
                    raise CreationError(
                        f"template {name!r} has two css blocks; keep one.")
                seen.add("css")
                partial["css"] = body

        if name in out:
            raise CreationError(
                f"template {name!r} is defined twice; template names must be unique.")
        out[name] = partial
    return out


def available_templates() -> tuple[list[str], str]:
    """(template names, where they came from) -- without raising on a bad file.

    Used by error messages and rm_health, which want to say what IS available
    and must not themselves fail on the malformed file they are describing.
    """
    path = creation_path()
    if not path.is_file():
        return [DEFAULT_TEMPLATE_NAME], "built-in default"
    try:
        return sorted(parse_creation(path.read_text(encoding="utf-8"))), str(path)
    except (CreationError, OSError):
        return [DEFAULT_TEMPLATE_NAME], f"{path} (unreadable -- falling back)"


def load_template(name: str | None = None) -> tuple[dict, str]:
    """The resolved template to render with, and a one-line provenance string.

    Reads CREATION.md fresh on every call. That is the point -- an edit takes
    effect on the next render, with no restart and nothing cached to go stale.
    The file is small and a render already costs a subprocess, so the read is
    not worth caching.

    Raises CreationError if the file is present but unusable, or if `name` is
    not in it. Never silently substitutes a different template.
    """
    wanted = (name or DEFAULT_TEMPLATE_NAME).strip() or DEFAULT_TEMPLATE_NAME
    path = creation_path()

    if not path.is_file():
        if wanted != DEFAULT_TEMPLATE_NAME:
            raise CreationError(
                f"no template {wanted!r}: {path} does not exist, so only "
                f"{DEFAULT_TEMPLATE_NAME!r} (built in) is available. "
                f"Run `python run_server.py --init` to write the file, then "
                f"add a '## Template: {wanted}' section to it.")
        return rm_render_content.resolve_template(None), "built-in default (no CREATION.md)"

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CreationError(f"could not read {path}: {exc}") from exc

    templates = parse_creation(text)
    if wanted not in templates:
        known = ", ".join(sorted(templates)) or "(none)"
        raise CreationError(
            f"no template {wanted!r} in {path}. Defined there: {known}. "
            f"Add a '## Template: {wanted}' section, or call with one of those.")

    try:
        resolved = rm_render_content.resolve_template(templates[wanted])
    except rm_render_content.TemplateError as exc:
        raise CreationError(f"template {wanted!r} in {path}: {exc}") from exc
    return resolved, f"{wanted!r} from {path}"


# -- writing ------------------------------------------------------------------

def creation_text() -> str:
    """The starter CREATION.md, generated FROM DEFAULT_TEMPLATE.

    Generated, not a hand-written constant, so the file `--init` writes and
    the template the code falls back to cannot drift apart. Restating these
    numbers in prose is how a doc ends up describing a page nobody renders.
    """
    tpl = rm_render_content.resolve_template(None)
    geometry = {k: tpl[k] for k in
                ("page_w_pt", "page_h_pt", "margin_left", "margin_right",
                 "margin_top", "margin_bot")}
    text_w = tpl["page_w_pt"] - tpl["margin_left"] - tpl["margin_right"]
    text_h = tpl["page_h_pt"] - tpl["margin_top"] - tpl["margin_bot"]
    return f"""# Creating documents for this reMarkable

Written by `python run_server.py --init` on {date.today().isoformat()}.
Edit it freely -- that is what it is for. `rm_create` reads this file on every
call, so a change here shows up in the next document with no restart and no
code edit.

The counterpart to `ROUTING.md`: that one says where things coming OFF the
device go, this one says what things going ON to it look like.

## The page, and why it is that size

The reMarkable 2 screen is **1404 x 1872 px** at 226 DPI -- a 3:4 panel.
A4 is 1:1.415, so an A4 page letterboxes: bars top and bottom, wasted margin
at the sides, and text smaller than it needed to be.

The page below is **{tpl["page_w_pt"]:.0f} x {tpl["page_h_pt"]:.0f} pt** -- exactly the screen in
pixels divided by three. It fills the display edge to edge.

Change the type all you like. Change the page size only if you know why --
anything that is not 3:4 will letterbox.

## Template: {tpl["name"]}

Text frame: {text_w:.0f} x {text_h:.0f} pt.

```json
{json.dumps(geometry, indent=2)}
```

```css
{tpl["css"].strip()}
```

## How to change it

- **Type** -- edit the `css` block. It is ordinary CSS over the HTML that
  markdown renders to, so `body`, `h1`, `h2`, `code`, `table`, `blockquote`
  are all addressable. Point sizes, not pixels.
- **Margins** -- edit the `json` block. `margin_left` and `margin_right` are
  separate on purpose: make one of them large and you have a gutter to write
  in, which is the thing a generic PDF generator never leaves you.
- **A second template** -- copy the `## Template:` heading, rename it, and
  call `rm_create(template="<your name>")`. Either fenced block may be left
  out, and the default fills it in.

Keys the `json` block understands: `page_w_pt`, `page_h_pt`, `margin_left`,
`margin_right`, `margin_top`, `margin_bot` (all points). `margin_x` is
accepted as shorthand for both sides.

If this file is malformed, `rm_create` refuses and says why. It will not fall
back to the default and leave you thinking your edit took effect.
"""


def write_creation(path: Path | None = None, *, overwrite: bool = False) -> tuple[Path, bool]:
    """Write the starter CREATION.md. Returns (path, written).

    Never overwrites by default: this file is the user's after the first
    write, and a re-run of `--init` that silently reverted their type choices
    would be a small betrayal of the one thing the file is for.
    """
    dest = path or creation_path()
    if dest.is_file() and not overwrite:
        return dest, False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(creation_text(), encoding="utf-8")
    return dest, True
