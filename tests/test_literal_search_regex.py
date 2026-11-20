"""Focused tests for bounded regex scanning in tools.literal_search."""
from __future__ import annotations

from tools.literal_search import scan_literal


def _write(tmp_path, name: str, text: str):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
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
