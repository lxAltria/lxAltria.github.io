#!/usr/bin/env python3
"""Backfill DOI / arXiv links into _data/publications.yml.

Sources, in order of preference:
  1. Crossref  (api.crossref.org)  -- open API, no bot-blocking, has DOIs
  2. arXiv     (export.arxiv.org)  -- open API, for preprints

NOT Google Scholar and NOT DBLP: Scholar blocks automated access outright,
and dblp.org now sits behind an anti-bot interstitial. Working around either
is not something this script does.

Matching is deliberately strict, in two tiers (see main()). Titles in this field are near-collisions --
"Toward Feature-Preserving Vector Field Compression" (TVCG '22) vs
"Towards Feature Preserving 2D and 3D Vector Field Compression"
(PacificVis '20) are different papers. A fuzzy match would silently attach
the wrong DOI, which is worse than no DOI. So:

  Tier 1  exact normalized title  AND year within +/-1   -> write
  Tier 2  one title is a prefix of the other (>=4 chars)
          AND the year matches EXACTLY                    -> write, flagged
          (Crossref stores truncated titles for some ACM/IEEE records:
           "DeepSZ", "TSM2", "FT-iSort", "New-Sum", "cuSZ", "waveSZ")
  anything else                                           -> report for review

Tier 2 is looser on the title, so it is tighter on the year. Audit its
output: the report lists those matches separately.

Usage:
  python3 scripts/backfill_links.py            # dry run, prints a report
  python3 scripts/backfill_links.py --write    # apply confident matches
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request

DATA = "_data/publications.yml"
UA = "lxaltria-homepage-backfill/1.0 (mailto:xin.liang@oregonstate.edu)"
CROSSREF = "https://api.crossref.org/works"


def norm(s):
    """Lowercase, strip punctuation and stopword noise for comparison only."""
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(towards?|a|an|the|for|of|with|on|in|and)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def crossref(title, timeout=25):
    q = urllib.parse.urlencode({
        "rows": 5,
        "select": "DOI,title,issued,container-title,type",
        "query.bibliographic": title,
    })
    req = urllib.request.Request(f"{CROSSREF}?{q}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["message"]["items"]


def year_of(item):
    parts = item.get("issued", {}).get("date-parts") or [[None]]
    return parts[0][0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="apply confident matches")
    args = ap.parse_args()

    try:
        import yaml  # noqa
    except ImportError:
        sys.exit("PyYAML required: python3 -m pip install --user pyyaml")
    import yaml

    with open(DATA) as f:
        raw = f.read()
    pubs = yaml.safe_load(raw)

    confident, review, skipped = [], [], []

    for p in pubs:
        links = p.get("links") or {}
        if links.get("doi"):
            skipped.append((p["id"], "already has a DOI"))
            continue

        try:
            items = crossref(p["title"])
        except Exception as e:
            review.append((p["id"], p["title"], None, f"lookup failed: {e}"))
            continue
        finally:
            time.sleep(1)  # be polite to the API

        want_title, want_year = norm(p["title"]), p["date"].year
        hit = tier = None

        # Tier 1: exact normalized title, year within +/-1.
        for it in items:
            got = (it.get("title") or [""])[0]
            yr = year_of(it)
            if norm(got) == want_title and yr and abs(yr - want_year) <= 1:
                hit, tier = (it["DOI"], got, yr), "exact"
                break

        # Tier 2: Crossref stores a truncated title for some ACM/IEEE records
        # ("DeepSZ", "TSM2", "FT-iSort"). Accept when one normalized title is a
        # prefix of the other AND the year matches EXACTLY -- the tighter year
        # requirement is what keeps this safe. Require >=4 chars so that a stub
        # like "SZ" cannot match half the corpus.
        if not hit:
            for it in items:
                got = (it.get("title") or [""])[0]
                yr, g = year_of(it), norm(got)
                if not g or yr != want_year:
                    continue
                short, long_ = sorted((g, want_title), key=len)
                if len(short) >= 4 and long_.startswith(short):
                    hit, tier = (it["DOI"], got, yr), "prefix"
                    break

        if hit:
            confident.append((p["id"], hit[0], hit[2], tier))
        else:
            top = items[0] if items else None
            why = "no result" if not top else (
                f"best guess {top['DOI']} = "
                f"{(top.get('title') or [''])[0][:60]!r} ({year_of(top)})")
            review.append((p["id"], p["title"], top, why))

    ex = [c for c in confident if c[3] == "exact"]
    pf = [c for c in confident if c[3] == "prefix"]
    print(f"\n{'='*72}\nCONFIDENT ({len(confident)})\n{'='*72}")
    print(f"-- exact title + year+/-1 ({len(ex)})")
    for pid, doi, yr, _ in ex:
        print(f"  {pid:36} {doi}  ({yr})")
    print(f"-- title-prefix + EXACT year ({len(pf)})  <- audit these")
    for pid, doi, yr, _ in pf:
        print(f"  {pid:36} {doi}  ({yr})")

    print(f"\n{'='*72}\nNEEDS REVIEW ({len(review)})  not written -- check by hand\n{'='*72}")
    for pid, title, _top, why in review:
        print(f"  {pid:34} {why}")

    if skipped:
        print(f"\nSKIPPED ({len(skipped)}): " + ", ".join(f"{i} ({w})" for i, w in skipped))

    if not args.write:
        print("\nDry run. Re-run with --write to apply the CONFIDENT matches.")
        return

    # Write only the confident DOIs, editing the YAML text in place so that
    # comments, ordering and formatting survive.
    out = raw
    for pid, doi, _yr, _tier in confident:
        i = out.index(f"- id: {pid}")
        j = out.find("\n- id:", i)
        block = out[i:] if j == -1 else out[i:j]
        if "  links:" in block:
            new_block = re.sub(r"(\n    doi:)\s*(\n|$)", rf"\1 {doi}\2", block, count=1)
            if new_block == block:  # links: exists but no doi key
                new_block = block.replace("  links:\n", f"  links:\n    doi: {doi}\n", 1)
        else:
            new_block = block.rstrip("\n") + f"\n  links:\n    doi: {doi}\n"
            if j != -1:
                new_block += "\n" if not block.endswith("\n\n") else ""
        out = out[:i] + new_block + (out[j:] if j != -1 else "")

    with open(DATA, "w") as f:
        f.write(out)
    print(f"\nWrote {len(confident)} DOIs into {DATA}.")


if __name__ == "__main__":
    main()
