"""Counters on the word lists nobody was counting (#322).

The failure this file exists for: a list that has silently stopped matching is
indistinguishable from a page that had nothing to match. `_CONSENT_SELECTORS`
missed a real banner by one letter, the sign-in under it failed for days, and
nothing anywhere said a list had found nothing.

**Two things are pinned here that are easy to skip.** First, that the counting
changed NO matching — asserted mechanically against the previous commit's source
rather than by reading, because this slice's whole value is a before-picture.
Second, that every counter's ZERO is reachable and means something: a check that
cannot fail is documentation wearing a test's clothes (`docs/roles.md`), and a
counter that can only go up is exactly that shape.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

from aish import agent as agent_module
from aish import approval, browse, browser, provenance, usage, vocab, web

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_tallies():
    """Every test starts with nothing tallied. The registry is process-global by
    design (a browser thread and the loop thread share it), so a test that
    inherited another's consultations would assert on somebody else's number."""
    vocab.reset()
    yield
    vocab.reset()


# ---------------------------------------------------------------------------


class TestTheCountingChangedNoMatching:
    """The scope fence, checked by machine.

    "Not one word added, removed or reordered" is a claim about ~28 lists in
    seven files, and a human reading a diff is exactly the instrument that
    misses one letter — which is the bug that started this. So it is compared
    against what git actually holds.
    """

    FILES = (
        "aish/browse.py",
        "aish/browser.py",
        "aish/signin.py",
        "aish/provenance.py",
        "aish/web.py",
        "aish/approval.py",
        "aish/agent.py",
    )

    @staticmethod
    def _lists(source: str) -> dict[str, list]:
        """Every module-level string collection, with `vocab.declare` unwrapped
        so a wrapped list compares against its own unwrapped ancestor."""
        out: dict[str, list] = {}
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            value = node.value
            if not names or value is None:
                continue
            if isinstance(value, ast.Call) and getattr(value.func, "attr", "") == "declare":
                value = next(
                    (kw.value for kw in value.keywords if kw.arg == "entries"), None
                )
            if isinstance(value, ast.Call) and getattr(value.func, "id", "") in (
                "frozenset",
                "set",
                "tuple",
            ):
                value = value.args[0] if value.args else None
            if isinstance(value, (ast.Tuple, ast.List, ast.Set)) and value.elts:
                if all(isinstance(e, ast.Constant) for e in value.elts):
                    out[names[0]] = [e.value for e in value.elts]
                elif all(
                    isinstance(e, ast.Tuple)
                    and all(isinstance(c, ast.Constant) for c in e.elts)
                    for e in value.elts
                ):
                    out[names[0]] = [tuple(c.value for c in e.elts) for e in value.elts]
            elif isinstance(value, ast.Dict) and value.keys:
                if all(isinstance(k, ast.Constant) for k in value.keys):
                    out[names[0]] = [k.value for k in value.keys]
        return out

    #: Lists deliberately changed SINCE the before-picture was taken, each with
    #: the issue that changed it and the entries it gained or lost. The fence
    #: is not "no list may ever change" — `docs/vocabularies.md` says a later
    #: change is *judged against* these numbers — it is "no list changes
    #: without a reader being told". An undeclared drift still fails; a
    #: declared one has to say what it did, here, in the diff that did it.
    DELIBERATE = {
        # #341: not a word list at all — aish's own tool NAMES, matched against
        # the tool the model called and never against page text or a label.
        # `browse` joined it because the identical page drew a card through
        # read_url and nothing through browse: the model's choice of tool was
        # deciding a permission.
        "aish/agent.py:EGRESS_TOOLS": {"added": ["browse"], "removed": []},
    }

    @staticmethod
    def _matches(before: list, after: list, declared: dict) -> bool:
        """Is `after` exactly `before` plus/minus what the entry DECLARED?

        Exactly, and never "at least": a declaration that waved through any
        change to a list it happened to name would be an exemption rather than
        a record, and the fence exists because a human reading a diff is the
        instrument that misses one letter."""
        expected = [e for e in before if e not in declared["removed"]] + list(
            declared["added"]
        )
        return after == expected

    def test_not_one_word_was_added_removed_or_reordered(self):
        """Against the commit BEFORE this work, so the comparison is with what
        shipped and not with the diff's own starting point."""
        base = subprocess.run(
            ["git", "log", "--format=%H", "-1", "--", "aish/vocab.py"],
            cwd=REPO, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert base, "aish/vocab.py has no commit — cannot establish a baseline"
        drifted = {}
        for name in self.FILES:
            before = subprocess.run(
                ["git", "show", f"{base}~1:{name}"],
                cwd=REPO, capture_output=True, text=True,
            )
            if before.returncode:
                continue  # a file that did not exist then
            old = self._lists(before.stdout)
            new = self._lists((REPO / name).read_text())
            for key, entries in old.items():
                if key not in new or new[key] == entries:
                    continue
                where = f"{name}:{key}"
                declared = self.DELIBERATE.get(where)
                if declared is not None and self._matches(entries, new[key], declared):
                    continue
                drifted[where] = (entries, new[key])
        assert not drifted, (
            "instrumenting a list CHANGED it, which destroys the before-picture "
            f"this slice exists to take: {drifted}"
        )

    def test_declare_hands_back_the_same_object_not_a_copy(self):
        """A frozenset that came back a tuple would turn every membership test
        in `approval.py` from O(1) into a scan, and would break the set algebra
        `curate.STRUCTURAL_BIND_TRIGGERS` does on one of these."""
        original = frozenset({"a", "b"})
        assert vocab.declare(
            "test.identity", original, languages="test", on_miss=vocab.FRICTION
        ) is original
        assert isinstance(approval.SAFE_COMMANDS, frozenset)
        assert isinstance(browse._MUTATING_WORDS, tuple)


class TestWhatACounterCounts:
    def test_a_consultation_that_matched_and_one_that_did_not(self):
        assert browse.is_worded("Zapłać teraz") is True
        assert browse.is_worded("Więcej informacji") is False
        counted = vocab.drain()["browse._MUTATING_WORDS"]
        assert counted == {"asked": 2, "matched": 1}

    def test_a_list_nobody_asked_is_absent_and_never_a_row_of_zeros(self):
        browse.is_worded("Zapłać")
        drained = vocab.drain()
        assert "browse._MUTATING_WORDS" in drained
        # Never consulted in this drain — and therefore not in it at all.
        assert "provenance._SIGN_IN_PHRASES" not in drained

    def test_draining_resets_so_a_task_reports_its_own_consultations(self):
        browse.is_worded("kup")
        assert vocab.drain()
        assert vocab.drain() == {}

    def test_a_candidate_count_is_absent_where_the_call_site_cannot_say(self):
        """Absence, never 0: a mean taken over a zero for every silent call site
        would be a number the log cannot support."""
        browse.is_worded("Zapłać")
        assert "candidates" not in vocab.drain()["browse._MUTATING_WORDS"]

    def test_a_candidate_count_is_recorded_where_it_is_free(self):
        # "Potwierdź" matches one mutating word and it is a chrome word, so the
        # demotion is choosing among exactly one candidate.
        browse.says_it_commits("Potwierdź", in_widget=True)
        counted = vocab.drain()["browse.CHROME_WORDS"]
        assert counted["candidates_asked"] == 1
        assert counted["candidates"] >= 1


class TestEveryCountersZeroIsReachable:
    """A check that cannot fail is documentation wearing a test's clothes.

    Each counter here is driven to zero-matched by a real consultation through
    the real call site, so "matched: 0" is proven to be a state the code can
    actually be in — not merely a state the dataclass allows.
    """

    def test_the_consent_list_records_an_obstruction_it_could_not_clear(self):
        browse.CONSENT_TALLY.note(dismissed=False)
        assert vocab.drain()["browser._CONSENT_SELECTORS"] == {"asked": 1, "matched": 0}

    def test_the_challenge_markers_record_a_page_with_no_marker_in_it(self):
        assert browser.is_challenge("an ordinary short page", 200) is False
        assert vocab.drain()["browser._CHALLENGE_MARKERS"] == {"asked": 1, "matched": 0}

    def test_the_mail_phrases_record_a_message_that_is_not_a_sign_in(self):
        provenance.looks_like_sign_in_mail("your parcel is on its way")
        assert vocab.drain()["provenance._SIGN_IN_PHRASES"] == {"asked": 1, "matched": 0}

    def test_the_safe_command_list_records_a_command_it_does_not_hold(self):
        approval.is_read_only("tar -xzf thing.tgz")
        counted = vocab.drain()["approval.SAFE_COMMANDS"]
        assert counted["matched"] == 0 and counted["asked"] >= 1

    def test_the_destructive_list_records_a_command_it_did_not_flag(self):
        assert approval.looks_destructive("ls -la") is False
        assert vocab.drain()["approval._DESTRUCTIVE_COMMANDS"]["matched"] == 0

    def test_the_destructive_list_is_not_credited_with_sudos_catches(self):
        """`sudo` and `--force` mark a command destructive without this list
        matching anything. Folding them in would report a list that had stopped
        matching as one still working."""
        assert approval.looks_destructive("sudo something") is True
        assert vocab.drain()["approval._DESTRUCTIVE_COMMANDS"]["matched"] == 0

    def test_the_availability_map_records_a_state_it_has_no_phrase_for(self):
        web.page_facts(
            [json.dumps({
                "@type": "Product", "name": "a thing",
                "offers": {"@type": "Offer", "price": "10", "priceCurrency": "PLN",
                           "availability": "https://schema.org/Rumoured"},
            })],
            "10 PLN",
            "https://shop.example/thing",
        )
        assert vocab.drain()["web._AVAILABILITY"]["matched"] == 0

    def test_the_download_words_record_a_press_that_named_no_file(self):
        assert browse.wants_download("Więcej", "") is False
        drained = vocab.drain()
        assert drained["browse._DOWNLOAD_WORDS"]["matched"] == 0
        assert drained["browse._DOWNLOAD_SUFFIXES"]["matched"] == 0

    def test_the_month_arrows_record_a_control_that_is_not_one(self):
        assert browse.month_step("Next offer in this carousel", forward=True) is False
        assert vocab.drain()["browse._FORWARD"] == {"asked": 1, "matched": 0}

    def test_the_irreversible_family_records_an_ordinary_label(self):
        assert browse.irreversible("Faktury i płatności") == ""
        drained = vocab.drain()
        # Every list the function reached, and only those: the short-circuit is
        # preserved, so a label that stopped at the first test counts one list.
        assert drained["browse._CLOSE_ACCOUNT_PHRASES"] == {"asked": 1, "matched": 0}
        assert drained["browse._CHANGE_VERBS"] == {"asked": 1, "matched": 0}

    def test_a_short_circuit_means_the_later_lists_were_never_ASKED(self):
        """`asked` is consultations, not opportunities. A label caught by the
        first test leaves the rest absent — which is the honest reading, and it
        is why `never_consulted` can mean anything."""
        assert browse.irreversible("Usun konto") == "account"
        drained = vocab.drain()
        assert drained["browse._CLOSE_ACCOUNT_PHRASES"] == {"asked": 1, "matched": 1}
        assert "browse._CREDENTIAL_PHRASES" not in drained


class TestTheRecordAndTheScan:
    def test_the_scan_sums_per_list_across_records(self):
        records = [
            {"step": {"kind": "vocab", "lists": {"a": {"asked": 3, "matched": 1}}}},
            {"step": {"kind": "vocab", "lists": {"a": {"asked": 2, "matched": 0},
                                                 "b": {"asked": 1, "matched": 1}}}},
            {"step": {"kind": "tool", "name": "run_command"}},
        ]
        counters = vocab.scan_counters(records)
        assert (counters["a"].asked, counters["a"].matched, counters["a"].records) == (5, 1, 2)
        assert counters["b"].asked == 1

    def test_a_log_written_before_this_shipped_reads_as_no_consultations(self):
        """Not as a list that behaved well. It reports what was RECORDED."""
        assert vocab.scan_counters([{"step": {"kind": "tool"}}]) == {}

    def test_mean_candidates_is_None_and_never_zero_when_nobody_reported_one(self):
        counters = vocab.scan_counters(
            [{"step": {"kind": "vocab", "lists": {"a": {"asked": 4, "matched": 2}}}}]
        )
        assert counters["a"].mean_candidates is None


class TestTheAnomalyFloor:
    """"Anomalous" is derived from the window's own data, never chosen.

    The test has exactly one number in it: at the rarest rate any working list
    in this window achieved, this list would have been expected to match at
    least once, and it matched zero.
    """

    @staticmethod
    def _counters(**spec):
        return {
            name: vocab.Counters(vocabulary=name, asked=a, matched=m)
            for name, (a, m) in spec.items()
        }

    def test_a_list_asked_two_hundred_times_and_matching_nothing_is_flagged(self):
        counters = self._counters(working=(400, 20), silent=(200, 0))
        assert [c.vocabulary for c in vocab.quiet(counters)] == ["silent"]
        assert "silent (200 asked)" in vocab.summary_line(counters)

    def test_a_list_asked_too_few_times_to_expect_a_match_is_not_flagged(self):
        """The floor is 5%, so 10 consultations expect 0.5 matches. Reporting
        that as an anomaly is the over-vocal reporting the owner has twice
        rejected — a warning that fires on ordinary browsing teaches him to stop
        reading it."""
        counters = self._counters(working=(400, 20), quietish=(10, 0))
        assert vocab.quiet(counters) == []
        assert vocab.summary_line(counters) == ""

    def test_nothing_is_flagged_when_no_floor_can_be_derived(self):
        """A window in which nothing ever matched cannot say which list is
        broken — it is as likely to be a window with no browsing in it."""
        counters = self._counters(one=(500, 0), two=(500, 0))
        assert vocab.floor(counters) is None
        assert vocab.quiet(counters) == []
        assert vocab.summary_line(counters) == ""

    def test_the_loudest_missing_expectation_is_named_first(self):
        counters = self._counters(working=(1000, 100), small=(20, 0), huge=(900, 0))
        assert [c.vocabulary for c in vocab.quiet(counters)] == ["huge", "small"]

    def test_a_barely_consulted_list_never_sets_the_bar_for_a_busy_one(self):
        """The over-flagging guard, and it is the rule that matters most here: a
        warning that fires on ordinary browsing teaches him to stop reading it.
        A list asked twice that happened to match once has no business judging
        one asked four hundred times, so each candidate's floor is taken over
        lists observed at least as often as IT was."""
        counters = self._counters(lucky=(2, 1), busy=(400, 0))
        assert vocab.floor(counters) == 0.5             # the window's rarest rate
        assert vocab.floor(counters, over=400) is None  # ...but nothing comparable
        assert vocab.quiet(counters) == []
        assert vocab.summary_line(counters) == ""

    def test_the_most_consulted_list_has_nothing_to_be_judged_against(self):
        """An absent comparison, never a free pass invented to fill it."""
        counters = self._counters(working=(50, 5), biggest=(9000, 0))
        assert vocab.expected_at_floor(counters, counters["biggest"]) is None
        assert vocab.quiet(counters) == []

    def test_the_summary_line_is_silent_when_nothing_is_anomalous(self):
        """Recorded always, surfaced on anomaly. A row that appears on every
        ordinary browse is the noise that hides the one that matters."""
        assert vocab.summary_line(self._counters(a=(100, 40), b=(80, 3))) == ""


class TestAbsenceIsNotZero:
    def test_a_declared_list_with_no_record_reads_as_not_consulted(self):
        absent = {v.name for v in vocab.never_consulted({})}
        assert "browse._MUTATING_WORDS" in absent

    def test_an_uncounted_list_is_never_reported_as_not_consulted(self):
        """It IS consulted — constantly — and simply not counted. Reporting it
        as "not consulted" would be the confident lie this module exists to
        stop, so `counted=False` routes it to its own heading instead."""
        absent = {v.name for v in vocab.never_consulted({})}
        assert "signin._SECOND_LEVEL" not in absent
        assert "signin._SECOND_LEVEL" in {v.name for v in vocab.not_counted()}

    def test_the_json_report_omits_the_floor_rather_than_calling_it_zero(self):
        report = vocab.json_report(
            {"a": vocab.Counters(vocabulary="a", asked=9, matched=0)}, 30
        )
        assert "floor_rate" not in report
        assert report["lists"]["a"]["matched"] == 0

    def test_the_render_says_what_an_empty_window_is_not(self):
        said = vocab.render({}, 30)
        assert "no chat in this window recorded a word-list consultation" in said
        assert "carries no `vocab` record at all" in said


class TestTheCatalogueIsTheInventory:
    """A list added to the code and not to the catalogue is a list that goes
    back to being uncounted silently, which is the whole defect."""

    #: Module-level string collections in the browsing path that are deliberately
    #: NOT vocabularies in #322's sense. Each carries why — an exclusion list
    #: without reasons rots into a list of things somebody gave up on.
    NOT_A_VOCABULARY = {
        "browser._OFFSCREEN_ARGS": "Chrome command-line flags",
        "browser._LOGIN_ARGS": "Chrome command-line flags",
        "browser._STEALTH_ARGS": "Chrome command-line flags",
        "browser._STEALTH_OMIT": "Chrome command-line flags",
        "browser._LOCK_FILES": "file names in aish's own profile directory",
        "browser._BODY_METHODS": "HTTP method names",
        "browse.CHECK": "a single string constant, not a collection",
        "signin._SUBMIT_METHODS": "HTTP method names",
        "signin._REFUSED_STATUS": "HTTP status codes",
        "browser.BLOCK_STATUS": "HTTP status codes — the STRUCTURAL half",
        "browse.QUERY_METHOD": "one HTTP method name",
    }

    @staticmethod
    def _counted_names() -> set[str]:
        """Every literal name a counting call site passes, swept from source.

        AST and not a grep, because one call site chooses its name with a
        conditional expression (`month_step`), which a string search over the
        file cannot see — and a check that silently misses a call site is the
        shape of the bug this whole module is about."""
        used: set[str] = set()
        for path in sorted((REPO / "aish").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                counting = (
                    getattr(node.func, "attr", "") in ("hit", "note", "looked_up")
                    and isinstance(getattr(node.func, "value", None), ast.Name)
                    and node.func.value.id == "vocab"
                )
                # `browse._has` is the one INDIRECT counting site: seven of the
                # irreversible lists are consulted through it, and it exists to
                # keep that boolean readable. Named here rather than inlined at
                # the seven call sites, because a sweep that cannot follow it
                # would report ten counted lists as uncounted — the exact
                # false alarm that teaches a reader to ignore the check.
                if not (counting or getattr(node.func, "id", "") == "_has"):
                    continue
                # EVERY argument, not the first: `_has` takes the name third,
                # and `month_step` builds it with a conditional expression.
                # Filtered to the `module.NAME` shape so an unrelated literal
                # passed to one of these cannot enter the set — a typo still
                # has that shape and still fails the catalogue check below.
                for arg in node.args:
                    for leaf in ast.walk(arg):
                        if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str):
                            if re.fullmatch(r"[a-z_]+\.[A-Za-z_][A-Za-z_0-9]*", leaf.value):
                                used.add(leaf.value)
        return used

    def test_every_name_passed_to_a_counter_is_in_the_catalogue(self):
        """A typo in a name string would create a second, silent bucket that
        nothing ever reads."""
        used = self._counted_names()
        assert used, "no counted call sites found — the sweep itself is broken"
        assert used <= set(vocab.CATALOGUE), (
            f"counted under names nothing declares: {sorted(used - set(vocab.CATALOGUE))}"
        )

    def test_every_counted_list_actually_has_a_call_site(self):
        """The other direction: a catalogue entry with nothing writing to it
        would read as "not consulted" for ever, which is a lie with the same
        shape as the bug."""
        orphans = sorted(
            name for name, v in vocab.CATALOGUE.items()
            if v.counted and not name.startswith("test.") and name not in self._counted_names()
        )
        assert not orphans, f"declared as counted, nothing counts them: {orphans}"

    def test_the_recorded_size_matches_the_real_list(self):
        assert vocab.CATALOGUE["browse._MUTATING_WORDS"].size == len(browse._MUTATING_WORDS)
        assert vocab.CATALOGUE["approval.SAFE_COMMANDS"].size == len(approval.SAFE_COMMANDS)

    def test_every_entry_declares_what_a_miss_costs(self):
        assert all(v.on_miss in vocab.VERDICTS for v in vocab.CATALOGUE.values())

    def test_the_verdict_is_never_printed_as_a_measurement(self):
        said = vocab.render(
            {"browse._MUTATING_WORDS": vocab.Counters(
                vocabulary="browse._MUTATING_WORDS", asked=9, matched=2)},
            30,
        )
        assert "asked/matched are MEASURED" in said
        assert "engineer's verdict" in said


class TestTheRecordReachesTheLog:
    def test_a_task_that_consulted_nothing_writes_no_record(self):
        """"No list was consulted" and "a list was consulted and matched
        nothing" are different facts. A record of zeros would say a set of
        controls had been asked when none was."""
        written: list[dict] = []
        agent = agent_module.Agent(
            model="fake", approve=lambda *a, **k: None,
            client_chat=lambda **k: None, step_log=written.append,
        )
        agent._flush_vocab()
        assert written == []

    def test_a_task_that_consulted_something_writes_one_record_for_all_of_it(self):
        """One record per task, not per consultation: a page of sixty controls
        asks these lists hundreds of times, and the counters are sums either
        way."""
        written: list[dict] = []
        agent = agent_module.Agent(
            model="fake", approve=lambda *a, **k: None,
            client_chat=lambda **k: None, step_log=written.append,
        )
        browse.is_worded("Zapłać")
        browse.is_worded("Więcej")
        provenance.looks_like_sign_in_mail("reset your password")
        agent._flush_vocab()
        assert len(written) == 1
        assert written[0]["kind"] == "vocab"
        assert written[0]["lists"]["browse._MUTATING_WORDS"] == {"asked": 2, "matched": 1}
        assert written[0]["lists"]["provenance._SIGN_IN_PHRASES"]["matched"] == 1

    def test_the_kind_is_renderless_so_it_cannot_open_an_empty_trace_card(self):
        from aish import session as session_module

        assert "vocab" in session_module.RENDERLESS_STEPS

    def test_a_failed_task_still_records_what_it_asked(self):
        """The flush is a `finally` around the loop, not a line after it: that
        method returns from a dozen places and a flush on any subset of them
        would silently lose the rest."""
        source = Path(agent_module.__file__).read_text()
        wrapper = source.split("    def run_task(")[1].split("\n    def ")[0]
        assert "finally:" in wrapper and "self._flush_vocab()" in wrapper

    def test_claude_max_flushes_too_because_it_never_enters_the_loop(self):
        from aish import claude_max

        source = Path(claude_max.__file__).read_text()
        block = source.split("    def run_task(")[1].split("\n    def ")[0]
        assert "finally:" in block and "_flush_vocab()" in block


class TestTheReader:
    def test_usage_stays_silent_about_word_lists_on_an_ordinary_window(self):
        """`aish usage` is a SPEND report. The pointer appears only when
        something is anomalous — otherwise it is a row he learns to skip."""
        session = usage.SessionUsage(name="s", path=Path("/tmp/s"))
        session.vocab_calls = [
            {"kind": "vocab", "lists": {"a": {"asked": 50, "matched": 20}}}
        ]
        assert not any("word list" in line for line in usage._caveats([session]))

    def test_usage_points_at_the_table_when_a_list_has_gone_quiet(self):
        session = usage.SessionUsage(name="s", path=Path("/tmp/s"))
        session.vocab_calls = [
            {"kind": "vocab", "lists": {"a": {"asked": 900, "matched": 90},
                                        "b": {"asked": 400, "matched": 0}}}
        ]
        said = " ".join(usage._caveats([session]))
        assert "b (400 asked)" in said and "aish vocab" in said

    def test_the_table_marks_the_quiet_list_and_says_what_a_miss_costs(self):
        said = vocab.render(
            {
                "browse._MUTATING_WORDS": vocab.Counters(
                    vocabulary="browse._MUTATING_WORDS", asked=900, matched=90),
                "browser._CONSENT_SELECTORS": vocab.Counters(
                    vocabulary="browser._CONSENT_SELECTORS", asked=400, matched=0),
            },
            30,
        )
        assert "⚑" in said
        assert "a miss silently breaks a feature" in said
        assert "a miss PERMITS something" in said
        assert "This is a pointer, not a verdict" in said

    def test_the_subcommand_runs_over_a_directory_with_no_logs(self, tmp_path, capsys):
        import os

        from aish import cli

        os.environ["AISH_STATE_DIR"] = str(tmp_path)
        try:
            assert cli._vocab_cli([]) == 0
            assert "word lists" in capsys.readouterr().out
        finally:
            os.environ.pop("AISH_STATE_DIR", None)

    def test_the_subcommand_rejects_an_argument_it_does_not_understand(self, capsys):
        from aish import cli

        assert cli._vocab_cli(["--nonsense"]) == 2
        assert "usage: aish vocab" in capsys.readouterr().out


class TestWhatCountingCosts:
    def test_a_consultation_costs_a_lock_and_two_increments(self):
        """Stated because the rule is "counting must not add a per-page cost
        that browsing will feel". The comparison is against the substring scan
        it wraps, on the real list, at the length real control names have."""
        import timeit

        bare = timeit.timeit(
            lambda: any(w in " zapłać teraz " for w in browse._MUTATING_WORDS),
            number=2000,
        )
        counted = timeit.timeit(lambda: browse.is_worded("Zapłać teraz"), number=2000)
        # Generous: the point is an order of magnitude, not a stopwatch. A CI box
        # under load must not fail this, and a regression that made counting cost
        # ten times the match would still be caught.
        assert counted < bare * 10 + 0.05
