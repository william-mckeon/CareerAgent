"""tests/test_gitops.py — clone/pull dispatch + token scrubbing (git mocked)."""
from types import SimpleNamespace

import pytest

import gitops
from safety import CodeProblem


def _ok(stdout="abc123\n"):
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


class TestCloneUrl:
    def test_is_always_tokenless(self):
        # The token rides an env header, NOT the URL — so nothing secret is persisted.
        assert gitops._clone_url("me/repo") == "https://github.com/me/repo.git"


class TestAuthEnv:
    def test_token_becomes_an_env_header_not_url(self):
        import base64
        env = gitops._git_env("TOK")
        b64 = base64.b64encode(b"x-access-token:TOK").decode()
        assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {b64}"
        assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
        assert env["GIT_TERMINAL_PROMPT"] == "0" and env["HOME"] == "/tmp"

    def test_no_token_no_auth_config(self):
        env = gitops._git_env(None)
        assert "GIT_CONFIG_COUNT" not in env and "GIT_CONFIG_VALUE_0" not in env


class TestScrub:
    def test_removes_credentials(self):
        s = "fatal: could not read from https://x-access-token:ghp_SECRET@github.com/me/repo.git"
        out = gitops._scrub(s)
        assert "ghp_SECRET" not in out and "***@github.com" in out


class TestCloneOrPull:
    def test_clones_when_absent(self, tmp_path, monkeypatch):
        calls = []
        def fake_run(argv, **kw):
            calls.append(argv)
            return _ok("deadbeef\n")
        monkeypatch.setattr(gitops.subprocess, "run", fake_run)
        repo_dir = tmp_path / "me" / "repo"          # does NOT exist yet
        head = gitops.clone_or_pull("me/repo", repo_dir, "TOK", timeout=30)
        assert head == "deadbeef"
        # first git call is a clone (contains 'clone'), and it's hardened + shallow
        clone = next(a for a in calls if "clone" in a)
        assert "--depth" in clone and "1" in clone
        assert "core.hooksPath=/dev/null" in clone and "core.symlinks=false" in clone
        # tokenless URL — the PAT is NEVER in argv/URL (it rides an env header)
        assert any("https://github.com/me/repo.git" in x for x in clone)
        assert not any("TOK" in x for x in clone)

    def test_pulls_when_present(self, tmp_path, monkeypatch):
        repo_dir = tmp_path / "me" / "repo"
        (repo_dir / ".git").mkdir(parents=True)     # already a checkout
        argvs = []
        monkeypatch.setattr(gitops.subprocess, "run",
                            lambda argv, **kw: argvs.append(argv) or _ok("newsha\n"))
        head = gitops.clone_or_pull("me/repo", repo_dir, "TOK", timeout=30)
        assert head == "newsha"
        assert any("fetch" in a for a in argvs) and any("reset" in a for a in argvs)
        assert not any("clone" in a for a in argvs)

    def test_nonzero_raises_scrubbed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gitops.subprocess, "run", lambda argv, **kw: SimpleNamespace(
            returncode=128, stdout="",
            stderr="remote: Invalid https://x-access-token:ghp_LEAK@github.com/me/repo.git"))
        with pytest.raises(CodeProblem) as e:
            gitops.clone_or_pull("me/repo", tmp_path / "me" / "repo", "TOK", timeout=30)
        assert e.value.status_code == 502
        assert "ghp_LEAK" not in e.value.detail        # scrubbed out of the error too

    def test_timeout_is_504(self, tmp_path, monkeypatch):
        import subprocess as sp
        def boom(argv, **kw):
            raise sp.TimeoutExpired(cmd="git", timeout=1)
        monkeypatch.setattr(gitops.subprocess, "run", boom)
        with pytest.raises(CodeProblem) as e:
            gitops.clone_or_pull("me/repo", tmp_path / "me" / "repo", None, timeout=1)
        assert e.value.status_code == 504
