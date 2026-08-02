#!/usr/bin/env python3
# ============================================================================
# careeragent-ats - request/response schemas
# ============================================================================

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field

# Input caps (mirror careeragent-fetch's defensive posture). Scoring is O(keywords ×
# distinct-résumé-tokens); an unbounded multi-MB body could burn CPU well past the
# api's read timeout. A résumé is KBs and a JD is smaller, so these are generous.
# Pydantic returns a 422 when either is exceeded — a clean rejection, not a hang.
_MAX_RESUME = 200_000   # chars
_MAX_JD = 100_000       # chars


class AtsRequest(BaseModel):
    """Body for POST /ats-score — a resume and the job description to score it against.

    Both are plain text (the caller has already fetched/extracted them, e.g. via
    careeragent-fetch). This service does no fetching and no file parsing.

    - resume_text: the candidate's resume as plain text. May be empty (→ score 0,
      everything missing). Never null. Capped at _MAX_RESUME chars (422 over that).
    - job_description: the target posting as plain text. Empty/whitespace is a 400
      (there is nothing to score against); see backend.api. Capped at _MAX_JD chars.
    """
    resume_text: str = Field(max_length=_MAX_RESUME)
    job_description: str = Field(max_length=_MAX_JD)


class AtsResponse(BaseModel):
    """POST /ats-score success (200) — deterministic keyword-coverage report.

    - score: 0-100, round(100 * matched / total) over the JD's extracted keywords.
    - coverage: "<matched>/<total>", e.g. "7/12" — the raw ratio behind the score.
    - matched: the JD keywords found in the resume (extraction/salience order).
    - missing: the JD keywords NOT found in the resume (extraction/salience order).
    """
    score: int
    coverage: str
    matched: List[str]
    missing: List[str]
