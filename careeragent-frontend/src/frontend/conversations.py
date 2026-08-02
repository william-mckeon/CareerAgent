#!/usr/bin/env python3
# ============================================================================
# careeragent-frontend - Conversation management
# Maintainer: William McKeon
# ============================================================================
#
# ROLE:
#   Owns the MULTI-CONVERSATION experience on top of careeragent-sessions. app.py
#   renders the chat; this module owns which conversation that chat is bound to
#   and the sidebar that switches between them. Keeping it here keeps app.py lean
#   (mirrors how sse_decoder.py owns the SSE protocol).
#
# OWNERSHIP BOUNDARY:
#   careeragent-sessions is the SYSTEM-OF-RECORD — it persists every transcript
#   and mints conversation ids (returned to app.py in the X-Conversation-Id
#   header on /chat). This module never stores anything itself; it just drives
#   sessions' HTTP CRUD and mirrors the *active* conversation into Streamlit:
#
#     st.session_state.messages          ← the transcript app.py renders
#     st.session_state.conversation_id   ← the bound conversation (sent on /chat)
#     st.query_params["c"]               ← so a page reload restores the same one
#
#   sessions endpoints used:
#     GET    /conversations          → list (sidebar)
#     GET    /conversations/{id}     → restore one transcript (load / URL restore)
#     DELETE /conversations/{id}     → remove one
#   (POST /chat mints + persists; that lives in app.py's stream path, not here.)
#
# "NEW CONVERSATION" is purely a LOCAL reset — clear the transcript and unbind
# the id. The next /chat has no conversation_id, so sessions mints a fresh row
# and returns its id. There is no explicit create call from the sidebar.
#
# FAIL-OPEN: every network call swallows errors and degrades gracefully (an
# empty list, a toast, a no-op) so a sessions hiccup never takes down the chat.
# ============================================================================

import logging
from typing import Dict, List, Optional

import requests
import streamlit as st

# Child of app.py's "careeragent.frontend" logger so one logging config in app.py
# covers this module too (same pattern as sse_decoder's logger).
logger = logging.getLogger("careeragent.frontend.conversations")


class ConversationManager:
    """Drives careeragent-sessions' conversation CRUD and the sidebar switcher.

    Construct once per script run with the same base URL / API key app.py uses
    for /chat and /health (the careeragent-sessions boundary), then:

        conversations = ConversationManager(url, key, timeout)
        conversations.restore_from_url()   # once, near the top of the run
        ...
        conversations.render_sidebar()     # after the health gate clears
    """

    def __init__(self, base_url: str, api_key: str, timeout: float) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout

    @property
    def _headers(self) -> Dict[str, str]:
        return {"X-API-Key": self._api_key}

    # The title careeragent-jobs' scheduler gives its singleton reminders thread.
    _REMINDERS_TITLE_PREFIX = "🔔 Reminders"

    @classmethod
    def _is_reminders(cls, conv: Dict) -> bool:
        """True for the scheduler's "🔔 Reminders" conversation (P7 #18b), which
        we pin to the top of the sidebar."""
        return (conv.get("title") or "").strip().startswith(cls._REMINDERS_TITLE_PREFIX)

    # ------------------------------------------------------------------ state
    def _bind_transcript(self, cid: str, messages: List[Dict]) -> None:
        """Make ``cid`` the active conversation and load its transcript into the
        chat. Mirrors the id into the URL so a reload restores this one. Only
        role/content survive into session_state — that is all app.py renders and
        all it resends on the next /chat."""
        st.session_state.messages = [
            {"role": m["role"], "content": m["content"]} for m in messages
        ]
        st.session_state.conversation_id = cid
        st.query_params["c"] = cid
        # P4: if this conversation has a PAUSED run, re-arm its question so the
        # answer controls render again after a reload or a sidebar switch.
        self._rehydrate_pending(cid)

    def _rehydrate_pending(self, cid: str) -> None:
        """Restore st.session_state.pending_request from the run's server-side
        state (GET /run-state). Fail open: any error leaves no pending request,
        so the conversation is simply resumable as a normal chat."""
        st.session_state.pending_request = None
        try:
            resp = requests.get(
                f"{self._base_url}/conversations/{cid}/run-state",
                headers=self._headers, timeout=self._timeout,
            )
            if resp.status_code != 200:
                return
            rs = resp.json()
        except Exception as err:
            logger.warning(f"Could not fetch run-state for {cid}: {err}")
            return
        if rs.get("status") == "paused" and rs.get("pending_call_id"):
            payload = rs.get("pending_payload") or {}
            options = payload.get("options")
            st.session_state.pending_request = {
                "call_id": str(rs["pending_call_id"]),
                "kind": str(rs.get("pending_kind") or "question"),
                "question": str(payload.get("question") or ""),
                "options": [str(o) for o in options] if isinstance(options, list) else [],
            }
            logger.info(f"Restored PAUSED run for {cid} (pending {rs.get('pending_kind')})")

    # ------------------------------------------------------------------- HTTP
    def _fetch(self, cid: str) -> Optional[Dict]:
        """GET one conversation's full transcript, or None on any failure."""
        try:
            resp = requests.get(
                f"{self._base_url}/conversations/{cid}",
                headers=self._headers,
                timeout=self._timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"GET /conversations/{cid} -> HTTP {resp.status_code}")
        except Exception as err:
            logger.warning(f"Could not fetch conversation {cid}: {err}")
        return None

    def list(self) -> List[Dict]:
        """Return the conversation list for the sidebar (newest-first per
        sessions' ordering). Fail open: any error returns [] so the sidebar
        collapses to just the 'New conversation' control."""
        try:
            resp = requests.get(
                f"{self._base_url}/conversations",
                headers=self._headers,
                timeout=self._timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            logger.warning(f"GET /conversations -> HTTP {resp.status_code}")
        except Exception as err:
            logger.warning(f"Could not list conversations: {err}")
        return []

    def restore_from_url(self) -> None:
        """Restore the conversation named by ?c=<id> on first load (e.g. after a
        page reload). One-shot per browser session, guarded by the
        ``history_loaded`` flag so later reruns don't fight explicit switches.
        Fails open: any error just leaves the chat empty to start fresh."""
        if st.session_state.get("history_loaded"):
            return
        st.session_state.history_loaded = True  # set first: never retry on failure
        cid = st.query_params.get("c")
        if not cid:
            return
        conv = self._fetch(cid)
        if conv is not None:
            self._bind_transcript(cid, conv.get("messages", []))
            logger.info(
                f"Restored conversation {cid} "
                f"({len(st.session_state.messages)} messages)"
            )
        else:
            logger.info(f"Nothing to restore for c={cid}")

    def load(self, cid: str) -> None:
        """Switch the active conversation to ``cid`` and pull its transcript.
        The caller reruns afterwards so the chat area redraws from history."""
        conv = self._fetch(cid)
        if conv is None:
            st.toast("Could not open that conversation.")
            return
        self._bind_transcript(cid, conv.get("messages", []))
        logger.info(
            f"Switched to conversation {cid} "
            f"({len(st.session_state.messages)} messages)"
        )

    def start_new(self) -> None:
        """Begin a fresh conversation: clear the transcript and unbind the id +
        URL param. The next /chat mints a new id server-side."""
        st.session_state.messages = []
        st.session_state.conversation_id = None
        # Drop ?c= so a reload doesn't restore the conversation we just left.
        try:
            del st.query_params["c"]
        except Exception:
            pass
        logger.info("Started a new conversation")

    def delete(self, cid: str) -> None:
        """Delete a conversation in sessions. If it was the open one, fall back
        to a fresh conversation so the chat isn't left showing a dead transcript."""
        try:
            resp = requests.delete(
                f"{self._base_url}/conversations/{cid}",
                headers=self._headers,
                timeout=self._timeout,
            )
            if resp.status_code == 200:
                logger.info(f"Deleted conversation {cid}")
                if st.session_state.get("conversation_id") == cid:
                    self.start_new()
                return
            st.toast(f"Could not delete that conversation (HTTP {resp.status_code}).")
            logger.warning(f"DELETE /conversations/{cid} -> HTTP {resp.status_code}")
        except Exception as err:
            st.toast("Could not delete that conversation.")
            logger.warning(f"delete_conversation {cid} failed: {err}")

    # --------------------------------------------------------------------- UI
    def render_sidebar(self) -> None:
        """Render the conversation switcher in the left sidebar.

        Top: a 'New conversation' button. Below: one row per saved conversation
        (title + message count) with a small delete control. The open
        conversation is highlighted (primary button). The list is re-fetched on
        every script run, so newly created / deleted conversations stay in sync.
        """
        with st.sidebar:
            st.markdown("### 💬 Conversations")

            if st.button(
                "➕  New conversation", use_container_width=True, key="new_conv"
            ):
                self.start_new()
                st.rerun()

            st.divider()

            conversations = self.list()
            if not conversations:
                st.caption("No conversations yet — send a message to start one.")
                return

            # P7 #18b: pin the scheduler's "🔔 Reminders" thread to the top so
            # recurring follow-up / freshness reminders are always one click away.
            # sorted() is stable, so every other conversation keeps sessions' order.
            conversations = sorted(
                conversations, key=lambda c: 0 if self._is_reminders(c) else 1
            )

            active = st.session_state.get("conversation_id")

            # P7 #18: a background job (spawn_job) injects its result into the
            # conversation out-of-band, so the server can have MORE messages than
            # this tab is showing. Surface that as a one-click refresh — if the
            # active conversation's server message_count exceeds what's rendered
            # locally, there's a background update waiting.
            if active:
                server_count = next((c.get("message_count", 0) for c in conversations
                                     if c.get("conversation_id") == active), 0)
                new_updates = server_count - len(st.session_state.get("messages", []))
                if new_updates > 0:
                    if st.button(f"🔔 {new_updates} background update(s) — load",
                                 use_container_width=True, type="primary", key="load_updates"):
                        self.load(active)
                        st.rerun()
                else:
                    if st.button("🔄 Check for updates", use_container_width=True,
                                 key="check_updates", help="Pull in any background job results"):
                        self.load(active)
                        st.rerun()
                st.divider()
            for conv in conversations:
                cid = conv["conversation_id"]
                title = (conv.get("title") or "Untitled").strip() or "Untitled"
                label = (title[:30] + "…") if len(title) > 30 else title
                count = conv.get("message_count", 0)
                is_active = cid == active

                open_col, del_col = st.columns([0.82, 0.18])
                with open_col:
                    if st.button(
                        label,
                        key=f"open_{cid}",
                        use_container_width=True,
                        type="primary" if is_active else "secondary",
                        help=f"{count} message(s) · {title}",
                    ):
                        self.load(cid)
                        st.rerun()
                with del_col:
                    if st.button(
                        "🗑",
                        key=f"del_{cid}",
                        use_container_width=True,
                        help="Delete this conversation",
                    ):
                        self.delete(cid)
                        st.rerun()
