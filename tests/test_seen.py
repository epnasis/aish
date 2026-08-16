"""The seen ledger (#232): when the OWNER last read each chat.

Every check here is one of the two properties that make sharing a read-state
across devices safe — MONOTONIC (a stamp only moves forward, so every merge is
a max and a re-send is free) and ONE CLOCK (a stamp offered by a client is
clamped to now, so a phone running fast cannot mark output read that nobody has
seen). Break either and the failure is silent: a dot that never clears, or an
answer the owner never saw quietly disappearing off the list.
"""

import json

from aish.seen import SEEN_MAX, SeenLedger


class TestMonotonic:
    def test_a_mark_is_recorded_and_reported_as_changed(self, tmp_path):
        ledger = SeenLedger(tmp_path / "seen.json")
        changed = ledger.merge({"a.jsonl": None}, now=1000.0)
        assert changed == {"a.jsonl": 1000.0}
        assert ledger.stamp("a.jsonl") == 1000.0

    def test_re_offering_a_held_stamp_changes_nothing(self, tmp_path):
        # This is the whole reason a lost broadcast needs no receipt: a client
        # can hand over everything it knows on every connect and be free.
        ledger = SeenLedger(tmp_path / "seen.json")
        ledger.merge({"a.jsonl": 1000.0}, now=2000.0)
        assert ledger.merge({"a.jsonl": 1000.0}, now=2000.0) == {}
        assert ledger.merge({"a.jsonl": 900.0}, now=2000.0) == {}
        assert ledger.stamp("a.jsonl") == 1000.0

    def test_a_stamp_never_walks_backwards(self, tmp_path):
        # An older look arriving late — the other device was offline when it
        # read the chat — must not un-read what this one has already seen.
        ledger = SeenLedger(tmp_path / "seen.json")
        ledger.merge({"a.jsonl": 2000.0}, now=3000.0)
        ledger.merge({"a.jsonl": 500.0}, now=3000.0)
        assert ledger.stamp("a.jsonl") == 2000.0

    def test_only_what_moved_is_reported(self, tmp_path):
        ledger = SeenLedger(tmp_path / "seen.json")
        ledger.merge({"a.jsonl": 1000.0}, now=5000.0)
        changed = ledger.merge({"a.jsonl": 1000.0, "b.jsonl": 1200.0}, now=5000.0)
        assert changed == {"b.jsonl": 1200.0}


class TestOneClock:
    def test_a_mark_from_the_future_is_clamped_to_now(self, tmp_path):
        # A phone five minutes fast. Trusting it would mark output read that
        # nobody has seen, and unread has no way back.
        ledger = SeenLedger(tmp_path / "seen.json")
        changed = ledger.merge({"a.jsonl": 9999.0}, now=1000.0)
        assert changed == {"a.jsonl": 1000.0}

    def test_a_mark_with_no_time_means_now(self, tmp_path):
        # The live "I just opened this": the server's own clock is the honest
        # answer, and the client does not have to guess it.
        ledger = SeenLedger(tmp_path / "seen.json")
        assert ledger.merge({"a.jsonl": None}, now=1234.0) == {"a.jsonl": 1234.0}

    def test_rubbish_degrades_to_now_rather_than_to_nothing(self, tmp_path):
        ledger = SeenLedger(tmp_path / "seen.json")
        changed = ledger.merge({"a.jsonl": "soon", "b.jsonl": -5, "c.jsonl": 0}, now=77.0)
        assert changed == {"a.jsonl": 77.0, "b.jsonl": 77.0, "c.jsonl": 77.0}

    def test_a_nameless_mark_is_ignored(self, tmp_path):
        ledger = SeenLedger(tmp_path / "seen.json")
        assert ledger.merge({"": 5.0, 7: 5.0}, now=100.0) == {}  # type: ignore[dict-item]


class TestStorage:
    def test_it_survives_a_restart(self, tmp_path):
        path = tmp_path / "seen.json"
        SeenLedger(path).merge({"a.jsonl": 1000.0}, now=2000.0)
        assert SeenLedger(path).stamp("a.jsonl") == 1000.0

    def test_an_unchanged_merge_does_not_rewrite_the_file(self, tmp_path):
        path = tmp_path / "seen.json"
        ledger = SeenLedger(path)
        ledger.merge({"a.jsonl": 1000.0}, now=2000.0)
        before = path.stat().st_mtime_ns
        ledger.merge({"a.jsonl": 900.0}, now=2000.0)
        assert path.stat().st_mtime_ns == before

    def test_a_missing_or_corrupt_ledger_is_not_an_error(self, tmp_path):
        # The clients hold the same map and re-offer it on connect, so the worst
        # case is one round of dots coming back — never a crashed server.
        assert SeenLedger(tmp_path / "nope.json").snapshot() == {}
        broken = tmp_path / "broken.json"
        broken.write_text("{{{")
        assert SeenLedger(broken).snapshot() == {}
        wrong = tmp_path / "wrong.json"
        wrong.write_text(json.dumps(["not", "a", "map"]))
        assert SeenLedger(wrong).snapshot() == {}

    def test_no_temporary_file_is_left_behind(self, tmp_path):
        path = tmp_path / "seen.json"
        SeenLedger(path).merge({"a.jsonl": 1.0}, now=2.0)
        assert [p.name for p in tmp_path.iterdir()] == ["seen.json"]

    def test_the_oldest_looks_are_dropped_first(self, tmp_path):
        # Capped like the client's map, and in the same order: neither side can
        # resurrect what the other has let go. A chat past the cap falls back to
        # the device's first-run floor, which reads as READ.
        path = tmp_path / "seen.json"
        ledger = SeenLedger(path)
        ledger.merge({f"s{i}.jsonl": float(i + 1) for i in range(SEEN_MAX + 50)}, now=1e9)
        held = ledger.snapshot()
        assert len(held) == SEEN_MAX
        assert "s0.jsonl" not in held
        assert f"s{SEEN_MAX + 49}.jsonl" in held
        assert len(SeenLedger(path).snapshot()) == SEEN_MAX
