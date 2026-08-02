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


class TestListOwnerRepos:
    """Slice E — owner-repo discovery via the GitHub REST API (urllib mocked)."""

    class _Resp:                       # a context-manager stand-in for urlopen()
        def __init__(self, body): self._body = body
        def read(self): return self._body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def test_no_token_raises_400(self):
        with pytest.raises(CodeProblem) as e:
            gitops.list_owner_repos(None)
        assert e.value.status_code == 400

    def test_returns_full_names_and_sends_bearer(self, monkeypatch):
        import json
        captured = {}
        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.headers.get("Authorization")
            captured["url"] = req.full_url
            return self._Resp(json.dumps([{"full_name": "me/a"}, {"full_name": "me/b"}]).encode())
        monkeypatch.setattr(gitops.urllib.request, "urlopen", fake_urlopen)
        repos = gitops.list_owner_repos("TOK", per_page=100)
        assert repos == ["me/a", "me/b"]
        assert captured["auth"] == "Bearer TOK"          # PAT rides a header
        assert "affiliation=owner" in captured["url"] and "sort=pushed" in captured["url"]

    def test_paginates_until_short_page(self, monkeypatch):
        import json, re
        pages = {1: json.dumps([{"full_name": "me/a"}, {"full_name": "me/b"}]).encode(),
                 2: json.dumps([{"full_name": "me/c"}]).encode()}     # short → stop
        seen = []
        def fake_urlopen(req, timeout=None):
            pg = int(re.search(r"[?&]page=(\d+)", req.full_url).group(1))  # not per_page
            seen.append(pg)
            return self._Resp(pages[pg])
        monkeypatch.setattr(gitops.urllib.request, "urlopen", fake_urlopen)
        repos = gitops.list_owner_repos("TOK", per_page=2)
        assert repos == ["me/a", "me/b", "me/c"] and seen == [1, 2]

    def test_http_error_raises_502_without_leaking_token(self, monkeypatch):
        import urllib.error
        def boom(req, timeout=None):
            raise urllib.error.HTTPError(url="x", code=401, msg="Unauthorized", hdrs=None, fp=None)
        monkeypatch.setattr(gitops.urllib.request, "urlopen", boom)
        with pytest.raises(CodeProblem) as e:
            gitops.list_owner_repos("TOK")
        assert e.value.status_code == 502 and "TOK" not in e.value.detail

    def test_malformed_json_raises_502(self, monkeypatch):
        monkeypatch.setattr(gitops.urllib.request, "urlopen",
                            lambda req, timeout=None: self._Resp(b"not json"))
        with pytest.raises(CodeProblem) as e:
            gitops.list_owner_repos("TOK")
        assert e.value.status_code == 502
