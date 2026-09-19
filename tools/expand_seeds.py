#!/usr/bin/env python3
"""Expand song requests into a seed-sweep batch manifest (JSONL).

Each input request (a JSON object with an `id`, `style`, and `lyrics`) becomes
N rows, one per seed, with a unique id `<id>_seed<seed>`. `yue2 batch` writes
each render to `output/<id>/`, and the seed is recorded in every row so it
lands in the per-song `request.json` and `result.json`.

Requests may be given as files or directories. Files referenced by `abc_path`
are copied into the staging directory (with `--stage`) so the manifest stays
self-contained and relative paths keep working after the pack is uploaded.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

EXCLUDE = {
    "batch.jsonl",
    "result.json",
    "config.json",
    "request.json",
    "failure.json",
    "plan_manifest.json",
}


def iter_requests(paths):
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found = sorted(f for f in p.rglob("*.json") if f.name not in EXCLUDE)
            if not found:
                raise SystemExit(f"no request JSON files under {p}")
            yield from found
        elif p.is_file():
            yield p
        else:
            raise SystemExit(f"no such request: {p}")


def build_manifest(requests, seeds, seed_start, out=None, stage=None):
    if seeds < 1:
        raise SystemExit("--seeds must be >= 1")
    files = list(iter_requests(requests))
    if not files:
        raise SystemExit("no requests given")
    roots = [f.resolve().parent for f in files]
    common = Path(os.path.commonpath([str(r) for r in roots]))

    if stage is not None:
        stage = Path(stage).resolve()
        stage.mkdir(parents=True, exist_ok=True)
        out = stage / "batch.jsonl"
    else:
        out = Path(out).resolve() if out else common / "batch.jsonl"
    out_dir = out.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    seen = set()
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SystemExit(f"{f}: request must be a JSON object")
        base_id = str(data.get("id") or f.stem)
        abc_src = None
        if data.get("abc_path"):
            abc_src = (f.resolve().parent / data["abc_path"]).resolve()
            if not abc_src.is_file():
                raise SystemExit(f"{f}: abc_path not found: {abc_src}")
        for i in range(seeds):
            seed = seed_start + i
            rid = f"{base_id}_seed{seed}"
            if rid in seen:
                raise SystemExit(f"duplicate id {rid}; give each request a unique id")
            seen.add(rid)
            row = dict(data)
            row["id"] = rid
            row["seed"] = seed
            if abc_src is not None:
                if stage is not None:
                    rel = Path(os.path.relpath(abc_src, common))
                    if rel.parts and rel.parts[0] == "..":
                        rel = Path("abc") / f"{f.stem}_{abc_src.name}"
                    dest = out_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(abc_src, dest)
                    row["abc_path"] = str(rel)
                else:
                    row["abc_path"] = os.path.relpath(abc_src, out_dir)
            rows.append(row)

    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {
        "output": str(out),
        "pack_dir": str(out_dir),
        "requests": len(files),
        "rows": len(rows),
        "seeds": [seed_start, seed_start + seeds - 1],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("requests", nargs="+", help="request JSON files or directories")
    ap.add_argument("--seeds", type=int, default=1, help="number of seeds per request (default 1)")
    ap.add_argument("--seed-start", type=int, default=831001, help="first seed (incremented per row)")
    ap.add_argument("--out", help="output JSONL (default: <common dir>/batch.jsonl)")
    ap.add_argument("--stage", help="copy the manifest and referenced files into this clean directory")
    args = ap.parse_args(argv)
    report = build_manifest(args.requests, args.seeds, args.seed_start, out=args.out, stage=args.stage)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())