"""Page-level primitives: ink fingerprints without rendering, and page images.

Two tools that between them remove the two biggest costs in the drain loop.

`rm_page_ink` answers "which pages have ink, and has each changed since last
time" WITHOUT rasterising anything. The survey previously used rm_render for
this, rendering every annotated page to PNG purely to count the PNGs and then
discarding them -- fetch plus full rasterise to answer a question the bundle
already contains. page_ink_shas reads the raw `.rm` bytes and hashes them,
which is cheap and total. It also returns exactly the ink_sha256 values the
reading ledger needs, which the cloud lane has never been able to compute --
so every cloud-drained page carries `ink_sha256: null` today and re-interprets
needlessly on the desktop. This closes that.

`rm_page_image` returns rendered pages as base64 PNGs rather than as paths on
the container's own filesystem. Every existing tool hands back a path, which is
useless to a remote caller -- it names a directory the caller cannot see. With
the bytes in hand the calling agent can read the page directly, which makes the
metered vision pass optional rather than mandatory, and allows a like-for-like
comparison between a vision backend and the caller's own reading.

Registered via register(mcp) so server.py stays under its size ceiling, the
same pattern manage.py uses.
"""

from __future__ import annotations

import asyncio
import base64
import subprocess
from pathlib import Path
from typing import Any

from . import config, device, render_profiles, roundtrip
from .config import new_out_dir
from .envelope import (REMEDIES, RmapiAuthError, RmapiThrottledError,
                       err_result, make_warning, ok_result, throttled_result)
from .runner import run_script

# A base64 PNG is ~1.37x the file size, and an analysis-profile page runs to a
# few hundred KB. Cap what one call may return so a wide page selection fails
# loudly with a remedy instead of producing a response no client can hold.
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_PAGES_PER_CALL = 8


def _fetch_and_extract(device_path: str, tool: str) -> tuple[Path, Path] | dict:
    # Both page tools rasterise arbitrary device content, so on a
    # network-exposed server they are confined to the managed roots. No-op on
    # the local stdio lane. See config.read_scope_enforced.
    from .manage import guard_read_path
    guard = guard_read_path(device_path)
    if guard is not None:
        return guard
    out = new_out_dir(tool)
    try:
        bundle = device.get(device_path, out / "download")
    except RmapiAuthError as exc:
        return err_result("rmapi", str(exc), REMEDIES["not_authenticated"])
    except RmapiThrottledError as exc:
        return throttled_result(exc)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return err_result(
            "cloud", f"could not fetch {device_path}: {exc}",
            "check the path with rm_list; if it exists but the get persistently "
            "fails: " + REMEDIES["stale_revision"])
    return out, roundtrip._extract_bundle(bundle, out / "extracted")


def register(mcp) -> None:
    """Attach the page-level tools to the server's MCPServer instance."""

    @mcp.tool()
    async def rm_page_ink(device_path: str) -> dict:
        """Per-page ink fingerprints for a document. NO rendering, NO vision spend.

        The cheap way to answer "which pages have ink" and "which pages changed
        since I last looked". Fetches the .rmdoc and hashes each page's raw .rm
        stroke bytes; pages with no stroke file are absent, which is exactly the
        no-ink-no-fingerprint semantics the delta selector wants.

        Prefer this over rm_render for surveying: rm_render rasterises every
        annotated page to PNG, which is pure waste when only the page list is
        needed.

        Args:
            device_path: Full device path to the document, e.g.
                "<project_root>/<project>/<notebook>" -- rm_health reports the
                configured roots.
        Returns:
            RmResult with data.pages = {page_no: sha256}, data.inked_pages
            (sorted list), data.inked_count. Feed the shas straight into a
            reading/longform ledger's ink_sha256 fields.
        """
        def work() -> dict[str, Any]:
            result = _fetch_and_extract(device_path, "rm_page_ink")
            if isinstance(result, dict):
                return result
            _, extracted = result
            sys_path = str(config.TOOLS_DIR)
            import sys
            if sys_path not in sys.path:
                sys.path.insert(0, sys_path)
            from rm_reading_ledger import page_ink_shas

            shas = page_ink_shas(extracted)
            pages = {str(k): v for k, v in sorted(shas.items())}
            return ok_result({
                "device_path": device_path,
                "pages": pages,
                "inked_pages": sorted(int(k) for k in pages),
                "inked_count": len(pages),
                "extracted_dir": str(extracted),
            })

        try:
            return await asyncio.to_thread(work)
        except Exception as exc:  # noqa: BLE001
            return err_result(
                "cloud", f"{type(exc).__name__}: {exc}",
                "run rm_health, then retry; if it persists the bundle may not "
                "parse -- try rm_render on the same path to compare")

    @mcp.tool()
    async def rm_page_image(device_path: str, pages: str | None = None,
                            profile: str = "analysis",
                            max_pages: int = MAX_PAGES_PER_CALL) -> dict:
        """Rendered page images as BASE64 PNG, so a remote caller can see them.

        Every other tool returns a path on the container's filesystem, which a
        remote caller cannot read. This returns the bytes, so the calling agent
        can read the page itself -- making the metered vision pass optional, and
        making a backend-vs-caller comparison possible at all.

        Metered only in bandwidth, not model spend. Rendering is free; the guard
        here is response size.

        Args:
            device_path: Full device path.
            pages: Selection like "1,3,5-7" (default: annotated pages only).
            profile: "analysis" (model-input render) | "publication".
            max_pages: Refuse rather than truncate beyond this many pages.
        Returns:
            RmResult with data.images = [{page, media_type, data (base64),
            bytes}], data.rendered, data.skipped.
        """
        def work() -> dict[str, Any]:
            try:
                rprofile = render_profiles.get_profile(profile)
            except ValueError as exc:
                return err_result("config", str(exc),
                                  "use profile=analysis or profile=publication")

            result = _fetch_and_extract(device_path, "rm_page_image")
            if isinstance(result, dict):
                return result
            out, extracted = result

            png_dir = out / "pngs"
            args = [str(extracted), "--out", str(png_dir),
                    *rprofile.render_flags()]
            args += ["--pages", pages] if pages else []
            proc = run_script("rm_render_page.py", args, "rm_render")
            if proc.returncode != 0 and not list(png_dir.glob("*.png")):
                return err_result(
                    "cloud", f"render failed: {(proc.stderr or '')[:300]}",
                    "check the path with rm_list and try rm_page_ink first to "
                    "confirm the document has ink at all")

            files = sorted(png_dir.glob("*.png"))
            if not files:
                return ok_result(
                    {"device_path": device_path, "images": [], "rendered": 0,
                     "no_ink": True},
                    warnings=[make_warning(
                        "zero_ink",
                        f"{device_path}: no inked pages rendered -- nothing to "
                        f"return")])
            if len(files) > max_pages:
                return err_result(
                    "config",
                    f"{len(files)} pages exceeds max_pages={max_pages}",
                    f"narrow the request with pages= (e.g. the first few of "
                    f"{[f.name for f in files[:6]]}), or raise max_pages "
                    f"deliberately -- base64 inflates each page by ~37%")

            images, total = [], 0
            for path in files:
                raw = path.read_bytes()
                total += len(raw)
                if total > MAX_IMAGE_BYTES:
                    return err_result(
                        "config",
                        f"response would exceed {MAX_IMAGE_BYTES // (1024*1024)}MB "
                        f"at page {path.name}",
                        "request fewer pages with pages=, or use "
                        "profile=analysis which renders smaller than publication")
                page_no = "".join(c for c in path.stem if c.isdigit())
                images.append({
                    "page": int(page_no) if page_no else None,
                    "media_type": "image/png",
                    "bytes": len(raw),
                    "data": base64.b64encode(raw).decode("ascii"),
                })
            return ok_result({
                "device_path": device_path,
                "profile": rprofile.name,
                "images": images,
                "rendered": len(images),
                "total_bytes": total,
            }, warnings=roundtrip.anchor_warnings(proc.stdout))

        try:
            return await asyncio.to_thread(work)
        except Exception as exc:  # noqa: BLE001
            return err_result("cloud", f"{type(exc).__name__}: {exc}",
                              "run rm_health, then retry with fewer pages")
