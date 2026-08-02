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
