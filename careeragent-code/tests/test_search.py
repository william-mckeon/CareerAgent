"""tests/test_search.py — ripgrep output parsing + bounds (rg mocked)."""
from types import SimpleNamespace

import pytest

import search
from safety import CodeProblem


def _rg(returncode, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_parses_matches(tmp_path, monkeypatch):
    # rg runs with cwd=repo_dir → emits RELATIVE paths (optionally "./"-prefixed).
    out = "./src/a.py:12:def foo():\nREADME.md:3:# Title\n"
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: _rg(0, out))
    r = search.grep(tmp_path, "foo", None, max_matches=50, timeout=5)
    assert not r["truncated"]
    assert r["matches"][0] == {"path": "src/a.py", "line": 12, "text": "def foo():"}
    assert r["matches"][1]["path"] == "README.md"


def test_preserves_leading_dot_in_dotfiles(tmp_path, monkeypatch):
    # Only a "./" prefix is stripped — a real dotfile like .github keeps its dot.
    monkeypatch.setattr(search.subprocess, "run",
                        lambda *a, **k: _rg(0, ".github/workflows/ci.yml:1:name: CI\n"))
    r = search.grep(tmp_path, "CI", None, max_matches=50, timeout=5)
    assert r["matches"][0]["path"] == ".github/workflows/ci.yml"


def test_no_matches_exit1_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: _rg(1, ""))
    r = search.grep(tmp_path, "nothing", None, max_matches=50, timeout=5)
    assert r["matches"] == [] and r["truncated"] is False


def test_rg_error_exit2_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: _rg(2, "", "bad regex"))
    with pytest.raises(CodeProblem) as e:
        search.grep(tmp_path, "(", None, max_matches=50, timeout=5)
    assert e.value.status_code == 502


def test_truncates_at_max(tmp_path, monkeypatch):
    out = "".join(f"f{i}.py:1:hit\n" for i in range(10))
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: _rg(0, out))
    r = search.grep(tmp_path, "hit", None, max_matches=3, timeout=5)
    assert len(r["matches"]) == 3 and r["truncated"] is True


def test_empty_pattern_is_400(tmp_path):
    with pytest.raises(CodeProblem) as e:
        search.grep(tmp_path, "   ", None, max_matches=50, timeout=5)
    assert e.value.status_code == 400


def test_pattern_passed_as_dash_e(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(search.subprocess, "run",
                        lambda argv, **k: seen.update(argv=argv) or _rg(1, ""))
    search.grep(tmp_path, "--foo", None, max_matches=5, timeout=5)   # a flag-looking pattern
    argv = seen["argv"]
    assert "-e" in argv and argv[argv.index("-e") + 1] == "--foo"     # not read as a flag
    assert "timeout" not in argv
    # .git and the LRU marker are always excluded (--hidden would otherwise hit .git)
    assert "!.git" in argv and "!.careeragent_used" in argv
