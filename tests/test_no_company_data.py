"""WP14: the open-source tree holds no company data.

Each forbidden term is kept only as the sha256 of its lowercase form, so this file names none of them.
Every lowercase word, token and uuid in the scanned files is hashed and compared, including every part
that starts a word inside a longer token (so a term still matches inside a path, an email or a name).
"""
import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BANNED = frozenset({
    "ea0c0f9a00aa522622eae1c6a19077dc7f23e51dcbfcdf6e4b73f8a0927ccaa7",   # a board id
    "1d3df5f990ad0d9661e99172a3bf375195bf4d4cf654e75f51d5e50fdb59b528",   # a board id
    "230df1a007239ac77b15defd9a10f4668c004539db1fd182745e9b461dbf2ecd",   # a user id
    "3106d9d44b21b4eaec16cd39851ab089666de8faa5b656e5b994224fa3dcc2f2",   # a person
    "2417ea41b7bf1b7f48b652a92f3e715090463e8398a8431ace61a00b25fd25af",   # a company
    "68a29165d2fa44efc0bb6e9f8d500babbfa70eba05569a556ada11247e61600c",   # a company
    "2b71394178c18e758492fdec86c53d6472be0629bdbc5f83e0f7b4282ee2b3ea",   # a repo folder
    "3606ea181b1e52bb40c379ba45f0fb27d2daad45a3b0309f3977166da882fed1",   # a PR label
    "8e205f08dd4bcd10ef74dfba03a75dcfd318b58685de43946bca2f36ce1acdd3",   # a skill name
    "4e56b8cb427cce90e94ec52daf33fb76efd10b0843da27d26968f33224254bb7",   # a skill name
    "a5bc116bd005ddf2786692ebf236f217e08052d1741e996de2ede30d1686c9ca",   # a vendor
    "ea36178702258ae28917bf4c0eb0146ae1636ed8d7829ff42a857d119bb32203",   # a repo name
    "ab0c9d88f33f8ccdbc2e998eb6355ab571620c93f5049e8758ed21d83d576cbb",   # a domain
    "35feefd0742fc1de1e3edf8d62d38b230d58bcc4ecd84a6f3514209f03af9184",   # a domain
    "8de5dfc17a7987581ebbeb01e1d34fcba7c9b7714164b885b898e26b1e10c722",   # a GitHub account
    "90e235b60333d864e4d28a1e4c2a6ff4ae410d904a99a62ad70ce1d1c9c5fb45",   # a person's email
    "aca50f0be6f6381cd67a68fe3475bfbe3415fd41d0b6be39574d96b108e12829",   # a person's email
})
# The author's own name may stay on the LICENSE copyright line, and only there.
ALLOWED = {"LICENSE": ("copyright", {"3106d9d44b21b4eaec16cd39851ab089666de8faa5b656e5b994224fa3dcc2f2"})}
# The public repo's address may appear anywhere, as exactly this URL (a .git suffix is fine); the name stays banned elsewhere.
ALLOWED_URLS = frozenset({"704beaf4d238cfbbd5d865b9a41dc439682aa422c753be69c6936ad80cb921f9"})
URL = re.compile(r"https://github\.com/[a-z0-9-]+/pl(?![a-z0-9_/-])")
TOKEN = re.compile(r"[a-z0-9_@.\-]+")
MAX_TERM = 40


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _token_hits(token, banned):
    """Every piece of the token that starts a word (at the start, or after a non-letter) and hashes to a banned term."""
    out = []
    for i in range(len(token)):
        if i and token[i - 1].isalpha():
            continue
        out += [token[i:j] for j in range(i + 1, min(len(token), i + MAX_TERM) + 1) if _sha(token[i:j]) in banned]
    return out


def _text_hits(label, text, banned=BANNED, allow=None, allowed_urls=ALLOWED_URLS):
    out, cache = [], {}
    for n, line in enumerate(text.lower().splitlines(), 1):
        line = URL.sub(lambda m: " " if _sha(m.group(0)) in allowed_urls else m.group(0), line)
        ok = allow[1] if allow and line.startswith(allow[0]) else set()
        for tok in TOKEN.findall(line):
            if tok not in cache:
                cache[tok] = _token_hits(tok, banned)
            out += [f"{label}:{n}: {m!r}" for m in cache[tok] if _sha(m) not in ok]
    return out


def _file_hits(p):
    rel = str(p.relative_to(ROOT))
    return (_text_hits(rel, p.read_text(errors="replace"), allow=ALLOWED.get(rel))
            + [f"{rel}: file name" for _ in _text_hits(rel, p.name)])


def _tree(*parts):
    return [p for part in parts for p in sorted((ROOT / part).rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts]


def test_no_company_data_in_the_package():
    assert [h for p in _tree("src/pl") for h in _file_hits(p)] == []


def test_no_company_data_in_the_docs():
    files = [ROOT / n for n in ("README.md", "INSTALL.md", "AGENTS.md", "CLAUDE.md", "pyproject.toml", "LICENSE")]
    assert [h for p in files + _tree("docs") for h in _file_hits(p)] == []


def test_no_company_data_in_the_tests():
    assert [h for p in _tree("tests") for h in _file_hits(p)] == []


def test_this_file_names_no_term():
    assert _file_hits(Path(__file__).resolve()) == []


def test_matcher_finds_a_term_inside_paths_emails_and_names():
    probe = frozenset({_sha("acme")})
    text = "a ~/.claude-acme2 dir\nmail sam@acme.test\nAcme_apps and Acme\nnot hacme or acmeish-x"
    assert _text_hits("t", text, probe) == ["t:1: 'acme'", "t:2: 'acme'", "t:3: 'acme'", "t:3: 'acme'",
                                            "t:4: 'acme'"]


def test_only_the_exact_repo_url_is_allowed():
    probe, url = frozenset({_sha("acme")}), frozenset({_sha("https://github.com/acme/pl")})
    ok = "clone https://github.com/acme/pl.git or open https://github.com/acme/pl."
    assert _text_hits("t", ok, probe, allowed_urls=url) == []
    bad = ["acme", "acme/pl", "github.com/acme/pl", "http://github.com/acme/pl", "https://github.com/acme/pl-x",
           "https://github.com/acme/other", "https://github.com/acme/pl/acme", "sam@acme.test", "~/.claude-acme"]
    assert [b for b in bad if not _text_hits("t", b, probe, allowed_urls=url)] == []


def test_the_docs_use_the_allowed_repo_url():
    assert len(ALLOWED_URLS) == 1
    for name in ("README.md", "INSTALL.md"):
        text = (ROOT / name).read_text().lower()
        assert any(_sha(m) in ALLOWED_URLS for m in URL.findall(text)), name
