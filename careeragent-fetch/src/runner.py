#!/usr/bin/env python3
# ============================================================================
# careeragent-fetch - isolated extraction runner (untrusted-file blast wall)
# ============================================================================
#
# The parse itself lives in extract.py (pure, unit-tested). This module wraps it
# in an ISOLATION boundary so a hostile upload cannot take the API worker down:
#
#   - A decompression-bomb PDF is a tiny compressed stream (well under the 10 MB
#     upload cap) whose FlateDecode content inflates to gigabytes. pdfminer
#     decompresses a page's content stream fully into memory with no bound, and
#     the page cap doesn't help (one page is enough). Run in-process, that either
#     freezes the single event-loop worker (blocking /health + every concurrent
#     request) or gets the whole worker OOM-killed.
#
# So extraction runs in a SEPARATE, short-lived process with (a) an address-space
# rlimit — a runaway allocation raises MemoryError in the CHILD (or, if the
# container cgroup fires first, kills only the child) instead of the API worker —
# and (b) a wall-clock timeout enforced by the parent. The API layer calls this
# via asyncio.to_thread, so the event loop is never blocked by the join either.
# ============================================================================

from __future__ import annotations

import multiprocessing as mp
import queue as _queue

from extract import CorruptFile, ExtractProblem, ExtractResult, extract_document

# Defaults; the API layer passes env-configured values in.
DEFAULT_EXTRACT_TIMEOUT = 20.0
DEFAULT_EXTRACT_MEM_BYTES = 1_073_741_824  # 1 GiB address-space ceiling for the child

# spawn: a fresh interpreter that never inherits the event loop / worker threads /
# open sockets of the API process. Safe to start from a worker thread.
_CTX = mp.get_context("spawn")


class ExtractionTimeout(ExtractProblem):
    status_code = 504          # parse exceeded the wall-clock budget


class ResourceExceeded(ExtractProblem):
    status_code = 413          # parse hit the memory ceiling (bomb) — treated like "too large"


def _apply_mem_limit(mem_bytes: int) -> None:
    """Cap the child's address space so a bomb raises a catchable MemoryError
    instead of exhausting the host. POSIX only; a no-op on dev Windows (the
    timeout + process isolation still bound the blast, and prod is Linux)."""
    try:
        import resource
    except Exception:
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
    except (ValueError, OSError):
        pass  # best-effort; timeout + isolation remain


def _child(q, data: bytes, max_pdf_pages: int, max_text_chars: int, mem_bytes: int) -> None:
    """Runs in the isolated process. Puts a tagged result on the queue; never
    prints or raises across the boundary."""
    _apply_mem_limit(mem_bytes)
    try:
        r = extract_document(
            data, max_pdf_pages=max_pdf_pages, max_text_chars=max_text_chars
        )
        q.put(("ok", (r.text, r.truncated, r.format, r.chars)))
    except ExtractProblem as exc:
        q.put(("problem", (exc.status_code, exc.detail)))
    except MemoryError:
        q.put(("mem", None))
    except BaseException as exc:  # any parser blow-up — surface as a clean 400
        q.put(("error", f"{type(exc).__name__}: {exc}"))


def extract_isolated(
    data: bytes,
    *,
    max_pdf_pages: int,
    max_text_chars: int,
    timeout: float = DEFAULT_EXTRACT_TIMEOUT,
    mem_bytes: int = DEFAULT_EXTRACT_MEM_BYTES,
) -> ExtractResult:
    """Blocking. Run extract_document in an isolated, memory- and time-bounded
    process and return its ExtractResult. Raises the right ExtractProblem subclass
    (mapped to an HTTP status by the API layer) on failure / timeout / memory.
    Call from the API via asyncio.to_thread so the event loop stays responsive."""
    q = _CTX.Queue()
    proc = _CTX.Process(
        target=_child,
        args=(q, data, max_pdf_pages, max_text_chars, mem_bytes),
        daemon=True,
    )
    proc.start()
    try:
        try:
            # Drain the (small) result BEFORE join — avoids the classic
            # join-before-drain deadlock and enforces the wall-clock bound.
            kind, payload = q.get(timeout=timeout)
        except _queue.Empty:
            exitcode = proc.exitcode
            if exitcode is not None and exitcode < 0:
                # killed by a signal (e.g. the cgroup OOM-killer) before replying
                raise ResourceExceeded("the file exceeded the extraction resource limit")
            raise ExtractionTimeout("extraction timed out")
    finally:
        if proc.is_alive():
            proc.terminate()
        proc.join(2)
        if proc.is_alive():
            proc.kill()
            proc.join(1)

    if kind == "ok":
        text, truncated, fmt, chars = payload
        return ExtractResult(text=text, truncated=truncated, format=fmt, chars=chars)
    if kind == "problem":
        status, detail = payload
        exc = ExtractProblem(detail)   # rebuild so the API maps the exact status
        exc.status_code = status
        raise exc
    if kind == "mem":
        raise ResourceExceeded("the file is too large/complex to extract within the memory limit")
    raise CorruptFile(f"extraction failed: {payload}")
