"""tests/test_workspace.py — the cache manager over real temp dirs (gitops mocked)."""
import pytest

import gitops
import workspace as ws_mod
from safety import CodeProblem
from workspace import Workspace


def _mk(tmp_path):
    return Workspace(str(tmp_path / "cache"), token="TOK",
                     max_file_bytes=20, max_tree_entries=100, max_grep_matches=50)


def _fake_checkout(ws, repo, files):
    """Materialize a fake synced repo (a .git dir + some files) under the cache."""
    d = ws._repo_dir(repo)
    (d / ".git").mkdir(parents=True)
    for rel, content in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return d


class TestSync:
    def test_sync_clones_and_measures(self, tmp_path, monkeypatch):
        ws = _mk(tmp_path)
        def fake_clone(repo, repo_dir, token, timeout):
            (repo_dir / ".git").mkdir(parents=True)
            (repo_dir / "a.py").write_text("hello")
            return "sha123"
        monkeypatch.setattr(gitops, "clone_or_pull", fake_clone)
        r = ws.sync("me/repo")
        assert r["repo"] == "me/repo" and r["head_sha"] == "sha123"
        assert r["files"] == 1 and r["bytes"] == 5 and r["cached"] is False

    def test_sync_rejects_bad_repo(self, tmp_path):
        with pytest.raises(CodeProblem) as e:
            _mk(tmp_path).sync("../evil")
        assert e.value.status_code == 400


class TestReadAndTree:
    def test_read_file_and_truncation(self, tmp_path):
        ws = _mk(tmp_path)  # max_file_bytes=20
        _fake_checkout(ws, "me/repo", {"src/a.py": "x" * 50})
        r = ws.read_file("me/repo", "src/a.py")
        assert r["bytes"] == 50 and r["truncated"] is True and len(r["content"]) == 20

    def test_read_missing_file_404(self, tmp_path):
        ws = _mk(tmp_path)
        _fake_checkout(ws, "me/repo", {"a.py": "x"})
        with pytest.raises(CodeProblem) as e:
            ws.read_file("me/repo", "nope.py")
        assert e.value.status_code == 404

    def test_read_traversal_blocked(self, tmp_path):
        ws = _mk(tmp_path)
        _fake_checkout(ws, "me/repo", {"a.py": "x"})
        with pytest.raises(CodeProblem) as e:
            ws.read_file("me/repo", "../../etc/passwd")
        assert e.value.status_code == 400

    def test_read_unsynced_repo_404(self, tmp_path):
        with pytest.raises(CodeProblem) as e:
            _mk(tmp_path).read_file("me/never", "a.py")
        assert e.value.status_code == 404

    def test_tree_lists_files_and_skips_git(self, tmp_path):
        ws = _mk(tmp_path)
        _fake_checkout(ws, "me/repo", {"a.py": "x", "src/b.py": "yy", "src/c.md": "z"})
        t = ws.tree("me/repo")
        paths = [e["path"] for e in t["entries"]]
        assert paths == ["a.py", "src/b.py", "src/c.md"]      # sorted, no .git
        assert all(".git" not in p for p in paths)


class TestGrepAndList:
    def test_grep_delegates(self, tmp_path, monkeypatch):
        ws = _mk(tmp_path)
        _fake_checkout(ws, "me/repo", {"a.py": "def foo"})
        monkeypatch.setattr(ws_mod.search, "grep",
                            lambda d, pat, glob, mx, to: {"matches": [{"path": "a.py", "line": 1, "text": "def foo"}], "truncated": False})
        r = ws.grep("me/repo", "foo", None)
        assert r["repo"] == "me/repo" and r["matches"][0]["path"] == "a.py"

    def test_list_repos(self, tmp_path, monkeypatch):
        ws = _mk(tmp_path)
        _fake_checkout(ws, "me/one", {"a": "x"})
        _fake_checkout(ws, "you/two", {"b": "y"})
        monkeypatch.setattr(gitops, "head_sha", lambda d, timeout=20.0: "sha")
        repos = sorted(r["repo"] for r in ws.list_repos())
        assert repos == ["me/one", "you/two"]


class TestCaps:
    def test_sync_rejects_oversized_repo_413(self, tmp_path, monkeypatch):
        ws = Workspace(str(tmp_path / "cache"), token=None, max_repo_bytes=10)
        def fake_clone(repo, repo_dir, token, timeout):
            (repo_dir / ".git").mkdir(parents=True)
            (repo_dir / "big.bin").write_text("x" * 100)   # 100 bytes > cap 10
            return "sha"
        monkeypatch.setattr(gitops, "clone_or_pull", fake_clone)
        with pytest.raises(CodeProblem) as e:
            ws.sync("me/huge")
        assert e.value.status_code == 413
        assert not ws._repo_dir("me/huge").exists()        # removed, not left on disk

    def test_read_file_is_bounded(self, tmp_path):
        # /file reports the TRUE size but reads at most cap+1 (no full materialization).
        ws = _mk(tmp_path)  # max_file_bytes=20
        _fake_checkout(ws, "me/repo", {"big.txt": "y" * 1000})
        r = ws.read_file("me/repo", "big.txt")
        assert r["bytes"] == 1000 and r["truncated"] is True and len(r["content"]) == 20

    def test_enforce_cap_never_evicts_excluded(self, tmp_path):
        ws = Workspace(str(tmp_path / "cache"), token=None, max_cache_bytes=1)  # evict all but excluded
        d1 = _fake_checkout(ws, "me/keep", {"a": "x" * 100})
        d2 = _fake_checkout(ws, "me/old", {"b": "y" * 100})
        ws._enforce_cache_cap(exclude=d1)
        assert d1.exists()          # the just-synced repo is never evicted
        assert not d2.exists()      # the other one is


class TestRefresh:
    """Slice E — the nightly warm sweep (discovery + gitops mocked)."""

    @staticmethod
    def _clone_factory(sizes):
        def fake_clone(repo, repo_dir, token, timeout):
            (repo_dir / ".git").mkdir(parents=True)
            (repo_dir / "f.py").write_text(sizes.get(repo, "x"))
            return "sha-" + repo.replace("/", "-")
        return fake_clone

    def test_refresh_discovers_and_syncs_all(self, tmp_path, monkeypatch):
        ws = Workspace(str(tmp_path / "cache"), token="TOK")
        monkeypatch.setattr(gitops, "list_owner_repos", lambda token, timeout=20.0: ["me/a", "me/b"])
        monkeypatch.setattr(gitops, "clone_or_pull", self._clone_factory({"me/a": "aa", "me/b": "bbb"}))
        r = ws.refresh()
        assert r["discovered"] == 2 and r["refreshed"] == 2 and r["errors"] == 0 and r["skipped"] == 0
        assert sorted(r["repos"]) == ["me/a", "me/b"]
        # bytes is the on-disk total (the SAME basis the cache cap measures), so it equals
        # the sum of _dir_bytes over the warmed repos (working tree + .git + LRU marker).
        expected = ws._dir_bytes(ws._repo_dir("me/a")) + ws._dir_bytes(ws._repo_dir("me/b"))
        assert r["bytes"] == expected and expected >= 5

    def test_refresh_bounded_by_max_repos(self, tmp_path, monkeypatch):
        ws = Workspace(str(tmp_path / "cache"), token="TOK", max_refresh_repos=1)
        monkeypatch.setattr(gitops, "list_owner_repos", lambda token, timeout=20.0: ["me/a", "me/b", "me/c"])
        monkeypatch.setattr(gitops, "clone_or_pull", self._clone_factory({}))
        r = ws.refresh()
        assert r["refreshed"] == 1 and r["discovered"] == 3 and r["skipped"] == 2 and r["errors"] == 0

    def test_refresh_explicit_limit_is_clamped_to_server_cap(self, tmp_path, monkeypatch):
        ws = Workspace(str(tmp_path / "cache"), token="TOK", max_refresh_repos=5)
        monkeypatch.setattr(gitops, "list_owner_repos", lambda token, timeout=20.0: ["me/a", "me/b", "me/c"])
        monkeypatch.setattr(gitops, "clone_or_pull", self._clone_factory({}))
        r = ws.refresh(max_repos=2)                 # caller asks 2, under the cap of 5
        assert r["refreshed"] == 2 and r["skipped"] == 1

    def test_refresh_fail_soft_on_one_bad_repo(self, tmp_path, monkeypatch):
        # An oversized repo is counted in errors; the sweep still warms the others.
        ws = Workspace(str(tmp_path / "cache"), token="TOK", max_repo_bytes=10)
        monkeypatch.setattr(gitops, "list_owner_repos", lambda token, timeout=20.0: ["me/ok", "me/huge", "me/ok2"])
        def fake_clone(repo, repo_dir, token, timeout):
            (repo_dir / ".git").mkdir(parents=True)
            (repo_dir / "f").write_text("x" * (100 if repo == "me/huge" else 3))
            return "sha"
        monkeypatch.setattr(gitops, "clone_or_pull", fake_clone)
        r = ws.refresh()
        assert r["errors"] == 1 and r["refreshed"] == 2 and "me/huge" not in r["repos"]

    def test_refresh_validates_discovered_names(self, tmp_path, monkeypatch):
        # A hostile GitHub-API payload name must be rejected (counted), never cloned.
        ws = Workspace(str(tmp_path / "cache"), token="TOK")
        monkeypatch.setattr(gitops, "list_owner_repos", lambda token, timeout=20.0: ["../evil", "me/good"])
        monkeypatch.setattr(gitops, "clone_or_pull", self._clone_factory({}))
        r = ws.refresh()
        # the good repo is warmed; the hostile name is counted as an error, not cloned
        assert r["repos"] == ["me/good"] and r["errors"] == 1 and r["discovered"] == 2

    def test_refresh_budget_stops_the_sweep(self, tmp_path, monkeypatch):
        # A tiny byte budget stops after the first repo pushes warmed_bytes over it.
        ws = Workspace(str(tmp_path / "cache"), token="TOK", refresh_budget_bytes=1)
        monkeypatch.setattr(gitops, "list_owner_repos", lambda token, timeout=20.0: ["me/a", "me/b", "me/c"])
        monkeypatch.setattr(gitops, "clone_or_pull", self._clone_factory({"me/a": "xxxxx"}))
        r = ws.refresh()
        assert r["refreshed"] == 1 and r["skipped"] == 2

    def test_refresh_discovery_failure_raises(self, tmp_path, monkeypatch):
        # A total discovery failure raises (the caller retries) — NOT a silent empty.
        ws = Workspace(str(tmp_path / "cache"), token=None)
        def boom(token, timeout=20.0):
            raise CodeProblem(502, "GitHub unreachable")
        monkeypatch.setattr(gitops, "list_owner_repos", boom)
        with pytest.raises(CodeProblem) as e:
            ws.refresh()
        assert e.value.status_code == 502

    def test_refresh_never_evicts_a_repo_it_just_warmed(self, tmp_path, monkeypatch):
        # Invariant #3: even when the warmed set exceeds the cache cap, the end-of-sweep
        # cache-cap enforcement evicts OLD (prior-day) repos, never one warmed this pass.
        ws = Workspace(str(tmp_path / "cache"), token="TOK",
                       max_cache_bytes=250, refresh_budget_bytes=10_000)  # cap far below what we warm
        old = _fake_checkout(ws, "me/old", {"a": "x" * 200})   # a stale prior-day repo
        (old / ".careeragent_used").write_text("1")            # epoch 1 → forced oldest (LRU victim)
        monkeypatch.setattr(gitops, "list_owner_repos", lambda token, timeout=20.0: ["me/a", "me/b"])
        monkeypatch.setattr(gitops, "clone_or_pull", self._clone_factory({"me/a": "y" * 90, "me/b": "z" * 90}))
        r = ws.refresh()
        assert set(r["repos"]) == {"me/a", "me/b"}
        # both freshly-warmed repos survive; the old one is the only eviction candidate
        assert ws._repo_dir("me/a").exists() and ws._repo_dir("me/b").exists()
        assert not old.exists()          # the stale repo was evicted to make room, not a warmed one

    def test_default_budget_leaves_headroom_for_one_repo(self, tmp_path):
        # The default budget = cache cap − per-repo ceiling (clamped to the cap), so the
        # check-before-warm overshoot of one last repo still fits under the cap.
        ws = Workspace(str(tmp_path / "cache"), token="TOK",
                       max_cache_bytes=2_000, max_repo_bytes=500)
        assert ws.refresh_budget_bytes == 1_500
        ws2 = Workspace(str(tmp_path / "c2"), token="TOK",
                        max_cache_bytes=1_000, refresh_budget_bytes=9_999)  # explicit over-cap → clamped
        assert ws2.refresh_budget_bytes == 1_000

    def test_sync_still_returns_same_shape_after_refactor(self, tmp_path, monkeypatch):
        # Regression guard: extracting _sync_one must leave sync()'s result identical.
        ws = Workspace(str(tmp_path / "cache"), token="TOK")
        monkeypatch.setattr(gitops, "clone_or_pull", self._clone_factory({"me/x": "hey"}))
        r = ws.sync("me/x")
        assert set(r) == {"repo", "head_sha", "files", "bytes", "cached"}
        assert r["repo"] == "me/x" and r["bytes"] == 3 and r["cached"] is False


class TestReadValidation:
    @pytest.mark.parametrize("bad", ["no-slash", "../evil", "a/../../x"])
    def test_read_paths_reject_bad_repo(self, tmp_path, bad):
        ws = _mk(tmp_path)
        for op in (lambda: ws.read_file(bad, "a.py"),
                   lambda: ws.tree(bad),
                   lambda: ws.grep(bad, "x", None)):
            with pytest.raises(CodeProblem) as e:
                op()
            assert e.value.status_code == 400
