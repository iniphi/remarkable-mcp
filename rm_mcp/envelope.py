"""RmResult envelope + subprocess result classification.

Every tool returns the same shape:

    {"ok": bool,
     "data": {...} | None,
     "warnings": [{"code", "message", "possible_data_loss", "data"}, ...],
     "error": None | {"system": ..., "message": ..., "remedy": ...},
     "log_tail": [str, ...]}

`system` names which external dependency failed so the calling agent can act
on the remedy instead of inferring a root cause (debugging rule: name the
unreachable system). `warnings` carries non-fatal signals that used to be
buried in log_tail (V2-6); `possible_data_loss` flags the rmscene
newer-format gap (V2-1c).

Known warning codes:
    rmscene_newer_format   .rm CONTENT blocks newer than the parser -- ink loss
    newer_metadata_ignored newer .rm metadata (SceneInfo/geometry) ignored -- no
                           ink affected; possible_data_loss=False (V2-1)
    mtime_only_touch       rm_diff: mtime moved but page did not (sync touch)
    zero_ink               document has no ink strokes (not an error, V2-7)
    anchor_unresolved      text-anchored ink could not be placed against the
                           typed text and was drawn at its raw position
    collection_name_resolved  rm_push_reading resolved a name to a key (V2-2)
    project_case_matched   project folder matched case-insensitively (V2-8)
    project_notebook_collision  a folder+notebook share a name case-insensitively
                           at the project root; both are unfetchable under rmapi.
                           Convention: folders use '_' (NNN_slug), notebooks use
                           '-' (NNN-slug). Rename the collided node on the device.
    epub_untested_roundtrip  .epub pushed; pull-side round-trip is unverified
    state_record_failed    device action succeeded but state write failed
    geta_placeholder       rmapi geta wrote a placeholder; fell back to get
    rmapi_not_found        the rmapi binary could not be resolved -- no device
                           lane at all (rm_health also returns ok=false)
"""

from __future__ import annotations

import subprocess
from typing import Any

# Single source of truth for the auth hints + wrapper exceptions is the
# substrate (tools/rm_config.py, importable here via config's sys.path
# insert). Re-exported so existing `from .envelope import RmapiAuthError`
# call sites keep working and share the substrate's class identity.
from .config import rm_config

AUTH_HINTS = rm_config.AUTH_HINTS
RmapiAuthError = rm_config.RmapiAuthError
# Subclasses RmapiError -> RuntimeError, so every existing `except RuntimeError`
# in server.py already turns a missing binary into a named err_result instead of
# letting a bare OSError escape as "[WinError 2]".
RmapiNotFoundError = rm_config.RmapiNotFoundError
# Also an RmapiError -> RuntimeError. Handlers that read a RuntimeError as
# "absent" or "empty" must catch this one FIRST (throttled_result below).
RmapiThrottledError = rm_config.RmapiThrottledError
RMAPI_NOT_FOUND_REMEDY = rm_config.RMAPI_NOT_FOUND_REMEDY
looks_unauthenticated = rm_config.looks_unauthenticated
make_warning = rm_config.make_warning

SYSTEMS = ("rmapi", "cloud", "device", "zotero", "vision", "calibration", "config")

LOG_TAIL_LINES = 40

REMEDIES = {
    # Owned by the substrate so the CLIs and the server give one answer.
    "rmapi_not_found": rm_config.RMAPI_NOT_FOUND_REMEDY,
    "not_authenticated": (
        "rmapi is not paired: run `rmapi ls` once in a terminal and enter a "
        "one-time code from https://my.remarkable.com/device/desktop/connect; "
        "the MCP never re-auths."
    ),
    "stale_revision": (
        "the cloud revision looks stale (May-2026 v4 schema issue): open the "
        "document once in the reMarkable desktop app, let it sync, then retry. "
        "This step cannot be automated."
    ),
    "zotero_keys": (
        "ZOTERO_API_KEY / ZOTERO_USER_ID missing from the environment or .env; "
        "add them to the workspace .env (the scripts use the Zotero web API)."
    ),
    "timeout": (
        "the operation timed out: re-run with dry_run=True or a narrower "
        "only=/pages= selection, and check the device has synced."
    ),
    "state_locked": (
        "another rm tool holds tools/.rm_state.lock (likely an rm_pull/"
        "rm_push in another Claude window); wait for it to finish and retry."
    ),
    "rmapi_throttled": (
        "the reMarkable cloud is rate-limiting this account (HTTP 429). WAIT "
        "until data.throttled_until before any rm tool call that reaches the "
        "cloud, and tell the person when that is: every rm tool refuses until "
        "then, and a retry that got through sooner would re-mint the login "
        "token and extend the throttle. rm_health shows the cooldown without "
        "calling the cloud."
    ),
    "name_exists": (
        "a document or folder with that name already exists at that device "
        "path. Pass a different title, or move or delete the existing one "
        "first (rm_list shows it; rm_move / rm_delete with dry_run=False). "
        "Repeating the same call fails the same way."
    ),
    "collection_not_found": (
        "the Zotero collection key 404'd: pass the collection NAME (the "
        "server resolves names to keys), or verify the key with the Zotero "
        "MCP's zotero_get_collections."
    ),
    "vision_keys": (
        "no vision credential was found: add GEMINI_API_REMARKABLE (or "
        "GEMINI_API_KEY) and/or ANTHROPIC_API_KEY to the workspace .env. "
        "rm_health.vision_keys reports which the deployment actually has."
    ),
    "vision_quota": (
        "the vision provider refused on QUOTA/BILLING, not on a missing key: "
        "top up or raise the limit on that account, or re-run with a backend "
        "whose key rm_health.vision_keys shows present. If no backend is "
        "usable, render the pages and interpret them in-session instead -- "
        "rm_page_image returns base64 PNGs, and "
        "tools/rm_interpret_claude_native.py --write persists the result in "
        "the same envelope the API backends produce."
    ),
}


def vision_quota_exhausted(output: str) -> bool:
    """The vision provider refused on quota or billing, not on a credential."""
    lowered = output.lower()
    if any(marker in lowered for marker in (
            "resource_exhausted", "credits are depleted", "insufficient_quota",
            "exceeded your current quota", "quota exceeded")):
        return True
    return "429" in output and ("quota" in lowered or "billing" in lowered)


def vision_key_missing(output: str) -> bool:
    """A vision credential is genuinely ABSENT.

    Matched on what the interpret CLIs print when they cannot find a key, and
    deliberately NOT on a bare key NAME. rm_interpret_gemini.py announces the
    key it picked on the HAPPY path -- "[gemini] using GEMINI_API_REMARKABLE" --
    so a name test reads a success line as a failure. That is not theoretical:
    on 2026-09-16 a remote drain of /Quick sheets died on "429
    RESOURCE_EXHAUSTED ... prepayment credits are depleted" and was reported as
    "add the missing vision API key", with the real cause visible only by
    reading log_tail by hand.
    """
    lowered = output.lower()
    if "no gemini key in .env" in lowered:
        return True
    if "anthropic_api_key missing" in lowered:
        return True
    absent = ("missing", "not set", "unset", "not found", "no ")
    named = ("gemini_api", "anthropic_api_key", "gemini key", "vision key")
    return any(n in lowered and any(a in lowered for a in absent) for n in named)


def vision_failure_line(output: str) -> str | None:
    """The line that says why the vision pass failed, for the error message.

    The interpret CLIs write their per-page refusals to STDOUT and rmscene
    writes benign format chatter to STDERR, so the stderr tail classify would
    otherwise quote is the least informative text in the run.
    """
    for line in output.splitlines():
        stripped = line.strip()
        if "failed:" in stripped and len(stripped) > len("failed:"):
            return stripped[:400]
    return None


def log_tail(proc: subprocess.CompletedProcess) -> list[str]:
    """Last LOG_TAIL_LINES lines of stdout+stderr, stdout first."""
    lines: list[str] = []
    for stream in (proc.stdout, proc.stderr):
        if stream:
            lines.extend(stream.splitlines())
    return lines[-LOG_TAIL_LINES:]


def ok_result(data: dict[str, Any] | None = None,
              tail: list[str] | None = None,
              warnings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"ok": True, "data": data or {}, "warnings": warnings or [],
            "error": None, "log_tail": tail or []}


def err_result(system: str, message: str, remedy: str,
               data: dict[str, Any] | None = None,
               tail: list[str] | None = None,
               warnings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"ok": False, "data": data or {}, "warnings": warnings or [],
            "error": {"system": system, "message": message, "remedy": remedy},
            "log_tail": tail or []}


def throttled_result(exc: RmapiThrottledError,
                     data: dict[str, Any] | None = None,
                     tail: list[str] | None = None,
                     warnings: list[dict[str, Any]] | None = None
                     ) -> dict[str, Any]:
    """The distinct throttled result: system cloud, a wait-not-retry remedy,
    and the retry time in data so the agent can tell the person."""
    return err_result("cloud", str(exc), REMEDIES["rmapi_throttled"],
                      data={**(data or {}),
                            "throttled_until": exc.until_iso,
                            "retry_after_s": exc.retry_after_s},
                      tail=tail, warnings=warnings)


def err_from_exception(exc: Exception, system: str, remedy: str,
                       data: dict[str, Any] | None = None,
                       tail: list[str] | None = None,
                       warnings: list[dict[str, Any]] | None = None
                       ) -> dict[str, Any]:
    """err_result for a caught exception, re-attributing a missing binary.

    RmapiNotFoundError subclasses RuntimeError so that every existing handler
    catches it -- but those handlers were written for a device/cloud failure
    and attribute it to `system="cloud"` with "retry after rm_health is green".
    Reporting an absent binary as a CLOUD fault is not a cosmetic mislabel on
    this desk: the reMarkable cloud is independently 429-throttling it, so
    "cloud" is a plausible-looking wrong cause, and the debugging rule that
    matters here is to name the system that is actually unreachable.

    A throttle is re-attributed the same way, for the opposite reason: its
    callers' generic remedies say "retry", which is the one thing it must not.

    So is a name conflict ("entry already exists"): it is the device's state,
    not a reachability fault, and repeating the call fails identically. Found
    live 2026-10-06 when a second push under the same title came back as a
    cloud error telling the agent to check reachability and retry.

    Everything else passes through with the caller's own attribution.
    """
    if isinstance(exc, RmapiThrottledError):
        return throttled_result(exc, data=data, tail=tail, warnings=warnings)
    if isinstance(exc, RmapiNotFoundError):
        return err_result("rmapi", str(exc), REMEDIES["rmapi_not_found"],
                          data=data, tail=tail, warnings=warnings)
    if looks_like_name_conflict(str(exc)):
        return err_result("device", str(exc), REMEDIES["name_exists"],
                          data=data, tail=tail, warnings=warnings)
    return err_result(system, str(exc), remedy,
                      data=data, tail=tail, warnings=warnings)


def looks_like_name_conflict(message: str) -> bool:
    """rmapi refused because the target name is already taken on the device."""
    return "entry already exists" in message.lower()


# Fallback log-line signatures for CLIs that do not (yet) emit structured
# warnings in their summaries/manifests. Kept deliberately narrow.
_TAIL_WARNING_SIGNATURES = (
    # rmscene's own line is generic -- "Some data has not been read. The data
    # may have been written using a newer format than this reader supports."
    # It says NOTHING about whether the unread bytes were ink or metadata, and
    # in practice the overwhelmingly common case is benign trailing metadata
    # (page geometry / SceneInfo fields) on a document that parsed fine.
    #
    # This fallback used to emit rmscene_newer_format with
    # possible_data_loss=True on that line alone, which meant a data-loss alarm
    # on essentially every drain taken through rm_render / rm_pull_notebook --
    # neither of which runs the classifier. A warning that always fires carries
    # no information and trains the reader to ignore it, which is worse than
    # not warning at all: it is precisely how a REAL ink loss would slip past.
    #
    # The honest fallback says only what it knows. The ink-vs-metadata decision
    # belongs to classify_excess / integrity_warning in rm_extract_highlights,
    # which inspects actual block types and IS authoritative -- it runs on the
    # rm_pull and rm_capture paths and emits the real rmscene_newer_format
    # (possible_data_loss=True) or newer_metadata_ignored (False).
    ("newer format", "rmscene_unread_data_unclassified",
     "rmscene reported unread bytes without saying whether they were ink or "
     "metadata -- this path (render/notebook) runs no block-type classifier, "
     "so severity is UNKNOWN, not confirmed loss. Usually benign trailing "
     "metadata. To settle it, drain via rm_pull or rm_capture, which classify "
     "block types and report rmscene_newer_format only on real ink loss.",
     False),
    ("mtime-only", "mtime_only_touch",
     "mtime moved but the open page did not -- likely a sync/push touch, "
     "not new ink", False),
    ("no page_", "zero_ink",
     "no rendered ink pages -- the document has no annotations yet", False),
)


def synthesize_warnings_from_tail(tail: list[str]) -> list[dict[str, Any]]:
    """Regex-free fallback: derive structured warnings from known log lines.

    Used when a wrapped CLI produced no structured warnings; one warning per
    code regardless of how many lines matched.
    """
    found: dict[str, dict[str, Any]] = {}
    for line in tail or []:
        low = line.lower()
        for signature, code, message, data_loss in _TAIL_WARNING_SIGNATURES:
            if signature in low and code not in found:
                found[code] = make_warning(code, message,
                                           possible_data_loss=data_loss,
                                           data={"log_line": line.strip()})
    return list(found.values())


def classify(proc: subprocess.CompletedProcess,
             tool: str) -> dict[str, Any] | None:
    """Map a failed CompletedProcess to an RmResult error dict.

    Returns None when the process succeeded (rc 0). Partial-failure semantics
    (rc 1 with partial output) are the caller's job -- this only names the
    failing system.
    """
    if proc.returncode == 0:
        return None

    stderr = proc.stderr or ""
    stdout = proc.stdout or ""
    combined = f"{stdout}\n{stderr}"
    message = (stderr.strip() or stdout.strip() or
               f"{tool} exited {proc.returncode}")[-800:]

    if looks_unauthenticated(combined):
        return {"system": "rmapi", "message": message,
                "remedy": REMEDIES["not_authenticated"]}
    if (proc.returncode == rm_config.EXIT_THROTTLED
            or "rmapi throttled" in combined.lower()):
        return {"system": "cloud", "message": message,
                "remedy": REMEDIES["rmapi_throttled"]}
    if proc.returncode == rm_config.EXIT_STATE_LOCKED or "rm_state.lock" in combined:
        return {"system": "config", "message": message,
                "remedy": REMEDIES["state_locked"]}
    if "404" in combined and "collections/" in combined:
        return {"system": "zotero", "message": message,
                "remedy": REMEDIES["collection_not_found"]}
    if "ZOTERO_API_KEY" in combined or "ZOTERO_USER_ID" in combined:
        return {"system": "zotero", "message": message,
                "remedy": REMEDIES["zotero_keys"]}
    # Vision, most specific first: a quota refusal is not a missing credential,
    # and the two need opposite actions from the caller.
    if vision_quota_exhausted(combined):
        return {"system": "vision",
                "message": vision_failure_line(combined) or message,
                "remedy": REMEDIES["vision_quota"]}
    if vision_key_missing(combined):
        return {"system": "vision", "message": message,
                "remedy": REMEDIES["vision_keys"]}
    if "device walk failed" in combined or "rmapi" in combined.lower():
        return {"system": "cloud", "message": message,
                "remedy": "check the device/cloud is reachable (rm_health), "
                          "then retry; if one document persistently fails: "
                          + REMEDIES["stale_revision"]}
    return {"system": "config", "message": message,
            "remedy": f"inspect log_tail; {tool} exited {proc.returncode}"}
