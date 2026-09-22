"""Locate a tools/ script by bare filename, in either layout.

Ships with the public tree (rm_build_public.TEST_SUPPORT_FILES), because the
tests that use it ship too and it has to give the right answer in both trees:

    private (104_stacks):  tools/rm/rm_render_page.py
    public  (standalone):  tools/rm_render_page.py

tools/ was split into rm/, zotero/, litgather/ and common/ on 2026-09-20
(S112); the public build keeps copying the manifest flat, so both shapes are
live at once. Ten places across seven test modules built a flat path by hand
and every one of them broke on the desk while staying correct in the tree they
were written for -- which is the argument for asking one function.
"""
from __future__ import annotations

from pathlib import Path

BUCKETS = ("rm", "zotero", "litgather", "common")


def tool_script(tools_dir: Path, name: str) -> Path:
    """The path to `name` under `tools_dir`, flat layout or split.

    Returns the FLAT path when nothing matches, so a caller's own
    "is it there?" check still reads sensibly and its error message still
    names a path a person can go and look at.
    """
    direct = tools_dir / name
    if direct.is_file():
        return direct
    for bucket in BUCKETS:
        candidate = tools_dir / bucket / name
        if candidate.is_file():
            return candidate
    return direct
