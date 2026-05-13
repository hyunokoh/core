#!/usr/bin/env python3
"""Generate sample NFT metadata for the zkCEX marketplace demo.

Writes 100 ERC721 metadata files (1.json … 100.json) plus matching SVG
thumbnails (1.svg … 100.svg) and 5 ERC1155 edition files (1001.json …
1005.json).

The SVG art is deterministic per token id: each id picks a small palette
seeded from id, draws a geometric arrangement (radial / grid / diagonal
ribbons), and writes the id in the corner so they're immediately
distinguishable in the gallery.

Re-runnable; overwrites existing files. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOTAL_721 = 100
ERC1155_IDS = [1001, 1002, 1003, 1004, 1005]

# Palette pools — pleasant, distinguishable hues. Each tuple is (bg, fg1, fg2, fg3).
PALETTES = [
    ("#0b1020", "#7c4dff", "#22d3ee", "#f472b6"),
    ("#0f172a", "#06b6d4", "#a855f7", "#facc15"),
    ("#1f1d33", "#f97316", "#10b981", "#fb7185"),
    ("#0a0f1e", "#2563eb", "#22c55e", "#fde047"),
    ("#111827", "#ec4899", "#3b82f6", "#84cc16"),
    ("#101820", "#fb923c", "#06b6d4", "#a78bfa"),
    ("#16161d", "#34d399", "#f43f5e", "#60a5fa"),
    ("#000814", "#ffd60a", "#003566", "#90e0ef"),
    ("#13111c", "#9d4edd", "#06d6a0", "#ef476f"),
    ("#10002b", "#7209b7", "#f72585", "#4cc9f0"),
]

PATTERNS = ["rings", "grid", "ribbons", "petals", "constellation"]
RARITIES = [
    ("Common",    60),
    ("Uncommon",  25),
    ("Rare",      10),
    ("Epic",       4),
    ("Legendary",  1),
]


def seeded_random(token_id: int) -> random.Random:
    # Hash so adjacent ids don't share visual structure.
    h = hashlib.sha256(f"zkcex-nft:{token_id}".encode()).digest()
    seed = int.from_bytes(h[:8], "big")
    return random.Random(seed)


def pick_rarity(rng: random.Random) -> str:
    # Weighted pick from RARITIES.
    total = sum(w for _, w in RARITIES)
    r = rng.randint(1, total)
    cum = 0
    for name, w in RARITIES:
        cum += w
        if r <= cum:
            return name
    return RARITIES[-1][0]


def svg_rings(rng: random.Random, palette: tuple[str, ...]) -> str:
    bg, fg1, fg2, fg3 = palette
    layers = []
    for i in range(rng.randint(3, 7)):
        cx = rng.randint(120, 380)
        cy = rng.randint(120, 380)
        r = rng.randint(40, 180)
        col = rng.choice([fg1, fg2, fg3])
        layers.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r}" stroke="{col}" stroke-width="{rng.randint(2, 8)}"'
            f' fill="none" opacity="{rng.uniform(0.5, 0.9):.2f}"/>'
        )
    return "\n".join(layers)


def svg_grid(rng: random.Random, palette: tuple[str, ...]) -> str:
    bg, fg1, fg2, fg3 = palette
    cells = []
    n = rng.choice([6, 8, 10])
    size = 500 // n
    for x in range(n):
        for y in range(n):
            if rng.random() < 0.45:
                continue
            col = rng.choice([fg1, fg2, fg3])
            cells.append(
                f'<rect x="{x*size}" y="{y*size}" width="{size}" height="{size}"'
                f' fill="{col}" opacity="{rng.uniform(0.6, 1.0):.2f}"/>'
            )
    return "\n".join(cells)


def svg_ribbons(rng: random.Random, palette: tuple[str, ...]) -> str:
    bg, fg1, fg2, fg3 = palette
    out = []
    for _ in range(rng.randint(5, 9)):
        y = rng.randint(0, 500)
        h = rng.randint(20, 80)
        col = rng.choice([fg1, fg2, fg3])
        out.append(
            f'<rect x="-50" y="{y}" width="600" height="{h}" fill="{col}"'
            f' opacity="{rng.uniform(0.4, 0.85):.2f}"'
            f' transform="rotate({rng.randint(-25, 25)} 250 {y + h//2})"/>'
        )
    return "\n".join(out)


def svg_petals(rng: random.Random, palette: tuple[str, ...]) -> str:
    bg, fg1, fg2, fg3 = palette
    out = []
    cx, cy = 250, 250
    n = rng.choice([6, 8, 12])
    for i in range(n):
        ang = (360 / n) * i
        col = rng.choice([fg1, fg2, fg3])
        out.append(
            f'<ellipse cx="{cx}" cy="{cy - 120}" rx="{rng.randint(28, 60)}"'
            f' ry="{rng.randint(60, 130)}" fill="{col}"'
            f' opacity="{rng.uniform(0.55, 0.9):.2f}"'
            f' transform="rotate({ang:.1f} {cx} {cy})"/>'
        )
    out.append(f'<circle cx="{cx}" cy="{cy}" r="32" fill="{fg2}"/>')
    return "\n".join(out)


def svg_constellation(rng: random.Random, palette: tuple[str, ...]) -> str:
    bg, fg1, fg2, fg3 = palette
    pts = [(rng.randint(40, 460), rng.randint(40, 460)) for _ in range(rng.randint(8, 16))]
    out = []
    for i, (x, y) in enumerate(pts):
        out.append(
            f'<circle cx="{x}" cy="{y}" r="{rng.randint(3, 8)}" fill="{fg3}"/>'
        )
        for j in range(i + 1, len(pts)):
            if rng.random() < 0.25:
                ox, oy = pts[j]
                out.append(
                    f'<line x1="{x}" y1="{y}" x2="{ox}" y2="{oy}" stroke="{fg1}"'
                    f' stroke-width="{rng.uniform(0.6, 1.6):.2f}" opacity="0.6"/>'
                )
    return "\n".join(out)


PATTERN_FNS = {
    "rings": svg_rings,
    "grid": svg_grid,
    "ribbons": svg_ribbons,
    "petals": svg_petals,
    "constellation": svg_constellation,
}


def build_svg(token_id: int) -> tuple[str, dict]:
    rng = seeded_random(token_id)
    palette = rng.choice(PALETTES)
    pattern = rng.choice(PATTERNS)
    rarity = pick_rarity(rng)
    bg, fg1, fg2, fg3 = palette
    body = PATTERN_FNS[pattern](rng, palette)

    # Always include a label with the token id in the bottom-right.
    label = (
        f'<text x="480" y="490" font-family="JetBrains Mono, monospace"'
        f' font-size="22" fill="{fg3}" text-anchor="end" opacity="0.9">'
        f'#{token_id}</text>'
        f'<text x="20" y="490" font-family="JetBrains Mono, monospace"'
        f' font-size="14" fill="{fg2}" opacity="0.8">zkCEX</text>'
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 500 500" width="500" height="500">'
        f'<rect width="500" height="500" fill="{bg}"/>'
        f'{body}'
        f'{label}'
        f'</svg>'
    )
    attrs = {
        "pattern": pattern,
        "palette_index": PALETTES.index(palette),
        "rarity": rarity,
    }
    return svg, attrs


def write_one_721(token_id: int) -> None:
    svg, attrs = build_svg(token_id)
    svg_path = HERE / f"{token_id}.svg"
    json_path = HERE / f"{token_id}.json"
    svg_path.write_text(svg, encoding="utf-8")
    meta = {
        "name": f"zkCEX Genesis #{token_id}",
        "description": (
            "Genesis-set NFT minted on the zkCEX hardhat demo chain. "
            "Art is deterministically derived from the token id."
        ),
        "image": f"/nft-meta/{token_id}.svg",
        "external_url": f"/app/nft.html?token={token_id}",
        "attributes": [
            {"trait_type": "Pattern", "value": attrs["pattern"]},
            {"trait_type": "Palette", "value": attrs["palette_index"]},
            {"trait_type": "Rarity", "value": attrs["rarity"]},
            {"trait_type": "Edition", "value": "Genesis"},
        ],
    }
    json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def write_one_1155(token_id: int) -> None:
    svg, attrs = build_svg(token_id)
    svg_path = HERE / f"{token_id}.svg"
    json_path = HERE / f"{token_id}.json"
    svg_path.write_text(svg, encoding="utf-8")
    edition_no = token_id - 1000
    meta = {
        "name": f"zkCEX Edition #{edition_no}",
        "description": (
            f"ERC1155 edition #{edition_no} on zkCEX — 50 copies in the wild. "
            "Drop a few in the marketplace and see them resurface in the "
            "browse view."
        ),
        "image": f"/nft-meta/{token_id}.svg",
        "external_url": f"/app/nft.html?token={token_id}",
        "attributes": [
            {"trait_type": "Pattern", "value": attrs["pattern"]},
            {"trait_type": "Palette", "value": attrs["palette_index"]},
            {"trait_type": "Rarity", "value": attrs["rarity"]},
            {"trait_type": "Edition", "value": f"Editions #{edition_no}"},
            {"trait_type": "Total Copies", "value": 50},
        ],
    }
    json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


def main() -> int:
    for i in range(1, TOTAL_721 + 1):
        write_one_721(i)
    for i in ERC1155_IDS:
        write_one_1155(i)
    print(f"[nft-meta] wrote {TOTAL_721} ERC721 + {len(ERC1155_IDS)} ERC1155 metadata files to {HERE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
