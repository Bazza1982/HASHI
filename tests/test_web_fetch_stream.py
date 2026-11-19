from __future__ import annotations

import io
import re

import pytest

from tools.web_fetch_stream import _MarkdownHTML, _PlainHTML, _replace_placeholder


@pytest.mark.parametrize("width", [1, 3, 17, 65536])
def test_streamed_plain_html_preserves_normalized_text_and_entities(width):
    html = ("  <html>\n\n\n<body><p> One &amp; &#x4e2d; two </p>"
            "\n\n\n<p>Second <b>paragraph</b></p>   </body></html>  ")
    expected = re.sub(r"<[^>]+>", "", html)
    expected = re.sub(r"\n{3,}", "\n\n", expected).strip()
    stream = io.BytesIO()
    converter = _PlainHTML(stream)
    for offset in range(0, len(html), width):
        converter.feed(html[offset:offset + width])
    converter.finish()
    assert stream.read().decode("utf-8") == expected


def test_placeholder_replacement_handles_chunk_boundaries():
    pieces = ["first &n", "bsp_place_", "holder;next ", "&nbsp_place_holder", ";last"]
    assert "".join(_replace_placeholder(iter(pieces))) == (
        "".join(pieces).replace("&nbsp_place_holder;", " ")
    )


@pytest.mark.parametrize("width", [1, 7, 65536])
def test_streamed_optional_markdown_matches_existing_converter(width):
    html2text = pytest.importorskip("html2text")
    html = ("<h1>Title 海</h1><p>See <a href='https://example.invalid/path'>link</a>"
            " &nbsp; <strong>bold</strong>.</p><ul><li>First</li><li>Second</li></ul>")
    legacy = html2text.HTML2Text()
    legacy.ignore_links = False
    legacy.ignore_images = True
    legacy.body_width = 0
    expected = legacy.handle(html)
    stream = io.BytesIO()
    converter = _MarkdownHTML(stream, html2text)
    for offset in range(0, len(html), width):
        converter.feed(html[offset:offset + width])
    converter.finish()
    actual = "".join(_replace_placeholder(iter([stream.read().decode("utf-8")])))
    assert actual == expected
# HASHI-Fingerprint: hfp_070f4206069b4085855c7063391776d8



@pytest.mark.parametrize("content", ["word " * 20000, "x" * 100000, "海词 " * 30000],
                         ids=["words", "unbroken", "unicode"])
def test_streamed_optional_markdown_preserves_emphasis_across_large_chunks(content):
    html2text = pytest.importorskip("html2text")
    html = "<p><strong>" + content + "</strong>after</p>"
    legacy = html2text.HTML2Text()
    legacy.ignore_links = False
    legacy.ignore_images = True
    legacy.body_width = 0
    expected = legacy.handle(html)
    stream = io.BytesIO()
    converter = _MarkdownHTML(stream, html2text)
    for offset in range(0, len(html), 4096):
        converter.feed(html[offset:offset + 4096])
    converter.finish()
    actual = "".join(_replace_placeholder(iter([stream.read().decode("utf-8")])))
    assert actual == expected
