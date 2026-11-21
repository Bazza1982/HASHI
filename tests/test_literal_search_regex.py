"""Focused tests for bounded regex scanning in tools.literal_search."""
from __future__ import annotations

from tools.literal_search import scan_literal


def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def test_regex_match_spanning_two_and_three_chunks(tmp_path):
    # A 50-char span across a 16-char read chunk must not be lost.
    f = _write(tmp_path, "two.txt", "A" + "x" * 48 + "B\n")
    r = scan_literal(f, terms=("A.*B",), case_sensitive=True, max_results=10,
                     context_chars=8, chunk_chars=16, regex=True)
    assert [m["term"] for m in r["matches"]] == ["A.*B"]
    assert r["matches"][0]["character_offset"] == 0
    assert r["coverage_complete"] is True
    assert r["coverage_warning"] is None

    # A span across three 32-char chunks.
    f2 = _write(tmp_path, "three.txt", "X" + "y" * 100 + "Z\n")
    r2 = scan_literal(f2, terms=("X.*Z",), case_sensitive=True, max_results=10,
                      context_chars=8, chunk_chars=32, regex=True)
    assert len(r2["matches"]) == 1
    assert r2["coverage_complete"] is True


def test_regex_anchors_and_unicode_cjk(tmp_path):
    f = _write(tmp_path, "cjk.txt", "第一行\n第二行世界\nplain\n")
    r = scan_literal(f, terms=("^第二行.*世界$",), case_sensitive=True,
                     max_results=10, context_chars=4, chunk_chars=8, regex=True)
    assert len(r["matches"]) == 1
    assert r["matches"][0]["character_offset"] == len("第一行\n")
    assert r["matches"][0]["excerpt"] == "第二行世界"


def test_regex_zero_length_pattern_is_bounded(tmp_path):
    f = _write(tmp_path, "z.txt", "abc\ndef\n")
    r = scan_literal(f, terms=("x*",), case_sensitive=True, max_results=5,
                     context_chars=2, chunk_chars=4, regex=True)
    assert len(r["matches"]) == 5
    assert r["result_limit_reached"] is True
    assert r["coverage_complete"] is False


def test_regex_continuation_no_duplicates_or_missing(tmp_path):
    f = _write(tmp_path, "cont.txt", "target a\ntarget b\ntarget c\n")
    state = None
    found = []
    for _ in range(10):
        r = scan_literal(f, terms=("target",), case_sensitive=True, max_results=1,
                         context_chars=4, chunk_chars=8, regex=True, state=state)
        found.extend(m["character_offset"] for m in r["matches"])
        if r["coverage_complete"]:
            state = None
            break
        state = r["continuation"]
    assert found == [0, 9, 18]  # no duplicates, no missing results
    assert state is None  # reached completion with no dangling continuation


def test_regex_overlong_record_reports_incomplete(tmp_path):
    # One record longer than the bounded scan limit, then a normal record.
    f = _write(tmp_path, "big.txt", "x" * 1_100_000 + "\nneedle here\n")
    r = scan_literal(f, terms=("needle",), case_sensitive=True, max_results=10,
                     context_chars=8, chunk_chars=64 * 1024, regex=True)
    assert r["coverage_complete"] is False
    assert r["coverage_warning"]
    assert "bounded scan limit" in r["coverage_warning"]


def test_regex_nested_quantifier_is_refused_honestly(tmp_path):
    f = _write(tmp_path, "cat.txt", "aaaaab\n")
    r = scan_literal(f, terms=("(a+)+b",), case_sensitive=True, max_results=10,
                     context_chars=4, chunk_chars=16, regex=True)
    assert r["matches"] == []
    assert r["coverage_complete"] is False
    assert "nested_quantifiers" in (r["coverage_warning"] or "")


def test_repeated_overlapping_alternatives_are_bounded_without_blocking_safe_regex(tmp_path):
    path = _write(tmp_path, "alternatives.txt", "a" * 38 + "!\ncatdog\n")
    for pattern in ("(a|aa)+$", "(?:a|aa)*$", "(a|aa){2,}$"):
        result = scan_literal(path, terms=(pattern,), case_sensitive=True, max_results=10,
                              context_chars=4, chunk_chars=16, regex=True)
        assert result["matches"] == []
        assert result["coverage_complete"] is False
        assert result["continuation"] is None
        assert "overlapping_repeated_alternation" in result["coverage_warning"]

    insensitive = scan_literal(path, terms=("(a|A)+$",), case_sensitive=False,
                               max_results=10, context_chars=4, regex=True)
    assert not insensitive["coverage_complete"]
    assert "overlapping_repeated_alternation" in insensitive["coverage_warning"]

    for pattern in ("(?:cat|dog)+", "(a|aa){2}", r"[(a|aa)+]"):
        result = scan_literal(path, terms=(pattern,), case_sensitive=True, max_results=100,
                              context_chars=4, chunk_chars=16, regex=True)
        assert result["matches"]
        assert result["coverage_complete"] is True

    literal = _write(tmp_path, "literal.txt", "(a|aa)+")
    result = scan_literal(literal, terms=(r"\(a\|aa\)\+",), case_sensitive=True,
                          max_results=10, context_chars=4, regex=True)
    assert result["matches"] and result["coverage_complete"]


def test_regex_character_budget_is_partial_and_resumable(tmp_path):
    f = _write(tmp_path, "budget.txt", "line one\ntarget\nline three\ntarget\n")
    r1 = scan_literal(f, terms=("target",), case_sensitive=True, max_results=10,
                      context_chars=4, chunk_chars=16, regex=True,
                      character_budget=12)
    # Budget stops the first call short of EOF.
    assert r1["coverage_complete"] is False
    assert r1["continuation"] is not None

    found = [m["term"] for m in r1["matches"]]
    state = r1["continuation"]
    # Resume until the scan reports completion.
    for _ in range(20):
        rn = scan_literal(f, terms=("target",), case_sensitive=True,
                          max_results=10, context_chars=4, chunk_chars=16,
                          regex=True, state=state)
        found.extend(m["term"] for m in rn["matches"])
        if rn["coverage_complete"]:
            break
        state = rn["continuation"]
    assert sorted(found) == ["target", "target"]


def _all_regex_pages(path, terms, *, chunk_chars=16):
    state = None
    matches = []
    for _ in range(100):
        result = scan_literal(path, terms=terms, case_sensitive=True,
                              max_results=1, context_chars=4, chunk_chars=chunk_chars,
                              regex=True, state=state)
        matches.extend(result["matches"])
        state = result["continuation"]
        if state is None:
            return matches, result
    raise AssertionError("regex continuation did not terminate")


def test_regex_pagination_retains_line_anchors_and_order(tmp_path):
    anchored = _write(tmp_path, "anchored.txt", "aaa\n")
    matches, result = _all_regex_pages(anchored, ("^a",))
    assert [item["character_offset"] for item in matches] == [0]
    assert result["coverage_complete"] is True

    ordered = _write(tmp_path, "ordered.txt", "a z a z\n")
    matches, result = _all_regex_pages(ordered, ("z", "a"))
    assert [(item["term"], item["character_offset"]) for item in matches] == [
        ("a", 0), ("z", 2), ("a", 4), ("z", 6),
    ]
    assert result["coverage_complete"] is True

    overlapping, _ = _all_regex_pages(anchored, ("a", "^a"))
    assert [(m["term"], m["character_offset"]) for m in overlapping] == [
        ("a", 0), ("^a", 0), ("a", 1), ("a", 2),
    ]
    empty_and_nonempty, _ = _all_regex_pages(anchored, (r"(?=a)|a",))
    assert [m["character_offset"] for m in empty_and_nonempty] == [0, 0, 1, 1, 2, 2]


def test_regex_page_lookahead_preserves_the_next_unicode_record(tmp_path):
    path = _write(tmp_path, "lookahead.txt", "aaa\n阿a\n")
    first = scan_literal(path, terms=("^a|^阿",), case_sensitive=True,
                         max_results=1, context_chars=4, chunk_chars=4, regex=True)
    assert [item["character_offset"] for item in first["matches"]] == [0]
    assert first["continuation"] is not None
    last = scan_literal(path, terms=("^a|^阿",), case_sensitive=True,
                        max_results=1, context_chars=4, chunk_chars=4, regex=True,
                        state=first["continuation"])
    assert [item["character_offset"] for item in last["matches"]] == [4]
    assert last["coverage_complete"] is True and last["continuation"] is None


def test_regex_crlf_uses_logical_line_and_physical_offsets(tmp_path):
    path = tmp_path / "windows.txt"
    path.write_bytes("中文\r\nfoo\r\nfoo\r\n".encode("utf-8"))
    matches, result = _all_regex_pages(path, ("^foo$",), chunk_chars=3)
    assert [(m["line"], m["character_offset"]) for m in matches] == [(2, 4), (3, 9)]
    assert result["coverage_complete"] is True


def test_overlong_regex_record_streams_and_exhausts_without_cursor(tmp_path, monkeypatch):
    import tools.literal_search as literal_search

    monkeypatch.setattr(literal_search, "MAX_REGEX_RECORD_CHARS", 64)
    for suffix in ("", "\nneedle\n"):
        path = _write(tmp_path, "overlong.txt", "x" * 1024 + suffix)
        state = None
        seen = []
        for _ in range(100):
            result = scan_literal(path, terms=("^needle$",), case_sensitive=True,
                                  max_results=10, context_chars=4, chunk_chars=16,
                                  regex=True, state=state, character_budget=32)
            assert result["characters_read"] <= 48
            seen.extend(result["matches"])
            state = result["continuation"]
            if state is None:
                break
            assert len(state["buffer"]) <= 80
        else:
            raise AssertionError("overlong record continuation did not terminate")
        assert result["coverage_complete"] is False
        assert "bounded scan limit" in result["coverage_warning"]
        assert [m["character_offset"] for m in seen] == ([1025] if suffix else [])


def test_overlong_regex_at_eof_is_partial_without_continuation(tmp_path, monkeypatch):
    import tools.literal_search as literal_search

    monkeypatch.setattr(literal_search, "MAX_REGEX_RECORD_CHARS", 64)
    path = _write(tmp_path, "overlong-eof.txt", "x" * 1024)
    for _ in range(2):
        result = scan_literal(path, terms=("needle",), case_sensitive=True,
                              max_results=10, context_chars=4, chunk_chars=16, regex=True)
        assert result["matches"] == []
        assert result["coverage_complete"] is False
        assert result["continuation"] is None
        assert "bounded scan limit" in result["coverage_warning"]


def test_regex_cancellation_interrupts_overlong_record(tmp_path, monkeypatch):
    import pytest
    import tools.literal_search as literal_search

    monkeypatch.setattr(literal_search, "MAX_REGEX_RECORD_CHARS", 64)
    path = _write(tmp_path, "cancel.txt", "x" * 1024)

    def cancel(characters):
        if characters >= 128:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        scan_literal(path, terms=("needle",), case_sensitive=True, max_results=10,
                     context_chars=4, chunk_chars=16, regex=True, progress=cancel)
# HASHI-Fingerprint: hfp_71235e0861344af4a3ecc2ad0a53366e
