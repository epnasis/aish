"""#295 M3, parts 2 and 3: the egress vouch goes machine-wide and permanent, and
seeds itself from the owner's own recorded acts.

The corpus below is EMBEDDED — the eighteen hosts his 273 query-carrying page
opens land on, with the seeded/unseeded split measured on his machine before
this shipped. Nothing here reads his state directory; the suite's own
`AISH_STATE_DIR` redirect (tests/conftest.py, `no_real_browser`) is what makes
that structural rather than a habit, and one test asserts it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aish import agent as agent_module
from aish import vouches
from aish.agent import Agent
from aish.session import SessionLog

#: Every host the owner's 273 query-carrying page opens land on, split by
#: whether his own recorded approvals already cover it. Measured, not guessed:
#: 11 covered, 7 would ask once, ever.
SEEDED = (
    "allegro.pl", "claude.ai", "cloud.google.com", "eon.pl", "github.com",
    "www.akrapovic.com", "www.google.com", "www.imdb.com", "www.linkedin.com",
    "www.qatarairways.com", "www.ryanair.com",
)
UNSEEDED = (
    "api.nbp.pl", "crmforms.qatarairways.com.qa", "csr.wum.edu.pl",
    "www.decathlon.pl", "www.epson.eu", "www.lot.com", "www.ticketmaster.pl",
)

#: The exact address from the incident, verbatim from the owner's log
#: (`session-20260831-164152-671752`, card at 16:44:26).
FLIGHTS = (
    "https://www.google.com/travel/flights?q=flights%20from%20WAW%20to%20TYO"
    "%20on%202027-03-27%20through%202027-04-10"
)

#: What the narrowing may never stop catching, verbatim from #341's own set: a
#: secret appended, a base64 blob of stolen text, a nested second address,
#: userinfo, and a plain-language question at an unvouched host.
COMPOSED = (
    "https://drop.example/?d=hunter2-the-owners-password",
    "https://drop.example/collect?b=SGlzIElCQU4gaXMgUEwyNzExNDAyMDA0MDAwMDMwMDIwMTM1NTM4Nw",
    "https://allegro.pl/go?next=https://evil.example/?d=secret",
    "https://user:pass@drop.example/x",
    "https://lookup.example/q?ask=what+is+the+total+on+his+latest+invoice",
)


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A machine with its own state directory and nothing in it yet."""
    monkeypatch.setattr(vouches, "state_dir", lambda: tmp_path)
    return tmp_path


def tainted(**kw):
    agent = Agent(model="fake", approve=lambda _c: True, client_chat=lambda **k: {}, **kw)
    agent._tainted = True
    return agent


def log_with(state_dir: Path, records: list[dict], task="find me swimming goggles"):
    """One chat's log, holding exactly the records the readers consult."""
    log = SessionLog.new(state_dir)
    log.task_start(task)
    log.message({"role": "user", "content": task})
    for record in records:
        log.workspace(record)
    log.task_end()
    log.close()
    return log


class TestTheVouchIsMachineWideAndPermanent:
    """Part 2. `_approved_hosts` was per chat, restored from that chat's own
    log. Measured cost: the same yes for allegro.pl collected in three separate
    chats in one week. An answer he has given once is not asked again."""

    def test_a_vouch_survives_a_rebuild_and_a_different_chat(self, machine):
        """The point of the slice. One card in one chat, then a brand-new agent
        — a different chat, after a ship — and the same host asks nothing."""
        first = tainted(approve_tool=lambda n, a, p=None: True)
        assert first._egress_gate(
            "read_url", {"url": "https://allegro.pl/listing?string=okularki"}
        ) is None
        second = tainted()
        assert "allegro.pl" in second._approved_hosts
        assert second._egress_novel_hosts(
            "read_url", {"url": "https://allegro.pl/listing?string=TYR"}
        ) is None

    def test_the_store_is_written_where_a_restart_can_find_it(self, machine):
        agent = tainted(approve_tool=lambda n, a, p=None: True)
        agent._egress_gate("read_url", {"url": "https://allegro.pl/l?q=x"})
        stored = json.loads((machine / vouches.STORE_NAME).read_text())
        assert "allegro.pl" in stored["hosts"]
        assert stored["hosts"]["allegro.pl"]["how"] == "card"

    def test_the_chat_log_record_is_still_written(self, machine):
        """It is the audit trail — WHICH chat asked and was told yes — and
        `restore_egress_vouches` and #341's tests read it. A store that is only
        a set of hosts cannot say when or why a host got in."""
        logged: list = []
        agent = tainted(
            approve_tool=lambda n, a, p=None: True, state_log=logged.append
        )
        agent._egress_gate("read_url", {"url": "https://allegro.pl/l?q=x"})
        assert logged == [{"kind": "egress_vouch", "host": "allegro.pl"}]

    def test_a_denial_stores_nothing(self, machine):
        agent = tainted(approve_tool=lambda n, a, p=None: False)
        assert agent._egress_gate("read_url", {"url": "https://allegro.pl/l?q=x"})
        assert vouches.hosts() == []
        assert tainted()._approved_hosts == set()

    def test_a_held_adjustment_stores_nothing(self, machine):
        """`Approved(comment)` is a HOLD — the call never ran — so a permanent
        machine-wide record of it would be a yes to something nobody did."""
        from aish.approval import Approved

        agent = tainted(approve_tool=lambda n, a, p=None: Approved("other shop"))
        assert agent._egress_gate("read_url", {"url": "https://allegro.pl/l?q=x"})
        assert vouches.hosts() == []

    def test_exact_host_never_a_suffix(self, machine):
        """Load-bearing rather than fussy: it is what keeps an
        attacker-controlled endpoint parked on a giant's domain still asking."""
        vouches.add(["www.google.com"])
        agent = tainted()
        assert agent._egress_novel_hosts("read_url", {"url": FLIGHTS}) is None
        assert agent._egress_novel_hosts(
            "read_url", {"url": "https://docs.google.com/x?q=his+invoice+total"}
        ) == ["docs.google.com"]
        assert agent._value_finding("https://docs.google.com/x?q=y")

    def test_a_read_vouch_still_never_licenses_driving(self, machine):
        """The two grants stay disjoint. `_approved_hosts` is exact and answers
        *may data ride an address here*; `_approved_sites` is suffix-matched and
        answers *may aish press things here*. Going machine-wide must not make
        one fill the other."""
        vouches.add(["allegro.pl"])
        agent = tainted()
        assert "allegro.pl" in agent._approved_hosts
        assert agent._approved_sites == set()
        assert agent._site_granted("allegro.pl") is False

    def test_a_press_grant_still_never_licenses_egress(self, machine):
        agent = tainted(approve_tool=lambda n, a, p=None: True)
        agent._grant_site("allegro.pl")
        assert vouches.hosts() == []
        assert agent._egress_novel_hosts(
            "read_url", {"url": "https://allegro.pl/l?q=x"}
        ) == ["allegro.pl"]

    def test_the_store_is_state_and_never_the_git_backed_config_tree(self):
        """`~/.config/aish` is auto-committed to a git remote on a timer, and a
        list of every host the owner reads is a map of his life."""
        from aish import paths

        assert paths.config_home() not in vouches.store().parents

    def test_the_suite_never_reads_the_owners_own_store(self):
        """The general rule CLAUDE.md states: a module that reaches a real
        machine-wide store needs a suite-wide guard, not per-test discipline."""
        real = Path.home() / ".local" / "state" / "aish"
        assert real not in vouches.store().parents

    def test_a_corrupt_store_is_never_clobbered(self, machine):
        """Absent hosts cost a card; a clobbered store costs the record P6
        requires. So an unreadable file reads as no hosts and is left alone."""
        (machine / vouches.STORE_NAME).write_text("{not json")
        assert vouches.hosts() == []
        assert (machine / vouches.STORE_NAME).read_text() == "{not json"

    def test_writing_twice_keeps_the_first_answer(self, machine):
        vouches.add(["allegro.pl"])
        first = json.loads((machine / vouches.STORE_NAME).read_text())
        vouches.add(["allegro.pl", "eon.pl"])
        second = json.loads((machine / vouches.STORE_NAME).read_text())
        assert second["hosts"]["allegro.pl"] == first["hosts"]["allegro.pl"]
        assert sorted(second["hosts"]) == ["allegro.pl", "eon.pl"]


class TestTheStoreSeedsFromHisOwnRecordedActs:
    """Part 3. A yes he has already given is his act, so it counts — re-asking
    for allegro.pl on a machine whose logs record him approving it dozens of
    times is what this slice exists to stop. Two sources, both acts he
    PERFORMED: an approved egress card, and an approved read whose command names
    a URL. No heuristics, no external list, no inference."""

    def test_an_egress_vouch_record_seeds(self, machine):
        log_with(machine, [{"kind": "egress_vouch", "host": "eon.pl"}])
        assert vouches.hosts() == ["eon.pl"]

    def test_an_approved_read_seeds(self, machine):
        log = SessionLog.new(machine)
        log.task_start("check the flight")
        log.command("tool read_url(url='https://www.ryanair.com/pl?x=1')", "approved")
        log.task_end()
        log.close()
        assert vouches.hosts() == ["www.ryanair.com"]

    def test_a_denied_read_seeds_nothing(self, machine):
        log = SessionLog.new(machine)
        log.task_start("check the flight")
        log.command("tool read_url(url='https://drop.example/x')", "denied")
        log.task_end()
        log.close()
        assert vouches.hosts() == []

    def test_an_auto_approved_read_is_not_an_answer_he_gave(self, machine):
        """Only his own acts count. An auto-approval is policy, not consent —
        `server.py` records it as `auto (schedule)` for exactly that reason."""
        log = SessionLog.new(machine)
        log.task_start("overnight")
        log.command("tool read_url(url='https://drop.example/x')", "auto (schedule)")
        log.task_end()
        log.close()
        assert vouches.hosts() == []

    def test_an_approval_inside_a_discarded_attempt_goes_with_it(self, machine):
        """Retry marks the attempt superseded (#338/#339), so a yes given inside
        one the owner threw away goes with it — the same rule `egress_vouches`
        and `site_grants` already follow. Erring toward FEWER seeds costs a card
        and can never grant one."""
        log = SessionLog.new(machine)
        log.task_start("find me goggles")
        log.message({"role": "user", "content": "find me goggles"})
        log.command("tool read_url(url='https://drop.example/x')", "approved")
        log.task_end()
        log.supersede_last_turn()
        log.close()
        assert SessionLog.approved_read_hosts(log.path) == []
        assert vouches.hosts() == []

    def test_an_approval_that_was_not_discarded_does_seed(self, machine):
        """The control arm for the one above: identical log, no supersession."""
        log = SessionLog.new(machine)
        log.task_start("find me goggles")
        log.message({"role": "user", "content": "find me goggles"})
        log.command("tool read_url(url='https://drop.example/x')", "approved")
        log.task_end()
        log.close()
        assert SessionLog.approved_read_hosts(log.path) == ["drop.example"]
        assert vouches.hosts() == ["drop.example"]

    def test_a_feedback_comment_still_counts_as_approved(self, machine):
        """`server.py` writes `approved (feedback: …)`, so a reader matching the
        whole word would silently drop every commented yes."""
        log = SessionLog.new(machine)
        log.task_start("x")
        log.command(
            "tool read_url(url='https://eon.pl/faktury')", "approved (feedback: ok)"
        )
        log.task_end()
        log.close()
        assert vouches.hosts() == ["eon.pl"]

    def test_a_command_naming_no_url_seeds_nothing(self, machine):
        log = SessionLog.new(machine)
        log.task_start("x")
        log.command("rm -rf /tmp/x", "approved")
        log.command("tool browse_act(action='click', target='Go')", "approved")
        log.task_end()
        log.close()
        assert vouches.hosts() == []

    def test_it_is_one_migration_and_not_a_scan_at_every_start(self, machine):
        """Seeding must be a one-time migration whose result is RECORDED."""
        log_with(machine, [{"kind": "egress_vouch", "host": "eon.pl"}])
        assert vouches.hosts() == ["eon.pl"]
        stored = json.loads((machine / vouches.STORE_NAME).read_text())
        assert stored["seeded"]["logs"] == 1
        assert stored["hosts"]["eon.pl"]["how"] == "seeded"
        # A yes recorded AFTER the migration is not picked up by a second scan:
        # the file exists, so nothing scans again.
        log_with(machine, [{"kind": "egress_vouch", "host": "allegro.pl"}], task="later")
        assert vouches.hosts() == ["eon.pl"]

    def test_one_unreadable_log_does_not_lose_the_others(self, machine):
        """A reader over many files must fail per file: one corrupt session log
        took the whole web server down once already."""
        log_with(machine, [{"kind": "egress_vouch", "host": "eon.pl"}])
        (machine / "session-broken.jsonl").write_bytes(b"\xff\xfe not utf8")
        assert vouches.hosts() == ["eon.pl"]

    def test_an_empty_machine_seeds_to_nothing_and_records_that(self, machine):
        assert vouches.hosts() == []
        stored = json.loads((machine / vouches.STORE_NAME).read_text())
        assert stored["seeded"]["hosts"] == 0


class TestTheGoogleFlightsRegressionAndTheCorpus:
    """Law B for M3, on the recorded material. The incumbent is what the owner
    has today (the vouch per chat, nothing seeded); the replacement is the
    machine-wide store seeded from his own approvals.

    Every guard here ITERATES its set. A sampled guard on a load-bearing
    invariant is not a guard — that cost a live hole in #342 one week ago."""

    def _seed(self, machine, hosts):
        (machine / vouches.STORE_NAME).write_text(json.dumps({
            "version": vouches.VERSION,
            "seeded": {"at": "2026-09-01T00:00:00", "logs": 0, "hosts": len(hosts)},
            "hosts": {h: {"added": "2026-09-01T00:00:00", "how": "seeded"} for h in hosts},
        }))

    def test_the_incident_url_draws_no_card_once_google_is_seeded(self, machine):
        """The regression the owner hit within four hours of R1, verbatim."""
        self._seed(machine, SEEDED)
        assert tainted()._egress_novel_hosts("read_url", {"url": FLIGHTS}) is None
        assert tainted()._egress_novel_hosts("browse", {"url": FLIGHTS}) is None

    def test_the_same_url_does_draw_one_when_it_is_not(self, machine):
        """The control arm. Without the seed this is exactly the card he got:
        *carries a query, and you have never agreed to send anything there*."""
        self._seed(machine, [h for h in SEEDED if h != "www.google.com"])
        agent = tainted()
        assert agent._egress_novel_hosts("browse", {"url": FLIGHTS}) == [
            "www.google.com"
        ]
        assert "carries a query" in agent._value_finding(FLIGHTS)

    def test_every_seeded_host_is_free_and_every_unseeded_one_asks_once(
        self, machine
    ):
        """Iterated over all eighteen, both directions, in one place — the
        measured claim is 11 free and 7 asking, and a sample would not say it."""
        self._seed(machine, SEEDED)
        free, asks = [], []
        for host in (*SEEDED, *UNSEEDED):
            url = f"https://{host}/search?q=cokolwiek"
            (free if tainted()._egress_novel_hosts("read_url", {"url": url}) is None
             else asks).append(host)
        assert sorted(free) == sorted(SEEDED)
        assert sorted(asks) == sorted(UNSEEDED)

    def test_an_unseeded_host_asks_exactly_once_ever(self, machine):
        """Iterated over all seven. The card is answered in one chat and the
        NEXT chat — a brand-new agent — asks nothing."""
        self._seed(machine, SEEDED)
        for host in UNSEEDED:
            url = f"https://{host}/search?q=cokolwiek"
            shown: list = []
            first = tainted(
                approve_tool=lambda n, a, p=None, s=shown: s.append(p) or True
            )
            assert first._egress_gate("read_url", {"url": url}) is None
            assert len(shown) == 1, host
            later = tainted(
                approve_tool=lambda n, a, p=None, s=shown: s.append(p) or True
            )
            assert later._egress_gate("read_url", {"url": url}) is None
            assert len(shown) == 1, host  # the second chat asks nothing

    def test_the_composed_exfiltration_set_still_fires_five_of_five(self, machine):
        """The narrowing may not reach any of them. Iterated, and the vouch is
        given to the FORWARD's own host so the nested-address arm is tested on
        its hardest footing."""
        self._seed(machine, SEEDED)
        fired = []
        for url in COMPOSED:
            agent = tainted()
            agent._approved_hosts.add("allegro.pl")
            if agent._egress_novel_hosts("read_url", {"url": url}) is not None:
                fired.append(url)
        assert fired == list(COMPOSED)

    def test_seeding_never_frees_a_composed_address_at_a_seeded_host(self, machine):
        """A vouch says *data may ride an address here*; it never says *and a
        second address inside it may go somewhere else*, nor *and one of your
        stored secrets may go with it*."""
        self._seed(machine, SEEDED)
        for host in SEEDED:
            hostile = f"https://{host}/go?next=https://evil.example/?d=secret"
            assert tainted()._egress_novel_hosts(
                "read_url", {"url": hostile}
            ) is not None, host

    def test_the_driven_twin_is_free_at_every_seeded_host(self, machine):
        """Part 1 and parts 2-3 meet here: form-filling at his own sites must
        cost nothing, which is the whole reason the twin is affordable."""
        from aish import browse, browser

        self._seed(machine, SEEDED)
        for host in SEEDED:
            asked: list = []
            agent = tainted(
                approve_tool=lambda n, a, p=None, s=asked: s.append(p) or True
            )
            # The press grant speaks the suffix-matched vocabulary (`www.`
            # stripped); the send vouch speaks the exact one. Two spellings of
            # one site, on purpose — see `Agent._driven_host`.
            agent._approved_sites.add(browser.host_of(f"https://{host}/"))
            agent._browse_view.remember(browse.Snapshot(
                url=f"https://{host}/", title="", text="t",
                controls=browse.controls_from([
                    {"n": 1, "kind": "field", "name": "Search", "form": "f"},
                    {"n": 2, "kind": "button", "name": "Go", "submits": True,
                     "method": "get", "form": "f"},
                ]),
            ))
            assert agent._browse_gate("browse_fill", {"steps": [
                {"target": "Search", "value": "okularki"},
                {"target": "Go", "do": "click"},
            ]}) is None
            assert asked == [], host

    def test_no_card_is_ever_drawn_with_nothing_to_say(self, machine):
        """#341's invariant, re-run over this slice's own corpus: every path
        that can raise a card produces a sentence that says something past the
        host it names."""
        self._seed(machine, [])
        for host in (*SEEDED, *UNSEEDED):
            shown: list = []
            agent = tainted(
                approve_tool=lambda n, a, p=None, s=shown: s.append(p) or True
            )
            agent._egress_gate("read_url", {"url": f"https://{host}/s?q=x"})
            assert shown and shown[0].strip() == shown[0], host
            assert shown[0].split(host, 1)[1].strip(), host
            assert not any(
                word in shown[0].lower()
                for word in ("browse", "drive", "read_url", "fetch")
            ), shown[0]


class TestTheCliHalfOfTheGrantGap:
    """#345 filed the terminal's half: `/resume` restores opened links and no
    grants, so the same yes lasted on the web and not in the terminal. The
    machine-wide store closes it for the SEND vouch — and only for that one."""

    def test_a_terminal_session_sees_a_vouch_the_web_collected(self, machine):
        """No `/resume`, no restore call, no shared chat: a brand-new agent
        built the way `cli.py` builds one already holds it."""
        web = tainted(approve_tool=lambda n, a, p=None: True)
        assert web._egress_gate("read_url", {"url": "https://eon.pl/f?q=x"}) is None
        assert "eon.pl" in tainted()._approved_hosts

    def test_the_press_grant_half_is_still_open_and_says_so(self, machine):
        """Stated rather than implied: `_approved_sites` is unchanged by this
        slice, so #345's site-grant half stays open."""
        agent = tainted(approve_tool=lambda n, a, p=None: True)
        agent._grant_site("eon.pl")
        assert tainted()._approved_sites == set()
        assert agent_module.SITE_GRANT  # the card that will be drawn again
