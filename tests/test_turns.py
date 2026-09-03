"""The per-chat evidence store (#352): bytes beside their chat.

Content-addressed WITHIN a chat, never across chats; evicted a whole chat at a
time, oldest first, behind a dated tombstone; re-hashed on read. The store is
dumb — these tests never ask it what a blob means.
"""

import json
import os
import time

from aish import turns

SESSION = "session-20260101-000000-000000.jsonl"
OTHER = "session-20260202-000000-000000.jsonl"


class TestPerChatStore:
    def test_put_is_content_addressed_within_the_chat_and_get_round_trips(self, tmp_path):
        digest = turns.put("what the model was handed", tmp_path, SESSION)
        assert digest == turns.digest_of("what the model was handed")
        assert turns.get(digest, tmp_path, SESSION) == "what the model was handed"
        assert (turns.chat_dir(tmp_path, SESSION) / digest).is_file()

    def test_identical_content_is_stored_once_per_chat_and_not_shared_across_chats(
        self, tmp_path
    ):
        """The history a chat repeats across its calls is on disk once; a second
        chat quoting the same bytes gets its own copy, so a chat is a directory
        and nothing else — the erasability argument."""
        first = turns.put("same bytes", tmp_path, SESSION)
        second = turns.put("same bytes", tmp_path, SESSION)
        assert first == second
        assert len([p for p in turns.chat_dir(tmp_path, SESSION).iterdir() if p.is_file()]) == 1
        turns.put("same bytes", tmp_path, OTHER)
        assert (turns.chat_dir(tmp_path, OTHER) / first).is_file()
        assert turns.delete_chat(tmp_path, SESSION)
        assert turns.get(first, tmp_path, SESSION) is None
        assert turns.get(first, tmp_path, OTHER) == "same bytes"

    def test_the_chat_is_keyed_on_its_log_stem_whatever_form_the_name_takes(self, tmp_path):
        by_path = turns.put("x", tmp_path, tmp_path / SESSION)
        assert turns.get(by_path, tmp_path, SESSION) == "x"
        assert turns.get(by_path, tmp_path, SESSION[: -len(".jsonl")]) == "x"

    def test_a_tampered_blob_is_not_handed_back(self, tmp_path):
        digest = turns.put("original", tmp_path, SESSION)
        (turns.chat_dir(tmp_path, SESSION) / digest).write_text("substituted")
        assert turns.get(digest, tmp_path, SESSION) is None

    def test_undecodable_bytes_read_as_absent_rather_than_raising(self, tmp_path):
        digest = turns.put("original", tmp_path, SESSION)
        (turns.chat_dir(tmp_path, SESSION) / digest).write_bytes(b"\xff\xd8 not text")
        assert turns.get(digest, tmp_path, SESSION) is None

    def test_no_state_dir_or_no_session_still_yields_a_reference(self, tmp_path):
        """A caller with nowhere to write records a digest the reader reports
        as unresolvable — never as never recorded."""
        assert turns.put("orphan", None, SESSION) == turns.digest_of("orphan")
        assert turns.put("orphan", tmp_path, None) == turns.digest_of("orphan")
        assert not turns.store_dir(tmp_path).exists()
        assert turns.get(turns.digest_of("orphan"), tmp_path, SESSION) is None

    def test_put_survives_an_unwritable_store(self, tmp_path):
        blocker = tmp_path / "state"
        blocker.write_text("a file where the state dir should be")
        assert turns.put("x", blocker, SESSION) == turns.digest_of("x")

    def test_a_name_that_is_not_a_digest_never_becomes_a_path(self, tmp_path):
        """`get` is what an HTTP endpoint reaches, so it may build a path from
        64 hex characters and nothing else."""
        turns.put("x", tmp_path, SESSION)
        secret = tmp_path / "turns" / "elsewhere.txt"
        secret.write_text("not for serving")
        assert turns.get("../elsewhere.txt", tmp_path, SESSION) is None
        assert turns.get("", tmp_path, SESSION) is None
        assert turns.get("X" * 64, tmp_path, SESSION) is None

    def test_a_session_name_never_escapes_the_store(self, tmp_path):
        """A chat is keyed on its log's NAME, so a path with directories in it
        is reduced to that name and lands inside the store; a name that is
        not a chat at all writes nothing."""
        digest = turns.put("x", tmp_path, "../escape")
        assert digest == turns.digest_of("x")
        assert not (tmp_path / "escape").exists()
        assert (turns.store_dir(tmp_path) / "escape" / digest).is_file()
        assert turns.put("x", tmp_path, ".hidden") == turns.digest_of("x")
        assert not (turns.store_dir(tmp_path) / ".hidden").exists()
        assert turns.put("x", tmp_path, "..") == turns.digest_of("x")
        assert not (tmp_path / digest).exists()

    def test_unlink_removes_only_the_named_blobs(self, tmp_path):
        keep = turns.put("keep", tmp_path, SESSION)
        go = turns.put("go", tmp_path, SESSION)
        assert turns.unlink({go, "not-a-digest"}, tmp_path, SESSION) == 1
        assert turns.get(go, tmp_path, SESSION) is None
        assert turns.get(keep, tmp_path, SESSION) == "keep"
        assert turns.unlink({go}, tmp_path, SESSION) == 0  # already gone

    def test_delete_chat_removes_the_directory_tombstone_included(self, tmp_path):
        turns.put("x", tmp_path, SESSION)
        turns.sweep(tmp_path, budget=0)
        assert turns.evicted_on(tmp_path, SESSION)
        assert turns.delete_chat(tmp_path, SESSION) is True
        assert not turns.chat_dir(tmp_path, SESSION).exists()
        assert turns.evicted_on(tmp_path, SESSION) is None
        assert turns.delete_chat(tmp_path, SESSION) is False


def _age(state_dir, session, seconds_ago):
    then = time.time() - seconds_ago
    for blob in turns.chat_dir(state_dir, session).iterdir():
        os.utime(blob, (then, then))


class TestEviction:
    """Bounded by chat, never by step (owner, 2026-09-02)."""

    def test_nothing_goes_while_the_tree_fits_the_budget(self, tmp_path):
        turns.put("a" * 100, tmp_path, SESSION)
        turns.put("b" * 100, tmp_path, OTHER)
        assert turns.sweep(tmp_path, budget=10_000) == []
        assert turns.evicted_on(tmp_path, SESSION) is None

    def test_the_oldest_chat_by_activity_goes_first_and_goes_whole(self, tmp_path):
        old_a = turns.put("a" * 100, tmp_path, SESSION)
        old_b = turns.put("b" * 100, tmp_path, SESSION)
        new = turns.put("c" * 100, tmp_path, OTHER)
        _age(tmp_path, SESSION, 3600)
        # 300 bytes on disk, budget 150: the older chat's 200 must go entirely
        # and the newer chat's 100 must stay untouched.
        assert turns.sweep(tmp_path, budget=150) == [turns.chat_key(SESSION)]
        assert turns.get(old_a, tmp_path, SESSION) is None
        assert turns.get(old_b, tmp_path, SESSION) is None
        assert turns.get(new, tmp_path, OTHER) == "c" * 100
        assert turns.evicted_on(tmp_path, OTHER) is None

    def test_a_tombstone_dates_the_eviction_and_later_blobs_still_land(self, tmp_path):
        """The tombstone lives INSIDE the directory: a chat is reopened long
        after it was active, and a request recorded then needs somewhere to go
        without erasing the fact that the earlier ones were evicted."""
        gone = turns.put("earlier", tmp_path, SESSION)
        turns.sweep(tmp_path, budget=0)
        when = turns.evicted_on(tmp_path, SESSION)
        assert when and when[:4].isdigit()
        record = json.loads((turns.chat_dir(tmp_path, SESSION) / turns.TOMBSTONE).read_text())
        assert record["blobs"] == 1 and record["bytes"] == len("earlier")
        later = turns.put("later", tmp_path, SESSION)
        assert turns.get(later, tmp_path, SESSION) == "later"
        assert turns.get(gone, tmp_path, SESSION) is None
        assert turns.evicted_on(tmp_path, SESSION) == when

    def test_an_already_evicted_chat_is_not_a_candidate(self, tmp_path):
        turns.put("x", tmp_path, SESSION)
        turns.sweep(tmp_path, budget=0)
        first = turns.evicted_on(tmp_path, SESSION)
        turns.put("y" * 50, tmp_path, OTHER)
        assert turns.sweep(tmp_path, budget=0) == [turns.chat_key(OTHER)]
        assert turns.evicted_on(tmp_path, SESSION) == first  # not re-dated

    def test_usage_reports_the_filesystem_alone(self, tmp_path):
        turns.put("abc", tmp_path, SESSION)
        turns.put("de", tmp_path, SESSION)
        (usage,) = turns.usage(tmp_path)
        assert usage.key == turns.chat_key(SESSION)
        assert usage.bytes == 5 and usage.blobs == 2
        assert usage.last_activity > 0
        assert turns.usage(tmp_path / "nowhere") == []
        assert turns.sweep(None) == []

    def test_the_budget_is_a_named_constant_far_above_the_corpus(self):
        """2 GiB against a corpus that would hold ~150 MB had every call ever
        been recorded (measured 2026-09-03; the number is in the module)."""
        assert turns.TURNS_BUDGET_BYTES == 2 * 1024**3
        assert "150 MB" in turns.__doc__ + open(turns.__file__).read()
