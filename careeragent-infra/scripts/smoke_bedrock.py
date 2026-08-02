#!/usr/bin/env python3
"""
scripts/smoke_bedrock.py

Pre-flight check for the careeragent-infra Bedrock config. Confirms the model
ids in your .env actually resolve AND your AWS credentials can invoke them —
BEFORE you build and run the whole stack. Catches a wrong model id or missing
model access in seconds, instead of via Docker logs after a /chat returns an
in-stream [ERROR].

Run from the careeragent-infra repo root (it reads .env):
    python scripts/smoke_bedrock.py

Exits 0 if the base model works, 1 otherwise. The optional routes
(nervous_system, embedding) are reported but do not fail the check when unset.
This script is standalone — it does NOT import src.api.main.
"""
import os
import sys

# Must be set before importing litellm (avoids a GitHub fetch at import).
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

try:
    import litellm
    from dotenv import load_dotenv
except ImportError as exc:  # pragma: no cover - dependency guard
    print(f"missing dependency: {exc}")
    print("install with: pip install -r requirements.txt")
    sys.exit(2)

load_dotenv()
litellm.drop_params = True
litellm.modify_params = True
litellm.suppress_debug_info = True

REGION  = os.environ.get("AWS_REGION_NAME", "") or os.environ.get("AWS_REGION", "")
EFFORT  = os.environ.get("REASONING_EFFORT", "medium")
BASE    = os.environ.get("BASE_MODEL", "")
NERVOUS = os.environ.get("NERVOUS_SYSTEM_MODEL", "")
EMBED   = os.environ.get("EMBEDDING_MODEL", "")


def _hint(exc: Exception) -> str:
    """Map a Bedrock error to an actionable one-liner."""
    msg = str(exc).lower()
    if "accessdenied" in msg or "access denied" in msg or "don't have access" in msg:
        return ("  -> AccessDenied: enable model access for this id in the Bedrock "
                "console, and check IAM bedrock:InvokeModel / "
                "bedrock:InvokeModelWithResponseStream.")
    if "validationexception" in msg or "not found" in msg or "invalid" in msg:
        return ("  -> looks like a bad model id. Confirm the exact id (including the "
                "`us.` inference-profile prefix for Claude) in the Bedrock console.")
    if any(s in msg for s in ("credential", "token", "could not connect", "region", "no region")):
        return ("  -> auth/region: set AWS_REGION_NAME and valid AWS credentials "
                "(keys, profile, IAM role, or AWS_BEARER_TOKEN_BEDROCK).")
    return ""


def _check_chat(label: str, model_id: str) -> bool:
    print(f"[{label}] {model_id} ... ", end="", flush=True)
    try:
        resp = litellm.completion(
            model=model_id,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            max_tokens=16,
            reasoning_effort=EFFORT,
            aws_region_name=REGION,
            timeout=120,
        )
        text = (resp.choices[0].message.content or "").strip()
        print(f"OK ({text[:40]!r})")
        return True
    except Exception as exc:
        print("FAIL")
        print(f"  {type(exc).__name__}: {str(exc)[:300]}")
        hint = _hint(exc)
        if hint:
            print(hint)
        return False


def _check_embed(label: str, model_id: str) -> bool:
    print(f"[{label}] {model_id} ... ", end="", flush=True)
    try:
        resp = litellm.embedding(model=model_id, input=["smoke test"],
                                 aws_region_name=REGION, timeout=120)
        item = resp.data[0] if getattr(resp, "data", None) else {}
        vec = item["embedding"] if isinstance(item, dict) else getattr(item, "embedding", [])
        print(f"OK ({len(vec)}-dim vector)")
        return True
    except Exception as exc:
        print("FAIL")
        print(f"  {type(exc).__name__}: {str(exc)[:300]}")
        hint = _hint(exc)
        if hint:
            print(hint)
        return False


def main() -> int:
    print(f"AWS region: {REGION or 'NOT SET'}\n")
    if not REGION:
        print("AWS_REGION_NAME is not set — Bedrock calls will fail. Set it in .env.\n")
    if not BASE:
        print("BASE_MODEL is not set — nothing to check. Set it in .env.")
        return 1

    base_ok = _check_chat("base", BASE)

    if NERVOUS:
        _check_chat("nervous_system", NERVOUS)
    else:
        print("[nervous_system] (not configured) — skipped")

    if EMBED:
        _check_embed("embedding", EMBED)
    else:
        print("[embedding] (not configured) — skipped")

    print()
    if base_ok:
        print("Base model reachable — careeragent-infra should serve /chat.")
        return 0
    print("Base model FAILED — fix the above before running the stack.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
