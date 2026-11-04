"""Vikunja descriptions are HTML; pl's card text is Markdown. The exact Markdown rides in a hidden trailing comment."""
import base64
import hashlib
import json
import re
from html.parser import HTMLParser

from markdown_it import MarkdownIt

from pl.trackers import metablock

MARK = "pl:card"
COMMENT_RE = re.compile(rf"<!-- {MARK} (\S+) -->\s*\Z")
HTML_START_RE = re.compile(r"\s*<(p|h[1-6]|ul|ol|pre|blockquote|div)[\s>/]", re.I)
_md = MarkdownIt("commonmark", {"html": False})


def _sha(html):
    return hashlib.sha256(html.encode()).hexdigest()


def encode(text, meta):
    text = text or ""
    html = _md.render(text)
    payload = {"text": text, "metadata": meta or {}, "sha": _sha(html)}
    b64 = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode("ascii")
    return f"{html}<!-- {MARK} {b64} -->\n"


def _payload(b64):
    try:
        data = json.loads(base64.urlsafe_b64decode(b64))
    except ValueError:
        return None
    ok = isinstance(data, dict) and isinstance(data.get("text"), str) and isinstance(data.get("metadata"), dict)
    return data if ok else None


def decode(desc):
    desc = desc or ""
    if (m := COMMENT_RE.search(desc)) and (data := _payload(m.group(1))):
        visible = desc[:m.start()]
        if _sha(visible) == data.get("sha"):
            return data["text"], data["metadata"]
        return html_to_md(visible), data["metadata"]
    if HTML_START_RE.match(desc):
        return html_to_md(desc), {}
    return metablock.split(desc)[:2]


class _ToMarkdown(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.lists = []
        self.in_pre = False
        self.fence = 0
        self.links = []
        self.quotes = []

    def _text(self):
        return "".join(self.out)

    def _break(self):
        if self.out:
            self.out.append("\n\n")

    def _in_item(self):
        return bool(self.lists)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if re.fullmatch(r"h[1-6]", tag):
            self._break()
            self.out.append("#" * int(tag[1]) + " ")
        elif tag == "p" and not self._in_item():
            self._break()
        elif tag in ("ul", "ol"):
            self.out.append("\n") if self.lists else self._break()
            self.lists.append([tag, 0])
        elif tag == "li" and self.lists:
            if not self._text().endswith("\n"):
                self.out.append("\n")
            kind, _ = self.lists[-1]
            self.lists[-1][1] += 1
            indent = "".join("   " if k == "ol" else "  " for k, _ in self.lists[:-1])
            self.out.append(indent + ("- " if kind == "ul" else f"{self.lists[-1][1]}. "))
        elif tag == "pre":
            self._break()
            self.in_pre = True
            self.out.append("```")
            self.fence = len(self.out) - 1
            self.out.append("\n")
        elif tag == "code":
            if self.in_pre:
                lang = next((c[9:] for c in (attrs.get("class") or "").split() if c.startswith("language-")), "")
                self.out[self.fence] = "```" + lang
            else:
                self.out.append("`")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "a":
            self.links.append(attrs.get("href"))
            self.out.append("[" if attrs.get("href") else "")
        elif tag == "br":
            self.out.append("  \n")
        elif tag == "blockquote":
            self._break()
            self.quotes.append(len(self.out))

    def handle_endtag(self, tag):
        if re.fullmatch(r"h[1-6]", tag) or (tag == "p" and not self._in_item()):
            self._break()
        elif tag in ("ul", "ol") and self.lists:
            self.lists.pop()
            if not self.lists:
                self._break()
        elif tag == "pre":
            while self.out and not self.out[-1].strip("\n"):
                self.out.pop()
            self.out[-1] = self.out[-1].rstrip("\n")
            self.out.append("\n```")
            self.in_pre = False
            self._break()
        elif tag == "code" and not self.in_pre:
            self.out.append("`")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "a" and self.links:
            if href := self.links.pop():
                self.out.append(f"]({href})")
        elif tag == "blockquote" and self.quotes:
            start = self.quotes.pop()
            quoted = "".join(self.out[start:]).strip("\n")
            self.out[start:] = ["\n".join("> " + ln if ln else ">" for ln in quoted.split("\n")), "\n\n"]

    def handle_data(self, data):
        if self.in_pre:
            self.out.append(data)
            return
        data = re.sub(r"\s+", " ", data)
        if data == " " and (not self.out or self._text().endswith("\n")):
            return
        self.out.append(data)


def html_to_md(html):
    parser = _ToMarkdown()
    parser.feed(html or "")
    parser.close()
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.out)).strip()
