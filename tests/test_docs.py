"""
Keeps the user guides in step with the product.

- every relative link (and #anchor) in README.md, docs/*.md and docs/features/*.md resolves;
- every feature plugin (software/portal/features/*.py, not starting with "_") is in docs/USAGE.md;
- every top-level table of os/prepper.toml.example is named in docs/SETUP.md as `[name]`;
- every top-level front-panel menu entry (software/panel/panel.py) is in docs/TOUR.md or docs/USAGE.md;
- docs/QUICKSTART.md covers the first-boot security checklist.

Standard library only. Run: python3.11 tests/test_docs.py
"""
from __future__ import annotations

import ast
import re
import tomllib
import unicodedata
import unittest
from pathlib import Path
from urllib.parse import unquote

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"


def doc_files() -> list[Path]:
    return [REPO / "README.md", *sorted(DOCS.glob("*.md")), *sorted((DOCS / "features").glob("*.md"))]


# ── Markdown helpers ─────────────────────────────────────────────────────────────────────────────

FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def prose_lines(text: str):
    """(line number, line) outside fenced code blocks."""
    fence = None
    for n, line in enumerate(text.splitlines(), 1):
        m = FENCE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)
            elif m.group(1) == fence:
                fence = None
            continue
        if fence is None:
            yield n, line


INLINE_CODE = re.compile(r"(`+)(.+?)\1")


def strip_code_spans(line: str) -> str:
    return INLINE_CODE.sub(lambda m: " " * len(m.group(0)), line)


def github_slug(heading: str) -> str:
    """GitHub's heading anchor: the rendered text, lower-cased, with everything but letters, digits,
    spaces, hyphens and underscores removed, and spaces turned into hyphens."""
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", heading)          # images → alt text
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)                  # links → their text
    t = re.sub(r"<[^>]+>", "", t)                                   # inline HTML tags
    t = t.replace("`", "")
    t = re.sub(r"(\*\*|__|\*)", "", t)                              # emphasis markers
    t = t.strip().lower()
    out = []
    for ch in t:
        cat = unicodedata.category(ch)
        if ch in " -_" or cat[0] in "LN":
            out.append("-" if ch == " " else ch)
    return "".join(out)


HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
HTML_ANCHOR = re.compile(r"""<a\s+(?:name|id)=["']([^"']+)["']""", re.I)


def anchors(path: Path, _cache: dict = {}) -> set[str]:  # noqa: B006 — a deliberate per-run cache
    if path not in _cache:
        seen: dict[str, int] = {}
        found: set[str] = set()
        text = path.read_text(encoding="utf-8")
        for _, line in prose_lines(text):
            for a in HTML_ANCHOR.findall(line):
                found.add(a)
            m = HEADING.match(line)
            if not m:
                continue
            base = github_slug(m.group(2))
            k = seen.get(base, 0)
            seen[base] = k + 1
            found.add(base if k == 0 else f"{base}-{k}")
        _cache[path] = found
    return _cache[path]


MD_LINK = re.compile(r"(?<!\\)!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REF_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
HTML_ATTR = re.compile(r"""\b(?:href|src)=["']([^"']+)["']""", re.I)


def links(path: Path):
    """(line number, target) for every link in prose (not in code blocks or inline code)."""
    for n, line in prose_lines(path.read_text(encoding="utf-8")):
        plain = strip_code_spans(line)
        for rx in (MD_LINK, HTML_ATTR):
            for m in rx.finditer(plain):
                yield n, m.group(1)
        m = REF_DEF.match(plain)
        if m:
            yield n, m.group(1)


def is_external(target: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target)) or target.startswith("//")


# ── front panel ──────────────────────────────────────────────────────────────────────────────────

def _label(node: ast.AST) -> str | None:
    """A menu label from the first element of a ("label", action) tuple; dynamic suffixes dropped."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                break
        text = "".join(parts)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _label(node.left)
    else:
        return None
    text = text.rstrip(" :([{•●-")
    return text.strip() or None


def _tuple_labels(nodes) -> list[str]:
    out = []
    for el in nodes:
        if isinstance(el, ast.Tuple) and el.elts:
            lab = _label(el.elts[0])
            if lab:
                out.append(lab)
    return out


def panel_menu_labels() -> list[str]:
    """Top-level entries of the front panel's main menu: the list `root()` returns, plus the entries
    of helper methods it splices in with *self.helper()."""
    src = (REPO / "software" / "panel" / "panel.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    methods: dict[str, ast.FunctionDef] = {}
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        for f in cls.body:
            if isinstance(f, ast.FunctionDef):
                methods.setdefault(f.name, f)
    if "root" not in methods:
        raise AssertionError("software/panel/panel.py has no root() menu method any more: update tests/test_docs.py "
                             "(panel_menu_labels) to find the main menu")
    root_list = None
    for node in ast.walk(methods["root"]):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.List):
            root_list = node.value
    if root_list is None:
        raise AssertionError("root() in software/panel/panel.py no longer returns a list literal: update "
                             "tests/test_docs.py (panel_menu_labels)")
    labels = _tuple_labels(root_list.elts)
    for el in root_list.elts:
        if not isinstance(el, ast.Starred):
            continue
        call = el.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr in methods):
            continue
        helper = methods[call.func.attr]
        for node in ast.walk(helper):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.List):
                labels += _tuple_labels(node.value.elts)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "append" and node.args):
                labels += _tuple_labels(node.args[:1])
    return list(dict.fromkeys(labels))


# ── tests ────────────────────────────────────────────────────────────────────────────────────────

class LinksTest(unittest.TestCase):
    def test_slug_rules(self):
        self.assertEqual(github_slug("Panic wipe, duress PIN, dead-man switch"),
                         "panic-wipe-duress-pin-dead-man-switch")
        self.assertEqual(github_slug("Sealed records (encryption at rest)"), "sealed-records-encryption-at-rest")
        self.assertEqual(github_slug("Settings (`[radio]` in prepper.toml)"), "settings-radio-in-preppertoml")
        self.assertEqual(github_slug("Limits — what only a real test can confirm"),
                         "limits--what-only-a-real-test-can-confirm")
        self.assertEqual(github_slug("Phones, iPads, laptops"), "phones-ipads-laptops")
        self.assertEqual(github_slug("Duress: when someone forces you"), "duress-when-someone-forces-you")

    def test_every_relative_link_resolves(self):
        problems = []
        for doc in doc_files():
            rel_doc = doc.relative_to(REPO)
            for n, target in links(doc):
                if is_external(target):
                    continue
                path_part, _, frag = target.partition("#")
                path_part = unquote(path_part.split("?", 1)[0])
                dest = doc if not path_part else (doc.parent / path_part).resolve()
                if not dest.exists():
                    problems.append(f"{rel_doc}:{n}: link to '{target}': {path_part} does not exist")
                    continue
                if frag and dest.suffix.lower() == ".md":
                    if unquote(frag).lower() not in anchors(dest):
                        problems.append(f"{rel_doc}:{n}: link to '{target}': no heading in "
                                        f"{dest.relative_to(REPO)} has the anchor #{frag}")
        self.assertEqual(problems, [], "Broken links — fix the link or the heading it points at:\n  "
                         + "\n  ".join(problems))


class GuidesInSyncTest(unittest.TestCase):
    def test_every_feature_plugin_is_in_usage(self):
        usage = (DOCS / "USAGE.md").read_text(encoding="utf-8")
        ids = sorted(p.stem for p in (REPO / "software" / "portal" / "features").glob("*.py")
                     if not p.name.startswith("_"))
        self.assertTrue(ids, "found no feature plugins in software/portal/features/")
        missing = [i for i in ids if f"`{i}`" not in usage]
        self.assertEqual(missing, [], "docs/USAGE.md doesn't describe these feature plugins (add each to its "
                         "'Feature reference' table, by id in backticks): " + ", ".join(missing))

    def test_every_config_table_is_in_setup(self):
        setup = (DOCS / "SETUP.md").read_text(encoding="utf-8")
        with open(REPO / "os" / "prepper.toml.example", "rb") as fh:
            cfg = tomllib.load(fh)
        tables = sorted(k for k, v in cfg.items() if isinstance(v, dict))
        self.assertTrue(tables, "found no tables in os/prepper.toml.example")
        spans = " ".join(m.group(2) for m in INLINE_CODE.finditer(setup))
        missing = [t for t in tables if f"[{t}]" not in spans]
        self.assertEqual(missing, [], "docs/SETUP.md doesn't mention these sections of os/prepper.toml.example "
                         "(name each at least once in backticks, e.g. `[radio]`): " + ", ".join(missing))

    def test_every_panel_menu_entry_is_documented(self):
        labels = panel_menu_labels()
        self.assertGreaterEqual(len(labels), 10, f"too few front-panel menu entries found: {labels}")
        text = (DOCS / "TOUR.md").read_text(encoding="utf-8") + (DOCS / "USAGE.md").read_text(encoding="utf-8")
        missing = [lab for lab in labels if lab not in text]
        self.assertEqual(missing, [], "These front-panel menu entries (software/panel/panel.py) are not in "
                         "docs/TOUR.md (Knob menu) or docs/USAGE.md: " + ", ".join(repr(m) for m in missing))

    def test_quickstart_covers_first_boot_checklist(self):
        qs = (DOCS / "QUICKSTART.md").read_text(encoding="utf-8").lower()
        items = {
            "the admin passphrase": ["admin passphrase"],
            "the recovery card / recovery key": ["recovery card", "recovery key"],
            "the panel code": ["panel code"],
            "the security posture": ["posture"],
            "\"What this Badger reveals\"": ["what this badger reveals"],
        }
        missing = [name for name, words in items.items() if not any(w in qs for w in words)]
        self.assertEqual(missing, [], "docs/QUICKSTART.md's first-boot security checklist is missing: "
                         + ", ".join(missing))

    def test_guides_are_linked(self):
        for doc in ("README.md", "docs/FEATURES.md"):
            text = (REPO / doc).read_text(encoding="utf-8")
            for guide in ("QUICKSTART.md", "SETUP.md", "USAGE.md"):
                self.assertTrue(guide in text, f"{doc} should link to docs/{guide}")
        self.assertTrue("QUICKSTART.md" in (DOCS / "OPERATIONS.md").read_text(encoding="utf-8"),
                        "docs/OPERATIONS.md should have a 'Start here' line pointing at QUICKSTART.md")


if __name__ == "__main__":
    unittest.main()
