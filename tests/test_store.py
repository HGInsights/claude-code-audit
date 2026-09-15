"""Dated report storage, the user id, and the trend history reader.

The redaction properties of `sanitize` live in test_redaction.py; this covers
the storage mechanics around it.
"""

import json
import os
from datetime import datetime

import pytest

import store


def payload(**kw):
    base = {
        "user_id": "alice-ab12",
        "generated": "2026-09-15T10:00:00",
        "window_days": 30,
        "sessions": 3,
        "api_calls": 100,
        "total_cost_usd": 42.0,
        "estimated_savings_usd": 10.0,
        "findings": [],
    }
    base.update(kw)
    return base


class TestUserId:
    def test_shape_is_user_plus_four_hex(self):
        uid = store.user_id()
        user, _, machine = uid.rpartition("-")
        assert user
        assert len(machine) == 4
        assert all(ch in "0123456789abcdef" for ch in machine)

    def test_is_stable_across_calls(self):
        assert store.user_id() == store.user_id()

    def test_unsafe_characters_are_normalised(self, monkeypatch):
        monkeypatch.setattr(store.getpass, "getuser", lambda: "Alice O'Brien")
        uid = store.user_id()
        assert " " not in uid and "'" not in uid
        assert uid.islower()

    def test_falls_back_when_the_username_is_unavailable(self, monkeypatch):
        def boom():
            raise OSError("no login name")
        monkeypatch.setattr(store.getpass, "getuser", boom)
        monkeypatch.delenv("USER", raising=False)
        assert store.user_id().startswith("unknown-")


class TestSave:
    def test_writes_a_dated_pair(self, tmp_path):
        md, js = store.save("# Report", payload(), None, root=str(tmp_path),
                            when=datetime(2026, 9, 15))
        assert os.path.basename(md) == "2026-09-15.md"
        assert os.path.basename(js) == "2026-09-15.json"
        assert open(md).read() == "# Report"
        assert json.load(open(js))["total_cost_usd"] == 42.0

    def test_files_land_under_the_user_directory(self, tmp_path):
        md, _ = store.save("# R", payload(), None, root=str(tmp_path))
        assert os.path.basename(os.path.dirname(md)) == "alice-ab12"

    def test_rerunning_the_same_day_overwrites_in_place(self, tmp_path):
        when = datetime(2026, 9, 15)
        store.save("# first", payload(), None, root=str(tmp_path), when=when)
        md, _ = store.save("# second", payload(), None, root=str(tmp_path),
                           when=when)
        assert open(md).read() == "# second"
        day_files = [f for f in os.listdir(os.path.dirname(md))
                     if f.startswith("2026-09-15")]
        assert sorted(day_files) == ["2026-09-15.json", "2026-09-15.md"]

    def test_creates_the_root_if_it_does_not_exist(self, tmp_path):
        root = tmp_path / "deep" / "nested" / "reports"
        md, _ = store.save("# R", payload(), None, root=str(root))
        assert os.path.exists(md)

    def test_latest_symlinks_point_at_todays_files(self, tmp_path):
        md, _ = store.save("# R", payload(), None, root=str(tmp_path),
                           when=datetime(2026, 9, 15))
        d = os.path.dirname(md)
        for ext in ("md", "json"):
            link = os.path.join(d, f"latest.{ext}")
            assert os.path.islink(link)
            assert os.readlink(link) == f"2026-09-15.{ext}"

    def test_latest_is_repointed_on_a_later_run(self, tmp_path):
        store.save("# old", payload(), None, root=str(tmp_path),
                   when=datetime(2026, 9, 14))
        md, _ = store.save("# new", payload(), None, root=str(tmp_path),
                           when=datetime(2026, 9, 15))
        link = os.path.join(os.path.dirname(md), "latest.md")
        assert os.readlink(link) == "2026-09-15.md"
        assert open(link).read() == "# new"

    def test_the_json_is_valid_and_newline_terminated(self, tmp_path):
        _, js = store.save("# R", payload(), None, root=str(tmp_path))
        raw = open(js).read()
        assert raw.endswith("\n")
        json.loads(raw)

    def test_falls_back_to_the_computed_user_id(self, tmp_path):
        p = payload()
        del p["user_id"]
        md, _ = store.save("# R", p, None, root=str(tmp_path))
        assert os.path.basename(os.path.dirname(md)) == store.user_id()


class TestHistory:
    def test_missing_root_is_empty(self, tmp_path):
        assert store.history(root=str(tmp_path / "nope")) == []

    def test_empty_root_is_empty(self, tmp_path):
        assert store.history(root=str(tmp_path)) == []

    def test_returns_reports_oldest_first(self, tmp_path):
        for day in ("2026-09-13", "2026-09-15", "2026-09-14"):
            store.save("# R", payload(total_cost_usd=1.0), None,
                       root=str(tmp_path),
                       when=datetime.fromisoformat(day))
        got = store.history(root=str(tmp_path))
        assert [r["date"] for r in got] == [
            "2026-09-13", "2026-09-14", "2026-09-15"]

    def test_latest_symlinks_are_not_read_as_reports(self, tmp_path):
        store.save("# R", payload(), None, root=str(tmp_path),
                   when=datetime(2026, 9, 15))
        assert len(store.history(root=str(tmp_path))) == 1

    def test_reads_every_user_by_default(self, tmp_path):
        store.save("# R", payload(user_id="alice-ab12"), None,
                   root=str(tmp_path), when=datetime(2026, 9, 15))
        store.save("# R", payload(user_id="bob-cd34"), None,
                   root=str(tmp_path), when=datetime(2026, 9, 15))
        assert len(store.history(root=str(tmp_path))) == 2

    def test_can_be_scoped_to_one_user(self, tmp_path):
        store.save("# R", payload(user_id="alice-ab12"), None,
                   root=str(tmp_path), when=datetime(2026, 9, 15))
        store.save("# R", payload(user_id="bob-cd34"), None,
                   root=str(tmp_path), when=datetime(2026, 9, 15))
        got = store.history(root=str(tmp_path), uid="alice-ab12")
        assert [r["user_id"] for r in got] == ["alice-ab12"]

    def test_a_corrupt_report_is_skipped_not_fatal(self, tmp_path):
        store.save("# R", payload(), None, root=str(tmp_path),
                   when=datetime(2026, 9, 15))
        d = tmp_path / "alice-ab12"
        (d / "2026-09-16.json").write_text("{not json")
        (d / "2026-09-17.json").write_text("")
        got = store.history(root=str(tmp_path))
        assert [r["date"] for r in got] == ["2026-09-15"]

    def test_a_scoped_read_of_an_unknown_user_is_empty(self, tmp_path):
        store.save("# R", payload(), None, root=str(tmp_path))
        assert store.history(root=str(tmp_path), uid="nobody-0000") == []
