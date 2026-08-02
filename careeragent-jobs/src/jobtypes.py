"""
src/jobtypes.py

The job-kind registry for careeragent-jobs.

Each handler is ``async def handler(spec: dict, deps: Deps) -> str`` and returns
a human-readable summary STRING that the worker (a) stores as the job result and
(b) injects into the job's conversation. A handler stays pure of HTTP details:
it calls a leaf-service client on ``deps`` and either returns a summary or raises
(the worker's retry_or_fail turns an exception into a retry/failure).

A handler may also return the EMPTY string to mean "ran fine, nothing to say" —
the worker marks the job done but SKIPS the inject. The recurring reminder kinds
(#18b) use this so a scan with no due follow-ups / no stale résumés doesn't spam
the "🔔 Reminders" conversation with "nothing to do" every interval.

Kinds:
  ``review_repos``     (#18a) — repo-review fan-out → careeragent-review.
  ``follow_up_scan``   (#18b) — applications whose follow-up date has arrived.
  ``resume_freshness`` (#18b) — applications whose saved résumé is now stale.
"""
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List

logger = logging.getLogger("careeragent-jobs")

# A reminder scan never lists more than this many applications inline — keeps an
# injected reminder readable (and bounds the message size) when a user has a huge
# backlog. The count line always reports the true total.
_MAX_LISTED = 10


@dataclass
class Deps:
    """Leaf-service clients a handler may use. Bundled so the worker can pass one
    object and new kinds can add fields without touching the loop signature.

    ``dossier``/``code`` are optional (None) so a deployment missing that key still
    starts and runs the kinds that don't need it; a handler that needs a missing
    client raises cleanly (→ retry/fail)."""
    review: Any  # client.review.ReviewClient (duck-typed so tests can pass a fake)
    dossier: Any = None  # client.dossier.DossierClient (only the reminder kinds need it)
    code: Any = None  # client.code.CodeClient (only repo_presync needs it — Slice E)


async def handle_review_repos(spec: Dict[str, Any], deps: Deps) -> str:
    """Run a repo-review fan-out off the request path and summarize it.

    Calls careeragent-review's /review-batch via deps.review. On a 2xx with the
    expected counts, returns a friendly one-liner. Otherwise RAISES with the
    error detail so the worker's retry_or_fail decides retry-vs-fail — keeping
    this handler free of HTTP status handling."""
    status, body = await deps.review.review_batch(
        repos=spec.get("repos"),
        limit=spec.get("limit"),
        focus=spec.get("focus"),
        force=bool(spec.get("force")),
    )
    if 200 <= status < 300 and isinstance(body, dict) and "reviewed" in body:
        reviewed = body.get("reviewed", 0)
        skipped = body.get("skipped", 0)
        errors = body.get("errors", 0)
        return (
            f"✅ Background repo review complete — reviewed {reviewed}, "
            f"skipped {skipped}, {errors} error(s). Your projects library is updated."
        )
    detail = body.get("detail") or body.get("error") if isinstance(body, dict) else body
    raise RuntimeError(f"review-batch failed (status={status}): {detail}")


def _app_line(app: Dict[str, Any]) -> str:
    """One '• Company — Title (status)' bullet for a reminder list."""
    company = (app.get("company") or "?").strip() or "?"
    title = (app.get("title") or "?").strip() or "?"
    status = (app.get("status") or "").strip()
    tail = f" ({status})" if status else ""
    return f"• {company} — {title}{tail}"


def _require_dossier(deps: Deps) -> Any:
    if deps.dossier is None:
        raise RuntimeError("dossier client is not configured for this job kind")
    return deps.dossier


def _require_code(deps: Deps) -> Any:
    if deps.code is None:
        raise RuntimeError("code client is not configured for this job kind")
    return deps.code


async def handle_repo_presync(spec: Dict[str, Any], deps: Deps) -> str:
    """Slice E — the nightly cache warm. Ask careeragent-code to discover the user's
    repos and clone-or-pull each into its cache, so the first deep code review of the
    day is warm instead of a cold clone. This does NOT change the on-demand path: a
    review always still pulls latest.

    Returns the EMPTY string on success so the worker marks the job done and SKIPS the
    inject — a cache warm is silent, it never posts to the "🔔 Reminders" conversation.
    A per-repo failure inside the sweep is already counted by careeragent-code (a 2xx
    body with an ``errors`` count) and is NOT retried. Only a TOTAL failure (careeragent-
    code unreachable / non-2xx, e.g. GitHub discovery down) RAISES, so the worker's
    retry_or_fail retries it — otherwise a transient outage just no-ops until the next
    nightly tick."""
    code = _require_code(deps)
    status, body = await code.refresh(limit=spec.get("limit"))
    if 200 <= status < 300 and isinstance(body, dict):
        logger.info("repo_presync ok — discovered=%s refreshed=%s skipped=%s errors=%s",
                    body.get("discovered"), body.get("refreshed"),
                    body.get("skipped"), body.get("errors"))
        return ""   # silent success — nothing to inject
    detail = body.get("error") or body.get("detail") if isinstance(body, dict) else body
    raise RuntimeError(f"repo pre-sync failed (status={status}): {detail}")


async def _scan_applications(deps: Deps, **filters: Any) -> List[Dict[str, Any]]:
    """Shared read for the reminder kinds: GET the tracker with the given filter,
    RAISE on a non-2xx (so the worker retries a transient dossier outage rather
    than reporting a false 'nothing due'). Returns the (possibly empty) row list."""
    dossier = _require_dossier(deps)
    status, body = await dossier.search_applications(**filters)
    if not (200 <= status < 300):
        detail = body.get("detail") or body.get("error") if isinstance(body, dict) else body
        raise RuntimeError(f"dossier search failed (status={status}): {detail}")
    return body if isinstance(body, list) else []


def _reminder_message(header: str, apps: List[Dict[str, Any]], footer: str) -> str:
    """Render a reminder body from a non-empty app list (caller guarantees ≥1)."""
    lines = [header, ""]
    lines += [_app_line(a) for a in apps[:_MAX_LISTED]]
    if len(apps) > _MAX_LISTED:
        lines.append(f"…and {len(apps) - _MAX_LISTED} more.")
    lines += ["", footer]
    return "\n".join(lines)


async def handle_follow_up_scan(spec: Dict[str, Any], deps: Deps) -> str:
    """Reminder: applications whose next_follow_up date has arrived/passed.

    Returns a reminder message when any are due, or the EMPTY string when none
    are (the worker then skips the inject — no noise on a quiet day)."""
    due = await _scan_applications(deps, follow_up_due=True)
    if not due:
        return ""
    n = len(due)
    return _reminder_message(
        f"🔔 **Follow-up reminder** — {n} application{'s' if n != 1 else ''} "
        f"{'are' if n != 1 else 'is'} due for a follow-up:",
        due,
        "Want me to draft a follow-up note for any of these? Just say which.",
    )


async def handle_resume_freshness(spec: Dict[str, Any], deps: Deps) -> str:
    """Reminder: applications whose SAVED résumé predates the current master
    profile (dossier's ``stale`` flag) — a nudge to re-tailor & re-render.

    Returns a reminder message when any are stale, or the EMPTY string when none
    are (the worker then skips the inject)."""
    stale = await _scan_applications(deps, stale=True)
    if not stale:
        return ""
    n = len(stale)
    return _reminder_message(
        f"🔔 **Résumé freshness** — your profile has changed since you tailored "
        f"{n} application{'s' if n != 1 else ''}:",
        stale,
        "Want me to re-tailor and re-render any of these to your latest profile?",
    )


# The single source of truth for valid job kinds. The API validates POST /jobs
# against these keys; the worker dispatches on them; the scheduler enqueues them.
HANDLERS: Dict[str, Callable[[Dict[str, Any], Deps], Awaitable[str]]] = {
    "review_repos": handle_review_repos,
    "follow_up_scan": handle_follow_up_scan,
    "resume_freshness": handle_resume_freshness,
    "repo_presync": handle_repo_presync,
}
