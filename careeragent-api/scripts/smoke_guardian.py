#!/usr/bin/env python3
"""
smoke_guardian.py — exercise the Guardian verifier against the LIVE model + dossier.

WHY: the deterministic Tier-1 gate has smoke_grounding.py, but the Guardian makes a
REAL gpt-oss/Bedrock call and had never run in production — "imports fine" is not
"verifies correctly". This drives run_guardian end-to-end against the live infra +
the real evidence corpus and reports the verdicts, asserting the two anchors:
  * a blatantly fabricated resume is BLOCKED (invented degree/employer/leadership),
  * a resume built only from the person's real evidence PASSES (no false block).
The model is non-deterministic, so a single run is a smoke, not a proof — but a
fabrication that passes, or clean evidence that blocks, is a real signal to dig in.

USAGE (against the running stack):
    docker exec -e PYTHONPATH=/app/src careeragent-api python /app/scripts_smoke_guardian.py
Exits non-zero if either anchor fails.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, "/app/src")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from agent import grounding, guardian          # noqa: E402
from client.dossier import DossierClient        # noqa: E402
from client.infra import InfraClient            # noqa: E402

FAILURES: list = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if detail:
        print(f"          {detail}")
    if not ok:
        FAILURES.append(label)


async def main() -> int:
    d_url, d_key = os.environ.get("DOSSIER_URL", "").rstrip("/"), os.environ.get("DOSSIER_API_KEY", "")
    # The app resolves the infra base URL from CAREERAGENT_INFRA_URL (default the
    # in-network host); accept either name so this runs the same way the app does.
    i_url = (os.environ.get("CAREERAGENT_INFRA_URL")
             or os.environ.get("INFRA_URL")
             or "http://careeragent-infra:8002").rstrip("/")
    i_key = os.environ.get("INFRA_API_KEY", "")
    if not (d_url and d_key and i_url and i_key):
        print("DOSSIER_URL/DOSSIER_API_KEY/INFRA_URL/INFRA_API_KEY must be set — run inside careeragent-api.")
        return 2

    dossier, infra = DossierClient(url=d_url, api_key=d_key), InfraClient(url=i_url, api_key=i_key)
    await dossier.start()
    await infra.start()
    try:
        status, body = await dossier.read_profile()
        profile = body.get("content", "") if isinstance(body, dict) else ""
        corpus = await grounding.build_corpus_from_dossier(profile, dossier)

        print(f"\n=== LIVE CONTEXT ===\n  corpus chars: {len(corpus)}")

        fabricated = (
            "## Professional Summary\nAward-winning engineer.\n"
            "## Experience\nStaff Engineer at Google (2018–2024) — led a team of 40.\n"
            "## Education\nPh.D. in Computer Science, Stanford University.\n"
            "## Certifications\nAWS Certified Solutions Architect – Professional.\n"
        )
        real = (
            "## Professional Summary\nBackend & AI engineer.\n"
            "## Experience\nComputer Scientist at Naval Undersea Warfare Center (NUWC).\n"
            "## Education\nB.S. Computer Science, University of Rhode Island.\n"
        )

        print("\n=== CATCHES A BLATANT FABRICATION ===")
        v = await guardian.run_guardian(infra, fabricated, corpus, effort="low", retries=1)
        print(f"  verdict: passed={v.passed} malfunction={v.malfunction}")
        for c in v.unsupported:
            print(f"    - {c.get('claim')}  ({c.get('why')})")
        check("a fabricated resume (Google/Stanford PhD/AWS cert/led 40) is NOT passed",
              not v.passed, f"rationale: {v.rationale}")

        print("\n=== DOESN'T FALSE-BLOCK REAL EVIDENCE ===")
        v2 = await guardian.run_guardian(infra, real, corpus, effort="low", retries=1)
        print(f"  verdict: passed={v2.passed} malfunction={v2.malfunction}")
        for c in v2.unsupported:
            print(f"    - {c.get('claim')}  ({c.get('why')})")
        check("a resume built from the person's real evidence passes",
              v2.passed, f"rationale: {v2.rationale}")
    finally:
        await dossier.aclose()
        await infra.aclose()

    print("\n" + ("SMOKE FAILED: " + ", ".join(FAILURES) if FAILURES else "SMOKE PASSED"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
