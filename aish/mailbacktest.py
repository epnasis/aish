"""`aish-mailbacktest` — the shadow pipeline measured on the owner's history
(#142 W5, slice S1).

    aish-mailbacktest --account pawel@wenda.eu --days 90 \\
        --arm gemini:<flash> --arm local:<mi model> --reader-max 300

**Frozen clocks.** The index and the case store are built in arrival order
inside a SCRATCH run directory, and every question the pipeline asks is asked
"as of" the mail under review: its conversation, its sender's grade, its
candidate cases and their history. Nothing on the Reader's side of the line
can see a later message. Only the ground truth reads the future — that is what
ground truth is.

**Deterministic first, models on demand.** The backfill fetches and folds
every mail in the window with no model call. The Reader then runs per ARM
(one model each) over a bounded selection, so the money spent is a number the
driver chose. Verdicts land in the run directory's own verdict store, keyed by
model, so a backtest interrupted half-way resumes where it stopped.

**Ground truth is TIERED by how much an observation can carry.** Strong: he
replied (an outbound message replies into the conversation after it), he
trashed it, he starred it. Weak: he archived it, or read it and left it in the
inbox. Near-null: it sits unread in the inbox. Spam is Gmail's verdict, not
his, and is set apart. Starred and read-and-kept are this slice's ADDITIONS to
the design's three tiers, and are reported under their own names so either can
be dropped by reading past it.

**Raw counts only, and no verdict on the pipeline** (`docs/replay.md` R3, R5):
confusion counts per axis per tier, `cannot_tell` as its own column, and a
headline that is the DESTRUCTIVE direction — mail the pipeline would have
expired that he kept or replied to. The report never prints a rate.

The mailbox is a seam (`list`, `get`), so the tests drive this end to end
against a fake one. `docs/mail-assistant.md`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import mailcases, mailindex, mailreader, mailverdicts, mailwatch, paths, roles

DEFAULT_DAYS = 90
DEFAULT_READER_MAX = 200
LISTING_LIMIT = 20000
DISAGREEMENT_SHOWN = 50

# Gmail empties Trash after this many days, so "he trashed it" is observable
# only for mail newer than this when the backtest runs. Older trashed mail is
# not in any listing at all.
TRASH_RETENTION_DAYS = 30

STRONG, WEAK, NEAR_NULL, SET_APART = "strong", "weak", "near-null", "set-apart"

# Truth label -> tier. Order is precedence: a mail he replied to and later
# trashed is "replied".
TRUTH_TIERS = {
    "replied": STRONG,
    "trashed": STRONG,
    "starred": STRONG,  # addition to the design's tiering
    "archived": WEAK,
    "kept_read": WEAK,  # addition to the design's tiering
    "unread": NEAR_NULL,
    "spam": SET_APART,
}
KEPT = frozenset({"replied", "starred", "archived", "kept_read", "unread"})
NO_VERDICT = "no_verdict"


@dataclass(frozen=True)
class Truth:
    label: str
    tier: str
    facts: tuple[str, ...]  # every observation, not only the winning one


def truth_of(
    index: mailindex.MailIndex, header: mailindex.Header, labels: tuple[str, ...]
) -> Truth:
    """What the mailbox shows he did with this mail. Reads the future on
    purpose; nothing on the Reader path calls it."""
    held = set(labels)
    facts = []
    if index.later_replies(header.digest):
        facts.append("replied")
    if "TRASH" in held:
        facts.append("trashed")
    if "STARRED" in held:
        facts.append("starred")
    if "SPAM" in held:
        facts.append("spam")
    if "INBOX" in held:
        facts.append("unread" if "UNREAD" in held else "kept_read")
    elif "TRASH" not in held and "SPAM" not in held:
        facts.append("archived")
    if "spam" in facts and "replied" not in facts:
        return Truth("spam", SET_APART, tuple(facts))
    label = next(t for t in TRUTH_TIERS if t in facts)
    return Truth(label, TRUTH_TIERS[label], tuple(facts))


# ---------------------------------------------------------------- the run


@dataclass
class Backfill:
    index: mailindex.MailIndex
    store: mailcases.CaseStore
    labels: dict[str, tuple[str, ...]]
    counts: dict[str, int] = field(default_factory=dict)


def backfill(
    mailbox: Any,
    state_dir: Path,
    owner_addresses: frozenset[str],
    *,
    days: int,
    now: float,
) -> Backfill:
    """Fetch the window (inbox, archive, sent and trash; read-only) and fold
    it, oldest first, into a scratch index and case store. No model call."""
    since = datetime.datetime.fromtimestamp(now - days * 86400, datetime.UTC)
    ids = mailbox.list(f"after:{since:%Y/%m/%d}", LISTING_LIMIT, include_trash=True)
    counts = {"listed": 0, "fetched": 0, "fetch_failed": 0, "unparseable": 0,
              "outside_window": 0}
    if ids is None:
        counts["listing_failed"] = 1
        ids = []
    counts["listed"] = len(ids)
    parsed: list[mailindex.Parsed] = []
    labels: dict[str, tuple[str, ...]] = {}
    floor = now - days * 86400
    for message_id in dict.fromkeys(ids):
        msg = mailbox.get(message_id)
        if msg is None:
            counts["fetch_failed"] += 1
            continue
        counts["fetched"] += 1
        item = mailindex.from_gmail(msg, owner_addresses)
        if item is None:
            counts["unparseable"] += 1
            continue
        if item.header.at < floor:
            counts["outside_window"] += 1
            continue
        parsed.append(item)
        labels[item.header.digest] = tuple(sorted(set(labels.get(item.header.digest, ())) |
                                                  set(item.labels)))
    index = mailindex.MailIndex(state_dir)
    store = mailcases.CaseStore(state_dir)
    mailwatch.ingest(index, store, parsed)
    counts["in_index"] = len(index.by_digest)
    counts["cases"] = len(store.cases)
    counts["index_unreadable"] = len(index.health.unreadable)
    counts["message_id_collisions"] = len(index.health.collisions)
    return Backfill(index, store, labels, counts)


def select(
    run: Backfill, truths: dict[str, Truth], reader_max: int
) -> list[mailindex.Header]:
    """Which inbound mails the arms read: strong tier first, then weak, then
    near-null, each oldest first, up to `reader_max`. Deterministic, so two
    arms always read the same mails."""
    inbound = [
        h for h in run.index.by_digest.values()
        if h.direction == "in" and truths[h.digest].tier != SET_APART
    ]
    order = {STRONG: 0, WEAK: 1, NEAR_NULL: 2}
    inbound.sort(key=lambda h: (order[truths[h.digest].tier], h.position))
    return inbound[:reader_max]


def run_arms(
    run: Backfill,
    selected: list[mailindex.Header],
    arms: dict[str, Callable[..., Any] | None],
    *,
    charter: roles.Charter,
    state_dir: Path,
    check_admission: bool = True,
    admission_state_dir: Path | None = None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """{arm: {mail digest: record}}. Resumable: a final reading already in the
    run's verdict store is reused, not re-bought.

    `state_dir` is the run's SCRATCH tree (index, cases, verdicts, evidence);
    admission is read from `admission_state_dir`, the machine's real state home
    by default — nothing written into a scratch tree can admit a model."""
    ledger = paths.state_home() if admission_state_dir is None else admission_state_dir
    verdicts = mailverdicts.VerdictStore(state_dir)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for spec, chat in arms.items():
        mine = out.setdefault(spec, {})
        for header in selected:
            if verdicts.has(header.digest, spec, charter.digest):
                held = (verdicts.read(header.digest) or {}).get("by_model", {}).get(spec)
                if isinstance(held, dict):
                    mine[header.digest] = held
                    continue
            try:
                record = mailwatch.assess(
                    run.index, run.store, header, charter=charter, model_spec=spec, chat=chat,
                    state_dir=state_dir, propose=False, check_admission=check_admission,
                    admission_state_dir=ledger,
                )
            except Exception as exc:  # noqa: BLE001 — one mail's failure is its own record
                record = mailverdicts.error_record(header, charter, spec, exc)
            verdicts.write(record)
            mine[header.digest] = record
    return out


# ---------------------------------------------------------------- the report


def _cell(record: dict[str, Any], axis: str) -> str:
    ending = str(record.get("ending") or "")
    if ending == mailreader.GATED:
        return "gated"
    verdict = record.get("verdict")
    if not isinstance(verdict, dict):
        return NO_VERDICT
    return str(verdict.get(axis) or "")


def _add(table: dict[str, dict[str, int]], row: str, col: str) -> None:
    table.setdefault(row, {})
    table[row][col] = table[row].get(col, 0) + 1


def report(
    run: Backfill,
    truths: dict[str, Truth],
    selected: list[mailindex.Header],
    results: dict[str, dict[str, dict[str, Any]]],
    *,
    now: float,
) -> dict[str, Any]:
    """Raw counts. Every table is (truth label) x (verdict value); `cannot_tell`,
    `gated` and `no_verdict` are columns like any other."""
    tiers: dict[str, dict[str, int]] = {}
    for header in run.index.by_digest.values():
        if header.direction == "in":
            truth = truths[header.digest]
            _add(tiers, truth.tier, truth.label)
    trashed = [h for h in run.index.by_digest.values()
               if h.direction == "in" and "trashed" in truths[h.digest].facts]
    oldest_trash_days = (
        round((now - min(h.at for h in trashed)) / 86400, 1) if trashed else None
    )
    arms: dict[str, Any] = {}
    for spec, records in results.items():
        disposition: dict[str, dict[str, int]] = {}
        wanted_vs_reply: dict[str, dict[str, int]] = {}
        destructive: dict[str, int] = {}
        archived_replied = 0
        gated: dict[str, dict[str, int]] = {}
        endings: dict[str, int] = {}
        for header in selected:
            record = records.get(header.digest)
            if record is None:
                continue
            truth = truths[header.digest]
            ending = str(record.get("ending") or "")
            endings[ending] = endings.get(ending, 0) + 1
            value = _cell(record, "disposition")
            _add(disposition, f"{truth.tier}:{truth.label}", value)
            replied = "replied" if "replied" in truth.facts else "not_replied"
            _add(wanted_vs_reply, f"{truth.tier}:{replied}", _cell(record, "wanted"))
            if value == "expire_after_grace" and truth.label in KEPT:
                destructive[truth.label] = destructive.get(truth.label, 0) + 1
            if value == "archive" and "replied" in truth.facts:
                archived_replied += 1
            if ending == mailreader.GATED:
                by = "+".join(((record.get("gate") or {}).get("newsletter") or {}).get("by") or ())
                _add(gated, by or "?", truth.label)
        arms[spec] = {
            "read": len([d for d in records if d in {h.digest for h in selected}]),
            "endings": endings,
            "destructive_expire_on_kept": destructive,
            "archive_on_replied": archived_replied,
            "disposition_by_truth": disposition,
            "wanted_by_reply": wanted_vs_reply,
            "gated_by_truth": gated,
        }
    disagreements = []
    specs = sorted(results)
    if len(specs) >= 2:
        for header in selected:
            cells = {
                spec: {axis: _cell(results[spec].get(header.digest, {}), axis)
                       for axis in ("disposition", "wanted", "case", "sender_case")}
                for spec in specs if header.digest in results[spec]
            }
            if len(cells) < 2:
                continue
            if len({json.dumps(v, sort_keys=True) for v in cells.values()}) > 1:
                disagreements.append({
                    "mail": header.digest,
                    "provider_id": header.provider_id,
                    "date": time.strftime("%Y-%m-%d", time.gmtime(header.at)),
                    "from": header.from_addr,
                    "subject": header.subject[:120],
                    "truth": truths[header.digest].label,
                    "arms": cells,
                })
    return {
        "window": run.counts,
        "truth_tiers": tiers,
        "trash_observability": {
            "trashed_inbound": len(trashed),
            "oldest_trashed_days": oldest_trash_days,
            "retention_days": TRASH_RETENTION_DAYS,
        },
        "selected": len(selected),
        "arms": arms,
        "disagreements": disagreements,
    }


def _pairs(table: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(table.items()))


def _table(title: str, table: dict[str, dict[str, int]]) -> list[str]:
    if not table:
        return [f"  {title}: none recorded"]
    cols = sorted({c for row in table.values() for c in row})
    lines = [f"  {title}", "    " + " | ".join(["truth".ljust(22)] + cols)]
    for row in sorted(table):
        cells = [str(table[row].get(c, 0)) for c in cols]
        lines.append("    " + " | ".join([row.ljust(22)] + cells))
    return lines


def render(result: dict[str, Any]) -> str:
    """The report a person reads. Counts as "N of M" — never a rate, never a
    word that grades the pipeline (`docs/replay.md` R3)."""
    w = result["window"]
    lines = [
        "Inbox assistant backtest — shadow pipeline, raw counts",
        f"window: {w.get('listed', 0)} listed, {w.get('fetched', 0)} fetched, "
        f"{w.get('fetch_failed', 0)} not fetched, {w.get('unparseable', 0)} unparseable, "
        f"{w.get('in_index', 0)} in the index, {w.get('cases', 0)} cases built",
    ]
    if w.get("listing_failed"):
        lines.append("the mailbox LISTING failed: nothing below describes the window")
    trash = result["trash_observability"]
    lines.append(
        f"trashed inbound observed: {trash['trashed_inbound']} (oldest "
        f"{trash['oldest_trashed_days']} days). Gmail empties Trash after "
        f"{trash['retention_days']} days, so mail he trashed earlier than that is in no "
        "listing — it is ABSENT from every count here, not counted as kept."
    )
    lines.append("truth, by tier (inbound mail in the window):")
    for tier, labels in sorted(result["truth_tiers"].items()):
        lines.append(f"  {tier}: " + ", ".join(f"{k} {v}" for k, v in sorted(labels.items())))
    lines.append(f"read by each arm: {result['selected']} selected mail(s)")
    for spec, arm in sorted(result["arms"].items()):
        lines.append("")
        lines.append(f"arm {spec}: {arm['read']} of {result['selected']} recorded")
        lines.append("  endings: " + _pairs(arm["endings"]))
        destructive = arm["destructive_expire_on_kept"]
        total = sum(destructive.values())
        lines.append(
            f"  HEADLINE — would have expired mail he kept: {total} of {arm['read']}"
            + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(destructive.items())) + ")"
               if destructive else "")
        )
        lines.append(f"  would have archived mail he replied to: {arm['archive_on_replied']}")
        lines += _table("disposition (rows: tier:truth)", arm["disposition_by_truth"])
        lines += _table("wanted (rows: tier:replied?)", arm["wanted_by_reply"])
        lines += _table("newsletter-gated, by list header (rows) x truth", arm["gated_by_truth"])
    shown = result["disagreements"][:DISAGREEMENT_SHOWN]
    lines.append("")
    lines.append(
        f"cross-model disagreements: {len(result['disagreements'])}"
        + (f" (first {len(shown)} below; all in report.json)" if shown else "")
    )
    for item in shown:
        arms = "; ".join(
            f"{spec}: " + "/".join(
                cells[axis] for axis in ("disposition", "wanted", "case", "sender_case")
            )
            for spec, cells in sorted(item["arms"].items())
        )
        lines.append(
            f"  {item['date']} {item['from']} — {item['subject']} [{item['truth']}] {arms}"
        )
    lines.append("")
    lines.append("These are counts of what was recorded. They are not a measure of whether the "
                 "pipeline is fit to act.")
    return "\n".join(lines)


def run_backtest(
    *,
    mailbox: Any,
    owner_addresses: frozenset[str],
    arms: dict[str, Callable[..., Any] | None],
    charter: roles.Charter,
    out_dir: Path,
    days: int = DEFAULT_DAYS,
    reader_max: int = DEFAULT_READER_MAX,
    now: float | None = None,
    check_admission: bool = True,
    admission_state_dir: Path | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    state_dir = out_dir / "state"
    run = backfill(mailbox, state_dir, owner_addresses, days=days, now=now)
    truths = {
        d: truth_of(run.index, h, run.labels.get(d, ())) for d, h in run.index.by_digest.items()
    }
    selected = select(run, truths, reader_max)
    results = run_arms(run, selected, arms, charter=charter, state_dir=state_dir,
                       check_admission=check_admission,
                       admission_state_dir=admission_state_dir)
    result = report(run, truths, selected, results, now=now)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(result, indent=1, sort_keys=True))
    (out_dir / "report.txt").write_text(render(result) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="backtest the shadow inbox pipeline (#142 W5)")
    parser.add_argument("--account", default=os.environ.get("AISH_MAIL_ACCOUNT", ""))
    parser.add_argument("--owner", action="append", default=[])
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--arm", action="append", default=[],
                        help="a Reader model spec; repeat for a cross-model comparison")
    parser.add_argument("--reader-max", type=int, default=DEFAULT_READER_MAX)
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)
    env, why = mailwatch.account_env(args.account)
    if env is None:
        print(f"refusing to run: {why}", file=sys.stderr)
        return 2
    try:
        charter = mailwatch.load_reader()
    except roles.CharterError as exc:
        print(f"the Reader's charter does not load: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else (
        paths.state_home() / "mail-backtests" / time.strftime("%Y%m%d-%H%M%S")
    )
    result = run_backtest(
        mailbox=mailwatch.GwsMailbox(env),
        owner_addresses=frozenset({args.account.lower(), *(a.lower() for a in args.owner)}),
        arms={spec: None for spec in args.arm},
        charter=charter,
        out_dir=out,
        days=args.days,
        reader_max=args.reader_max,
    )
    print(render(result))
    print(f"\nwritten to {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
