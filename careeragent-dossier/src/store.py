"""
src/store.py

Persistence for careeragent-dossier: the master profile + the application
tracker (applications, contacts, resume_versions).

Own Postgres by default; everything is created in (and queried against) the
DOSSIER_DB_SCHEMA schema (default ``careeragent_dossier``) via the connection
``search_path``, so pointing DOSSIER_DB_HOST/NAME at a SHARED instance later is a
config-only change — no code edit. See specs/0001-dossier.md.

Async SQLAlchemy Core over asyncpg; all values are parameterized (no string
interpolation of values — the only interpolation is over a fixed allowlist of
column names in update_application). Tables are created by database/init.sql on
first DB boot — this module only reads/writes rows.

No vectors. Lookup is structured filters + full-text search (the generated
``search_vector`` column) + trigram fuzzy matching (pg_trgm ``%`` operator).
"""
import json
import os
from typing import Any, List, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

SCHEMA = os.environ.get("DOSSIER_DB_SCHEMA", "careeragent_dossier")

# Columns update_application may set — a FIXED allowlist (the only place a column
# name is interpolated into SQL, so it stays injection-safe). Date/time values
# arrive already parsed into datetime/date objects (by the Pydantic model), which
# asyncpg binds natively — so no SQL casts are needed.
_UPDATABLE = {
    "status",
    "last_contact",
    "next_follow_up",
    "applied_at",
    "posting_url",
    "location",
    "salary_range",
    "notes",
    "company",
    "title",
    "job_description",
}

# Columns create_project / update_project may set — a FIXED allowlist (the only
# place a projects column name is interpolated into SQL, so it stays
# injection-safe, exactly like _UPDATABLE above).
_PROJECT_FIELDS = {
    "name",
    "source",
    "external_id",
    "repo_url",
    "summary",
    "role",
    "tech_stack",
    "highlights",
    "languages",
    "stars",
    "last_reviewed_at",
    "commit_sha",
}


class EditError(Exception):
    """An exact-match edit could not be applied safely.

    kind == "not_found" : old_string does not appear in the target.
    kind == "not_unique": old_string appears >1 time and replace_all is False.
    """

    def __init__(self, kind: str, count: int = 0):
        self.kind = kind
        self.count = count
        super().__init__(kind)


class ConflictError(Exception):
    """A write would violate a uniqueness constraint (e.g. a duplicate
    external_id). The API layer maps this to HTTP 409 with a teaching message,
    rather than letting it fall through to a generic 500."""


def _clean(value: Optional[str]) -> Optional[str]:
    """Strip lone UTF-16 surrogates before storing (model output can split a
    multi-byte char across tokens and surface a broken surrogate, which then
    breaks Postgres/JSON encoding). Keeps all valid text."""
    if value is None:
        return None
    return value.encode("utf-8", "ignore").decode("utf-8", "ignore")


def _as_json(value: Any) -> Optional[str]:
    """Serialize a value for a jsonb column (bound as text + CAST(... AS jsonb) in
    the SQL). None stays NULL."""
    if value is None:
        return None
    return json.dumps(value)


def _apply_edit(current: str, old: str, new: str, replace_all: bool) -> str:
    """Exact-match-or-fail replace. Raises EditError; never a partial write."""
    count = current.count(old)
    if count == 0:
        raise EditError("not_found")
    if count > 1 and not replace_all:
        raise EditError("not_unique", count)
    return current.replace(old, new) if replace_all else current.replace(old, new, 1)


def _database_url() -> str:
    """Build the asyncpg URL from DOSSIER_DB_* parts, or use DOSSIER_DATABASE_URL."""
    explicit = os.environ.get("DOSSIER_DATABASE_URL", "").strip()
    if explicit:
        return explicit
    user = os.environ.get("DOSSIER_DB_USER", "careeragent_dossier")
    password = os.environ.get("DOSSIER_DB_PASSWORD", "")
    host = os.environ.get("DOSSIER_DB_HOST", "dossier-db")
    port = os.environ.get("DOSSIER_DB_PORT", "5432")
    name = os.environ.get("DOSSIER_DB_NAME", "careeragent_dossier")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"


class Store:
    """Async persistence over the profile / applications / contacts / resume_versions tables."""

    def __init__(self, url: Optional[str] = None, schema: Optional[str] = None):
        self._schema = schema or SCHEMA
        self._engine = create_async_engine(
            url or _database_url(),
            pool_pre_ping=True,
            connect_args={"server_settings": {"search_path": self._schema}},
        )

    async def ping(self) -> bool:
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def stop(self) -> None:
        await self._engine.dispose()

    # ---------------------------------------------------------------- profile
    async def get_profile(self) -> dict:
        async with self._engine.connect() as conn:
            r = await conn.execute(
                text("SELECT content, version, updated_at FROM profile WHERE id = 1")
            )
            return dict(r.first()._mapping)

    async def edit_profile(self, old: str, new: str, replace_all: bool) -> dict:
        """Exact-match edit of the master profile; bumps version. Raises EditError."""
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text("SELECT content, version FROM profile WHERE id = 1 FOR UPDATE")
            )
            row = r.first()
            updated = _clean(_apply_edit(row.content, old, new, replace_all))
            new_version = row.version + 1
            await conn.execute(
                text(
                    "UPDATE profile SET content = :c, version = :v, updated_at = now() "
                    "WHERE id = 1"
                ),
                {"c": updated, "v": new_version},
            )
            return {"content": updated, "version": new_version}

    async def save_profile(self, content: str) -> dict:
        """Set the master profile wholesale (seed/replace); bumps version."""
        cleaned = _clean(content)
        async with self._engine.begin() as conn:
            r = await conn.execute(text("SELECT version FROM profile WHERE id = 1 FOR UPDATE"))
            new_version = r.scalar_one() + 1
            await conn.execute(
                text(
                    "UPDATE profile SET content = :c, version = :v, updated_at = now() "
                    "WHERE id = 1"
                ),
                {"c": cleaned, "v": new_version},
            )
            return {"content": cleaned, "version": new_version}

    async def _profile_version(self, conn) -> int:
        r = await conn.execute(text("SELECT version FROM profile WHERE id = 1"))
        return r.scalar_one()

    # ----------------------------------------------------------- applications
    async def create_application(self, company: str, title: str, job_description: str) -> str:
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text(
                    "INSERT INTO applications (company, title, job_description) "
                    "VALUES (:company, :title, :jd) RETURNING id"
                ),
                {
                    "company": _clean(company),
                    "title": _clean(title),
                    "jd": _clean(job_description),
                },
            )
            return str(r.scalar_one())

    async def get_application(self, application_id: str) -> Optional[dict]:
        async with self._engine.connect() as conn:
            ar = await conn.execute(
                text(
                    "SELECT a.*, "
                    "(a.profile_version_at_render IS NOT NULL AND "
                    " (SELECT version FROM profile WHERE id = 1) > a.profile_version_at_render) AS stale, "
                    "(SELECT COUNT(*) FROM resume_versions rv WHERE rv.application_id = a.id) "
                    "  AS resume_versions "
                    "FROM applications a WHERE a.id = :id"
                ),
                {"id": application_id},
            )
            app = ar.first()
            if app is None:
                return None
            out = dict(app._mapping)
            out.pop("search_vector", None)  # internal FTS column, not part of the contract
            cr = await conn.execute(
                text(
                    "SELECT id, name, role, source, contact_info, notes, created_at "
                    "FROM contacts WHERE application_id = :id ORDER BY created_at ASC"
                ),
                {"id": application_id},
            )
            out["contacts"] = [dict(row._mapping) for row in cr]
            return out

    async def search_applications(
        self,
        status: Optional[str] = None,
        company: Optional[str] = None,
        q: Optional[str] = None,
        applied_after: Optional[str] = None,
        applied_before: Optional[str] = None,
        stale: Optional[bool] = None,
        follow_up_due: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[dict]:
        where = []
        params = {"limit": limit, "offset": offset}
        stale_expr = (
            "(a.profile_version_at_render IS NOT NULL AND "
            "(SELECT version FROM profile WHERE id = 1) > a.profile_version_at_render)"
        )

        if status:
            where.append("a.status = :status")
            params["status"] = status
        if company:
            where.append("a.company % :company")  # pg_trgm fuzzy similarity
            params["company"] = company
        if q:
            where.append("a.search_vector @@ plainto_tsquery('english', :q)")
            params["q"] = q
        if applied_after:
            where.append("a.applied_at >= :applied_after")
            params["applied_after"] = applied_after
        if applied_before:
            where.append("a.applied_at <= :applied_before")
            params["applied_before"] = applied_before
        if follow_up_due is True:
            # a follow-up whose date has arrived / passed (P7 #18b reminders)
            where.append("a.next_follow_up IS NOT NULL AND a.next_follow_up <= CURRENT_DATE")
        if stale is True:
            where.append(stale_expr)
        elif stale is False:
            where.append("NOT " + stale_expr)

        rank_select = (
            ", ts_rank(a.search_vector, plainto_tsquery('english', :q)) AS rank"
            if q
            else ", 0.0 AS rank"
        )
        order = "rank DESC, a.updated_at DESC" if q else "a.updated_at DESC"
        where_sql = (" AND " + " AND ".join(where)) if where else ""

        sql = (
            "SELECT a.id, a.company, a.title, a.status, a.last_contact, a.next_follow_up, a.updated_at, "
            f"{stale_expr} AS stale{rank_select} "
            f"FROM applications a WHERE 1 = 1{where_sql} "
            f"ORDER BY {order} LIMIT :limit OFFSET :offset"
        )
        async with self._engine.connect() as conn:
            r = await conn.execute(text(sql), params)
            return [dict(row._mapping) for row in r]

    async def update_application(self, application_id: str, fields: dict) -> Optional[dict]:
        """Update structured fields (never final_resume). Returns the updated row, or None."""
        provided = {k: v for k, v in fields.items() if v is not None and k in _UPDATABLE}
        if not provided:
            return await self.get_application(application_id)

        set_parts, params = [], {"id": application_id}
        for k, v in provided.items():
            set_parts.append(f"{k} = :{k}")
            params[k] = _clean(v) if isinstance(v, str) else v
        set_parts.append("updated_at = now()")

        async with self._engine.begin() as conn:
            r = await conn.execute(
                text(
                    f"UPDATE applications SET {', '.join(set_parts)} "
                    "WHERE id = :id RETURNING id"
                ),
                params,
            )
            if r.first() is None:
                return None
        return await self.get_application(application_id)

    async def delete_application(self, application_id: str) -> bool:
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text("DELETE FROM applications WHERE id = :id"), {"id": application_id}
            )
            return r.rowcount > 0

    # ---------------------------------------------------------------- contacts
    async def add_contact(self, application_id: str, contact: dict) -> Optional[str]:
        async with self._engine.begin() as conn:
            exists = await conn.execute(
                text("SELECT 1 FROM applications WHERE id = :id"), {"id": application_id}
            )
            if exists.first() is None:
                return None
            r = await conn.execute(
                text(
                    "INSERT INTO contacts (application_id, name, role, source, contact_info, notes) "
                    "VALUES (:aid, :name, :role, :source, :info, :notes) RETURNING id"
                ),
                {
                    "aid": application_id,
                    "name": _clean(contact.get("name")),
                    "role": _clean(contact.get("role")),
                    "source": _clean(contact.get("source")),
                    "info": _clean(contact.get("contact_info")),
                    "notes": _clean(contact.get("notes")),
                },
            )
            return str(r.scalar_one())

    # ------------------------------------------------------------ preferences
    # Agent-authored durable coaching preferences (P7 #17). User-STATED standing
    # instructions, kept physically distinct from the profile/projects so they
    # never enter the grounding corpus (ADR-002).
    async def add_preference(self, content: str) -> str:
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text("INSERT INTO preferences (content) VALUES (:content) RETURNING id"),
                {"content": _clean(content)},
            )
            return str(r.scalar_one())

    async def list_preferences(self) -> List[dict]:
        async with self._engine.connect() as conn:
            r = await conn.execute(
                text("SELECT id, content, created_at FROM preferences ORDER BY created_at ASC")
            )
            return [dict(row._mapping) for row in r]

    async def delete_preference(self, preference_id: str) -> bool:
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text("DELETE FROM preferences WHERE id = :id"), {"id": preference_id}
            )
            return r.rowcount > 0

    # ----------------------------------------------------------------- resumes
    async def _next_resume_version(self, conn, application_id: str) -> int:
        r = await conn.execute(
            text(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM resume_versions "
                "WHERE application_id = :id"
            ),
            {"id": application_id},
        )
        return r.scalar_one()

    async def _write_resume(self, conn, application_id: str, content: str) -> int:
        """Record a new resume version + point the application at it. Assumes the
        caller has verified the application exists and is inside a transaction."""
        version = await self._next_resume_version(conn, application_id)
        pv = await self._profile_version(conn)
        await conn.execute(
            text(
                "INSERT INTO resume_versions (application_id, version, content) "
                "VALUES (:aid, :v, :c)"
            ),
            {"aid": application_id, "v": version, "c": content},
        )
        await conn.execute(
            text(
                "UPDATE applications SET final_resume = :c, "
                "profile_version_at_render = :pv, updated_at = now() WHERE id = :aid"
            ),
            {"c": content, "pv": pv, "aid": application_id},
        )
        return version

    async def save_resume(self, application_id: str, content: str) -> Optional[int]:
        """Replace the tailored resume wholesale; returns the new version, or None."""
        async with self._engine.begin() as conn:
            exists = await conn.execute(
                text("SELECT 1 FROM applications WHERE id = :id"), {"id": application_id}
            )
            if exists.first() is None:
                return None
            return await self._write_resume(conn, application_id, _clean(content))

    async def edit_resume(
        self, application_id: str, old: str, new: str, replace_all: bool
    ) -> Optional[dict]:
        """Exact-match edit of the tailored resume; returns {content, version}, or None. Raises EditError."""
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text("SELECT final_resume FROM applications WHERE id = :id FOR UPDATE"),
                {"id": application_id},
            )
            row = r.first()
            if row is None:
                return None
            updated = _clean(_apply_edit(row.final_resume, old, new, replace_all))
            version = await self._write_resume(conn, application_id, updated)
            return {"content": updated, "version": version}

    # -------------------------------------------------------- resume artifacts
    async def save_artifact(
        self,
        application_id: str,
        fmt: str,
        filename: str,
        content: bytes,
        ats: Optional[dict] = None,
    ) -> Optional[dict]:
        """Store a rendered document (PDF/DOCX bytes) for an application under the
        next monotonic version. Returns {id, version, byte_size}, or None if the
        application does not exist. `ats` (optional) carries reserved coverage
        fields {score, coverage, matched, missing} — all nullable today."""
        ats = ats or {}
        # The next version is computed with a non-atomic MAX(version)+1, so two
        # concurrent saves for the SAME application can both pick the same version
        # and one loses the UNIQUE(application_id, version) race. Retry on that
        # IntegrityError (the retry re-reads MAX and succeeds) instead of 500-ing
        # and discarding a successfully-rendered document — mirrors create_project.
        for attempt in range(5):
            try:
                async with self._engine.begin() as conn:
                    exists = await conn.execute(
                        text("SELECT 1 FROM applications WHERE id = :id"), {"id": application_id}
                    )
                    if exists.first() is None:
                        return None
                    v = await conn.execute(
                        text(
                            "SELECT COALESCE(MAX(version), 0) + 1 FROM resume_artifacts "
                            "WHERE application_id = :id"
                        ),
                        {"id": application_id},
                    )
                    version = v.scalar_one()
                    r = await conn.execute(
                        text(
                            "INSERT INTO resume_artifacts "
                            "(application_id, version, format, filename, content, byte_size, "
                            " ats_score, ats_coverage, ats_matched, ats_missing) "
                            "VALUES (:aid, :v, :fmt, :fn, :c, :sz, :score, :cov, "
                            "        CAST(:matched AS jsonb), CAST(:missing AS jsonb)) "
                            "RETURNING id"
                        ),
                        {
                            "aid": application_id,
                            "v": version,
                            "fmt": fmt,
                            "fn": filename,
                            "c": content,
                            "sz": len(content),
                            "score": ats.get("score"),
                            "cov": ats.get("coverage"),
                            "matched": _as_json(ats.get("matched")),
                            "missing": _as_json(ats.get("missing")),
                        },
                    )
                    return {"id": str(r.scalar_one()), "version": version, "byte_size": len(content)}
            except IntegrityError:
                if attempt == 4:
                    raise
                continue

    async def get_artifact(
        self, application_id: str, artifact_id: Optional[str] = None
    ) -> Optional[dict]:
        """Fetch one rendered artifact's bytes + metadata. With `artifact_id`, that
        specific artifact SCOPED to the application (so a caller can't pull another
        application's bytes by guessing an id); otherwise the LATEST version. Returns
        {id, format, filename, content: bytes, byte_size, version}, or None."""
        async with self._engine.connect() as conn:
            if artifact_id:
                r = await conn.execute(
                    text(
                        "SELECT id, format, filename, content, byte_size, version "
                        "FROM resume_artifacts WHERE id = :aid AND application_id = :app"
                    ),
                    {"aid": artifact_id, "app": application_id},
                )
            else:
                r = await conn.execute(
                    text(
                        "SELECT id, format, filename, content, byte_size, version "
                        "FROM resume_artifacts WHERE application_id = :app "
                        "ORDER BY version DESC LIMIT 1"
                    ),
                    {"app": application_id},
                )
            row = r.first()
            if row is None:
                return None
            m = row._mapping
            return {
                "id": str(m["id"]),
                "format": m["format"],
                "filename": m["filename"],
                "content": bytes(m["content"]),
                "byte_size": m["byte_size"],
                "version": m["version"],
            }

    # ----------------------------------------------------------------- projects
    async def create_project(self, fields: dict) -> dict:
        """Insert a project, or UPSERT by external_id when one is supplied (so
        re-reviewing a GitHub repo refreshes the row instead of duplicating).
        Returns {id, upserted}. Column names come from the fixed _PROJECT_FIELDS
        allowlist, so the interpolation is injection-safe."""
        cols = {
            k: (_clean(v) if isinstance(v, str) else v)
            for k, v in fields.items()
            if k in _PROJECT_FIELDS and v is not None
        }
        # A blank external_id is "no key": store it as NULL so it escapes the
        # partial unique index (WHERE external_id IS NOT NULL) and the upsert
        # branch below (a truthiness test) stays consistent with that predicate —
        # otherwise two '' rows would collide on the index and 500.
        ext = cols.get("external_id")
        if isinstance(ext, str):
            if not ext.strip():
                cols.pop("external_id", None)
            else:
                # Canonicalize the repo dedupe key — GitHub 'owner/repo' is
                # case-insensitive, so upsert/lookup must be too, or re-review of
                # 'Owner/Repo' vs 'owner/repo' would duplicate / miss the skip.
                cols["external_id"] = ext.strip().lower()
        names = list(cols.keys())
        placeholders = ", ".join(f":{c}" for c in names)
        async with self._engine.begin() as conn:
            try:
                if cols.get("external_id"):
                    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in names if c != "external_id")
                    sql = (
                        f"INSERT INTO projects ({', '.join(names)}) VALUES ({placeholders}) "
                        "ON CONFLICT (external_id) WHERE external_id IS NOT NULL "
                        f"DO UPDATE SET {updates}, updated_at = now() "
                        "RETURNING id, (xmax <> 0) AS updated"
                    )
                    row = (await conn.execute(text(sql), cols)).first()
                    return {"id": str(row.id), "upserted": bool(row.updated)}
                sql = f"INSERT INTO projects ({', '.join(names)}) VALUES ({placeholders}) RETURNING id"
                r = await conn.execute(text(sql), cols)
                return {"id": str(r.scalar_one()), "upserted": False}
            except IntegrityError:
                raise ConflictError("a project with that external_id already exists")

    async def get_project(self, project_id: str) -> Optional[dict]:
        async with self._engine.connect() as conn:
            r = await conn.execute(
                text("SELECT * FROM projects WHERE id = :id"), {"id": project_id}
            )
            row = r.first()
            if row is None:
                return None
            out = dict(row._mapping)
            out.pop("search_vector", None)  # internal FTS column, not part of the contract
            return out

    async def search_projects(
        self,
        q: Optional[str] = None,
        source: Optional[str] = None,
        name: Optional[str] = None,
        external_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[dict]:
        where = []
        params = {"limit": limit, "offset": offset}
        if source:
            where.append("p.source = :source")
            params["source"] = source
        if name:
            where.append("p.name % :name")  # pg_trgm fuzzy similarity
            params["name"] = name
        if external_id:
            # Exact lookup by repo identity — the idempotency read path for
            # careeragent-review (return the stored commit_sha for owner/repo).
            # Lower-cased to match the canonical form create_project stores.
            where.append("p.external_id = :external_id")
            params["external_id"] = external_id.strip().lower()
        if q:
            where.append("p.search_vector @@ plainto_tsquery('english', :q)")
            params["q"] = q

        rank_select = (
            ", ts_rank(p.search_vector, plainto_tsquery('english', :q)) AS rank"
            if q
            else ", 0.0 AS rank"
        )
        order = "rank DESC, p.updated_at DESC" if q else "p.updated_at DESC"
        where_sql = (" AND " + " AND ".join(where)) if where else ""
        # NOTE: summary/role/highlights/languages/stars are SELECTed deliberately.
        # They were omitted, which starved two consumers at once:
        #   1. careeragent-api's grounding gate builds its evidence corpus from these
        #      exact fields — without them a skill evidenced only in a project's
        #      summary/highlights looks unbacked, so the gate FALSE-FLAGS real evidence.
        #   2. the coach's own search_projects tool returns this payload, so the model
        #      could see only a name + tech_stack when tailoring a resume — it had no
        #      real project detail to ground on, which invites it to invent some.
        # Anything added here must stay in sync with careeragent-api/src/agent/
        # grounding.py::build_corpus.
        sql = (
            "SELECT p.id, p.name, p.source, p.external_id, p.repo_url, p.tech_stack, "
            "p.summary, p.role, p.highlights, p.languages, p.stars, "
            "p.commit_sha, p.last_reviewed_at, p.updated_at"
            f"{rank_select} FROM projects p WHERE 1 = 1{where_sql} "
            f"ORDER BY {order} LIMIT :limit OFFSET :offset"
        )
        async with self._engine.connect() as conn:
            r = await conn.execute(text(sql), params)
            return [dict(row._mapping) for row in r]

    async def update_project(self, project_id: str, fields: dict) -> Optional[dict]:
        provided = {k: v for k, v in fields.items() if v is not None and k in _PROJECT_FIELDS}
        if not provided:
            return await self.get_project(project_id)
        set_parts, params = [], {"id": project_id}
        for k, v in provided.items():
            set_parts.append(f"{k} = :{k}")
            params[k] = _clean(v) if isinstance(v, str) else v
        set_parts.append("updated_at = now()")
        async with self._engine.begin() as conn:
            try:
                r = await conn.execute(
                    text(f"UPDATE projects SET {', '.join(set_parts)} WHERE id = :id RETURNING id"),
                    params,
                )
            except IntegrityError:
                raise ConflictError("another project already uses that external_id")
            if r.first() is None:
                return None
        return await self.get_project(project_id)

    async def delete_project(self, project_id: str) -> bool:
        async with self._engine.begin() as conn:
            r = await conn.execute(
                text("DELETE FROM projects WHERE id = :id"), {"id": project_id}
            )
            return r.rowcount > 0
