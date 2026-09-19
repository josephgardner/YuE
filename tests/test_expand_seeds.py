import importlib.util
import json
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
_spec = importlib.util.spec_from_file_location("expand_seeds", TOOLS / "expand_seeds.py")
expand_seeds = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(expand_seeds)


def write_request(path, **overrides):
    data = {"id": "song", "style": "pop", "lyrics": "[Verse]\nla", "cot": "full", "seed": 1}
    data.update(overrides)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_expands_unique_ids_and_records_seed(tmp_path):
    req = write_request(tmp_path / "song.json")
    stage = tmp_path / "stage"
    report = expand_seeds.build_manifest([req], seeds=3, seed_start=100, stage=stage)

    rows = [json.loads(line) for line in Path(report["output"]).read_text().splitlines()]
    assert [r["id"] for r in rows] == ["song_seed100", "song_seed101", "song_seed102"]
    assert [r["seed"] for r in rows] == [100, 101, 102]
    assert report["rows"] == 3
    assert report["seeds"] == [100, 102]


def test_stage_copies_abc_and_rewrites_relative_path(tmp_path):
    abc = tmp_path / "pack" / "sub" / "tune.abc"
    abc.parent.mkdir(parents=True)
    abc.write_text("X:1\nK:C\nCDEF|\n", encoding="utf-8")
    req = write_request(tmp_path / "pack" / "sub" / "song.json", abc_path="tune.abc")
    stage = tmp_path / "stage"

    report = expand_seeds.build_manifest([req], seeds=2, seed_start=7, stage=stage)
    rows = [json.loads(line) for line in Path(report["output"]).read_text().splitlines()]

    assert all(r["abc_path"] == "tune.abc" for r in rows)
    assert (stage / "tune.abc").read_text() == abc.read_text()
    assert (Path(report["output"]).parent / rows[0]["abc_path"]).is_file()


def test_rejects_duplicate_ids(tmp_path):
    a = write_request(tmp_path / "a.json", id="dup")
    b = write_request(tmp_path / "b.json", id="dup")
    with pytest.raises(SystemExit, match="duplicate id"):
        expand_seeds.build_manifest([a, b], seeds=1, seed_start=1, stage=tmp_path / "stage")
