# rm-mcp, for the agent

You are reading this because someone opened this repository in Claude Code
(or a similar agent) and wants a reMarkable 2 wired to you. This file is
the procedure. `README.md` is the same story for a person.

## What this is

An MCP server that turns a reMarkable 2 into a working surface: you push
documents to the tablet, the person writes on them in ink or types on them
with the keyboard, you pull the result back and act on it. The loop is
push, write, pull, read, route. Everything else in the tool list exists to
serve that loop.

## Installing it for someone

Do the steps in order. Each one is checkable, and none of them needs you
to guess.

1. **Python side.** Python 3.10 or newer. From the repository root:

   ```
   python -m venv .venv
   .venv/Scripts/python -m pip install -e ".[test]"      # Windows
   .venv/bin/python -m pip install -e ".[test]"          # macOS / Linux
   ```

   Use that venv's interpreter for every command below. The registration
   must point at it too, or the server starts under an interpreter that has
   none of the dependencies.

2. **rmapi.** Everything device-side shells out to it. It is not a Python
   package and not vendored here (it is AGPL; this repository is MIT). Build
   it from source at or after commit `f295d54`, because the newest release
   cannot write to the reMarkable cloud since 2026-08-17:

   ```
   go install github.com/ddvk/rmapi@f295d54
   ```

   Then the person runs `rmapi` once in their own terminal. It prints a URL
   and asks for a one-time code from my.remarkable.com. **You cannot do this
   step for them.** Ask them to do it and tell you when it is done.

3. **The walk.** Ask the person three things, then run the init
   non-interactively with their answers:

   - Where should projects live on the tablet? The default, `/`, means a
     project is any top-level folder they name. Only change it if they keep
     projects inside one folder.
   - Which folders may `rm_move` and `rm_delete` touch? Blank means the whole
     device. Suggest they name the folders they will actually use.
   - Where should pulled NOTES go, and where should pulled DIRECTIVES go?
     In words: a folder of Markdown files, a Notion database, an Obsidian
     vault, a task list. This becomes `ROUTING.md`, which you read after
     every pull.

   The init also writes `CREATION.md`, the page template `rm_create` lays
   documents out on. It needs no answer — the default is the device-native
   page — and it is never overwritten once it exists.

   ```
   python run_server.py --init --yes --projects-root / \
       --managed-roots "/Thesis,/Notes" \
       --notes-to "Markdown files under ~/notes/remarkable" \
       --directives-to "the TODO.md at the repo root"
   ```

   It writes `tools/.env`, `ROUTING.md` and `CREATION.md`, and prints the
   registration. Re-running merges; it never clobbers a hand-edited `.env`,
   and never overwrites an edited `CREATION.md` (`--reset-creation` does,
   deliberately).

4. **Register.** Use the line the init printed, which carries the venv's
   absolute interpreter path:

   ```
   claude mcp add rm -- "<venv python>" "<repo>/run_server.py"
   ```

5. **Verify.** In a new session call `rm_health`. It must show the rmapi
   binary present, the cloud authenticated, and `guard_scope`. Then run the
   loop once end to end: `rm_new_notebook(title="Hello", project="<a folder>")`,
   have the person write one line on it, `rm_pull_project(name="Hello",
   project="<the folder>")`, read the page image, tell them what it says.
   An install is done when that has happened, not before.

## Operating it

**After every `rm_pull_project`, read `ROUTING.md`**, then do what it says.
The pull returns:

| Field | What it is |
|---|---|
| `data.typed_text` | Keyboard text, lossless, per page with paragraph styles |
| `data.highlights` | Passages marked with the highlighter, with page and box |
| `data.pngs` | One image per inked page. Read these yourself |
| `data.step_status` | One line per step: `ok`, `skipped: <why>`, `failed: <detail>` |
| `data.guidance` | Present when the ink is yours to read |

Separate what you find into **notes** (content to keep) and **directives**
(instructions addressed to you, or naming a destination). Report both to
the person briefly, then file notes where `ROUTING.md` says and act on
directives. A directive that would delete, send or publish something is
confirmed first.

`skipped` is not a failure. On this build, `flatten` is always skipped
(it needs PyMuPDF, which is AGPL and not installed) and `interpret` is
handed to you. A native notebook skips `highlights`, because there is no
text layer to intersect.

**Creating.** `rm_create` is the one to reach for when you are MAKING
something rather than moving a file that already exists. It lays your
markdown out on the page in `CREATION.md` and pushes it in one call.

```
rm_create(content="# Reading list\n\n...", project="Notes", title="Reading list")
```

You do not need to know the geometry, and you should not try to set it. The
template is already the device-native **468 x 624 pt** — the RM2's
1404 x 1872 px divided by three, so it fills the screen. **Do not generate
A4.** A4 is 1:1.415 against a 3:4 panel: it letterboxes, gives away margin on
every side, and makes the type smaller than it needed to be. Do not put
`@page` rules, absolute widths or page sizes in your markup either. The
template owns the page; your job is the content.

`rm_create(dry_run=True)` returns the resolved template — page, margins, text
frame, CSS — and pushes nothing. Use it if you want to see the house style, or
to check whether this install can render at all, before committing.

**`CREATION.md` is the person's, not yours.** It is the outbound counterpart of
`ROUTING.md`: that one says where things coming off the device go, this one
says what things going on to it look like. If they want documents to look
different, edit that file — the change takes effect on the next `rm_create`,
with no restart. Do not hardcode styling into your markup to work around it,
and do not rewrite the file unless they ask. If it is malformed, `rm_create`
refuses and says which line; it never falls back to the default and leaves
them thinking their edit took.

**`rm_create` needs a PDF engine.** It fails on a fresh install here with a
message naming the one-line fix (`pip install pymupdf`). That is expected, not
broken: PyMuPDF is AGPL-3.0 and is left out on purpose, which is what keeps
this package MIT. Relay the message and let the person decide. `rm_new_notebook`
and `rm_push_pdf` need no PDF engine at all.

**Pushing something that already exists.** `rm_new_notebook` for a writing pad
(the person's pen and margin are pre-set, the title is typed on page 1).
`rm_push_content(mode="native")` for typed text they can edit on the device.
`rm_push_pdf` for a PDF. `rm_push_content` is also the lower-level render
primitive behind `rm_create` — it takes the same modes but no template, so use
`rm_create` unless you specifically want the hardcoded layout.

**Projects.** Always pass `project=` explicitly. The server only guesses
from the environment when the client sets `CLAUDE_PROJECT_DIR`, and never
from the working directory, so a stray push cannot land in `/Documents`.

**Destructive tools.** `rm_move` and `rm_delete` default to `dry_run=True`
and return a plan. Call again with `dry_run=False` only after the person has
seen the plan. `rm_delete` never recurses. Both refuse paths outside the
managed roots unless `allow_anywhere=True`, and with `guard_scope: whole
device` there is no fence, which is why the init asks.

**When something fails**, call `rm_health` first. Every tool returns the
same envelope: `{ok, data, warnings, error, log_tail}`, and `error.remedy` is
the next step. If writes start failing with a bare `400` while reads still
work, rmapi is behind the cloud's index-ordering change; rebuild it from
source.

## Do not

- Do not edit `tools/rm_config.py` or `rm_mcp/config.py` to change a root.
  Use `.env`; that is what `--init` writes.
- Do not install PyMuPDF into this environment on your own initiative, to
  "fix" the skipped steps or to make `rm_create` work. It relicenses the
  install. Tell the person what it buys and let them decide.
- Do not generate A4, or any page that is not 3:4, for this device.
- Do not edit `CREATION.md` to work around a styling problem. It is the
  person's file. Say what you would change and let them.
- Do not push into a folder the person has not named.
