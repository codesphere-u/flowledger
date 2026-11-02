"""CLI exports use synthetic reconciliation data only."""

import csv
import json
from dataclasses import replace

import pytest

from flowledger import cli
from flowledger.reconciliation.engine import ReconciliationStatus


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


@pytest.mark.parametrize("json_output", [False, True])
def test_demo_exports_all_exceptions_and_proposals(tmp_path, capsys, monkeypatch, json_output):
    simulation = cli.run_synthetic_simulation()
    monkeypatch.setattr(cli, "run_synthetic_simulation", lambda **kwargs: simulation)
    path = tmp_path / "audit.csv"
    args = ["demo", "--export-csv", str(path)]
    if json_output:
        args.append("--json")
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    if json_output:
        report = json.loads(output)["reconciliation"]
        expected = [item for item in report["items"] if item["status"] != "MATCHED"]
    else:
        assert "FLOWLEDGER" in output
        expected = [item.to_dict() for item in simulation["reconciliation"].items
                    if item.status != ReconciliationStatus.MATCHED]
    rows = read_csv(path)
    assert len(rows) == len(expected) > 0
    for row, item in zip(rows, expected):
        for field in ("payment_id", "provider", "status", "currency", "explanation"):
            assert row[field] == item[field]
        assert int(row["discrepancy_delta"]) == item["discrepancy_delta"]
        assert json.loads(row["adjustment_proposal"]) == item["adjustment_proposal"]


def test_empty_export_has_headers(tmp_path, capsys):
    path = tmp_path / "audit.csv"
    assert cli.main(["demo", "--count", "0", "--export-csv", str(path)]) == 0
    assert read_csv(path) == []
    assert "payment_id" in path.read_text()


def test_csv_round_trips_quotes_newlines_unicode_and_large_integers(tmp_path, monkeypatch, capsys):
    simulation = cli.run_synthetic_simulation()
    report = simulation["reconciliation"]
    item = next(item for item in report.items if item.adjustment_proposal)
    description = 'Synthetic "quoted", café\nnext line'
    proposal = replace(item.adjustment_proposal, description=description)
    item = replace(item, explanation=description, discrepancy_delta=2**60 + 1,
                   adjustment_proposal=proposal)
    simulation["reconciliation"] = replace(report, items=[item], adjustment_proposals=[proposal])
    monkeypatch.setattr(cli, "run_synthetic_simulation", lambda **kwargs: simulation)
    path = tmp_path / "audit.csv"
    assert cli.main(["demo", "--export-csv", str(path)]) == 0
    row, = read_csv(path)
    assert row["explanation"] == description
    assert row["discrepancy_delta"] == str(2**60 + 1)
    assert json.loads(row["adjustment_proposal"]) == proposal.to_dict()


def test_export_write_failure_is_reported(tmp_path, capsys):
    path = tmp_path / "missing" / "audit.csv"
    assert cli.main(["demo", "--export-csv", str(path)]) == 1
    assert "CSV" in capsys.readouterr().err
    assert not path.exists()
