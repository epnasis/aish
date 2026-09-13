"""The pin ledger: which chats the OWNER has pinned, shared by every device.

Every check here is one of the three properties that make sharing a TOGGLE
across devices safe — ONE CLOCK, THE SERVER'S (no client time is trusted, not
even clamped: under last-writer-wins a clamp would promote a stale toggle and
honouring a slow clock would discard the owner's newest one, so every accepted
toggle is stamped as it lands and a re-offer of what is held is free), A SEED
MAY ONLY INTRODUCE (the upgrade migration cannot resurrect a pin the owner
deliberately removed on another device), and FORGETTING DROPS TOMBSTONES FIRST
(the cap never silently unpins what the owner pinned).
"""

import json

from aish.pins import PINS_MAX, PinLedger


def pin(pinned, seed=False):
    mark = {"pinned": pinned}
    if seed:
        mark["seed"] = True
    return mark


class TestOneClock:
    def test_a_pin_is_recorded_stamped_by_the_server(self, tmp_path):
        ledger = PinLedger(tmp_path / "pins.json")
        changed = ledger.merge({"a.jsonl": pin(True)}, now=1000.0)
        assert changed == {"a.jsonl": {"pinned": True, "at": 1000.0}}
        assert ledger.state("a.jsonl") is True

    def test_a_client_supplied_time_is_ignored(self, tmp_path):
        # The wire carries no time on purpose; one smuggled in changes nothing.
        # Clamping it (the seen ledger's move) would be WRONG here: a max-merge
        # has a safe direction for a clock mistake, last-writer-wins has none.
        ledger = PinLedger(tmp_path / "pins.json")
        changed = ledger.merge({"a.jsonl": {"pinned": True, "at": 9999.0}}, now=1000.0)
        assert changed == {"a.jsonl": {"pinned": True, "at": 1000.0}}

    def test_a_differing_offer_always_lands(self, tmp_path):
        # Last-RECONNECT-wins: whichever device speaks latest sets the state.
        ledger = PinLedger(tmp_path / "pins.json")
        ledger.merge({"a.jsonl": pin(True)}, now=1000.0)
        ledger.merge({"a.jsonl": pin(False)}, now=2000.0)
        assert ledger.state("a.jsonl") is False
        ledger.merge({"a.jsonl": pin(True)}, now=3000.0)
        assert ledger.state("a.jsonl") is True

    def test_re_offering_what_is_held_changes_nothing(self, tmp_path):
        # What makes re-offering unconfirmed toggles on every connect free —
        # and therefore what makes a lost broadcast self-healing without a
        # receipt: an offer that agrees is skipped, published to nobody.
        ledger = PinLedger(tmp_path / "pins.json")
        ledger.merge({"a.jsonl": pin(True)}, now=1000.0)
        assert ledger.merge({"a.jsonl": pin(True)}, now=2000.0) == {}
        assert ledger.snapshot()["a.jsonl"]["at"] == 1000.0

    def test_a_garbled_mark_is_ignored(self, tmp_path):
        ledger = PinLedger(tmp_path / "pins.json")
        assert ledger.merge({"": pin(True), "b.jsonl": "yes", 3: pin(True)}, now=1000.0) == {}


class TestSeedsOnlyIntroduce:
    def test_a_seed_lands_on_an_unknown_name(self, tmp_path):
        # The upgrade migration: this device's pre-sync local pin becomes the
        # shared one, from whichever device connects first.
        ledger = PinLedger(tmp_path / "pins.json")
        changed = ledger.merge({"a.jsonl": pin(True, seed=True)}, now=1000.0)
        assert changed == {"a.jsonl": {"pinned": True, "at": 1000.0}}

    def test_a_seed_never_overturns_a_recorded_action(self, tmp_path):
        # A device offline across the upgrade comes back holding a local pin
        # the owner has since removed on another device. Its seed must lose —
        # offered as an ordinary mark it would count as a fresh action and win.
        ledger = PinLedger(tmp_path / "pins.json")
        ledger.merge({"a.jsonl": pin(True, seed=True)}, now=1000.0)
        ledger.merge({"a.jsonl": pin(False)}, now=2000.0)
        assert ledger.merge({"a.jsonl": pin(True, seed=True)}, now=3000.0) == {}
        assert ledger.state("a.jsonl") is False


class TestForgettingDropsTombstonesFirst:
    def test_the_oldest_tombstones_are_dropped_first(self, tmp_path):
        ledger = PinLedger(tmp_path / "pins.json")
        for i in range(PINS_MAX):
            ledger.merge({f"s{i}.jsonl": pin(False)}, now=float(i + 1))
        ledger.merge({"kept.jsonl": pin(True)}, now=10_000.0)
        assert ledger.state("kept.jsonl") is True
        held = set(ledger.snapshot())
        assert "s0.jsonl" not in held  # the oldest tombstone paid for the pin
        assert f"s{PINS_MAX - 1}.jsonl" in held

    def test_a_pin_is_never_silently_dropped(self, tmp_path):
        ledger = PinLedger(tmp_path / "pins.json")
        marks = {f"s{i}.jsonl": pin(True) for i in range(PINS_MAX + 10)}
        ledger.merge(marks, now=100_000.0)
        # All pins, nothing evictable: the ledger runs long rather than
        # breaking the promise an entry records.
        assert len(ledger.snapshot()) == PINS_MAX + 10


class TestStorage:
    def test_it_survives_a_restart(self, tmp_path):
        path = tmp_path / "pins.json"
        PinLedger(path).merge({"a.jsonl": pin(True)}, now=1000.0)
        assert PinLedger(path).state("a.jsonl") is True

    def test_an_unchanged_merge_does_not_rewrite_the_file(self, tmp_path):
        path = tmp_path / "pins.json"
        ledger = PinLedger(path)
        ledger.merge({"a.jsonl": pin(True)}, now=1000.0)
        before = path.stat().st_mtime_ns
        ledger.merge({"a.jsonl": pin(True)}, now=2000.0)
        assert path.stat().st_mtime_ns == before

    def test_a_missing_or_corrupt_ledger_is_not_an_error(self, tmp_path):
        path = tmp_path / "pins.json"
        assert PinLedger(path).snapshot() == {}
        path.write_text("not json")
        assert PinLedger(path).snapshot() == {}
        path.write_text(json.dumps({"pins": {"a.jsonl": {"pinned": True, "at": "bad"}}}))
        assert PinLedger(path).snapshot() == {}

    def test_no_temporary_file_is_left_behind(self, tmp_path):
        path = tmp_path / "pins.json"
        PinLedger(path).merge({"a.jsonl": pin(True)}, now=1000.0)
        assert [p.name for p in tmp_path.iterdir()] == ["pins.json"]

    def test_a_forgotten_chat_leaves_the_ledger(self, tmp_path):
        # Deleted chat: gone is gone, pin included ([MIRROR-FORGET]). Session
        # names are never reused, so nothing can inherit the entry.
        path = tmp_path / "pins.json"
        ledger = PinLedger(path)
        ledger.merge({"a.jsonl": pin(True)}, now=1000.0)
        ledger.forget("a.jsonl")
        assert ledger.snapshot() == {}
        assert PinLedger(path).snapshot() == {}
