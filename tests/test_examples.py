import contextlib
import io
import json
import runpy
from pathlib import Path

import txo_engine


ROOT = Path(__file__).resolve().parents[1]


def test_synthetic_snapshot_analyzes_offline():
    snapshot = json.loads((ROOT / "examples" / "snapshot.example.json").read_text(encoding="utf-8"))
    report = txo_engine.analyze_snapshot(snapshot)
    assert report["status"] == "analyzed"
    assert report["quote_count"] == 4
    assert report["mode"] == "offline_research_only"


def test_paper_demo_runs_end_to_end_without_external_services():
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        runpy.run_path(str(ROOT / "examples" / "paper_demo.py"), run_name="__main__")
    report = json.loads(output.getvalue())
    assert report["fixture"] == "synthetic_only"
    assert report["order"]["status"] == "closed"
    assert report["order"]["settlement"]["source_authenticated"] is False
    assert [event["event"] for event in report["audit"]] == ["submit", "fill", "close"]
