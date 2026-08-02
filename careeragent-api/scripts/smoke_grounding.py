#!/usr/bin/env python3
"""
smoke_grounding.py — exercise the grounding gate against the LIVE dossier.

WHY THIS EXISTS
---------------
The gate shipped unvalidated. A live audit found that every claim about it rested
on unit tests with synthetic corpora, while the real deployment had two faults the
tests could not see:

  1. careeragent-dossier's search_projects omitted summary/role/highlights/languages
     — the exact fields build_corpus reads — so the LIVE corpus was starved (~4.2k
     chars instead of ~11k) and flagged genuinely-evidenced skills as phantoms.
  2. The gate checked only skill/domain vocabularies, so an INVENTED PROJECT
     ("Secure Data Pipelines", built from ordinary words) shipped with grounded=True.

Unit tests pass with a hand-written corpus. Only a live probe catches a corpus that
is wrong in production. Run this after any change to grounding.py, to
careeragent-dossier's project fields, or after a rebuild.

USAGE (from the repo root, against the running stack):
    docker exec -e PYTHONPATH=/app/src careeragent-api python /app/scripts/smoke_grounding.py

Exits non-zero if any assertion fails, so it can gate a deploy.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, "/app/src")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent import grounding                      # noqa: E402
from client.dossier import DossierClient         # noqa: E402

FAILURES: list = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if detail:
        print(f"          {detail}")
    if not ok:
        FAILURES.append(label)


async def main() -> int:
    url = os.environ.get("DOSSIER_URL", "").strip().rstrip("/")
    key = os.environ.get("DOSSIER_API_KEY", "").strip()
    if not url or not key:
        print("DOSSIER_URL / DOSSIER_API_KEY not set — run me inside careeragent-api.")
        return 2

    client = DossierClient(url=url, api_key=key)
    await client.start()
    try:
        status, body = await client.read_profile()
        profile = body.get("content", "") if isinstance(body, dict) else ""
        corpus = await grounding.build_corpus_from_dossier(profile, client)
        status_p, projects = await client.search_projects({})
        projects = projects if isinstance(projects, list) else []
    finally:
        await client.aclose()

    print("\n=== LIVE DOSSIER ===")
    print(f"  profile chars : {len(profile)}")
    print(f"  projects      : {len(projects)}")
    print(f"  corpus chars  : {len(corpus)}")

    # 1. The corpus must actually carry the project detail fields. A starved corpus
    #    is the false-positive generator that made TypeScript look invented.
    print("\n=== CORPUS INTEGRITY ===")
    got_fields = {f for p in projects if isinstance(p, dict)
                  for f in ("summary", "role", "highlights", "languages")
                  if p.get(f)}
    check("dossier returns the corpus fields (summary/role/highlights/languages)",
          bool(got_fields), f"present: {sorted(got_fields) or 'NONE — corpus is starved'}")
    check("corpus is substantive (>2000 chars)", len(corpus) > 2000, f"{len(corpus)} chars")

    # 2. A real project of the user's must NOT be flagged (false-positive guard).
    real_names = [str(p.get("name")) for p in projects
                  if isinstance(p, dict) and p.get("name")][:3]
    print("\n=== NO FALSE POSITIVES ON REAL EVIDENCE ===")
    if real_names:
        real_draft = ("## Professional Summary\nEngineer.\n## Selected Projects\n"
                      + "".join(f"**{n}** – real project\n" for n in real_names)
                      + "## Education\nB.S.\n")
        v = grounding.grounding_verdict(real_draft, corpus)
        check(f"the user's real projects survive: {real_names}",
              not v.phantom_projects, f"flagged: {v.phantom_projects}")
    else:
        check("projects library is non-empty", False, "no projects to test against")

    # 3. An INVENTED project must be caught — the case that shipped clean before.
    print("\n=== CATCHES FABRICATION ===")
    fake_draft = ("## Professional Summary\nEngineer.\n## Selected Projects\n"
                  "**Quantum Trading Engine** – led a team of 40 at Goldman Sachs\n"
                  "## Education\nB.S.\n")
    v = grounding.grounding_verdict(fake_draft, corpus)
    check("invented project 'Quantum Trading Engine' is flagged",
          "Quantum Trading Engine" in v.phantom_projects, f"verdict: {v.phantom_projects}")

    # 4. A plain chat reply must never be gated.
    print("\n=== SCOPE GUARD ===")
    v = grounding.grounding_verdict("You could highlight TypeScript if you know it.", corpus)
    check("a plain chat reply is not gated", v.checked is False)

    print("\n" + ("SMOKE FAILED: " + ", ".join(FAILURES) if FAILURES else "SMOKE PASSED"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
