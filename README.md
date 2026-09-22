# rm-mcp

An MCP server that makes a **reMarkable 2** a working surface for an AI
agent: push documents to the tablet, write on them in ink or type on them
with the keyboard, pull the result back as text, highlights and page images
the agent can read and act on.

The loop is push, write, pull, read, route. An agent creates a notebook or
sends you a document; you mark it up on e-paper, away from a screen; the
marks come back; the agent tells you what it found, separates notes from
instructions, and files each where you said it should go.

If you are an agent reading this repository, `CLAUDE.md` is your copy.

## You need a reMarkable 2

This is hardware-bound. Without a tablet and a paired cloud account there is
nothing here to run: the test suite passes offline, but every tool that
touches the device fails at the first call.

- A **reMarkable 2**. The geometry constants are calibrated to its
  1404 x 1872 screen; a Paper Pro renders at the wrong scale.
- A **reMarkable cloud account**, synced.
- **[rmapi](https://github.com/ddvk/rmapi)**, built from source, paired once.
  See "Third-party" below for why from source.
- **Python 3.10 or newer.**
- Optionally, a **Gemini API key**. Nothing here needs one: the agent that
  calls the server reads the page images itself.

## Install

```bash
git clone https://github.com/iniphi/remarkable-mcp.git && cd remarkable-mcp
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[test]"     # Windows
.venv/bin/python -m pip install -e ".[test]"         # macOS / Linux
.venv/bin/python run_server.py --init
```

`--init` walks you through it: checks the Python side, finds rmapi and hands
it the terminal if it still needs pairing, lists the top-level folders on
your device, asks where projects live and which folders the destructive
tools may touch, asks where pulled notes and directives should go, writes
`tools/.env`, `ROUTING.md` and `CREATION.md`, and prints the registration line
with the right interpreter path. Re-running it merges into what you have and
leaves an edited `CREATION.md` alone.

Every question has a flag (`--init --yes --projects-root / --notes-to "..."`),
so an agent can run the whole walk on your behalf. `run_server.py --check`
prints the tool count and exits; `rm_health`, once the server is registered,
reports everything else.

Register it with the line `--init` prints:

```
claude mcp add rm -- "/absolute/path/to/.venv/bin/python" "/absolute/path/to/remarkable-mcp/run_server.py"
```

## The loop

```
rm_new_notebook(title="Field notes", project="Thesis")
    -> a notebook at /Thesis/Field notes, small margin, fineliner medium black,
       the title already typed on page 1. Start writing.

rm_pull_project(name="Field notes", project="Thesis")
    -> data.typed_text   keyboard text, per page, with paragraph styles
       data.highlights   highlighter passages with page and box (PDF-backed docs)
       data.pngs         one image per inked page, for the agent to read
       data.step_status  what ran, what was skipped and why, what failed
```

The agent then reads `ROUTING.md`, which `--init` wrote from your answers,
and files what it found. Nothing is written anywhere without a destination
you named.

Four ways to put something on the tablet:

| Tool | Gives you |
|---|---|
| `rm_create` | A document laid out on the page in `CREATION.md` and pushed, in one call. The one to use when the agent is writing something rather than moving a file |
| `rm_new_notebook` | A blank notebook with your pen and margin pre-set and the title typed on page 1. Templates: `default`, `lined`, `plain`, or your own in `tools/rm_templates.local.json` |
| `rm_push_content(mode="native")` | Typed text you can edit on the device with the keyboard, from markdown-ish input |
| `rm_push_pdf`, `rm_push_file`, `rm_push_image` | A PDF, an EPUB, or an image fitted onto a device-sized page |

## Making documents that fit: `CREATION.md`

Ask any agent for "a PDF for my reMarkable" and you get A4, because A4 is what
a PDF is when nobody said otherwise. A4 is 1:1.415. The reMarkable 2 screen is
1404 x 1872 px — 3:4. So an A4 page letterboxes: bars top and bottom, wasted
margin at the sides, type smaller than it needed to be.

`rm_create` fixes that by not asking the agent to know:

```
rm_create(content="# Reading list\n\n1. ...", project="Notes", title="Reading list")
```

The page it uses is **468 x 624 pt**, exactly the screen in pixels divided by
three, and it comes from `CREATION.md` — a file `--init` writes at the repo
root, which is yours to edit:

````markdown
## Template: default

```json
{ "page_w_pt": 468, "page_h_pt": 624,
  "margin_left": 48, "margin_right": 48,
  "margin_top": 60, "margin_bot": 60 }
```

```css
body { font-family: Helvetica, Arial, sans-serif; font-size: 11pt; line-height: 1.45; }
h1 { font-size: 16pt; margin-bottom: 6pt; }
```
````

That is the whole interface. The template that ships is deliberately basic.
Change the `css` block and documents look different — it is ordinary CSS over
the HTML your markdown renders to, so `body`, `h1`, `code`, `table` and
`blockquote` are all addressable, in points. Change the `json` block and the
margins move; `margin_left` and `margin_right` are separate on purpose, so
making one of them large gives you a gutter to write in, which is the thing a
generic PDF generator never leaves you. Copy the heading, rename it, and call
`rm_create(template="<your name>")` for a second one.

The file is re-read on every call, so an edit takes effect on the next
document. No restart, no code change. If you break it, `rm_create` refuses and
tells you where — it will not quietly use the old template and let you think
your edit took.

`rm_create(dry_run=True)` prints the resolved template and pushes nothing.
`rm_health` reports the live one under `config.creation`.

**One caveat:** `rm_create` needs a PDF engine, and this package does not
install one. PyMuPDF is AGPL-3.0; bundling it would relicense the project, so
it is deliberately absent (see *Third-party* below). `pip install pymupdf` in your
venv if you want `rm_create`, `rm_render` and the flatten step — that is your
call to make, not the package's. `rm_new_notebook` and `rm_push_pdf` work with
no PDF engine at all.

A project is a folder. By default projects live at the device root, so
`project="Thesis"` is `/Thesis`; set `RM_MCP_PROJECTS_ROOT` if you keep them
under one folder. Pass `project=` explicitly. The server only infers it when
the client sets `CLAUDE_PROJECT_DIR`, never from the working directory.

## What it does

18 tools, all on by default:

- **Device.** `rm_health`, `rm_list`, `rm_diff` (what changed on the device
  since last look), `rm_ensure_project_folder`.
- **Push.** `rm_create`, `rm_new_notebook`, `rm_push_content`, `rm_push_pdf`,
  `rm_push_file`, `rm_push_image`, `rm_push_dir`.
- **Pull and read.** `rm_pull_project` (the loop), `rm_get_highlights`,
  `rm_render`, `rm_page_image`, `rm_page_ink`.
- **Manage.** `rm_move`, `rm_delete`. Both default to `dry_run=True` and
  return a plan; `rm_delete` never recurses. Both refuse paths outside the
  managed roots unless told otherwise.

Worth knowing:

- **Every tool returns the same envelope**, `{ok, data, warnings, error,
  log_tail}`, where `error.system` names the failing dependency and
  `error.remedy` is the actual next step.
- **A skipped step is reported, not hidden.** `step_status` says why. On this
  build `flatten` (a composited annotated PDF) is always skipped, because it
  needs PyMuPDF and this repository is MIT. A native notebook skips
  `highlights`, because there is no text layer to intersect.
- **Reading handwriting costs nothing.** The page images go to the agent that
  called, which reads them itself. There is no per-page vision spend and no
  key to set.
- **Highlights need a text layer.** Free-drawn highlighter strokes are
  intersected with the PDF's own text; snap-to-text highlights carry their
  text already. Neither needs OCR.
- **`rm_push_content`'s `markdown`, `html` and `svg` modes** render through
  PyMuPDF and fail here with a message saying so. `native` works. For a PDF,
  render it any way you like and use `rm_push_pdf`.

## Configuration

`.env.example` is generated from the code and documents every setting; the
build refuses to publish a tree where the two disagree. `--init` writes the
ones that matter:

| Variable | Purpose |
|---|---|
| `RMAPI_BIN` | Path to the rmapi binary. Defaults to `rmapi` on `PATH` |
| `RM_MCP_PROJECTS_ROOT` | Where projects live. Default `/`, the device root |
| `RM_MCP_MANAGED_ROOTS` | Folders `rm_move`, `rm_delete` and `rm_push_dir` may touch. Default: the two roots, which at `/` is the whole device. `rm_health` reports `guard_scope` so that is never silent |
| `RM_MCP_PROJECT_PATTERN` | Shape of a project folder name. Default: any name |
| `GEMINI_API_KEY` | Not needed. Kept for tools that are not in this build |

### The network lane (optional)

Nothing in the loop needs this. The stdio server above is the product; the
network lane exists for one case, driving the same server from a machine
that has no rmapi and no pairing, such as a phone, and it is fine never to
turn it on.

`RM_MCP_TRANSPORT=streamable-http` puts the server on a network, so it is
gated. Install the extra, `pip install -e ".[remote]"`, and set at least one
**scoped** token; the server refuses to start with none:

| Variable | Grants |
|---|---|
| `RM_MCP_READ_TOKEN` | list, diff, pull, render, page images, highlights |
| `RM_MCP_WRITE_TOKEN` | the above, plus pushes and folders |
| `RM_MCP_ADMIN_TOKEN` | the above, plus `rm_move` and `rm_delete` |

A leaked read token cannot wipe the device, and neither can a leaked write
one. `tools/list` is filtered to match, and a call outside the token's scope
gets a 403 naming the scope it needs. The header is `x-api-key`. Reads are
confined to the managed roots on this lane, so set `RM_MCP_MANAGED_ROOTS`
before exposing it. `RM_MCP_ALLOWED_IPS`, `RM_MCP_TRUST_FORWARDED_FOR` and
two rate-limit buckets are documented in `.env.example`; `rm_health` reports
which are armed without echoing a token.

The container recipe for a hosted deployment is not in this repository. It
is specific to one deployment and carries its own project ids and secret
wiring. The server has no opinion about how it is containerised; a
Dockerfile that builds rmapi from source, installs `.[remote]` and injects
the pairing at start is all it takes.

## Tests

```bash
.venv/bin/python -m pytest
```

Offline, no device, no network. One failure is expected on a clean install:
`test_push_content.py::TestRenderThenPush::test_markdown_renders_pdf_then_pushes`
needs PyMuPDF, which is deliberately not a dependency. Everything else must
pass. The render path has a regression guard on synthetic fixtures under
`tests/fixtures/render_neutral/`, every byte of which is computed by
`tests/gen_neutral_fixtures.py`; no real handwriting ships.

CI is deliberately not configured: the most valuable tests compare rendered
pixels, and rasterisation drifts across platforms. A badge that is red on
day one teaches everyone to ignore the badge.

## Example renders

`examples/` holds two rendered pages produced by
`examples/make_render_examples.py` from computed coordinates, so the render
output can be shown without publishing anyone's handwriting. Regenerate with
`python examples/make_render_examples.py`.

## Third-party

[rmapi](https://github.com/ddvk/rmapi) is **AGPL-3.0** and is not vendored.
This project invokes it as a separate child process, which is why it must be
on your `PATH` rather than installed by this package. If you distribute a
built image containing it, its source obligations are yours.

Build it from source rather than taking a release: on 2026-08-17 the
reMarkable cloud began rejecting root indexes that were not sorted by
document ID, and every write started failing with `400 invalid root schema`
while reads carried on working. The fix landed upstream the next day, commit
`f295d54`, ahead of any release. If writes start returning a bare 400 while
reads keep working, check whether rmapi is behind before looking anywhere
else.

PyMuPDF is AGPL and is not a dependency. The one piece of this toolkit that
needed it, the composited annotated PDF, stays out of the public build for
that reason, and the highlight extractor was ported to pypdfium2 so it could
ship. `rm_create` needs it too — nothing permissive can DRAW into a PDF;
pypdfium2 rasterises but cannot lay out a page — so `rm_create` reports a
missing engine rather than being quietly absent. Nothing in this package
imports it at any scope, in any case: the render runs in a child interpreter,
and a test asserts the boundary on every build.

## Provenance

Extracted from a larger private workspace as a fresh repository rather than a
filtered history: "public is what I explicitly copied" is a claim you can
enumerate, where "public is what survived my filter" is one you can only ever
disprove. The tree is rebuilt from the private one by a script that audits
it for licence, closure and personal content before every publish.

## License

MIT, see [LICENSE](LICENSE).
