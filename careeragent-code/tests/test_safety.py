"""tests/test_safety.py — repo-name validation + path-traversal guard (pure)."""
import pytest

from safety import CodeProblem, require_repo, resolve_in_repo, valid_repo


class TestValidRepo:
    @pytest.mark.parametrize("r", [
        "octocat/Hello-World", "Islander-Intel/Resume-Helper", "a/b", "a.b_c/d-e.f",
    ])
    def test_accepts_good(self, r):
        assert valid_repo(r)

    @pytest.mark.parametrize("r", [
        "", "no-slash", "a/b/c", "../etc/passwd", "/abs/path", "a/..", "../a",
        "-flag/repo", "a/-flag", "a b/c", "a/b c", "https://x/y",
    ])
    def test_rejects_bad(self, r):
        assert not valid_repo(r)

    def test_require_raises_on_bad(self):
        with pytest.raises(CodeProblem) as e:
            require_repo("../evil")
        assert e.value.status_code == 400


class TestResolveInRepo:
    def test_resolves_a_normal_path(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("x")
        p = resolve_in_repo(tmp_path, "src/a.py")
        assert p == (tmp_path / "src" / "a.py").resolve()

    @pytest.mark.parametrize("bad", ["../secret", "../../etc/passwd", "src/../../out"])
    def test_rejects_escape(self, tmp_path, bad):
        with pytest.raises(CodeProblem) as e:
            resolve_in_repo(tmp_path, bad)
        assert e.value.status_code == 400

    def test_absolute_path_is_neutralized_to_repo_relative(self, tmp_path):
        # A leading '/' is stripped → treated as a repo-relative path (safe, stays
        # inside the repo), NOT an escape. Reading it later just 404s if absent.
        p = resolve_in_repo(tmp_path, "/etc/passwd")
        assert p == (tmp_path / "etc" / "passwd").resolve()

    def test_rejects_empty(self, tmp_path):
        with pytest.raises(CodeProblem):
            resolve_in_repo(tmp_path, "   ")

    def test_blocks_dot_git(self, tmp_path):
        with pytest.raises(CodeProblem):
            resolve_in_repo(tmp_path, ".git/config")

    def test_rejects_symlink_escape(self, tmp_path):
        # A symlink inside the repo pointing OUT must not be followed past the base.
        outside = tmp_path.parent / "outside_secret.txt"
        outside.write_text("top secret")
        repo = tmp_path / "repo"
        repo.mkdir()
        link = repo / "link"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported here")
        with pytest.raises(CodeProblem):
            resolve_in_repo(repo, "link")
