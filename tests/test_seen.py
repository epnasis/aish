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
        # resurrect what the other has let go. What SPEAKS for a dropped look is
        # the floor below, not the chat's own stamp.
        path = tmp_path / "seen.json"
        ledger = SeenLedger(path)
        ledger.merge({f"s{i}.jsonl": float(i + 1) for i in range(SEEN_MAX + 50)}, now=1e9)
        held = ledger.snapshot()
        assert len(held) == SEEN_MAX
        assert "s0.jsonl" not in held
        assert f"s{SEEN_MAX + 49}.jsonl" in held
        assert len(SeenLedger(path).snapshot()) == SEEN_MAX


class TestForgettingSaysSo:
    """A dropped look must not read as "never read" (#378).

    The cap is what the owner actually hit — 300 stamps against 851 chats — and
    every eviction silently turned an old, read chat back into an unread one,
    because the only evidence it had been read was the stamp being thrown away.
    `floor` is what the ledger says INSTEAD: the newest look it has forgotten.
    Everything here is about that claim staying honest.
    """

    def test_a_fresh_ledger_claims_nothing(self, tmp_path):
        # Zero is not "1970": it is the floor asserting nothing at all, which is
        # the only truthful answer while nothing has been forgotten.
        assert SeenLedger(tmp_path / "seen.json").floor() == 0.0

    def test_forgetting_a_look_raises_the_floor_to_it(self, tmp_path):
        # The NEWEST dropped look, which is the most this can honestly claim:
        # every forgotten look happened at or before it.
        ledger = SeenLedger(tmp_path / "seen.json")
        ledger.merge({f"s{i}.jsonl": float(i + 1) for i in range(SEEN_MAX + 50)}, now=1e9)
        assert ledger.floor() == 50.0  # s0..s49 dropped; s49's stamp is 50.0

    def test_the_floor_survives_a_restart(self, tmp_path):
        # Without this the very first reconnect after a restart re-opens the
        # whole hole: the stamps are gone from disk and nothing speaks for them.
        path = tmp_path / "seen.json"
        ledger = SeenLedger(path)
        ledger.merge({f"s{i}.jsonl": float(i + 1) for i in range(SEEN_MAX + 50)}, now=1e9)
        assert SeenLedger(path).floor() == ledger.floor() == 50.0

    def test_the_floor_never_walks_backwards(self, tmp_path):
        # Monotonic for the same reason the stamps are: a floor that fell would
        # un-read whatever it passed on the way down. Here a ledger that has
        # already forgotten as far as 1000 drops a batch of much older looks —
        # a device that was offline for a month coming back is exactly this.
        path = tmp_path / "seen.json"
        path.write_text(json.dumps({
            "floor": 1000.0,
            "seen": {f"s{i}.jsonl": float(i + 1) for i in range(SEEN_MAX + 50)},
        }))
        assert SeenLedger(path).floor() == 1000.0

    def test_a_look_below_the_floor_is_not_taken_back(self, tmp_path):
        # A device re-offering its whole map on connect will keep offering the
        # ones the ledger dropped. Taking them would evict them again on the
        # next trim and broadcast a stamp with a lifetime of microseconds; the
        # floor already covers them, so nothing changed is the truth.
        ledger = SeenLedger(tmp_path / "seen.json")
        ledger.merge({f"s{i}.jsonl": float(i + 1) for i in range(SEEN_MAX + 50)}, now=1e9)
        assert ledger.merge({"s0.jsonl": 1.0}, now=1e9) == {}
        assert "s0.jsonl" not in ledger.snapshot()

    def test_a_held_chat_is_still_updated_below_the_floor(self, tmp_path):
        # The guard is about names the ledger has FORGOTTEN. A chat it still
        # holds keeps taking marks normally, floor or no floor.
        ledger = SeenLedger(tmp_path / "seen.json")
        ledger.merge({f"s{i}.jsonl": float(i + 1) for i in range(SEEN_MAX + 50)}, now=1e9)
        held = f"s{SEEN_MAX + 49}.jsonl"
        assert ledger.merge({held: 40.0}, now=1e9) == {}  # older than its own stamp
        assert ledger.merge({held: 1e8}, now=1e9) == {held: 1e8}

    def test_a_ledger_written_before_the_floor_gets_one_on_load(self, tmp_path):
        # The upgrade seam, and the reason it is not optional: the file on the
        # owner's machine had 300 stamps and no floor, so without this the fix
        # would ship INERT — the floor would stay 0 until the next eviction and
        # the deploy carrying it would change nothing anybody could see.
        path = tmp_path / "seen.json"
        path.write_text(json.dumps({
            "seen": {f"s{i}.jsonl": float(i + 1) for i in range(SEEN_MAX)},
        }))
        assert SeenLedger(path).floor() == 1.0  # its oldest surviving look

    def test_a_ledger_below_the_cap_claims_nothing_on_load(self, tmp_path):
        # It has never dropped anything, so there is nothing to speak for. A
        # floor invented here would mark chats read on no evidence at all.
        path = tmp_path / "seen.json"
        path.write_text(json.dumps({"seen": {"a.jsonl": 5.0, "b.jsonl": 9.0}}))
        assert SeenLedger(path).floor() == 0.0

    def test_a_new_look_still_lands_after_forgetting(self, tmp_path):
        # The floor must not become a wall: reading a chat now is newer than
        # anything forgotten, so it is recorded and published as usual.
        ledger = SeenLedger(tmp_path / "seen.json")
        ledger.merge({f"s{i}.jsonl": float(i + 1) for i in range(SEEN_MAX + 50)}, now=1e9)
        assert ledger.merge({"s0.jsonl": None}, now=1e9) == {"s0.jsonl": 1e9}
