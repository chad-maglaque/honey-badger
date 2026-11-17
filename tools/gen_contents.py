#!/usr/bin/env python3
"""Regenerate docs/CONTENTS.md from content/catalog.toml (the single source of truth).

  python3 tools/gen_contents.py            rewrite docs/CONTENTS.md
  python3 tools/gen_contents.py --check    exit 1 if docs/CONTENTS.md is out of date
"""
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CATS = [("reference", "Reference"), ("medical", "Medical"), ("survival", "Survival"), ("food-water", "Food & water"),
        ("energy-repair", "Power, repair & rebuilding"), ("comms", "Comms"), ("maps", "Maps"), ("learn", "Learn"),
        ("books", "Books"), ("video", "Video"), ("ai", "Offline AI"), ("tools", "Apps & software"), ("files", "Your files")]
TIERS = ["pocket", "field", "base", "ultimate"]


def load_items(path: Path = REPO / "content/catalog.toml") -> list[dict]:
    return tomllib.loads(path.read_text())["item"]


def total(items: list[dict], tier: str, langs=("eng",)) -> float:
    return sum(i["size_gb"] for i in items if tier in i["tiers"] and i["lang"] in (*langs, "mul", "zxx"))


def render(items: list[dict]) -> str:
    def mark(i, t):
        return "●" if t in i["tiers"] else ""

    def t(tier, langs=("eng",)):
        return total(items, tier, langs)

    out = ["# Contents", "",
           "_Generated from `content/catalog.toml` by `tools/gen_contents.py` — edit the catalog, not this file._", "",
           "Sizes are decimal GB from the Kiwix mirror on 2026-11-16; they drift with every upstream release.", "",
           "| Edition | Drive | English only | + Spanish |", "|---|---|---:|---:|",
           f"| Pocket (laptop go-bag pool, filled by priority to a budget) | — | {t('pocket'):,.0f} GB | {t('pocket', ('eng', 'spa')):,.0f} GB |",
           f"| Field | 512 GB | {t('field'):,.0f} GB | {t('field', ('eng', 'spa')):,.0f} GB |",
           f"| Base | 1 TB | {t('base'):,.0f} GB | {t('base', ('eng', 'spa')):,.0f} GB |",
           f"| Ultimate | 2 TB (4 TB for growth) | {t('ultimate'):,.0f} GB | {t('ultimate', ('eng', 'spa')):,.0f} GB |", "",
           "Each drive also needs free space for one new copy of its largest item during updates (127 GB for full Wikipedia).", "",
           "Legend: **P** pocket · **F** field · **B** base · **U** ultimate · priority 1 (essential) to 9 (nice to have) · "
           "_manual_ = you supply it (licensing or no public source).", ""]
    for cid, title in CATS:
        xs = [i for i in items if i["category"] == cid]
        if not xs:
            continue
        out += [f"## {title}", "", "| Item | GB | P | F | B | U | Pri | Source | License |", "|---|---:|:-:|:-:|:-:|:-:|:-:|---|---|"]
        for i in sorted(xs, key=lambda i: (i["priority"], i["title"])):
            src = i["source"]["type"]
            src = "_manual_" if src == "manual" else f"`{i['source'].get('name', src)}`" if src == "kiwix" else src
            lang = "" if i["lang"] in ("eng", "zxx") else f" ({i['lang']})"
            out.append(f"| **{i['title']}**{lang}<br><sub>{i['description']}</sub> | {i['size_gb']:.2f} | "
                       + " | ".join(mark(i, t) for t in TIERS) + f" | {i['priority']} | {src} | {i['license']} |")
        out.append("")
    out += ["## Built-in tools (always present, cached on phones/iPads)", "",
            "Morse code (send by sound or screen-flash, tap-to-decode) · Emergency phrasebook (EN→ES/FR/DE/PT, show full-screen, speak) · "
            "Water disinfection calculator (EPA bleach table) · Radio frequency reference (FRS/GMRS, MURS, CB, NOAA WX, marine, aviation, ham, "
            "WWV, PMR446, Meshtastic) · Unit & battery-runtime converter · Sun/moon/twilight calculator with no-compass navigation tips · "
            "Check-in board bridged to LoRa mesh · NOAA Weather Radio alert banner · Ask-the-Library offline AI with citations.", ""]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    items = load_items()
    text = render(items)
    target = REPO / "docs/CONTENTS.md"
    if "--check" in argv:
        if target.read_text() != text:
            print("docs/CONTENTS.md is out of date: run python3 tools/gen_contents.py", file=sys.stderr)
            return 1
        print(f"docs/CONTENTS.md: up to date ({len(items)} items)")
        return 0
    target.write_text(text)
    print(f"docs/CONTENTS.md: {len(items)} items")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
