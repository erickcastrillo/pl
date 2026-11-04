import pathlib
import tomllib

import pytest

from pl import board
from pl.trackers import metablock, vikunjahtml

SAMPLE = """# PIPELINE: INPUT

## Where this came from

- one with `code`
- two

```python
print("x")
```

# PIPELINE: SPEC

Plain *text* and **bold**.
"""
META = {"approved_at": "2026-10-28", "n": 1}


def _visible(html):
    return html[:html.index("<!-- pl:card")]


def test_encode_renders_html_without_markdown_headings():
    out = vikunjahtml.encode(SAMPLE, META)
    for tag in ("<h1>", "<h2>", "<ul>", "<li>", "<code>", "<pre>"):
        assert tag in out
    assert not [ln for ln in out.splitlines() if ln.startswith(("# ", "## "))]


@pytest.mark.parametrize("text", [SAMPLE, "", "a -- b --> c <script>x</script>", "_emph_ and\n\n* bullets\n", "ñandú ✓\n", "no newline"])
def test_encode_decode_round_trip_is_exact(text):
    assert vikunjahtml.decode(vikunjahtml.encode(text, META)) == (text, META)


def test_raw_html_in_card_text_is_escaped():
    out = vikunjahtml.encode("<script>alert(1)</script>", {})
    assert "<script>" not in out and "&lt;script&gt;" in _visible(out)


def test_metadata_lives_in_hidden_comment():
    out = vikunjahtml.encode("hi", {"secret_key": "v"})
    assert "secret_key" not in _visible(out)
    assert vikunjahtml.decode(out)[1] == {"secret_key": "v"}


def test_editor_html_reads_as_sections():
    html = "<h1>PIPELINE: INPUT</h1><p>one</p><p>two</p><ul><li><p>a</p></li><li><p>b</p></li></ul>"
    text, meta = vikunjahtml.decode(html)
    assert text == "# PIPELINE: INPUT\n\none\n\ntwo\n\n- a\n- b"
    assert meta == {}
    assert board.sections(text)["INPUT"] == "one\n\ntwo\n\n- a\n- b"


@pytest.mark.parametrize("html, md", [
    ("<ol><li>a<ul><li>sub</li></ul></li><li>b</li></ol>", "1. a\n   - sub\n2. b"),
    ("<p>use <code>x</code> now</p>", "use `x` now"),
    ('<pre><code class="language-py">a = 1\nb = 2\n</code></pre>', "```py\na = 1\nb = 2\n```"),
    ("<p><strong>b</strong> <b>c</b> <em>i</em> <i>j</i></p>", "**b** **c** *i* *j*"),
    ('<p><a href="http://x.y/z">link</a></p>', "[link](http://x.y/z)"),
    ("<p>one<br>two</p>", "one  \ntwo"),
    ("<p>a &amp; b &lt; c&nbsp;d</p>", "a & b < c d"),
    ("<p>a <span>kept</span></p>", "a kept"),
    ("<blockquote><p>q</p></blockquote>", "> q"),
])
def test_html_to_md_rich_forms(html, md):
    assert vikunjahtml.html_to_md(html) == md


def test_edited_visible_html_falls_back_to_conversion():
    out = vikunjahtml.encode("# T\n\nhello\n", META)
    edited = out.replace("hello", "hellp")
    assert vikunjahtml.decode(edited) == ("# T\n\nhellp", META)


def test_plain_markdown_is_unchanged():
    assert vikunjahtml.decode(metablock.join("body", {"a": 1})) == ("body", {"a": 1})
    assert vikunjahtml.decode("just *text*\n") == ("just *text*\n", {})
    assert vikunjahtml.decode(None) == ("", {})


def test_bad_comment_payload_is_ignored():
    text, meta = vikunjahtml.decode("<p>x</p><!-- pl:card not-base64 -->")
    assert text.startswith("x") and meta == {}


def test_markdown_it_comes_from_existing_deps():
    import markdown_it  # noqa: F401
    deps = tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["dependencies"]
    assert not [d for d in deps if "markdown-it" in d]
