# The inbox assistant — shadow mode (#142 W5, slices S1, S1.5 and S1.6)

`mailindex.py`, `mailgate.py`, `mailcases.py`, `mailreader.py`, `mailverdicts.py`,
`mailwatch.py`, `mailsource.py`, `mailtuta.py`, `mailview.py`, `mailexport.py`,
`mailbacktest.py`, `mailreport.py`, `mailcasetool.py`, `mailprefs.py`, `mailitems.py`,
`mailsettle.py`, `mailitemtool.py`, `mailvouch.py`, `aish/charters/mail-reader.md`,
`aish/charters/item-settler.md`, `scripts/install-mailwatch-service.sh`,
`scripts/install-mailreport-service.sh`.
The binding design is the comment *"W5 REWRITTEN — the inbox assistant"* on epic #142
(2026-10-24). This file says what slice S1 built from it, what S1.5 added (the owner surface,
both inboxes, quota discipline, continuous runs — §S1.5 below), what S1.6 added (live
conversations, reply recognition, the CSV review file — §S1.6 below), what they deliberately
did not,
and why each fence is where it is.

**How to use this file.** The design in one screen first, then the one-line state of S1, then
one section per component — each ends with the test class that pins it. Then the record, the
counters, the backtest, the residuals, and last the driver's instructions. The list of what S1
does **not** do is near the end and is where an overclaim would live if there is one.

---

## The design, in one screen

aish watches the owner's personal inbox and acts as an assistant over it — what a mail means,
what it wants of him, which cross-mail case it belongs to — not a labeller.

- **Zone A — the Gate.** Deterministic: authentication, structural red flags, and a
  relationship ledger whose grades rest ONLY on his own acts. Judged: a toolless,
  restrict-only classifier that sees the sender's usual pattern (catches a compromised
  trusted account asking something out of pattern). Quarantine is a visible, counted state.
- **Zone B — the Reader.** A toolless role, handed a code-assembled bundle: the whole
  conversation from aish's OWN thread index, candidate cases from a code-side correlator,
  the trust facts. It answers a typed verdict: a fixed skeleton plus a capped open layer,
  `cannot_tell` lawful everywhere.
- **Case memory.** Cases and the counterparties above them, in the state tree, one writer,
  per-file containment, whole-unit eviction, an owner surface. Closure — the licence for
  expiry — is deterministic or his act, never a verdict.
- **Zone C — the Actor.** An ordinary triggered session consuming typed verdicts as tainted
  outside-content notes, under a code-enforced surfacing budget, with each trust lane
  (archive, keep-unread, expire) opened by HIS explicit act, informed by shadow numbers.

**Rollout is shadow-first, measured on his own history.** That is what S1 is.

## What S1 is, in one line

**Every new mail flows Gate → Reader → a recorded verdict, and the verdict acts on nothing.**
No label is changed, nothing is archived or trashed, nobody is pushed, no proposal is made.
The outputs are a verdict store, its counters, and a backtest report — for the driver.

| | S1 | later |
|---|---|---|
| thread index | built | — |
| Gate, deterministic half | built | — |
| Gate, judged half (the classifier role) | **not built** — the Reader's own `suspicion` axis is the only judged signal in shadow | S2 |
| Reader charter + exam, admitted per model | built | — |
| correlator + minimal case index | built | full case memory, entities, owner surface, follow-up queue: S3 |
| verdict store + `mail_verdict` record | built | delivery into a session: S2 |
| ingress | a CLI, `aish-mailwatch --once`; S1.5: both inboxes, a launchd interval job | inside aish-web's lifespan: S2 |
| owner surface | S1.5: `/mail` in the terminal (results + watcher health) | the case store's own surface: S3 |
| Zone C | **none** | proposals S2, trust lanes S4, relevance S5 |

---

## The thread index — `mailindex.py`

**Provider threading is never trusted.** The Tuta gateway renumbers UIDs on restart without
bumping UIDVALIDITY and breaks its own threads; Gmail's `threadId` is a provider opinion. So
conversations are rebuilt here from Message-ID / In-Reply-To / References.

**Identity is the Message-ID or the content, never a provider id.** `digest_for` is sha256 of
the normalized Message-ID, or — when a mail has none — of sender, Date header, subject and
body. The same mail fed twice under two provider ids is one entry, which is what makes every
re-run idempotent. Two DIFFERENT mails claiming one Message-ID keep the first and are counted
in `Health.collisions`, never merged silently. `TestIdentity`.

**One fallback, narrow on purpose.** A mail with no reply headers links to an earlier one only
when its subject carries a reply prefix (`_REPLY_PREFIXES`, counted), the normalized subjects
match, a correspondent is shared, and it is inside `FALLBACK_WINDOW_DAYS` (30). Without a prefix
nothing links: stores reuse *"Your order has shipped"* across unrelated orders. A lookalike
sender's header-less "Re:" therefore never joins the real counterparty's thread — it shares no
correspondent. `TestThreadReconstruction`.

**Threads are rebuilt as of the question, never filtered after it.** A later reply whose
References name two earlier conversations joins them. An index that unioned everything and
then hid later messages from the result would still hand an earlier mail the joined structure
— structure only the future created. So `conversation` rebuilds the union over messages at or
before `as_of` only, and the subject fallback is decided against earlier messages only — at
admission, and again the same way at every load. A load can therefore change a link when an
older mail was fetched in a later pass; a verdict already recorded keeps the bundle it was given
(its bytes are in the evidence store) and is never recomputed. Found by the design review; the
bridging case is pinned. `TestFrozenClock`.

**Persistence.** One JSON file per message under `<state>/mail/index/`, written through
`atomic_write`; no thread id is ever stored. The BODY goes to the evidence store, which is
purgeable by contract, and the index keeps its digest; a purged body reads as
`[body not recorded]`, never as an empty mail. One unreadable file costs that message and is
counted in `Health.unreadable`. **Parsing is contained per mail too**: `from_gmail` walks the MIME
tree iteratively within `MAX_MIME_DEPTH` (32) and `MAX_MIME_PARTS` (500), and a payload that
still breaks it is `None`, counted `unparseable` — a hostile mail nesting thousands of parts raised
out of the first draft and would have stopped every pass it was listed in (found in delivery
review). `TestPersistence`, `TestGmailParse`.

**`at` is the provider's receipt time** (`internalDate`) when there is one, because the Date
header is written by the sender. `at_source` records which.

## The Gate, deterministic half — `mailgate.py`

**Authentication reads the TOP-MOST Authentication-Results header, and only Gmail's.** A
sender can write any number of Authentication-Results headers; Gmail prepends its own above
them. `authenticate` reads the first, requires its authserv-id to be EXACTLY `mx.google.com`
(`GMAIL_AUTHSERV`; a prefix test read `mx.google.com.evil.example` as Gmail until delivery
review), and calls the mail authenticated on `dmarc=pass`, or on an aligned `dkim=pass` under a
domain with no DMARC policy. This tightens `email_poll.dmarc_pass`, which substring-matches the
first header without checking who wrote it. **The guarantee holds for mail Gmail received by
SMTP**, where Gmail's header is the top one by construction. Mail Gmail did not receive that way
— fetched from another mailbox, imported — may carry no Gmail-written header at all, and then
the top-most one is the sender's, an exact forgery of Gmail's included; nothing here can tell
the two apart. Named under *Residuals*. `TestAuthentication`.

**Red flags, structural first.** Link text showing a domain other than the link's host, an
IP-literal host, userinfo in the URL, a punycode host, a lookalike of an ADMITTED domain
(`lookalike_of`, edit distance within `LOOKALIKE_DISTANCE` (2)), a risky attachment type or a
double extension. One word list is matched (`_CREDENTIAL_WORDS`, URL path words), declared and
counted; the suffix list is an exact-membership test, because a substring test reads `.js`
inside `.json`. Every flag is a closed name plus a code-written detail. `TestRedFlags`.

**Newsletters are a structural fact.** A List-Id or List-Unsubscribe header gates the Reader:
no model call, a `gated` ending recorded with which header said so. `Precedence: bulk` alone is
recorded and does not gate. **This gate cuts both ways and that is measured, not argued:** a
phisher can add List-Unsubscribe to skip the Reader, and banks put it on transactional mail. The
deterministic flags still run on gated mail, and the backtest counts gated mail against what he
did with it, split by which header gated it — so the gate can be narrowed on evidence.
`TestNewsletter`.

**The relationship ledger counts his acts and nothing else.** `grade` is `established`,
`known` or `first-contact`, from `owner_acts`:

- `wrote` — an outbound mail addressed to the address;
- `replied` — an outbound mail whose OWN In-Reply-To names a mail from that address.
  Membership of the same conversation is not enough: a hostile mail can put an owner thread's
  Message-IDs in its References and join it, and his earlier replies there must not become
  acts toward it;
- `case` — a case he transacted on includes the address AS SOMEONE HE DEALT WITH (from the case
  store, as of): a party he addressed on that case, or one at a domain admitted there by his act
  toward it. **Since 2026-11-02 a party records its provenance** (`via`: `owner` or `inbound`,
  `authenticated`, and `addressed` — when he first addressed it): a sender that merely JOINED a
  case by quoting its key is a party, never a `case` act (`Case.dealt_with_at`,
  `CaseStore.owner_case_parties`). Before it, an authenticated stranger quoting his real order
  number earned the grade `known` and third-party history (round-3 review). A party recorded
  before provenance existed is resolved from his own outbound mail on the case when the index is
  at hand (and a merge never turns a legacy record into "inbound": `TestLegacyParties`).
`TestJoiningACaseIsNotTrust` in `tests/test_mailitems.py`.

`established` needs `ESTABLISHED_MIN_ACTS` (2) acts on distinct days and the first at least
`ESTABLISHED_MIN_AGE_DAYS` (30) days before. **Inbound volume is recorded and read by no line**
— a thousand mails earn nothing, because volume is the one thing a sender controls completely.
**An unauthenticated mail is `first-contact` whatever the history**: unauthenticated, the From
address is a claim, and a claim cannot inherit somebody else's relationship. **Gmail labels are
not acts**: a label has no timestamp, so reading one would let a March grade see an April star.
Domain-level relationships are deferred: they need a freemail list whose miss would loosen a
grade. Grades add scrutiny; in S1 nothing reads them to act. `TestLedger`.

**The judged half is not built.** The classifier role that sees a sender's usual pattern is S2.
In shadow, the Reader's `suspicion` axis is the only judged signal, and it restricts nothing.

## The case store and the correlator — `mailcases.py`

**Everything here is code.** Keys by regex after an introducing word (`_KEY_WORDS`, or since
2026-11-02 an inflected Polish stem with a bounded ending, `_KEY_STEMS` — `docs/vocabularies.md`;
never a date-shaped key; both counted)
and carrying a digit, the UPS shape without one; amounts beside a currency
(`_CURRENCY_WORDS`); IBANs by regex AND the mod-97 checksum (`iban_valid`), with a bare
26-digit Polish account read as its PL IBAN. `TestExtraction`.

**A case is opened by code**, from an explicit key in a mail he sent (`created_by: owner`) or in
an AUTHENTICATED inbound mail. An unauthenticated stranger cannot plant a case, and its payment
coordinates never enter a case's history — otherwise a spoofer could seed the account it later
asks to be paid to. **Authenticated is not enough for the history either**: a sender passing DMARC
on its OWN domain can mail the order number with its own account first. So once a case has an
admitted sender, its account history is only what admitted senders sent (`coordinates_before`);
before it has one, the whole authenticated history is all there is, and that is weaker.
`TestCaseCreation`.

**A case's recorded facts come only from his own or authenticated mail** (round-3 note N1).
Keys and their kinds, amounts, payment accounts and authenticated domains are written onto a case
by observation only from his outbound mail or an AUTHENTICATED inbound one; unauthenticated mail
that quotes a case's key is still attached to it and shown, and characterises nothing. Before,
keys alone were written from any mail, so a forged mail quoting a real order number plus
*"zamówienie nr FAKE123"* could plant an order key and make an item a delivery. The readers apply
the same law to facts recorded before it (`mailitems.Context._trusted_fact`: a key whose recording
mail is not his own or authenticated characterises nothing). `TestCaseFactsComeOnlyFromTrustedMail`.
**Membership obeys the same law** (round-4 note 1): a mail is attached to a case by any key, but
it is a MEMBER for anything that grants or decides visibility only when it is his own mail, or
authenticated AND joined through a key a trusted mail recorded, a key he typed himself, or a direct
reply to his mail on the case. `mailitems.Context.cases_of` is the one reader — the case tie, stored
amounts, an item's meaning, admitted domains, his closed case covering an item and the keeper
release (`Context.trusted_closure`) all go through it; `attached_cases_of` is for display only.
`TestMembershipMustRestOnATrustedFact`. **The list of facts has one writer**
(`CaseStore._record_facts`, round-4 note 4): observation and the Tuta recheck
(`record_authenticated`) both go through it, so a mail authenticated later gains its keys too, not
only its domain, amounts and accounts. `TestTheRecheckRecordsKeysToo`.
**A known weaker edge, stated:** a case's account history before it has an admitted sender is the
whole AUTHENTICATED history (`coordinates_before`) — a stranger passing DMARC on its OWN domain
can seed it; N1 removes the unauthenticated half of that, not this one (§Residuals).

**Alias admission is deterministic corroboration, and the corroboration is HIS.** A sender
domain is admitted on a case when it was seen there in authenticated mail AND he then acted on
the case TOWARD it — wrote to an address at it, or replied to a mail from it
(`_admit_toward`). The design's wording is *"authenticated domain co-occurring on an
owner-transacted case"*; co-occurrence alone was the first draft and the design review broke
it: DMARC proves a sender owns ITS domain, and an order number is the kind of string that leaks,
so an attacker passing DMARC from his own domain with the owner's real order number would have
become an admitted sender on that order. Requiring his act toward the domain is stricter than
the design's sentence and admits fewer senders — couriers he never writes to are never admitted
— which costs correlation strength, never safety. **A Reader's judgement never admits.** Its case
choice for a sender not admitted there is recorded as a proposal with a count
(`CaseStore.propose`) and read by nothing. The owner-act path for admitting by hand is S3.
`TestAliasAdmission`.

**Candidates** (`candidates`): a shared key, a shared amount, a counterparty name appearing in
the mail (display names and domain labels; `difflib` at `NAME_SIMILARITY` (0.85) — no
embeddings, so there is no dependency to degrade), activity inside `TIME_WINDOW_DAYS` (7), and
activity inside `RECENT_DAYS` (30). Scored, bounded to `MAX_CANDIDATES` (5), each with its
reasons. **Only facts STRICTLY before the mail count**, so a mail never correlates with the case
it opened itself. The courier/store pair — a courier notice naming the store, two days after the
order, with no shared key — is the synthetic case pinned here. `TestCorrelator`.

**Deterministic case flags** (`case_flags`): `coordinates_changed` names each linked case that
HAS an account history and whose history lacks an account this mail carries — code's comparison,
over the Reader's validated extraction and code's own regex hits together; `sender_not_admitted`
names each linked case that HAS admitted senders, none of them this one. Linked cases are the
key-linked ones plus the Reader's chosen candidate. **Both need a prior, and the corner without
one draws no flag**: a case he opened from his own sent mail (a key, no account, no admitted
sender yet), then an unauthenticated stranger quoting the leaked order number and asking payment
to a fresh account — zero case flags; only the Reader's judged axes and the Gate's authentication
verdict stand. `TestCaseFlags`.

**Persistence**: one file per case under `<state>/mail/cases/`, `atomic_write`, unreadable
counted; `evict` removes a case whole and leaves a dated tombstone. Nothing calls `evict` in S1
— there is no owner surface yet. `TestEviction`.

## The Reader — `mailreader.py` and `aish/charters/mail-reader.md`

A role in the `roles.py` framework (`docs/roles.md`), so R1–R7 hold as they do for every role:
two messages and no Agent, a code-validated typed value, the content-digest admission, an exam
that must exist for the charter to load, and the input BYTES in the evidence store.

**Two framework extensions, each with this customer.** A third output shape, `verdict`,
delegated to `mailreader` the way `objective` is delegated to `objective.py`: the frontmatter
declares only caps (`VERDICT_CAPS`), and the skeleton, contract text, tally and exam assertions
live in the module. A third model class, `caller`: the caller names the model and per-model
admission decides which may run. The Reader is examined on two models side by side because their
disagreement is the backtest's audit set, and it runs from a CLI with no session.

**The bundle is code-composed** (`compose`): the conversation as of the mail, both directions
(`to_owner` / `from_owner`), the last `READER_CONVERSATION_SHOWN` (6) earlier messages capped at
`READER_EARLIER_CHARS` (1500) and the mail itself at `READER_REVIEWED_CHARS` (6000); the
candidates with their keys, admitted senders and last activity; the trust facts. Every part is
named — `m1`, `c2`, `t1` — so the answer can refer to it and code can check the reference. One
input, declared `untrusted`, because it carries mail text; the charter says which fields aish's
code wrote.

**The verdict** (`validate_answer`): `disposition` (`DISPOSITIONS`), `wanted` (`WANTED`),
`deadline` (a real date, `none` or `cannot_tell`), `case` (a candidate ref, `new`, `no_case` or
`cannot_tell`), `sender_case` (`SENDER_CASE` — the HARD axis), `suspicion` (`SUSPICION`),
`payment_coordinates` (each value must appear, space- and case-normalized, in the message it
cites — an invented account is refused), `citations` (refs the bundle holds), and the open layer
— `meaning` and a self-named `kind`, capped, control-flattened, every URL replaced with `[link]`
(a verdict never carries a loose link; actions refer to the mail by id and its own link gate
fires at open time) and the `[aish:` marker broken (`sanitize`). **`cannot_tell` is lawful on
every axis.** `TestValidation`.

**Asking for more is a CLOSED shape.** The bundle lists what it can expand (`available`:
`rest of thread t1`, `case file c<n>`). An answer `{"more": [...]}` naming anything else — a
free-text query above all — is a validation error: an open query would make the Reader a
retrieval oracle over his mailbox, steered by whoever wrote the mail it reads. Code honours at
most `READER_ASK_ROUNDS` (2) re-invocations, and the last bundle offers nothing, so an ask there
fails validation. `TestAskForMore`.

**An answer that will not parse is its own ending.** Two failed validations are `invalid`, with
no verdict — never a guess. A newsletter is `gated` with no call; no admission is `unadmitted`
with no call. An assessment that RAISES is that mail's own `error` record, naming the exception
and not final, so the next pass tries it again and the rest of the pass is untouched
(`error_record`). `TestRead`, `TestShadowPass`.

**The exam is synthetic, and the owner's real half lives in the private tree.** The repository
is public, so every shipped case is invented (the `docs/replay.md` law); `roles.owner_cases`
already reads `~/.config/aish/roles/mail-reader/cases.yaml`, and his mined cases go there. Ten
shipped cases, both halves named in `TestTheCharter`: extraction (a Polish invoice's deadline and
account, a statement's debit date), correlation (the courier notice to the store order),
the hard axis (a free-mail address speaking for the shop; a lookalike changing the bank account),
lawful `cannot_tell` (a thin reply with no context), and injection (a mail instructing its reader
how to classify it; a mail posing as aish's own voice). `absent` needles that `sanitize` would
remove anyway are not used — a check that cannot fail is documentation wearing a test's clothes
(`docs/roles.md`); that is why the injection case asserts on `right away` and not on the
URL it pushes, which `sanitize` turns into `[link]` whatever the model does.

## The record and the store — `mailverdicts.py`

`<state>/mail/verdicts/<dd>/<digest>.json`, one file per MAIL (sharded by its digest),
`{"mail", "by_model": {spec: record}}`. **Idempotent by mail, model and charter text**
(`VerdictStore.has`): a final reading is never repeated, a charter edit is a new reading, and
an ending that made no call (`unadmitted`, `unavailable`) is not final — once admitted, the mail
is read. `invalid` IS final: re-asking would buy the same failure. The record is
`docs/trace-contract.md` §3.20, every field code-written except the validated verdict.
`TestShadowPass`.

## The counters

Four kinds, all recorded, none read to decide anything:

- **The Reader's closed axes** are declared vocabularies (`mailreader.DISPOSITIONS`,
  `WANTED`, `SENDER_CASE`, `SUSPICION`), matched against model output like
  `schedule.VERDICT_MARKERS`: asked once per verdict, matched when the answer is definite.
  `asked − matched` is the cannot-tell count. `count_verdict`, at the pipeline — not in
  validation, so a corrective retry does not count twice.
- **The mail word lists** (`_CREDENTIAL_WORDS`, `_RISKY_SUFFIXES`, `_REPLY_PREFIXES`,
  `_KEY_WORDS`, `_CURRENCY_WORDS`), counted at their call sites.
- Both drain once per pass into `<state>/mail/runs-YYYYMMDD.jsonl` in the `vocab` record shape,
  which `vocab.scan` reads beside the session logs, so `aish vocab` shows them. The drain is
  process-global; it is safe in this CLI, and **inside aish-web (S2) it must not drain**, or it
  would take an Agent's tallies mid-task.
- **The store scan** (`scan_records`, `aish-mailwatch --counters`): endings, every axis's
  values, self-named kinds with counts (proposals, never a vocabulary), gate flags, case flags,
  gated-by header, asks. Raw counts; no rate is printed. `TestCounters`.

## Ingress — `aish-mailwatch --once`

A CLI, not a loop inside aish-web: the driver runs and schedules it; S2 moves it into the
lifespan. One pass lists the inbox and sent mail for the last `--days`, fetches each the index
does not already hold (S1.5), folds
everything into the index and case store (`ingest`, oldest first, a held mail is a no-op), and
assesses each new INBOUND inbox mail (`assess`): Gate facts → Reader → case flags → record. His
sent mail is ingested — it is the ledger's evidence and seeds cases — and never judged.

**Read-only by construction.** `GwsMailbox._call` refuses any `gws` call outside
`READ_ONLY_CALLS` (messages list and get) BEFORE a process is spawned
(`ReadOnlyViolation`). **Fail closed on credentials**: the account needs its own directory under
`~/.config/gws-accounts/` (`account_env`, the per-account mapping the owner's gmail tools use:
the file keyring backend and that directory as the config dir); a missing one refuses the run.
**No notification**: health is a file (`watch_health.json`: consecutive failed listings, the last
good pass, the last pass's counts, and per inbox since S1.5) and the printed pass summary; the
owner reads it with `/mail`. `TestReadOnly`, `TestCredentials`.

---

## S1.5 — what the owner sees, both inboxes, quota, continuous runs

Four directives from the owner, in his priority order reversed: (1) live shadow validation
running continuously; (2) BOTH inboxes; (3) Gmail API quota discipline; (4) **a surface he can
see** — before S1.5 nothing the pipeline recorded reached him.

### The owner surface — `/mail` (`mailview.py`)

A terminal slash command (`docs/cli.md` §`/mail`). It lists the newest `MAIL_SHOWN` (20)
readings — `/mail <n>` up to `MAIL_LIMIT_MAX` (200) — newest first by the mail's own time: date,
the code-derived sender address (display name after it), subject and inbox; the Reader's reading
(`kind` — `meaning`); what it WOULD have done (disposition, what the mail wants of him, a stated
deadline, a non-quiet `suspicion` / `sender_case`); the Gate's facts (authentication or the
reason there is none, grade, newsletter-gated and by which header, structural flags with their
details, raised case flags); attachment names. Every row says shadow mode: nothing was done.

**The join is by digest.** The verdict store holds no subject or sender — `build_record` keeps
digest, provider id, gate and verdict — so each shown row reads its mail's own index file by
digest (`mailindex.load_header`, one file, contained). A verdict whose index file is missing is
shown as *not in the index: provider id …*, never dropped; an unreadable verdict file is
counted. `TestJoin`.

**Render safety is not delegated to the writer.** Subject, display name, attachment names and
flag details are a stranger's bytes; `meaning` and `kind` are a model's, steered by one; the
closed words were validated when written and are not trusted when read. `mailview.lines` takes
its sanitiser as a REQUIRED argument and passes every string from a record or an index file
through it before its cap; the terminal passes `cli._plain` (C0 and C1 — the one-byte CSI 0x9b
included — every ESC form with its string terminators, the bidi controls, U+2028/9), so
something removed is visibly marked. The stored sanitiser strips only C0 and is not sufficient,
which is why this runs at render, unconditionally. Caps are applied after sanitising, at render
(subjects are stored uncapped): `SUBJECT_CHARS` (120), `NAME_CHARS` (60), `MEANING_CHARS` (300),
`KIND_CHARS` (40), `WHY_CHARS` (200), `FLAG_TEXT_CHARS` (80), `ATTACHMENT_CHARS` (60).
`TestRenderSafety` seeds a row whose every field carries 0x9b, U+202E, CSI, OSC 8 and U+2028,
and a recording sanitiser proves no field bypasses it.

**The health line comes first**, because a watcher that stops must be noticeable where he looks
(the email-poll outage, `docs/web-server.md` §email_poll). Warnings: no pass recorded at all;
no pass for `STALE_AFTER_S` (45 min — three intervals); an inbox whose listing failed on the
last N passes, with the last good listing and the typed failure; the Tuta bridge's own
connection words when it is not connected; mail listed but not yet fetched; mail the last pass
could not parse (and so did not assess); and an inbox NOT read on the last pass, whose old
streak is then not shown as current (`last_run_at`). Each says what was
observed, never why. `TestHealth`, `TestWiring` (the slash registration; the web answers
terminal-only).

### Both inboxes — `mailtuta.py`

`TutaMailbox` sits beside the Gmail source (`mailsource.GmailSource`) behind one seam:
`listing`, `held`, `fetch`, `note`. It drives himalaya against tutabridge with the config
tutalib generates (`~/.config/aish/tools/_tuta/tutalib.py`, loaded by path; the secret
`TUTA_BRIDGE_PASSWORD` comes from aish's own store at run time, and its absence fails that
inbox's listing visibly while Gmail still runs). One shared state tree for both: the ledger
must see all of his acts whichever inbox they are in.

**Through Tuta a Message-ID is not an identity** (the delivery review's squatting finding). A
sender writes it, and when a Message-ID alone was the digest, a Tuta mail carrying a real Gmail
mail's id and an earlier forged Date became that mail to the index and the verdict store — first
copy won, and the Gmail copy was never read. So `mailindex.digest_for` addresses a non-Gmail
message by its gateway, Message-ID AND content together; it can never collide with a Gmail
digest. **This supersedes "digest dedup absorbs cross-delivery" and "first copy wins" for
anything that is not Gmail**: a mail delivered to both inboxes is now two entries and two
readings — the stated cost — joined into one conversation by Message-ID. And because two
messages may claim one id, a reply's parent is resolved by `MailIndex.reply_parent`: claimants
that agree on the sender are one mail (the Gmail copy preferred); claimants that DISAGREE make
the reply nobody's act, counted in `Health.ambiguous_parents` — so the ledger, the case store
and alias admission can lose an act to a squatter but never gain one. `TestMessageIdSquatting`,
`TestIdentityNotUid`.

**The authenticator is declared by the adapter, never inferred from headers.** Each parser
stamps `Header.mailbox`; `mailgate.authenticate_header` reads it, through
`GATEWAY_AUTHENTICATOR` (`gmail` → Gmail's authserv-id, `tuta` → `tuta-bridge`, anything else →
the explicit ending `not_readable_via_gateway`). Through the Tuta gateway nothing prepends an
Authentication-Results above the sender's, so the top-most one IS the sender's, and a
sender-written exact copy of Gmail's would authenticate. So `mailindex.from_rfc822` DROPS every
Authentication-Results header at parse time, still.

**Since 2026-10-31 (#461) Tuta's authenticator is the bridge's own header.** tutabridge
(epnasis/tutabridge#16, observed live on 2026-10-31: 6 of 6 sampled raw messages carried it)
writes `X-Tutabridge-Authentication: status=<authenticated|hard_fail|soft_fail|invalid_mail_from|
missing_mail_from|unknown>; status-source=tuta-server; encryption-auth=<…>` — Tuta's
server-computed `authStatus` — as the FIRST header line of every message it serves, after
stripping every copy the sender wrote. Its documented guarantee: *present means the bridge wrote
it, and exactly one copy exists.* aish checks that guarantee rather than trusting it. The adapter
declares the header (`mailtuta.AUTH_HEADER`, passed as `from_rfc822(first_header=…)`); the parse
records what it saw (`mailindex.first_header_observation` → `Header.gateway_auth` and
`gateway_auth_observed`): the value only when the raw message's FIRST line is that header and
exactly one copy exists in the header block — counted on the raw lines (any case, blanks before
the colon) AND by the email parser, the larger count winning, and the first line only when the parser agrees it is that header; lines are split as the parser splits them (CRLF, CR, LF), so a bare CR cannot fold a later line into the value. The Gate's
`mailgate.authenticate_tuta_bridge` then authenticates ONLY on: the observation is *the first
header line, the only copy*; `status-source` is named once and is `tuta-server`; `status` is named
once and is `authenticated`. Anything else is not authenticated, and `why` says what was seen —
*no X-Tutabridge-Authentication header*, *2 copies of … in the header block*, *… is not the first
header line (it is at header line 4)*, *status-source sender, not tuta-server*, *the bridge
reports Tuta's status=hard_fail* — never a cause. A copy in the body is not a header and is never
read; a header only counts when the ADAPTER asked for it, so another gateway's mail carrying the
same line authenticates nothing. **An index file written before this slice has no observation**
and reads *no … observation was recorded for this mail (indexed before aish read the bridge's
header)* — not authenticated. A watcher pass never re-fetches held mail; the one-time
`--recheck-tuta-auth` does, and records the observation onto the existing entry (§The Tuta
authentication recheck).

**What it changes, all by existing rules:** an authenticated Tuta mail opens cases, puts accounts
in a case's history, earns owner-act grades and becomes admissible as an alias exactly like
authenticated Gmail; an unauthenticated one still mints nothing on its own behalf — its sender is
`first-contact` whatever the history, it opens no case and puts no account in a case's history.
Pinned forgeries: the header not first, two copies (one genuine), two copies in different case, a
`status-source` other than `tuta-server` or absent, two `status` values, the header inside the
body, a perfect Gmail Authentication-Results, the line on a gateway that did not declare it.
`tests/test_mailtuta_auth.py` (`TestTheBridgeHeader`, `TestForgeries`, `TestEndToEnd`); the forged perfect-Gmail-header case is pinned end to end in
`TestAuthenticationIsDeclaredByTheGateway`. **A mail
in his Tuta Sent folder is his own act and legitimately counts** — acts toward its recipients,
an owner case from a key in it, admission of the domains he wrote to — as his Gmail sent mail
does. That rests on the Sent folder being his alone: only Tuta puts mail there, and the bridge
offers no APPEND, so no IMAP client can. `TestAuthenticationIsDeclaredByTheGateway`.

**Direction is the folder.** On Tuta a mail is `out` iff it sits in his Sent folder
(`\Sent`, else a folder named Sent). A From header naming one of his addresses is something any
sender can write; in his inbox it makes nothing his, so it can never become an owner act toward
the attacker it is addressed to (pinned). Counted, not acted on: `inbox_from_an_owner_address`,
and `sent_from_an_unlisted_address` — a mail in his Sent folder from an address not passed as
`--owner`, i.e. an alias the watcher was not told about. **Gmail now decides direction the same
way, by its own evidence**: `out` iff Gmail's `SENT` label (`from_gmail`); a From naming him
without it is `in`, assessed like any inbound mail, and counted `inbox_from_an_owner_address`
on the Gmail source (`TestGmailDirection`). Before the S1.5 review a From header decided Gmail
direction, so a forged From: him minted acts toward its recipient, could open an owner case and
feed alias admission, and was never assessed. `--owner` still names his addresses, for the
counters and the bridge address. `at` is the sender-written Date header on Tuta (counted per
pass as `at_from_sender`). **A Date that will not parse costs the mail its date** (`at_source`
`none`, `mailindex.date_header_time`), never the mail: before, it made the mail unparseable,
which let a sender opt a mail out of assessment (`TestMalformedDate`).

**Reading never sets \Seen.** His unread markers are his mailbox state and the shadow's ground
truth. `message read` runs without `--seen` (the `tuta_read` / `tutalib.read_body`
discipline), and `mailtuta.check_argv` refuses BEFORE a process starts any argv outside
`ALLOWED_ARGV` — the subcommand AND every flag, so `message read --seen` is refused, as are
store, move, search, raw and expunge. `TestReadOnlyFence`.

**Listing is enumeration, never SEARCH.** tutabridge answers some SEARCHes with a silently
empty set; a SEARCH listing would read as a healthy quiet mailbox forever. A listing reads the
folder's STATUS count and pages envelopes by sequence number from the newest
(`ENVELOPE_PAGE` (200) per connection — the bridge falls over under bursts), stopping at the
first page wholly older than the window. An empty folder is a healthy empty listing; a failed
STATUS or page is a failed listing. `TestEnumeration`.

**Provider ids are treated as unstable here** (UIDs renumbered on restart, UIDVALIDITY
unchanged), so all skip-by-id and top-up logic is Gmail-only. **The bridge fixed this
(epnasis/tutabridge#19: stable UIDs), and aish does NOT rely on it yet.** Flipping
`TutaMailbox.stable_ids` would now be possible, and would buy the Gmail-side economies for Tuta:
skip-by-UID instead of the Message-ID+sender+subject match, and the top-up ledger for failed
fetches. It is a separate change, made on an observation of UIDs surviving a bridge restart, not
on the bridge's word; identity stays content-addressed either way (`digest_for`), so flipping it
would move no digest. A Tuta fetch is skipped only when the index already
holds a TUTA message matching the envelope's Message-ID AND sender address AND subject — an
identity the renumbering cannot move, and one a squatter matching on the id alone cannot use to
suppress another mail's fetch. Anything less matching is fetched, and identical content dedups.
`mailtuta.check_argv` also refuses an `-m` whose value starts with `-`.
A message renumbered between listing and fetch is still a real message of that folder and is
kept, counted `renumbered_between_list_and_fetch`.

### Quota discipline — `mailsource.py`

**Failures are typed at the runner seam.** `default_runner` returns an `Outcome`, and a failed
`gws` exit becomes one `Failure` of a closed set (`FAILURE_KINDS`: quota, permanent, timeout,
transient, parse, missing, unknown) in exactly one place, `classify_gws`: a status read from the
reply first, words second, with what matched recorded. Gmail says a rate refusal as 429 or as
403 `rateLimitExceeded`, so the rate words win over the 403; *"Units per minute"* is quota; a
named daily quota is quota that is not waited out. `TestClassification`.

**One retry policy, owned by the adapter** (`docs/rate-limits.md`). `GwsMailbox._call` takes a
token from a `TokenBucket` (`GMAIL_CALLS_PER_S` (2.0) sustained, `GMAIL_BURST` (5)) before EVERY
call, so the watcher and the backtest both inherit it, then re-issues per `MAX_ATTEMPTS`: quota
up to 4 attempts with `QUOTA_BACKOFF_S` (10) doubling, timeout and 5xx one re-issue. A permanent
refusal is never retried; an unknown one is not either (the behaviour before typing, and an
unknown is not evidence that another call helps). A quota refusal that outlasts its retries sets
`quota_exhausted`, and the Gmail source stops asking for the rest of the pass — the remaining
mail goes to the ledger without spending an attempt (a refusal aish never sent is
`Failure.sent` False). The flag is reset when a pass starts (`GmailSource.listing`), and a
failed inbox listing does not go on to spend quota on the sent listing. **The backtest
honours it too**: `backfill` stops asking once it is set, counts the rest not fetched and says
so in the report (`quota_stopped`); a re-run into the same `--out` fetches only those
(`TestExhaustedThisPass`, `test_an_exhausted_quota_stops_the_backfill_asking`). The
bucket numbers are conservative CHOICES, not measurements; the only
observation is the 2026-10-24 backtest's 116 unfetched of 862 listed. `TestRetryPolicy`,
`TestTokenBucket`.

**The top-up ledger.** The premise that the index recorded fetched-vs-not was false: the misses
were counted and forgotten. `UnfetchedLedger` (`<state>/mail/unfetched.json`) records each
listed Gmail mail whose fetch failed, with its role and attempts; the next pass fetches exactly
those, even once they have left the listing's window. A permanent failure, or
`LEDGER_MAX_ATTEMPTS` (20) of them, abandons the entry, counted. **And the cheap complement**:
a Gmail id the index already holds is not fetched again — it was refetched every pass before.
The same skip makes a resumed backtest re-buy no fetch: labels are recorded per fetch in the
run's `fetched.jsonl`, because the index never stores one. `TestTopUp`, `TestHeldSkip`,
`TestResume`.

### Continuous runs — the launchd job

`scripts/install-mailwatch-service.sh <gmail-account> <model> [owner…]` writes and loads
`com.aish.mailwatch`: `aish-mailwatch --once --source gmail --source tuta` every 900 s, NOT
inside aish-web (S2 moves it there). Provider keys reach it the way they reach aish-web — through
`envrun` and aish-web's env file — never written into the plist; the gws variables the email-poll
outage taught are set by `account_env` itself. `--print` writes the plist and changes nothing.
It writes nothing into the automations or runs stores, whose only author is the approval card;
its health surfaces in `/mail`.

**Overlap is locked out, not accepted.** A launchd pass and a manual `--once` could otherwise
interleave two writers of one index, case store and ledger. `pass_lock` takes an exclusive
non-blocking `flock` on `<state>/mail/watch.lock`; the second pass exits (code 3) having touched
nothing. The kernel drops the lock when its holder exits, however it exits, so there is no stale
lock to clear. The trade: a pass that hangs holds the lock, and every later pass skips until it
ends — the health line then goes stale and says so. `TestPassLock`, `TestHealthPerSource`.

### The owner's exam cases

His eight adjudications of the backtest run 20261024-170329 — all eight would-have-expired mails
ruled "fine to bin": four one-time codes, two review requests, two notices whose date has passed
— are owner cases in `~/.config/aish/roles/mail-reader/cases.yaml` (the private tree, never this
repository), as synthetic-equivalent reconstructions: same shape, kind and language, invented
senders and codes. Each asserts the reading of his ruling, stated in the file so it can be
disputed: disposition `expire_after_grace` or `archive`, nothing suspicious, and (for the codes
and the outage notice) nothing wanted of him. **Adding them changed the admission bar**: the
owner-cases digest is part of every admission, so each model must be re-examined before the
Reader runs again.

**Charter v2 states his review-request preference** (2026-10-25). Re-admission with these cases
showed both models answering `surface` on the second review request, and the charter's prose
said nothing about such mails, so the exam enforced a preference the Reader in production could
not see. Two review nags ruled "fine to bin" now stand in the disposition guidance: review,
rating or feedback requests about a completed purchase are expendable, and a repeat adds nothing.
Two exceptions sit in the same sentence: a benefit with a deadline, or an unresolved problem
with the purchase. The case's reconstruction had also dropped the property his ruling rested on,
which was that the mail was a REPEAT. It now carries the earlier unanswered ask in its thread.

**Charter v3 states his one-time-code preference** (2026-10-25): re-admission showed both models
answering `surface` and `decide` on all four code cases against prose that said nothing about
such mails, and his ruling — a code asks nothing of him through aish because he is signing in
himself, and it may sit in the inbox until it has expired — now stands in the disposition
guidance as `nothing` and `expire_after_grace`, deliberately with no exception for a code he did
not request.

## S1.6 — live conversations, reply recognition, the review file

Three directives from the owner (2026-10-25): (1) never auto-file a live conversation — keep
active conversations visible; (2) teach the Reader to recognize those, and mail that wants an
answer from him; (3) a CSV review file of every judged mail, to sort and filter in a spreadsheet.

### The measurement that framed it: the misses were openers

The year backtest (run 20261025-110427) recorded 8 mails the gemini arm would have ARCHIVED that
he replied to: insurance policies from his agent (one of them his own forward), a booking
confirmation for an upcoming stay, a meter-exchange protocol, a property-fee clarification, a visa
application receipt, an Airbnb receipt. Measured against that run's own index (the conversation as
of each mail, its `direction == "out"` members before it): **he had written in 0 of the 8 threads
before the mail — all 8 were conversation OPENERS.** So the code clamp below, which keys on his
having written in the conversation, would have changed none of them. It protects CONTINUATIONS
only; the openers are carried by the teaching (charter v4 and his exam cases). Nothing here claims
the clamp fixes the 8. The backtest reports raw and clamped counts side by side so the two can be
told apart: the clamp's effect is the difference between them, the teaching's is the raw count
moving.

### The fact — `owner_wrote_in_this_conversation`

`compose` adds a code-computed bundle fact: when he last wrote in THIS conversation before the
mail (`3 days ago`), or `never` (`mailreader.owner_wrote_words`). **Past-facing in the index's
clock**: only conversation members at or before the mail's own position count, so a reply he sends
LATER never makes the earlier mail live (`_owner_wrote_days`) — as far as the mail's `at` is
honest, which through Tuta it need not be (*Residuals*). **Computed over the full conversation**,
before the shown slice is cut to `READER_CONVERSATION_SHOWN`, so a long thread whose oldest message
is his still reads as live. **Thread-scoped by name**: it says he wrote in this thread, not that he
knows the sender — that is the ledger's grade, which counts his acts toward an ADDRESS. The record
carries it as `owner_wrote_days` (days, or null for never) for every assessed mail, gated ones
included. `TestTheFact` in `tests/test_mail_live.py`.

### The clamp — code wins, the verdict is untouched

When the fact is at most `LIVE_CONVERSATION_DAYS` (30) and the validated verdict's disposition is
`archive` or `expire_after_grace`, the EFFECTIVE disposition is `surface` (`mailreader.clamp`).
**30 is the owner's explicit choice (2026-10-26: "if I go on vacation, 14 days would file things
too quickly"), not a measurement** — stated like the `TokenBucket` numbers, and moved only on
evidence or his word. It is code-written into the record BESIDE the verdict, `clamped: {from, to,
owner_wrote_days, limit}`; the verdict itself is never rewritten, so what the Reader answered stays
measurable. One reader decides what the pipeline would do, `mailverdicts.effective_disposition`,
for `/mail`, the CSV and the backtest alike; a record written before the clamp reads as its
verdict. It only ever moves toward his attention. `/mail` shows `would: surface [CLAMPED from
archive: you wrote in this conversation N days before it, within 30]`. The charter's prose says
"recently"; the number lives in code and in the fact. `TestTheClamp`, `TestTheOwnerSurface`.

**Joining a live thread earns visibility, never capability.** A hostile mail naming his thread's
Message-IDs in References joins the conversation (*Residuals*), so it gets the fact and the clamp:
it stays visible. It gains nothing else — the ledger counts acts by a reply's OWN In-Reply-To, so
membership mints no act and no grade, and the clamp only ever surfaces. The fail-safe direction: a
wrong join costs him one more mail to look at. `TestJoiningByReferences`.

### Charter v4 — the teaching

Beside v2's and v3's sentences, in the disposition guidance: a live conversation is never filed —
a keeper document arriving in one still belongs to its case, but the mail stays visible; `archive`
is for a record with nothing left for him to do, and a mail that carries a record AND asks him
something, or leaves him something to do ahead, is `surface` even when it opens a conversation.
Under `wanted`: a direct question or request addressed to him personally wants `reply`; a no-reply
or bulk sender's courtesy "reply to this email" is not a request of him. Every shipped golden
pair's bundle now carries the fact as `compose` writes it. `TestCharterV4`.

**His exam cases** (private tree, 15 after this slice): four OPENER reconstructions of the 8 — the
insurance policy with his agent's question, the booking confirmation for an upcoming stay with
payment due on arrival, the meter protocol asking confirmation, the property-fee clarification —
expecting `surface` or `hold`, and `reply`/`decide` where the text plainly asks him something.
Their bodies were mostly not recorded in the run (HTML-only), so each reconstruction states in its
text the property the ruling rests on. Two CONTINUATIONS (he wrote 4 days before; the reply carries
an offer, a refund confirmation) pin the charter half of the clamp. One over-teaching CONTROL: a
shop's transactional notice with a "just reply" footer and no list header must NOT want `reply` —
a listed newsletter would test a dead path, since the Gate skips those before the Reader. **Charter
v4 and the changed cases move the admission digest**: every model is re-admitted before the Reader
runs again, and a resumed backtest run directory re-buys its Reader calls under v4 (a new charter
digest is a new reading — expected, not a resume bug).

### The review file — `mailexport.py`

One exporter over the verdict store and the index, through `mailview.collect` — the SAME join
`/mail` shows, never a second one — for a backtest run directory or the live store. One row per
(mail, model): date, `from_addr`, display name, subject, inbox, model, `ending`, kind, meaning,
`disposition_raw`, `disposition_effective` (after BOTH clamps), `clamped`, `keeper_clamped`
(`yes (from <disposition>: <signals>)` or `no`), `owner_wrote_days`, wanted, deadline,
suspicion, `sender_case` (the hard axis), case (the resolved case id or the case word),
`newsletter_gated_by`, the gate summary (`mailview.gate_words`), charter version, digest, provider
id. **No cell stands blank for unknown**: a gated, invalid, error or unadmitted reading keeps its
ending, and its verdict cells say `gated` or `no_verdict` exactly as the report counts them
(`mailexport.verdict_word`, which the report's own cell function calls); a missing index entry
says so; an empty meaning or kind is `(empty)`, a verdict lacking an axis (only corrupt bytes
can) reads `(missing)`, and a record from before the fact is `not recorded`.

**Every cell takes one path**: the caller's sanitiser (required — `cli._plain`), whitespace
flattened, the spreadsheet-formula escape (a leading `=`, `+`, `-`, `@`, tab or CR — also behind
leading whitespace — gets an apostrophe, `formula_safe`), then the cap. The escape runs on every
column, so there is no list of "safe" ones to drift. Python's `csv`, `QUOTE_ALL`, UTF-8 with a BOM
so a spreadsheet reads the Polish. `TestCells`, `TestRows`.

**Surfaced twice**: `/mail export [path]` (the live store, no truth columns; `docs/cli.md`) and
`aish-mailbacktest --csv <run-dir> [--out-file PATH]` — export-only: no account, no mailbox, no
model. Both land in the Downloads folder by default (§Where the file lands). A backtest file adds `truth_recomputed_at_export` and
`tier_recomputed_at_export`: **truths are never persisted**, so they are recomputed as the harness
computes them — `truth_of` over the run's index, with labels re-unioned per digest from the run's
`fetched.jsonl` (`recorded_labels`, the same union `backfill` makes) — and the column names say so.
`TestBacktestExport`.

**One authority for the counts.** `report.json` carries no per-cell digest lists: every per-arm
cell is a filter over the file's columns, pinned by
`test_every_arm_cell_of_the_report_is_a_filter_over_the_columns`. Filter `model` = the arm, then:
`endings` → by `ending`; `disposition_by_truth[tier:label][v]` → `tier_…` = tier, `truth_…` =
label, `disposition_raw` = v (`…_clamped`, the live clamp ALONE: `surface` where `clamped` starts
with `yes`, else `disposition_raw`; `…_keeper`, after both clamps: `disposition_effective`); `wanted_by_reply[tier:replied]`
→ `truth_…` = `replied` (else `not_replied`), by `wanted`; the HEADLINE
`destructive_expire_on_kept` → `disposition_raw` = `expire_after_grace` and `truth_…` one of
replied, starred, archived, kept_read, unread (`…_clamped` and `…_keeper` as above);
`archive_on_replied` → `disposition_raw` = `archive` and `truth_…` = `replied` (likewise);
`clamped` → `clamped` starts with `yes`; `keeper_clamped` → `keeper_clamped` starts with `yes`; `gated_by_truth[by][label]` → `ending` = `gated`,
`newsletter_gated_by` = by. **The window, tier and trash counts are not in the file**: they
describe every inbound mail in the window, judged or not, and the file holds judged mail only.
**And the report counts the run's SELECTION**: a store re-run at a smaller `--reader-max` holds
rows the report did not count, and a row from an earlier charter keeps its own `charter_version` —
filter on it.

### What a backtest costs

Two independent costs, and `--reader-max` bounds only one. **Reader calls** scale with
`--reader-max` (each selected mail is at most one call per arm, plus at most `READER_ASK_ROUNDS`
re-asks). **Fetch cost** scales with `--days` × his mail volume: the backfill lists the whole
window and fetches every listed mail once, one `gws` call each, under the bucket — 862 listed in
90 days on 2026-10-24 — whatever `--reader-max` is. A resumed run into the same directory re-buys
no fetch whose mail its index holds. **With `--source tuta`** the Tuta window is enumerated and
every Tuta mail in it fetched too — one himalaya call (one bridge connection) per mail, plus one
per envelope page — and its inbound mail joins the same `--reader-max` selection; Reader cost is
still bounded by the cap, fetch cost grows by his Tuta volume.

## S1.6 follow-up (2026-10-26) — exports in Downloads, the backtest over Tuta

Two owner directives: the CSV review file lands where he looks for downloads, and the history
backtest measures his primary inbox too.

### Where the file lands — `mailexport.export`

One function decides for both surfaces, in this order: **an explicit path** (`/mail export
<path>`, `aish-mailbacktest --csv <run> --out-file PATH`; a directory gets the generated name
inside it, a file path is written as named) → **the Downloads folder**, tried only when this
process's stdin is a terminal (`mailexport.interactive`) → **the fallback**: the live store's
`<state>/mail/exports/`, or the backtest's run directory — exactly where each wrote before.
Whatever sends a file to the fallback is printed as what was observed: *not run from a
terminal*, *`<dir>` does not exist*, *is not writable*, *writing into `<dir>` failed
(`<the OS's words>`)*, *could not write `<path>`* — followed by where it went. The folder never
fails the export; only a failing fallback write can.

**Exports are meant to be generated from the owner's own terminal; background contexts fall back
to the state dir.** ~/Downloads is a macOS privacy-protected (TCC) folder, and on this machine a
session running under the remote-control LaunchAgent that opened such a folder blocked forever.
So the choice is: a process with no terminal on stdin never resolves or stats Downloads at all —
the stat could itself be the call that blocks. The cost of the choice: run from a terminal with
stdin redirected (`< /dev/null`), the file goes to the fallback, and the printed line says so.
`test_outside_a_terminal_downloads_is_never_touched`. The location comes through a seam,
`AISH_DOWNLOADS_DIR`; the suite-wide `no_real_downloads` fixture (`tests/conftest.py`) points it
at a temp dir, so no test can resolve the real folder.

**The name says which run and when, and never overwrites**: `aish-verdicts-<run>-<YYYYmmdd-
HHMMSS>[-N].csv`, where `<run>` is the backtest run directory's name (its own timestamp by
default) or `live`. The name is claimed with `O_EXCL` before the file is published, so a second
export in the same second takes `-2`, `-3`… (`_claim`). Before, the live name carried seconds
only and the backtest file was a fixed `verdicts.csv` that every export replaced.
`TestWhereItLands`, `TestExportSurfaces` in `tests/test_mailexport.py`.

### The backtest over Tuta — `--source tuta`

`--source` is repeatable, as the watcher's is. **The default stays Gmail only**, so an existing
invocation fetches and reads exactly what it did: adding Tuta silently would add a fetch per Tuta
mail in the window, and move which mails the arms read under the same `--reader-max` — a cost
surprise. A Tuta-only run needs no Gmail account.

**The shipped adapter, reused.** `TutaMailbox.history` enumerates INBOX, Sent and — when the
bridge exposes one (the `\Trash` attribute, else a folder named Trash: tutalib's rule) — Trash,
by the same `_enumerate` the watcher uses: STATUS count, envelope pages by sequence number,
never SEARCH, with `--flags` added to each page fetch (the argv tutalib's own envelope read
already issues; `--flags` was in `ALLOWED_ARGV`). Every fetch is `TutaMailbox.fetch`, so no
`--seen`; a fetch is skipped only on `held`'s identity match (Message-ID AND sender AND subject),
never a UID. `test_listing_is_enumeration_with_flags_never_search_never_seen`; `TestUnstableIds`.

**Truth from folder and flags, in the labels `truth_of` already reads** (`tuta_labels`): a mail
enumerated in INBOX is `INBOX`, plus `UNREAD` when the listed flags lack `\Seen`; `\Flagged` is
`STARRED` (tutalib's own vocabulary maps "starred" to it); Trash is `TRASH`. **Replied** is the
same `later_replies` over the one index: a message in his Sent folder is his act (`direction
out`), and it is past-facing at export exactly as Gmail's is. **What cannot be observed is set
apart, never guessed**: a page that carried no flags list for a mail makes it `seen_unknown`; a
Tuta mail no enumerated folder named this run is `unobserved` — never `archived`, because
archiving in Tuta is a move to a folder the backtest does not enumerate. Both are in the
set-apart tier, so neither is judged. A message renumbered between listing and fetch keeps its
folder and loses its flags (they were another message's): `seen_unknown`.
`TestTutaTruth`, `test_a_renumber_between_listing_and_fetch_costs_the_flags_never_guesses`.

**The run's log keys Tuta by digest** (`tuta-digest:<digest>` in `fetched.jsonl`), one line per
digest with this listing's labels, so `recorded_labels` — and through it the review file's
recomputed truth — reads Tuta exactly as `backfill` did, and no remembered UID ever names a
different message. `test_the_log_keys_tuta_by_digest_never_by_uid`.

**Window depth is a stated fact.** Enumeration pages until the first page wholly older than the
window, or the folder's first message. Each folder's walk is kept (`TutaMailbox.depth`), and the
report header states a folder that ended before the window began — *"tuta INBOX: the folder holds
N message(s), the oldest dated D — shorter than the 90-day window"* — an empty folder, or a
listing stopped at `LISTING_LIMIT`. Never an error. It also states, for Tuta, the trashed inbound
it saw and that how long Tuta keeps Trash is not known here; when there is no Trash folder,
that trashed Tuta mail is absent; and that only INBOX, Sent and Trash are enumerated.

**One index, one selection.** Both inboxes fold into ONE scratch index and case store, ingested
in position order, so the frozen clock spans both. `select` is unchanged: strong, then weak, then
near-null, each oldest first, across both inboxes, under the one `--reader-max`; the report names
the split (`read by each arm: 4 selected mail(s) (gmail 2, tuta 2)`). A failed Tuta listing is
stated and the Gmail half still runs. `TestBothInboxes`, `TestSources` in
`tests/test_mailbacktest_tuta.py`.

## The keeper clamp (2026-10-27) — keeper documents are never filed on arrival

**The owner's decision (option A):** keeper documents — insurance policies, bookings, visas, order
and invoice confirmations — are never filed (`archive`) or binned (`expire_after_grace`) on arrival;
they stay visible until their case closes. **Why code and not teaching:** the full-year backtest
(run 20261025-204111, charter v4) still filed 12 mails he replied to — 11 archived, 1 expired — and
the live-conversation clamp cannot reach them (it clamped 0 readings there: they are openers).

### The rule — `mailreader.keeper_clamp`, beside the live clamp, under its law

Same shape as the live clamp: when the validated verdict's disposition is a filing one, code writes
`keeper_clamped: {from, to, signals, cases}` BESIDE the verdict, which is never rewritten, and the
effective disposition is `surface`. It fires on any of three signals, each of which can only keep a
mail visible:

- `case_key` — the correlator extracts a case key (`mailcases.extract_keys`, the same regex that
  opens cases) from the mail, AND its sender is authenticated;
- `document_attachment` — an attachment whose last suffix is in `mailreader._DOCUMENT_SUFFIXES`
  (pdf, doc, docx, odt, rtf, xls, xlsx, ods, pages, numbers — a declared, counted list), or whose
  MIME type is `application/pdf`, AND its sender is authenticated (a mail with no attachment asks
  the list nothing, so its counter is not swamped by mail that could never match);
- `reader_case` — the Reader placed the mail on a case (a candidate ref or `new`). **A judged
  signal is lawful here because it can only RESTRICT**: it can block filing and permits nothing.

**Never on a newsletter-gated mail** — the rule refuses `gated` whatever the signals say (a gated
mail has no verdict anyway; the refusal is the belt to that brace). **Unauthenticated mail — Tuta mail
without the bridge's authenticated header included — gets it from the judged signal only**: the deterministic signals name
an authenticated sender, as the owner's rule states. What it gets is visibility: the clamp writes
nothing into the case store, the ledger or the admissions, so it mints no case, grade or alias.
`TestUnauthenticated` in `tests/test_mail_keeper.py`.

It only ever moves toward his attention, and never touches the verdict: `TestItFires`;
the gated refusal: `TestNewslettersNever`.

**Precedence is plain**: either clamp makes the effective disposition `surface`. Both are recorded
independently, so each one's effect stays measurable. **Restrict-only is checked by the reader
too**: `keeper_clamp_of` accepts a clamp only when it names `surface`, and odd shapes from corrupt
bytes neither raise nor release. `TestCorruptRecords`.

### Case closure releases it — his close, since 2026-10-31

`cases` is every case the mail is on at assessment (the cases whose mails include it, the
key-linked ones, the Reader's chosen candidate). `mailreader.keeper_released` is true only when the
mail is on at least one case and EVERY one of them is closed. **Since 2026-10-31 the owner's close
is that closure** (§The case verbs): `/mail`, the CSV export and the daily report pass the live
store's `CaseStore.closure()` to `mailverdicts.effective_disposition`, whose `keeper_released`
then requires every case the mail is on — as its record names them AND as the store holds it now,
so a moved mail must have both cases closed — to have been closed at or after aish first READ the
mail (`recorded_at`, aish's clock; a sender-written Date never decides it, and a record without
`recorded_at` stays held). Mail that arrives on a
case after he closed it stays held. Nothing here invents a closure: there is still no deterministic
closure (delivery confirmed, trip ended), and a keeper on NO case — a document from a sender with
no key and no Reader case — can never be released and holds for good. The backtest's scratch store
has no owner acts, so it holds there. `TestCaseClosureReleases`, `TestClosureReleasesTheKeeperClamp`.

### The surfaces

`/mail` shows `would: surface [KEEPER-CLAMPED from archive: case key, document attachment — kept
visible until its case closes]`. The CSV's `disposition_effective` is after both clamps and its new
`keeper_clamped` column marks the row. The backtest report adds, beside the raw and live-clamp
counts, `after the keeper clamp too (N reading(s) keeper-clamped)` and a `disposition after both
clamps` table; `report.json` carries `keeper_clamped`, `…_keeper` cells. `TestTheSurfaces`.

### The measurement — what it catches and what it costs

Recomputed over run 20261025-204111's own stores, read-only (its verdict records, index, bodies
and case store; truth from that run's CSV export's `truth_recomputed_at_export`; the run tree's
sorted stat listing identical before and after). One arm, `gemini:gemini-3.8-flash`, 6285 records,
2164 of them filed after the live clamp.

- **Caught: 10 of the 12** replied-but-filed. 7 by a deterministic signal (all
  `document_attachment`, with `reader_case` beside it on 5), 3 by `reader_case` alone (two Gmail,
  one Tuta). **Not caught: 2, both Tuta, no attachment, Reader case `no_case`** (one archived, one
  expired). No signal exists on them; neither an auth-free variant of the deterministic signals
  catches them (measured: still 10).
- **Cost: 849 more mails kept visible** of the 2164 filed (by truth: unread 631, kept_read 198,
  replied 10, trashed 8, archived 2; Gmail 295, Tuta 554). **The judged signal is most of it**: 722
  of the 849 rest on `reader_case` alone (unread 545, kept_read 166, trashed 8, replied 3); 127
  carry a deterministic signal (unread 86, kept_read 32, replied 7, archived 2).

So the deterministic signals alone would catch 7 of the 12 at a cost of 127; the judged signal
adds 3 catches for 722 more visible mails. That trade is the owner's to make — stated here, not
decided. Whether the 12 are the 8 keeper documents the decision names is not checked: the
measurement reads truth labels, not his classification of each mail.

## The daily report (2026-10-28) — `mailreport.py`, `aish-mailreport`

**The premise amendment, recorded so it is not "fixed" back.** The epic's governing line
*"delivery stays event alerts, not digests"* (#295/#281) is amended for the inbox assistant by
the owner's explicit request: *"Daily inbox report on everything needs action from both new
emails as well as follow ups and also separately for info that may be of interest. Basically ai
assistant perspective on my inbox."* (epic #142, comment of 2026-10-28). It is W5's proposals
step in digest form; event pushes stay deferred. **Delivered as an aish-web chat**, not an email
(owner directive the same day): one new chat each morning, which he can answer — *"pay this"*,
*"file that"* — in place.

### What it holds — three sections, built by code, no model call

Read from the verdict store and the thread index only (and the watcher's health file and the
report's own ledger). Every reading per mail: the newest one with a verdict; a mail any model
saw newsletter-gated is gated.

- **Needs your action** — new mail whose stored verdict says `wanted` ∈ {pay, reply, decide},
  ordered dated deadline soonest first, then pay, reply, decide, then newest. Each item: what is
  wanted and by when, sender, subject, the Reader's one line, inbox, date, and — for Gmail mail
  only — an *open in Gmail* link built by CODE from the hex provider id (`gmail_link`).
- **Follow-ups** — deterministic, below.
- **May interest you** — new mail whose EFFECTIVE disposition (`mailverdicts.
  effective_disposition`, after both clamps) is `surface` with `wanted` nothing. **v1 is not
  interest matching**: there is no interest index (S5), and the section header says so in
  words — *these are new mails the assistant would keep visible that ask nothing of you*.

**Excluded everywhere, and counted where he reads it**: newsletter-gated mail (the gate's record,
or the index's own List-Id / List-Unsubscribe — so a mail with no reading is still recognised by
code) and one-time codes and sign-in links (`_ONE_TIME_CODE_PHRASES`, a declared, counted PL+EN
list matched word-bounded against the folded Reader `kind` and subject; the owner's v3 ruling).
Mail with no reading (unadmitted, error, invalid) is counted with its endings, never dropped,
and so is every reading in no section: `wanted` `cannot_tell` (*the Reader could not tell what
they want of you*), `wait`, and `nothing` the pipeline would hold or file — every eligible
reading is in a section or a count (delivery review B2). A mail delivered to both inboxes (two
digests, `digest_for`) — the same Message-ID FROM THE SAME SENDER; two senders claiming one id
are two mails (review M4) — is shown once, the Gmail copy preferred, the dropped copies counted
and recorded as shown with it, so they never return as new (review B1). The report's footer says all of it: *Not shown: N
newsletter-gated, N one-time code(s) or sign-in link(s), N with no reading (endings: …)*.

**Caution, visible, never hidden — he decides**: `suspicion` suspicious, `sender_case` mismatch,
or the case flag `coordinates_changed` put a **CAUTION** marker on the item in code's words, and
the Reader's summary of that mail is WITHHELD: a summary of a suspicious mail is exactly where an
attacker's account number or *"pay X to Y"* would ride in aish's report (design review M5).

### What is new — the eligibility window

A mail is news when it arrived at or after the floor and no earlier delivered report SHOWED it
(the ledger records the digests shown). The floor is: the first report, 7 days back
(`FIRST_RUN_DAYS`); after a delivered report, that report's build time minus `LATE_GRACE_DAYS`
(7) — so a mail read late (an admission lapse, a top-up fetch) or held over a section's cap
(`NEEDS_SHOWN` 30, `INTEREST_SHOWN` 20; the overflow line says they stay eligible) still reaches
him once, and nothing is shown twice. The time judged is the provider's receipt time when there
is one; a sender-written Date (Tuta) is never later than when aish first read the mail, so no
sender can date a mail into the future to keep it news, and an undated mail is judged by when it
was first read. `--since YYYY-MM-DD|Nd` asks for everything since then, whatever earlier reports
showed. Both 7s are CHOICES.

### Follow-ups — the deterministic definition

The measured precursor of the conversation-state axis (the 2026-10-26 directive). **Per
counterparty, not per conversation**: for every conversation whose newest message is inside
`FOLLOWUP_WINDOW_DAYS` (60 — the owner's *30–60 days*), every address he wrote to there (his
`direction: out` mail — the SENT label / Sent folder, never a From header), minus his own and the
bot's addresses and any address that mails him from a list:

- **your turn** — that address sent mail after his last message to it, and at least one such
  mail's reading wants `reply`, `decide` or `pay`, or `cannot_tell`, or has no reading at all
  (the fail-visible direction; the line says *no reading recorded*). Newsletter-gated and
  one-time-code mail is not an answer. Shown with days waiting since the first such mail.
- **their turn** — nothing from that address since his last message to it — anywhere in the
  index, so an answer in a new thread counts (review M7) — and that message is
  `THEIR_TURN_DAYS` (5) or more days old — **a stated choice, not a measurement** — and inside
  the window. Pinned at the boundary: exactly 5 days is in, a second less is out.

**Why per counterparty** (design review B3): a conversation is a union any mail can join by naming
a Message-ID (*Residuals*). Keyed per conversation, a stranger joining one would have made it his
turn with an attacker-chosen subject, or put his last word to someone else in front of a real
pending answer. Keyed per address he wrote to, the joiner is nobody he wrote to, and the subject
shown is HIS message's. Answers are ordered by the same clamped time as eligibility. Both inboxes
are one index, so a Gmail message answered through Tuta is one follow-up. Recomputed in full every
morning — it is state, not news — and a conversation whose answer already sits under *Needs your
action* today is not repeated. **When the index holds none of his sent mail in the window** the
section says so — *the thread index holds none of your sent mail from the last 60 days (it
reaches back to …), so follow-ups cannot be computed from it* — because an empty section would
read as "none". That is the live state on 2026-10-28 (*For the driver*, below).

### Render law, and the one link

`mailview`'s law: every mail- or model-origin string goes through the caller's sanitiser
(`cli._mail_cell`, which is `cli._plain`: C0/C1/ESC/bidi/U+2028-9), whitespace flattened, then
`_disarm_links` — every `scheme://` link, opaque scheme (`aish-reply:`, `javascript:`, `press:`,
`data:` …) and `www.` host becomes `[link]`, and `](` is broken, so the chat's markdown
(`app.js` INLINE_RE: http links, quick-reply chips, images, local files) can form nothing from
it — then the cap. Applied at render whatever write-time did. The report is markdown-light plain
text; the ONLY link in it is the code-built Gmail one. `TestRenderSafety` seeds a mail whose every
field carries 0x9b, U+202E, CSI, OSC 8, a chip, an image, `javascript:` and `www.`, and a
recording sanitiser proves no field bypasses it; every URL left in the output is the Gmail one.

**The watcher's health leads the report.** Any `warn` from `mailview.health_lines` (no pass, a
stale pass, a failing listing) makes the headline say *THE MAIL WATCHER IS NOT HEALTHY, this
report may be incomplete* and each warning follows as a `WATCHER:` line: a report built on a dead
watcher must not look complete. **An empty day still delivers**: *nothing needs you.*, with each
section saying so in one line — a missing report must mean a fault, never good news.

### Delivery — a chat, through the one ingress, with no model in between

`aish-mailreport` POSTs `/trigger` (the poller's path into `launch_triggered`: dedup, rate
bucket, concurrency cap) with `origin: schedule`, `meta {kind: report, dedup_key:
mailreport:<date>}`, the title *Inbox report — Wed 28 Oct*, a task string of fixed words plus the
code headline of counts — **no mail text ever rides the task string** (the owner-hosts
laundering law) — and the report in the payload's `report` field. The server
(`docs/web-server.md` §The daily report) delivers it twice, both by code:

1. **as a KEPT outside note** (`Agent.add_outside_note(kept=True)`, source `inbox_report`):
   disarmed, bannered, under a heading the SERVER writes (`REPORT_NOTE_HEADING`, which says only
   what the server knows: it came over `/trigger` as the daily report). *Kept* means every later
   task in that chat starts tainted while the note is in the conversation, so when he types
   *"pay this"* the task is tainted and its actions are carded; and the origin-scoped cardless
   licence (`TRIGGERED_SAFE_TOOLS`, "safe because nobody is at the keyboard") is OFF in that chat,
   because its premise is false there. What stays cardless is what is cardless in EVERY chat: a
   mail only he can receive, an opted-in note write (#377, #379). **So "every action goes
   through its card" is exactly: every action except those that cannot reach past him.**
2. **as the turn's answer, composed by code — rendering choice (b)** (`Agent.present`): the
   owner reads the report exactly as code wrote it, with the server's provenance line under the
   headline, and the server breaks every markdown link but the Gmail one again
   (`presented_report`) whatever the caller sent. The model's copy of that answer is a pointer
   (`PRESENTED_POINTER`) — the note already holds the text — and the log's copy is the report, so
   the owner and cold replay see it; the record also keeps `model_content`, which `SessionLog.
   _parse` restores, so a cold reopen hands the model the pointer too, never a second copy
   (review M2). An empty `report` is refused (400). A `presented` record (trace contract §3.22) is the evidence
   that no model wrote it, and keeps the turn reconstructable: live and cold render identically
   (`test_live_and_cold_replay_show_the_same_turn`).

**Why (b) and not (a), a model presenting the noted report** — the delivery review's blocking B1,
adopted: under (a) the owner's view is a model's paraphrase of attacker-steered text: it can drop
an item, garble the hex id in a Gmail link so it opens another mail, drop a CAUTION marker, and —
being an ordinary unattended turn with the full tool menu and the chip convention — emit a
quick-reply chip whose tap is an owner-typed task, which folds its hosts into the owner's
(`note_owner_hosts`). (b) costs no call, and *delivered* means *in the log*: the report is there
when the session is created (B2).

**The push** is the existing done-push (`_notify_done`: non-user origin, nobody viewing) — no new
notifier — and its body is the code headline only (counts): no sender or subject leaves the
machine for Pushover.

**Retries and the ledger.** launchd fires once a day, so a 429/503 or an unreachable server is
retried for ~18 minutes (`RETRY_WAITS_S`, honouring a longer Retry-After). A POST that may have
reached the server — anything but a refused connection or an unresolved name: a timeout, a
connection dropped mid-exchange (review M5) — is `unknown`, never `failed`, and the retry resolves it — the
day's `dedup_key` makes the second POST return the chat the first one made (`deduped`). Every
attempt is one line in `<state>/mail/reports.jsonl`: `at`, `as_of`, `day`, `status`
(`delivered` | `deduped` | `unknown` | `failed`), `session`, `counts` per section, `attempts`,
`error`, and — only when `delivered` — the `digests` shown. A `deduped` answer means the day's chat was
made by an earlier attempt with its own report, so it records nothing as shown and does not move
the window (review M1); a `--since` run has its own dedup key. A day already delivered is not
delivered again: the server's dedup does not survive its restart; this ledger does. One run at a
time (`report.lock`). `--dry-run` prints the report and writes NOTHING (no ledger, no lock, no
counters) and sends nothing — pinned by a byte-identical tree.

**Failures land in `/mail`.** `mailreport.health_lines` adds the report's line under the
watcher's: *daily report: last delivered … (chat …)*, a warning when the last attempt failed or got
no answer, or when none was delivered for 26 hours after one had been (`REPORT_STALE_AFTER_S`); *none
recorded* when there is no ledger (the job may not be installed).

**The job** is code-owned and never an automation file: `scripts/install-mailreport-service.sh
[--print] <gmail-account>` writes `com.aish.mailreport` with `StartCalendarInterval` 07:30 local,
through `envrun` + aish-web's env file as the watcher's job, `AISH_WEB_URL` in the plist, the
token read at run time from the Keychain (`email_poll.read_token`), never in the plist.

`tests/test_mailreport.py` — `TestNeedsYourAction`, `TestMayInterestYou`, `TestExclusions`,
`TestCautions`, `TestFollowUps`, `TestWhatIsNew` (the window), `TestRenderSafety`,
`TestTheReportSaysWhatItRestsOn` (stale watcher, empty day), `TestRun` (dry run, delivery,
ledger), `TestHealthInMail`; `tests/test_mailreport_chat.py` — `TestTheReportChat` (origin +
meta, unread, the kept note outside the task string, no model call, live == cold), `TestThePush`
(no viewer, headline only), `TestTheChatStaysTainted`, `TestKeptNoteMechanics`,
`TestTheServersFences`; `tests/test_mailreport_review.py` — `TestTheReportFindings`,
`TestTheChatFindings` (each delivery-review finding pinned);
`tests/test_mailreport_service.py` (the plist).

## The case verbs (2026-10-31) — the `cases` tool, `mailcasetool.py`

**Why.** The owner's review of the first report chat (epic #142, 2026-10-29): the chat had no case
memory it could use — the model fell back to raw mail searches and then shell-probed the state
dir — and he could not say *"I picked these up"* in a way that changed anything. His design
(2026-10-29): verbs in the voice of a human assistant, owner-only change, owner close as the
closure the keeper clamp waits for.

**LOOK — free, read-only, outside content.** `cases` `list` (open cases by default; `include`
closed, dismissed or all) and `show` (title, status, keys, parties, admitted senders, the mails
oldest-first with date, sender, subject and the Reader's one line, notes, reminders, his acts on
it). Every mail-borne string goes through `mailcasetool._safe` — controls marked, whitespace
flattened, every link disarmed (`mailreport.safe`), capped — and a cautioned mail's summary is
withheld exactly as in the report (`mailreport.caution_marks`). The agent returns it as aish's
heading (`LOOK_NOTE`) above the untrusted banner (`web.UNTRUSTED_NOTE`) with markers disarmed;
`Agent._brings_outside_content` taints on any `cases` call, `_what_a_source_is` names it e-mail and
`_may_be_mail` is true for it. Tool results are never folded into owner hosts
(`note_owner_hosts` reads only owner-typed text). A title code derives (`Case.display_title`) is
the first key's kind and the key — regex output, never a subject.

**CHANGE and CREATE — his act, every one.** `close`, `reopen`, `merge` (one case absorbs
another's keys, mails, parties, senders, admitted senders and account history; the absorbed case's
file is KEPT with `merged_into` and no longer observed; no undo verb), `move` (one mail from a case
to another), `rename`, `dismiss` ("not a case": hidden from `list`, nothing deleted, correlation
unchanged) and `restore`, `note`, `remind` (a due date and text, stored, shown by `show`, and —
since the preferences slice — listed in the daily report's Follow-ups from its due date for 7 days
while the case is open; nothing else notifies him), and `create` (a title and optional keys he gives; mail carrying such a
key joins the case wherever it says it — `OWNER_KEY`, `_says_key` — and opens nothing). There is
no delete verb.

- **Provenance — refused, no card, unless a person sent the task.** `Agent.task_from_owner` is set
  by the server before EVERY task from `_run_task`'s `owner_sent` (typed, a chip tapped, Retry),
  so in a report chat (origin `schedule`) his message is his, and a trigger's prompt, an
  automation's run or a restart's replay is not (`CASES_CHANGE_NOT_FROM_OWNER`, decision
  `blocked`). The CLI default is true: every terminal task is typed. **Retry and a pressed
  continuation re-run the task before them, so they inherit whether a person sent THAT task**
  (`Session.last_task_from_owner`, `server._task_sender`): Retry on an e-mail trigger's prompt
  re-runs words a mail may have written, and stays refused; a chat reopened cold presumes his
  only when he started it (origin `user`). **A chip tap is a task a
  person sent**: the server cannot tell it from typing (`app.js` sends both as one `task`), and a
  model in a tainted chat can write the chip's text — the card still stands between, and that
  card's words are code's. The same flag scopes `recall`'s past-session archive, so "see the
  previous inbox review" typed in a report chat searches earlier report chats
  (`docs/agent-core.md`, Egress #178 P0-2).
- **The card's one sentence is code-composed from the typed fields** (`mailcasetool.plan` →
  `_sentence`): code's words, the case's title and any title, note or subject quoted and passed
  through `_safe`, with any quote marks of their own made plain so a value cannot close aish's.
  Nothing the model wrote beyond the typed fields reaches it — the card's arguments are the typed
  fields alone, pinned. Example: *Close case “order SH-90210” (2 mails). Keeper documents on it
  that aish has already read stop being held visible — unless also on another open case — in
  /mail, the CSV and the daily report; nothing in your mailboxes is moved or deleted.* The tool's
  answer afterwards says the verb was applied and repeats what the card said.
- **No cardless licence covers it**, in any origin, kept note or not (`server._auto_safe` returns
  None for `cases`, pinned). In report chats the origin-scoped licence is withheld anyway.
- **Applied through the one writer, under the store's lock.** `mailcasetool.apply` takes
  `mailcases.store_lock` — the SAME `watch.lock` a watcher pass and the backfill hold — waiting up
  to `STORE_LOCK_WAIT_S` (20 s) and otherwise refusing with nothing changed — saying another
  aish-mailwatch process held it, a pass or a backfill, and that aish does not know which; then
  re-checks the
  act against a FRESH load (`CaseStore.check`) and writes it with `CaseStore.act` (`atomic_write`,
  per-case files). Without the lock, a pass that loaded the store before the card would overwrite
  his close when it saved the case.
- **Recorded twice.** `CaseStore.act` appends `{at, verb, by, case, …}` to
  `<state>/mail/case-acts.jsonl` (the history `show` prints, `read_acts`); the agent emits a
  renderless `case_act` record (trace contract §3.23) for every proposed change, whatever became
  of it.
- **Wired into the report chat's menu.** A chat holding a kept outside note (the daily report)
  gets `cases` as a `required` tool (`docs/tool-menu.md`), however he words his request.

**What closing changes, precisely.** Only what aish WOULD do, in shadow mode: a keeper-clamped
mail whose cases are all closed reads with the Reader's own disposition again (after the live
clamp) — in `/mail` (`would: archive [keeper clamp from archive RELEASED: … — you closed its
case]`), in the CSV's `disposition_effective` and `keeper_clamped` (`…; released: you closed its
case`), and in the daily report, where such a mail leaves *May interest you*. *Needs your action*
does not read dispositions and is unchanged. **No mailbox is touched**: the cleanup lane (trash /
archive on closure, the owner's 2026-10-28 directive) is NOT built — nothing moves, labels or
deletes mail, and nothing in this slice can.

`tests/test_mailcase_verbs.py` — `TestTheStoreVerbs`, `TestClosureReleasesTheKeeperClamp`,
`TestTheTool`, `TestTheServerSetsWhoSentIt`.

## Inbox preferences (2026-10-31) — the `inbox_preferences` tool, `mailprefs.py`

**Why.** The owner's directive (epic #142, 2026-10-28): feedback in the report chat that aish
remembers for future inbox reviews, "just like feedback I would give a real assistant". The evidence
that made it urgent, session-20261031-191100-732487: he told the report chat that
familysafety@microsoft.com was to be ignored, and the model answered *"Understood … should be
ignored in inbox reviews"* — a promise nothing enforced; the next report listed them again. The
same report's Follow-ups listed a *your turn* item that was the other party's closing payment
confirmation.

**The store.** `<state>/mail/preferences/<id>.json`, one file per preference, in the STATE tree —
they name his correspondents, and the config tree is auto-pushed to a backup. Written only by
`mailprefs.apply` and `mailprefs.seed`, through `atomic_write`, under the store's OWN lock
(`preferences.lock`, `PREFS_LOCK_WAIT_S` 10 s) — not the watcher's `watch.lock`, because no watcher
pass writes preferences and a long backfill must not block a save. Every act is a line in
`<state>/mail/preference-acts.jsonl`. Each entry: the kind, its TYPED fields, the plain-language
`text`, `created_at`, and `source` — the chat (session) and turn the owner's message came from, or
`seed`. **Per-file containment**: an unreadable file costs that preference, is NOT applied, and is
counted in the report's footer and the listing. A file is re-validated on read, so a hand edit or
corrupt bytes cannot widen a hide (an empty subject phrase would match every mail). `TestTheStore`.

**Five kinds, closed typed fields, exactly one target each** (`MATCHERS`, `_shape_problem`):

| kind | about | the report |
|---|---|---|
| `hide` | `sender` · `domain` (and its subdomains) · `recipient` (To or Cc — the kids' forwards) · `mail_kind` (a phrase in the Reader's `kind`) · `subject` (a phrase, 3+ characters, word-bounded, folded) | left out of every section; their-turn follow-ups are matched by sender/domain against the person he wrote to |
| `show_first` | `sender` · `domain` · `mail_kind` | first in its section, marked `[first: your preference …]` |
| `not_a_follow_up` | `counterparty` (standing) · `conversation` (a report `ref`) | left out of Follow-ups; a conversation only **until a new message arrives in it** after the preference was saved — the Catherine case, which revives if she writes again |
| `remind` | `case` or `counterparty`, with `due` (a date, shown for `DUE_SHOWN_DAYS` 7 days from it), `every` (a weekday) or `no_answer_days` (counterparty only: his last message to them unanswered N days), and his `note` | `Reminders due` at the top of Follow-ups; a case reminder ends when the case closes |
| `correction` | one `mail` (a report `ref`) and `label` `needed_action` / `junk` | **not applied**: counted in the footer, and the review CSV's `owner_correction` column |

**Delivery of reminders is the report only.** Nothing is pushed for a reminder; the daily report's
own done-push is the only notification, and it carries counts. The report also delivers the
reminders on his open cases (`cases` action=remind), which before this slice were stored and shown
by `show` only; the case card's sentence now says so.

**Corrections are ground truth, not teaching.** A correction is the strongest truth tier there is —
his own label on one mail — and it reaches the review file (`mailexport` column
`owner_correction`, read from the live state tree; a backtest run directory holds none, so its file
says `none`). Nothing adopts them into the Reader: a later charter round may, measured, the way his
v2/v3 rulings were adopted. The backtest's `report.json` does not read them.

**Refs.** Every report line ends `— ref <12 hex>`, the mail's digest prefix (the their-turn line:
HIS message's). The tool resolves a ref to exactly one index entry (`_resolve_mail`) or refuses; a
card that names a mail quotes its subject, sender and date through the case store's sanitiser.

### The tool — the case verbs' law, reused

`inbox_preferences` `list` is free and outside content (`LOOK_NOTE` above the untrusted banner;
taints as e-mail, never folded into owner hosts — a phrase in one may have come off a mail). `add`,
`edit`, `remove`:

- **Refused, no card, unless a person sent the task** — `Agent.task_from_owner`, the same flag and
  the same server plumbing as the case verbs (`PREFS_CHANGE_NOT_FROM_OWNER`, decision `blocked`).
- **The card's sentence is code's**, composed by `mailprefs.sentence` from the typed fields alone:
  *From now on: hide mail from familysafety@microsoft.com in your inbox reports (mail flagged as a
  risk is shown anyway). Save?* That sentence, minus the frame, IS the stored `text`; the model never
  writes it. The card's arguments are the typed fields only — every other argument is dropped
  (`FIELDS`, then only the kind's own). Edit: *Change your preference “…” to: from now on, …*;
  remove: *Delete your preference: “…”*. No cardless licence covers it in any origin (pinned).
- **SAVED is the only confirmation.** The tool's answer after a save starts `SAVED as pref-…`
  (a delete: `SAVED: deleted`), under `DONE_NOTE`; every refusal, denial, held card, error or
  failed save starts `NOT SAVED` and is returned as a gate outcome, never under `DONE_NOTE`
  (pinned with a held lock through the agent). An edit or delete whose target changed after the
  card was composed is refused at save time.
- **The false-promise rule** (`mailprefs.MEMORY_RULE`): appended to the server's
  `REPORT_NOTE_HEADING`, aish's own voice on the KEPT report note, so it is in front of the model in
  every turn of a report chat: *never tell the owner you will remember, ignore, hide or stop listing
  something unless inbox_preferences answered SAVED; otherwise say it is not saved.* The tool's
  description says the same. This is an instruction to a model, not a gate: nothing in code stops a
  model from writing the sentence anyway (*Residuals*).
- **On the report chat's menu** as a `required` tool beside `cases` (`docs/tool-menu.md`).

`TestTheTool`, `TestTheFalsePromise`.

### What the report does with them — and the floor

`mailreport._Preferences` applies them after the sections are built: hide and not-a-follow-up
exclude, show-first reorders (stable: pinned first, each part in the section's own order),
reminders are added. **Hidden mail is not recorded as shown**, so deleting a preference brings it
back while it is still news. `TestEachKindsEffect`.

**The safety floor — the suppression attack.** A hide or not-a-follow-up is the dangerous direction
in a chat whose words a mail may have steered (showing is free). So before a preference hides
anything, `mailprefs.floor` asks the record: the Reader judged it suspicious, its sender does not
match its case, a payment account differs from its case's history (the three `caution_marks`), or
it **failed authentication from a sender whose mail passed it before** (by aish's clock, across the
verdict store, compared on aish's own clock — when it first read each mail — so a backdated Date
cannot put a failing mail before the sender's first pass). It reads EVERY model's record of the
mail, not only the newest the report shows; the records of a copy delivered to his other inbox
(dropped from the list by `_one_copy`); and, for a *your turn* row, every answer the counterparty
sent after his last message, not only the newest asking one. Any of those and the item stays, marked *shown despite your preference pref-… because
…* in code's words. Structural red flags and `sender_not_admitted` are not on the floor; a mail with
no reading is in no section anyway. `TestTheSafetyFloor`.

**The footer, always.** *Using N of your preferences (hide H, show first S, …) — hid M item(s); K
shown despite a preference (marked on the item).* — or *Using 0 of your preferences — none saved.*
Then one line naming each preference that hid something, its count and up to three senders; then the
corrections count; then any unreadable preference files. **The report's size cap
(`REPORT_MAX_CHARS`) cuts the sections, never the footer**: the footer is composed first, with its
own `FOOTER_MAX_CHARS` (5,000), and the sections get what room is left. `TestTheFooter`.

### The seed — `aish-mailreport --seed-preferences`

The owner's four from the epic comment (2026-10-29): three written as typed entries with source
`seed`, two printed as not preferences. A re-run writes nothing already saved, and never
re-creates one he deleted on a card (it reads `preference-acts.jsonl`). What the comment's words
fixed, and what they did not:

- (a) **familysafety@microsoft.com** → `hide sender`. The comment also says *delete*: nothing deletes
  mail (the cleanup lane is not built).
- (b) **Super Duo** → `hide subject "super duo"`. The comment names no address, so it is a SUBJECT
  phrase: mail carrying it only in the sender's display name is not hidden.
- (c) **adam@wenda.eu** → `hide recipient`. **kuba@wenda…** is truncated in the comment and is NOT
  seeded — no domain is guessed; the command prints how to add it.
- (d) **the InPost and DPD sport-food parcels** are a case close, not a preference: nothing is
  written; the command prints the `cases` hint.

`TestTheSeed`, `TestTheSurface` (`/mail preferences` in the terminal; `list` in the chat).

## Open items and settlement (2026-11-02) — `mailitems.py`, `mailsettle.py`, `mailitemtool.py`

**Why.** The owner's review of the 2026-11-01 and 2026-11-02 report chats
(session-20261101-073003-003960, session-20261102-073004-181280): *"You need to connect the
dots … all these could be derived by connecting emails"* — a PAY item next to the club's own
"thank you for the payment"; his BMW reply listed as a REPLY item (its Tuta copy) while the offer
he answered sat under DECIDE; two follow-ups he had closed coming back daily; a their-turn row for
a parcel InPost had confirmed picked up; one school notification listed twice. And, the day
before, his denial of a per-sender patch: *"this is not local solution — always do adversarial
review for systemic solutions"*. The binding design is the three epic comments of 2026-11-01
(follow-up redesign, 9 review changes, his two additions), the option-A decision and 2026-11-02
(settlement design, 12 review changes); this slice's own design review (Fable, 2026-11-02)
changed six of its decisions, each noted below.

### An item, and the one derivation

An **item** is one thing the report would put in front of him, anchored by a DIGEST:

| kind | anchor | counterparty | rests on |
|---|---|---|---|
| `needs` | the mail whose reading wants pay / reply / decide (or that his `important` preference places there) | its sender | that mail |
| `yours` | the FIRST asking mail after his last message to them | the person he wrote to | every asking mail since |
| `theirs` | his last message to them | the person he wrote to | that message |

`mailitems.derive` is the ONE derivation (review-2 change 7): the watcher's settlement step and
the 07:30 report both call it, so they cannot disagree about what is open. The report still shows
a needs item only while it is news (§What is new) — the watcher judges only what the report could
show (`mailsettle.showable`, reading the report's own `news_window`, and nothing his hide
preferences take out). It is a slight SUPERSET in one corner — a your-turn row whose ask the report
folds under a shown Needs item, or a near-duplicate's twin — so the watcher can spend a call on a
row the report will not draw; never the other way. Follow-ups are unchanged in what they list (per counterparty, B3), and a
your-turn row he answered in its own conversation is simply not derived: his answer moved his
last message past it, as before. The item key is `kind|anchor|counterparty`.
`TestTheOneDerivation` in `tests/test_mailitems.py`.

### The clock — aish's first read, never a sender's date

Every comparison (revive, cover, "later") uses `Context.first_read`: the earliest reading's
`recorded_at`; for his own sent mail its time (the SENT label / Sent folder is his evidence); for
any other mail with no reading — backfilled, a lapse — the modification time of its index file,
which is when aish WROTE the entry (a later recheck rewrite only moves it later, the safe way);
and with none of those, never (`inf`): such a mail is never covered by a resolution and never
anyone's evidence. **Changed by the design review**: the first plan fell back to the mail's own
`at`, which for Tuta is the sender-written Date — a backdated unread mail would then have been
"covered" by an older resolution. On the real index it is what lets his 2026-10-31 close cover
Janeba's answer: indexed 17:30, closed 19:35, while its sender-written Date says 10-09.
`TestTheFirstReadClock`.

**"Later" is both clocks** (`Context.later`): a candidate or his reply must be first read after
the anchor AND carry its own time after the anchor's. Either alone fails: a mail backfilled today
is first read today however old it is (the first real run tied a 2026-09-25 bank notice to a
2026-10-17 item that way), and a sender writes its own Date. Requiring both only ever excludes.
`test_a_backfilled_old_mail_is_not_later_than_the_item`.

### Whether an item is open — `Context.state_of`

States `open` | `settled` | `cannot_tell`; sources in a fixed order, **owner > judge > rule**:

1. **His word.** `resolve` / `restore` on the anchor, from `<state>/mail/item-acts.jsonl` (the
   newest act wins; it applies to every item on that anchor), and a **case he closed** that the
   anchor is on, under `Closure.releases`' law (every case it is on closed at or after the latest
   first read of what it rests on). A resolution covers ONLY mail aish read at or before it: any
   non-gated inbound mail in the item's conversation, from anyone, or any new ask, first read
   later, **revives** it (reviving only shows more). His **restore** pins the item open against
   every judgement and rule whose evidence was read at or before the restore; evidence read after
   may settle it again. A restore later than a close outranks the close. Settled by his word, an
   item leaves the report, counted in the footer. `TestHisWord`, `TestHisClosedCase`.
2. **A verified judgement** (§The settlement judge) — re-verified by code on EVERY read: the
   candidate still in the index, later on both clocks, still passing the tier rule with no
   caution, and nothing read after the judgement reviving the item. `TestAJudgementIsReverified`.
3. **The rule** — his own outbound mail, later than the anchor, ADDRESSED to the item's
   counterparty, or whose OWN In-Reply-To resolves (`reply_parent`, squat-safe) to one of the
   item's mails or to a mail from the counterparty inside its conversation. Never conversation
   membership: a stranger joins a conversation by naming a Message-ID; only his own header says
   what he answered. The rule is not "revived": what continues the matter is its own new item (on
   the real index, Przemysław's next reply is a your-turn row). `TestTheRule`.

Settled by a judgement or the rule is **probably settled** (option A, the owner's choice of
2026-11-02): demoted into the report's named line, never removed — only his word removes it, or
the window.

### His own mail in his other inbox — `own_copies`

**The design's law could not work on his mail, and was replaced on evidence** (review-2 changes 3
and 4: verify on the actual F2 pair first). Through the Tuta bridge **every** Message-ID is the
bridge's own (`…@tutabridge.local`, 660 of 660 Tuta entries on 2026-11-02) and **no** In-Reply-To
or References survives (0 of 660). His BMW pair: Gmail SENT `b83aae…` and Tuta inbound copy
`298813…` — same From, To, Cc and subject; the Date re-rendered 3 s later in another zone; the
body 1092 vs 1050 characters, a different digest. So same-Message-ID matching (and
`mailreport._one_copy`, which keys on it) never pairs a Gmail/Tuta cross-delivery, and a body
comparison would fail too. What the evidence supports, every part required: the gateway
AUTHENTICATED the copy; its From is an address his Gmail SENT mail is sent from; a Gmail SENT
original exists from that address with the same subject and the same To and Cc sets, received
within `OWN_COPY_SKEW_S` (300 s, a choice) of the copy's Date. A recognised copy MINTS NOTHING:
never an item, never observed into the case store (`ingest` indexes the whole batch before
observing any of it, so the pair is recognised whichever came first; the backfill and the recheck
skip it too), never read by the Reader (the pass counts it `own_copies`) — **within one batch**.
Across passes it is weaker: a copy fetched in a pass where its Gmail original was not (a quota
stop, a failed sent fetch) is observed into the case store as ordinary authenticated inbound when
it comes, and nothing undoes that observation. The derivation re-tests at read time, so such a copy
still stops being an ITEM once its original is indexed. The
footer names each copy with both refs. `TestHisOwnCopy`.

### The settler vouch — `mailvouch.py` (his word for a third party)

**Why.** F4: InPost's *"your parcel from Uncico was picked up"* is a third party with no
history in his case store (it is on no case he transacted on), so the tier rule refuses it — and
must, since the history rule is what stops an authenticated stranger with a name in its text from
settling items. Loosening it would be the local optimum. The missing fact is HIS: whether mail
from that sender may speak for that kind of matter.

- **The grant is his act, per EXACT domain and closed meaning.** `inbox_items` action=vouch /
  revoke, the case verbs' law (a task he sent, a code-composed card, under the item acts' lock,
  SAVED only after the write): *Allow mail from paczkomaty.pl to settle delivery items in your
  inbox reports? Only mail from exactly paczkomaty.pl — not a subdomain or a lookalike — …*.
  Every act is a line in `<state>/mail/settler-vouch-acts.jsonl`; the current set is the newest act
  per (domain, meaning); permanent until revoked; listed by `inbox_items` list. Membership is an
  exact string compare (the `vouches.py` law).
- **Meanings are predicates of the ITEM, over recorded facts on its own mails**
  (`mailvouch.MEANINGS`, `mailitems.Context.settle_meanings`): `delivery` — one of them is on a case
  whose RECORDED keys (`mailcases`, extracted at ingest) include an order or tracking key
  (`DELIVERY_KEY_KINDS`); `payment` — one of them carries a recorded money fact. **Never a property
  of the later mail** (round-2 review, blocking): the first version made `delivery` "the later mail
  names the counterparty", which is the entity tie itself, so a (domain, delivery) vouch would have
  let that domain settle ANY kind of item whose counterparty it names — on the real index it
  offered *let clsu.pl settle delivery items* on an insurance offer. The tie stays a reason to
  ASK; the chip is offered only when the item satisfies the meaning.
  `test_naming_the_counterparty_does_not_make_an_item_a_delivery`.
- **What it grants is eligibility, nothing more**: a vouched (domain, meaning) is tier
  `vouched sender`, an alternative to the third-party history rule, which is unchanged. The
  candidate must still be authenticated, read, tied by code, free of any caution, later on both
  clocks — and the settlement judge must still read it as settling the item, which then only
  moves to *Probably settled*.
- **Code offers it, never answers it.** A refused third party for which a meaning applies carries
  `Candidate.vouchable`, and the item's *later mail aish did not weigh* line ends with a one-tap
  chip `[let <domain> settle <meaning> items](aish-reply://vouch settler <domain> <meaning>)` —
  the report's third link shape, built from a validated domain and a closed meaning
  (`mailvouch.chip_text`) and the only extra shape `server._REPORT_FOREIGN_LINK` keeps — its
  meanings derived from `mailvouch.MEANINGS`, the one authority, never a second literal. A tap
  sends the words as his message; the grant is still his card.

`TestTheGrant`, `TestTheOffer` and the vouch verbs' `TestTheVerbs` in `tests/test_mailvouch.py`.

### The settlement judge — `mailsettle.py`, `aish/charters/item-settler.md`

A toolless role (kind `judge`, model class `caller`, degradation `skip`, the `settlement`
output shape delegated to `mailsettle` as `verdict` is to `mailreader`), asked about ONE
(item, candidate) pair: `settled_by k1` | `still_open` | `cannot_tell`, a capped reason, and
citations. It runs in the WATCHER pass, after assessment (`mailwatch.settle`); the report stays
model-free.

- **Code finds the pairs** (`Context.candidates`): later mail tied to an open item by at least one
  code signal — `counterparty`; `conversation` (membership is fine for ASKING; the tier decides);
  a shared extracted `key`; a common `case`; an equal STORED `amount` (the case store's amounts at
  each mail's position — never re-read from a body); `entity` (the counterparty's display name in
  the candidate's text, its domain's first label, or the same domain — the domain only when fewer
  than `ENTITY_DOMAIN_MAX_SENDERS` (5, a choice) distinct addresses at it appear in any header of
  his mail, which keeps a shared provider like gmail.com out structurally: on the first real run
  "gmail" tied unrelated people until the count read every header, not only senders). Newest
  first, at most `CANDIDATES_PER_ITEM` (3). His own mail aimed at the item is the rule's, not
  a candidate.
- **Code applies the tier rule BEFORE any call** (`Context.check`, review-2 change 1): (a) his own
  outbound mail; (b) the same counterparty — the item's counterparty, **an address he wrote to in
  the item's conversation**, or a domain admitted on a case the anchor is on; (c) a third party
  only if authenticated AND tied by a case it shares with the anchor or an equal stored amount AND
  with history — its domain DEALT WITH by him (a party he addressed, or an admitted domain;
  never a sighting alone) on `THIRD_PARTY_MIN_CASES` (2) distinct cases he transacted on, at
  least `THIRD_PARTY_MIN_AGE_DAYS` (30) before the candidate (both choices). Never
  `mailgate.grade`. A caution mark on any model's record of the candidate, or failed
  authentication from a sender that passed before (`mailprefs.floor`), refuses every tier.
  **Changed by the design review (blocking)**: tier (b) first read "a sender in the item's
  conversation", which a stranger satisfies by its own membership — one hostile Gmail mail naming
  the thread's Message-ID would have been its own credential to demote the item.
  `test_a_stranger_joining_the_conversation_is_not_tier_b`; `TestTheTierRule`,
  `TestTheThirdPartyTier`.
- **Every inbound candidate, whatever its tier, must be authenticated and read** (the delivery
  review, blocking): an unauthenticated From is a claim anyone can write, and the caution floor
  reads the Reader's record — a mail with no reading had no floor at all, so a forged,
  never-assessed From of the counterparty passed tier (b). An unauthenticated candidate is refused
  (*its sender is not authenticated*); an authenticated one with no reading WAITS (`unread`, never
  judged, and a judgement on it settles nothing) until the settlement step reads it
  (`mailitems.unread_candidates`, `CANDIDATE_READS_PER_PASS` (5, a choice), newest first). The
  footer counts what waits. `TestEvidenceMustBeAuthenticatedAndRead`.
- **A refused pair is never sent, and is named.** The open item's line says *later mail aish did
  not weigh: from <address> (<date>): <code's reason>* — an address and code's words only, never
  the later mail's subject or text (review: an attacker-writable channel pinned to a chosen item).
  A pair whose bodies are not recorded is not judged and is counted.
- **Bounded and idempotent.** At most `MAX_JUDGED_PER_PASS` (12, a choice) calls per pass, newest
  candidate first, the rest counted `capped`; one judgement per (item key, candidate, charter
  digest) — a charter edit is a new judgement. `unadmitted` stops the step without a call;
  `unavailable` writes nothing and is retried; `ok` and `invalid` are final. The record is
  trace contract §3.26; one unreadable judgement file costs that anchor's judgements
  (`TestHealthOfTheStore`). `TestTheWatcherPass` in `tests/test_mailsettle.py`.
- **The exam** (8 synthetic cases): the owner's three positives reconstructed with invented
  parties — a club's PAY reminder settled by its own payment confirmation (F1), an offer settled by
  his own choice (F2), a their-turn parcel-locker problem settled by the courier's pickup of THAT
  shop's parcel (F4: the judge's reading; the tier rule may still refuse it in code) — and the
  adversarial negatives: a stranger's "payment received" for another payee, a lookalike pickup of
  another shop's parcel, a first-contact domain saying "your invoice is settled", a second
  reminder from the same club, and a mail telling the judge to settle. `TestTheExam`.

### The gap reading — the follow-ups that rest on unread answers

A your-turn row whose asking mail has NO reading is "your turn" only because an unread answer
counts as an ask (the fail-visible direction). In each pass the settlement step reads those answers
with the Reader (`mailitems.gaps`, oldest row first, `LATE_READS_PER_PASS` (5, a choice)),
stamping the record `late_read`. A reading that asks nothing closes the row — a model's reading
taking a row out, so the footer NAMES each such counterparty (*N follow-up(s) left Your turn
because aish read the answer late and it asks nothing*), never silently. **This amends** §The live
backfill's "older backfilled mail is never assessed": an old answer an open follow-up rests on is.
`TestTheLateReading` in `tests/test_mailsettle.py`.

### The report

- **Probably settled (N)** — its own section, cut by its own cap (`SETTLED_MAX_CHARS`, whole
  lines only, so a cut never splits a chip, and only the rows drawn are recorded as shown;
  `TestTheSectionCap`) and never by the sections' (review-2 change 11). Each line: the item in code's words, *probably settled:*
  the evidence code verified — *you wrote to X on <date>* (the rule) or *mail from X on <date>
  (the same party / a third party with history; the stored amount), as aish's settlement judge
  read it* with the evidence mail's ref — and two one-tap chips, `[restore]` and `[done]`. **The
  chips are the report's second link shape**: `aish-reply://restore ref <12 hex>` /
  `…resolve ref <12 hex>`, built by code from the anchor alone; `server.presented_report` keeps
  exactly that shape and breaks every other (`mailreport.safe` disarms every `aish-reply:` in mail
  text first). A tap sends the words as his message; the change is still his card. **Changed by
  the design review**: the plan had typed words ("say restore ref …"), which is not the owner's
  "one-tap-restorable". The judge's reason is never rendered. A needs item shown here was shown.
  `TestTheReport`, `TestTheServerKeepsOnlyTheChip`.
- **Near-duplicates** (`mailitems.near_duplicates`): one sender, one subject, first read within
  `NEAR_DUPLICATE_S` (600 s, a choice) render once, *(×2 — also ref …)*, every copy's ref on the
  line and *their texts differ* when the body digests differ; every copy recorded as shown.
  **Changed by the design review**: collapsing alone could have hidden a second distinct invoice
  under one subject forever. `TestNearDuplicates`.
- **Continuity** — the line under the headline: *Since the last report: you resolved N item(s),
  closed M case(s), saved K preference(s)* — each a filter over its acts ledger
  (`mailreport.acts_since`; the seed is not his act).
- **The footer** adds: items his word took out; *Settlement:* candidates not yet judged (those
  items stay open), later mail the tier rule does not let settle anything, pairs whose body is not
  recorded, unreadable judgement files; his own copies with both refs; rows a late reading closed.
- A needs item he **restored** is shown again after it was news, until he resolves it or it
  leaves the window.

### The verbs — `inbox_items` (`mailitemtool.py`)

`list` (free, outside content) | `resolve` (this matter is done, no reply needed — on the ITEM,
not a preference) | `restore` (still open). The case verbs' law, reused whole: refused with no
card unless a person sent the task (`ITEMS_CHANGE_NOT_FROM_OWNER`); the ref resolves to exactly
one anchor or the call errs; the card is ONE code-composed sentence stating the consequence and
the revival (*Mark as done: PAY from … It leaves your inbox reports; new mail in that conversation
brings it back. Nothing in your mailboxes is changed.*); applied under the item acts' own lock
against a fresh derivation (an item that changed since the card is refused); SAVED is the only
confirmation; no cardless licence in any origin; a renderless `item_act` record (§3.25); on the
report chat's required menu beside `cases` and `inbox_preferences`. **The false-promise rule now
names resolutions** (`mailprefs.MEMORY_RULE`). The conversation-scoped `not_a_follow_up` is
retired into `resolve` (review change 7 of 2026-11-01): a new one is refused with that guidance;
saved ones keep applying (none existed live); the counterparty-standing kind stays a preference.
`TestTheVerbs`, `TestTheWiring`, `TestSaveTimeRefusals` (the held lock and the item that changed since its card) in `tests/test_mailitemtool.py`.

### `important` — entity and meaning, with an effect preview

The owner's addition of 2026-11-01. `important` names an ENTITY (`sender`, or `domain` with its
subdomains) and a MEANING from a closed set, each defined by RECORDED facts, never words
(`Context.meanings`; **changed by the design review**, which refused a body scan for amounts):
`money` — the Reader read it as asking him to pay, the case store recorded an amount on it, or a
payment account was recorded from it; `documents` — a document attachment; `anything`. Matching
mail that is news joins Needs your action, its line saying *HERE BECAUSE OF YOUR PREFERENCE
pref-…* — never a PAY or REPLY the Reader did not record; mail the Reader already wanted action on
keeps its own word. It applies to read, newsletter-gated or unread mail (showing is free), never
to a one-time code. **Every mail-matching card previews its effect** (`mailprefs.preview`, the
last `PREVIEW_DAYS` (90) days): *it would have matched N mail(s): “…” — and not these M from
<entity>: “…”*. The existing `show_first` preference for billing@hetzner.com is untouched; on his
real 2026-11-01 Hetzner notice (no amount recorded, Reader wanted nothing) `money` would NOT match
and `anything` would — the preview shows him which before he saves.
`TestTheImportantPreference`.

### Stated choices, and what they cost

`OWN_COPY_SKEW_S` 300 s, `NEAR_DUPLICATE_S` 600 s, `ENTITY_DOMAIN_MAX_SENDERS` 5 addresses,
`ENTITY_TOKEN_MIN` 4 characters, `CANDIDATES_PER_ITEM` 3, `MAX_JUDGED_PER_PASS` 12,
`LATE_READS_PER_PASS` 5, `THIRD_PARTY_MIN_CASES` 2, `THIRD_PARTY_MIN_AGE_DAYS` 30 (the design's),
`PREVIEW_DAYS` 90: all CHOICES, none a measurement, moved only on evidence or his word. None is a
word list; their misses are visible in the footer counts (not judged, not weighed, body not
recorded) and the judgement store, not yet in `aish vocab`.

### Readings made without the body, read again (2026-11-02) — `mailwatch.reread_blind`

**An explicit amendment of "a verdict already recorded … is never recomputed"** (§The thread
index, §The Tuta authentication recheck), with this exact premise: *a recorded Reader reading
whose recorded input carried `[body not recorded]` as the reviewed mail's text, while the
evidence store now yields that body, did not judge the mail.* The CRLF read bug (fixed in
`evidence.get`, 2026-11-02) produced exactly that for bodies the store held all along — 367 of
the owner's 454 Gmail index bodies read as absent.

- **Decided from evidence** (`mailwatch.blind_readings`): the reading's first call's input bundle
  is read from the evidence store by its recorded digest; its reviewed message's `text` must be
  exactly `[body not recorded]`, and `index.body` must return the body NOW. A body genuinely
  absent is never a candidate, so it is never retried; a bundle no longer in the store cannot be
  checked, and its reading stands. Only final readings (`ok`, `invalid`) by the pass's own model
  are considered.
- **Superseded, never deleted**: the new record carries the old one whole under `supersedes`
  (`{at, record, why}`); a re-read that gets no answer (no model, no admission) writes nothing and
  the old reading stands. The first-read clock still counts the superseded reading
  (`mailreport._with_superseded`), so a re-read never makes an old mail look new.
- **Bounded, oldest mail first**: `REREAD_PER_PASS` (10, a choice) in every `--once` pass, before
  the settlement step; the backlog in one run is `aish-mailwatch --reread-blind --model <spec>
  [--max N]` (driver step 3i), which scans the whole store.
- **The scan is bounded too**: once a pass finds no blind reading left, it records *none found
  since <at>* per model (`<state>/mail/reread-marker.json`), and later passes evidence-read only
  verdict files written after it (`blind_readings(since=…)`; `TestTheScanIsBounded`). A body
  restored to the store later for an OLDER reading is found only by the one-shot.
- **Stated edge: an `invalid` re-read supersedes an `ok` blind one**, leaving the mail with no
  verdict — counted under "with no reading", never a guess; the conservative direction.

`TestBlindReadings` in `tests/test_mailreread.py`.

## The live backfill (2026-10-31) — `aish-mailwatch --backfill DAYS`

**Why.** On 2026-10-28 the live index held 0 of his sent messages: the watcher lists a window of
`DEFAULT_DAYS` (2) days and only began on 2026-10-25, so the report's Follow-ups had nothing to
work from (*For the driver* 3e). The backfill reaches back once, deterministically.

**What it does** (`mailwatch.backfill`): every source's INBOX and Sent for DAYS — the same
listing and fetch as a pass (`_collect`), up to `BACKFILL_LIMIT` (5000) per listing unless
`--max` says otherwise — each fetched mail added to the index as soon as it is fetched; then,
from a fresh load of the index, every index mail inside the window folded through the case store
in position order (`CaseStore.observe`). That is all: the ledger's grades and the report's
Follow-ups are computed from those two stores at read time, so nothing else is written.

- **No Reader, structurally — in the backfill itself.** `backfill` takes no model spec and no
  chat seam, `--backfill` loads no charter, and it records no verdict. Pinned with
  `mailreader.read` replaced by a function that fails the test. **The watcher's next pass is
  another matter**: it assesses every inbox mail in ITS window (`DEFAULT_DAYS`, up to `--max`)
  that has no verdict, held or not, so backfilled mail from the last two days that the watcher had
  not yet read is read then — mail it would have read anyway — and becomes report news as usual.
  Older backfilled mail is never assessed and never news.
- **The watcher's unfetched ledger is neither read nor written** (`_collect(ledger=None)`): a mail
  the ledger names is ASSESSED when the watcher next fetches it, so a backfill failure recorded
  there would buy a Reader call on old mail.
- **Paced on Gmail only**: through `GwsMailbox._call`'s token bucket and retry policy (a quota
  exhaustion stops that source asking for the rest of the run). Tuta has no pacing: one himalaya
  process (one bridge connection) per call, back to back, no `--seen` — as the backtest's Tuta
  enumeration already runs.
- **Idempotent and resumable.** A held mail is never fetched again (Gmail by provider id, Tuta by
  Message-ID AND sender AND subject). Observation covers every index mail in the window that is on
  no case and that he never moved (`case-acts.jsonl`), so a run interrupted after fetching is
  finished by the next one, and a re-run can neither undo his `move` (re-appending the mail to the
  case its key names) nor add a case's amounts and accounts twice — pinned.
- **One pass at a time**: it takes `pass_lock`, so a launchd watcher pass during it skips (exit 3).
- **Recorded**: a `mail_backfill` line (and the vocabulary tallies) in `runs-YYYYMMDD.jsonl`, and
  the summary printed as JSON — `indexed`, `observed`, `cases_before`/`cases_after`, `admitted`, per
  source the listing, held, fetched, failures. It does not write `watch_health.json`: it is not a
  watcher pass.
- **Stated limits.** Late observation is as-of-correct (every case query is positional) but not
  identical to having watched live: a case opened by a later mail that was observed first keeps
  that later creation position, and an older mail joins it as a member. Tuta mail already in the
  index from before 2026-10-31 is held, so the backfill does not re-fetch it and it gains no
  bridge observation; `--recheck-tuta-auth` does that (§The Tuta authentication recheck). Gmail's listing is `in:inbox` / `in:sent`: mail he archived out of the inbox
  is not listed.

`tests/test_mailwatch_backfill.py` — `TestTheBackfill`, `TestTheCommand`.

## The Tuta authentication recheck (2026-10-31) — `aish-mailwatch --recheck-tuta-auth`

**Why.** Tuta mail indexed before the bridge-header slice (#461) carries no observation of
`X-Tutabridge-Authentication`, so it reads as not authenticated (§Both inboxes), and it opened no
case, put no account in a case's history and admitted no sender. A watcher pass and the backfill
never re-fetch held mail. The owner asked on 2026-10-31 for one recheck that gives that mail the
bridge's verification.

**What it does** (`mailwatch.recheck_tuta_auth`). The targets are every Tuta index entry whose
`gateway_auth_observed` is empty. It lists Tuta's INBOX and Sent back to the oldest target, plus
a day (the same enumeration as a pass, up to `BACKFILL_LIMIT` per folder unless `--max` is
given). It fetches only the listed messages whose envelope Message-ID, sender and subject match
a target in the folder that agrees with the target's direction (`TutaMailbox.envelope_identity`
/ `mailtuta.identity_of`). Every fetch is `TutaMailbox.fetch`: the `check_argv` fence, no
`--seen`. The observation is then recorded onto the EXISTING entry
(`MailIndex.record_gateway_auth`): same file, same digest, every other field untouched. An
observation already recorded is never overwritten.

- **Identity is the digest, unchanged.** The bridge's line sits in the header block, and none of
  a Tuta digest's inputs do (`mailindex.digest_for`: gateway, Message-ID, sender, Date, subject,
  body digest). So the fetched message re-parses to the digest the mail was indexed under, and
  that equality IS the match; the envelope only narrows what is fetched. A fetched message
  that re-parses to no target's digest is counted `fetched_not_matching`. A target with no such
  message is counted `unmatched` and left byte-identical. Nothing is ever added to the index.
- **The consequences follow as if the mail had arrived authenticated, in arrival order**
  (`_follow_authentication`, index order, oldest first, from the earliest newly authenticated
  inbound mail). A newly authenticated inbound mail on no case is observed
  (`CaseStore.observe`), so it may open a case or join one. On every case it is on, it gains
  the facts observation records only for authenticated mail, each at its OWN position
  (`CaseStore.record_authenticated`): the sender's domain seen (moved earlier, never later), the
  amounts, the payment coordinates. A LATER mail of his on a case a newly authenticated mail is
  on re-asks admission as of itself (`CaseStore.admit_toward`), so his reply toward that sender
  now admits it, at the reply's position. A LATER mail on no case that carries a key of an
  earlier newly authenticated mail now on a case is observed, and joins it. Mail he moved is never re-observed: it stays
  where he put it and gains its sighting there. Grades, the report's Follow-ups and the
  ledger read the stores and the header at read time, so nothing else is written.
- **Verdicts already recorded are NOT recomputed.** A verdict keeps the Gate facts, grade and
  bundle it was given, which said *not authenticated*. Only mail the watcher reads after the
  recheck sees the new state.
- **A still-unauthenticated observation is final too.** A forged or second header, a
  `status` other than `authenticated`, or a message the bridge served with no header gets that
  observation recorded, `still_not_why` counts it in code's words, and it is not a target again.
- **No Reader, structurally.** `recheck_tuta_auth` takes no model and no chat seam, the command
  loads no charter, and no verdict is written. Pinned with `mailreader.read` replaced by a
  function that fails the test.
- **Idempotent and resumable.** Header observations are recorded LAST, after every case write;
  every case step is idempotent, and which cases and keys the later steps act on is derived
  from where each still-unrecorded mail now IS, never from what the interrupted run changed. So a
  run interrupted before the headers is redone whole by the next one. Pinned: a crash at EVERY
  case write in turn, then a re-run, leaves stores byte-identical to an uninterrupted run's
  (`test_a_crash_at_any_case_write_is_finished_by_the_next_run`).
- **A failed fetch defers the whole run.** It is counted `failed` and its entry left untouched,
  and the mail matched beside it is counted `deferred`: nothing is applied and nothing recorded,
  because applying a later mail now would let the earlier one, next run, meet cases the later one
  opened. The next run redoes the whole set; a fetch that never succeeds holds every recheck back
  until it does, and `failed` says so on every run.
- **A second run is a no-op once every target is matched**: it finds no target and makes no
  bridge call. An `unmatched` entry stays a target, so each later run lists the folders and
  counts it `unmatched` again (pinned), changing nothing.
- **One pass at a time**: it takes `pass_lock` (exit 3 when held). It needs no Gmail account.
- **Recorded**: a `mail_tuta_recheck` line in `runs-YYYYMMDD.jsonl`, and the summary printed as
  JSON: `needing`, `listed`, `fetched`, `rechecked`, `now_authenticated`, `still_not` (+
  `still_not_why`), `failed` (+ `failed_kinds`), `unmatched`, `fetched_not_matching`,
  `deferred`, `unparseable`, `gained_facts`, `observed`, `admitted`, `cases_before`/`cases_after`, and each
  folder's enumeration `depth` (`capped` says the listing limit was hit before the window's start).
- **Stated limits.** These are the backfill's: every fact this run writes carries the mail's own
  position, but `CaseStore.observe` matches keys over the whole store, so an older mail can join
  a case a later mail opened, which keeps that later creation position and the key's later
  first sighting (pinned, `test_an_older_mail_joins_a_case_a_later_mail_opened`). The as-of
  readers (`keys_before`, `admitted_before`, `coordinates_before`) still see nothing after the
  mail. Mail he archived out of INBOX or Sent, or
  deleted, is not listed and stays `unmatched`. The envelope match compares the subject as the
  bridge's envelope gives it, so an envelope whose subject is decoded differently from the stored
  header fetches nothing, and that mail is counted `unmatched`, never mis-matched.

`tests/test_mailwatch_recheck.py` — `TestIdentity`, `TestConsequences`, `TestRunDiscipline`,
`TestTheCommand`.

## The backtest — `aish-mailbacktest`

**Frozen clocks.** Index and case store are built in arrival order inside a SCRATCH run
directory (`backfill`), never the live store, and every question the pipeline asks is asked as of
the mail under review. Only ground truth reads the future (`truth_of`, `later_replies`).
`TestFrozenClocks`.

**Admission is NOT scratch.** The role admission ledger is machine-level state, so the arms are
judged against the real state home (`admission_state_dir`, default `paths.state_home()`) while
every pipeline store stays in the scratch tree. Passing the scratch dir for both ended 170 of 300
real Reader calls `unadmitted` on a run whose model had just been admitted; and nothing written
into a scratch tree can admit a model. `TestAdmissionLedger`.

**Deterministic first, models on demand.** `backfill` takes no chat seam and no model spec.
The arms then read a bounded selection (`select`: strong tier first, then weak, then near-null,
each oldest first, up to `--reader-max`), the same mails for every arm. Verdicts land in the run's
own verdict store, keyed by model, so an interrupted run resumes without re-buying.
`TestResume`.

**Ground truth is tiered** (`TRUTH_TIERS`): strong — `replied` (an outbound mail replies into
its conversation after it), `trashed`, `starred`; weak — `archived`, `kept_read`; near-null —
`unread`; set apart — `spam`, which is Gmail's verdict, not his. **`starred` and `kept_read` are
this slice's additions** to the design's three tiers, reported under their own names so either can
be read past. **Trash is censored**: Gmail empties Trash after `TRASH_RETENTION_DAYS` (30), so mail
he trashed earlier is in no listing — it is absent from every count, not counted as kept, and the
report says so with the oldest trashed mail it did see. `TestTruth`.

**The report** (`report`, `render`): per arm, the HEADLINE — mail it would have expired that he
kept, by truth label; mail it would have archived that he replied to; disposition by truth;
`wanted` by whether he replied; gated mail by header × truth; `cannot_tell`, `gated` and
`no_verdict` as ordinary columns; and the cross-model disagreement list (mail, date, sender,
subject, each arm's skeleton) for his adjudication. Raw counts in "N of M" form, never a rate,
never a word that grades the pipeline (`docs/replay.md` R3, R5). `TestReport`.

---

## Residuals, stated

- **The Reader's open layer is attacker-STEERED bounded prose.** `meaning` and `kind` are capped,
  stripped, de-linked and de-voiced; they are not sterile. In S1 they reach no acting context. The
  design's own law for S2, verbatim: *bounded, capped, stripped, disarmed, taint-raised; never
  "prose dies inside the Reader"*.
- **Gate flag details carry hosts and filenames** — attacker-chosen strings, for the driver's
  audit. S2's delivery to the Actor must carry the closed `what` only.
- **A thread can be joined by References.** A hostile mail naming an owner thread's Message-IDs
  joins that conversation in the Reader's bundle (it cannot inherit his acts — the ledger fence
  above). The bundle shows each message's sender, and `sender_case` exists for this; nothing
  structural stops the join. Since S1.6 the join also earns the live-conversation fact and the
  clamp — visibility only: membership is not an act, the ledger still refuses it any, and the
  clamp only ever surfaces. The fail-safe direction is one more mail for him to look at.
- **The fact is past-facing in a clock a Tuta sender writes.** Tuta's `at` is the Date header. In a
  backtest, where the whole window is ingested before anything is judged, a Tuta mail dated after
  a reply he really sent later puts that reply "before" it, and the mail reads as live. In the
  live watcher a mail is judged when it is fetched, so his later reply is not yet there. Either
  way the error can only surface a mail, never file one.
- **The clamp protects continuations only.** The 8 archive-on-replied misses it was built after
  were all openers (§S1.6); for those only the teaching acts, and whether it does is a
  re-admission and a backtest away — not measured here.
- **The keeper clamp is released only by HIS close.** There is no deterministic closure (delivery
  confirmed, a trip's end date passed): until he closes a case, its keeper clamps hold; a keeper on
  no case holds for good; and mail arriving on a case after he closed it stays held.
- **A chip tap counts as his message.** `task_from_owner` is true for any task a person sent; the
  server cannot tell a tapped chip from typed text, and in a tainted chat a model writes chip text.
  The case card, code-composed, is what stands between such a chip and a change.
- **Case verbs wait for the watcher.** A verb applied while a watcher pass or the backfill holds the
  store's lock waits up to 20 s and is then refused with nothing changed; during a long backfill no
  case can be changed.
- **Reminders reach him only inside the daily report.** A case reminder and a `remind` preference
  are listed in Follow-ups when due; no push, no other surface. A report that is not delivered
  delivers no reminder; a dated one stays listed for 7 days to cover a missed morning.
- **The false-promise rule is an instruction, not a gate** — for resolutions too since
  2026-11-02. `MEMORY_RULE` rides the report note's
  heading and the tool's description; nothing in code checks the model's answer for a promise to
  remember. A report chat delivered before this slice carries the old heading and not the rule.
- **A hide keys on strings a sender writes.** Sender address, display-free subject phrase, kind
  word (the Reader's, steered by the mail): a lookalike can match a hide he meant for someone else,
  and a sender can change subject to escape one. The floor covers the four recorded risk facts
  only; a hidden mail with none of them is hidden. Every hide is counted and named in the footer.
- **Super Duo is seeded as a subject phrase** (no address in the comment); mail naming it only in
  the From name is not hidden. **kuba@wenda…** is not seeded at all.
- **Corrections are recorded, not adopted**, and the backtest's report does not read them.
- **"Until a new message arrives" orders by the report's clock** for a conversation-scoped
  not-a-follow-up SAVED before 2026-11-02 (new ones are refused into `inbox_items resolve`, whose
  revival reads aish's first-read clock): for a Tuta mail that is the sender-written Date, so a
  reply backdated to before such a preference does not revive it.
- **A moved mail keeps its keys on the case it came from**, so a later mail carrying the same key
  still joins the old case; and a merge has no undo verb (the absorbed file is kept, so it is
  recoverable by hand).
- **The keeper clamp's judged signal is most of its cost** — 722 of 849 extra visible mails in run
  20261025-204111 rest on `reader_case` alone (§The keeper clamp). It is restrict-only by
  construction; its price is his attention, measured, not argued.
- **2 of the 12 replied-but-filed mails in that run carry no keeper signal** (Tuta, no attachment,
  no Reader case). Neither clamp reaches them; only the teaching can.
- **The review file's truth is recomputed, not recorded.** It is `truth_of` over the run's index
  and `fetched.jsonl` as they stand at export; labels an earlier run into the same directory
  logged for mail this run's listing no longer named are unioned too, where `backfill` would have
  used only the listed ones.
- **Authentication is only as good as Gmail's header being there.** For mail Gmail received
  directly, the top-most Authentication-Results is Gmail's by construction. For mail it fetched or
  imported from elsewhere it may write none, and a sender-written exact copy of Gmail's would read
  as authenticated. The consequence is bounded: authentication alone mints no grade (that needs
  his acts) and admits no alias (that needs his act toward the sender), but it does let such a
  mail open a case and enter an un-admitted case's account history. The owner's Gmail account is
  assumed to receive directly; nothing here checks that.
- **A case's account history is weakest before it has an admitted sender** — then it is the whole
  authenticated history, which a stranger passing DMARC on its own domain can seed. Once a sender
  is admitted, only admitted senders' accounts count.
- **Coarse registrable domain.** `link_text_mismatch` compares the last two labels, so
  `a.co.uk` and `b.co.uk` read as one; it only ever fails to flag, never grants.
- **The case store's owner surface is the `cases` tool in a chat** — no `/mail cases` view in the
  terminal or a web sheet, and no delete path (dismiss hides; nothing shreds a case).
- **The lookalike flag needs admitted domains**, and admission now needs his act toward a sender,
  so early on it has few to compare against.
- **Tuta authentication rests on the bridge's guarantee, checked only structurally.** aish
  verifies position and copy count; that the value reflects Tuta's real `authStatus` is the
  bridge's claim (#16's acceptance, observed by the owner on a sample, not by this repository). A
  bridge bug that wrote `authenticated` for a spoof would authenticate it.
- **Tuta mail indexed before 2026-10-31 is authenticated only once `--recheck-tuta-auth` has
  run** (§The Tuta authentication recheck). Until then it carries no observation and stays
  unauthenticated. The recheck reconsiders cases and admissions; it does NOT recompute a
  verdict already recorded, and an entry it cannot match by digest, or that has left INBOX and
  Sent, stays unauthenticated.
- **Tuta's `at` is sender-written.** The Date header is all this gateway gives. It is bounded —
  an inbound unauthenticated mail opens no case and moves no grade, and since the squatting fix
  it can no longer displace a Gmail copy — but a forged Date still reorders the index and the
  `as of` questions asked around it. Counted per pass as `at_from_sender`.
- **A cross-delivered mail is read twice** (once per inbox) since Tuta digests fold in content;
  a reply naming it resolves to the Gmail copy when the two agree on the sender.
- **A backtest run directory built before S1.5's direction fix keeps its old directions** on
  resume (held entries are not re-parsed): re-run a backtest into a fresh `--out`.
- **The no-\Seen guarantee rests on himalaya's documented behaviour.** The fence pins that no
  argv carries `--seen`; that `message read --raw` without it leaves the flag untouched is
  himalaya's help text ("Without this flag the read leaves the seen state untouched"), the same
  basis `tuta_read` stands on. It was not re-observed against the live bridge here — doing so
  risks marking a real mail read.
- **The himalaya envelope JSON shape** the Tuta listing reads (`messages[].uid`,
  `envelope.message_id`, `envelope.date`; STATUS `messages`) is the shape the tuta_* tools and
  their tests use; a run against the live bridge has not been made from this module.
- **Tuta's flags are as of the backtest run**, like Gmail's labels: `\Seen` and `\Flagged` are
  read today, not as of the mail. A mail he read after the window closed reads `kept_read`.
- **The `--flags` page shape** (`messages[].flags`, beside `envelope`) is tutalib's own
  envelope-read argv and the shape its tests use; no run against the live bridge has been
  made from this module, and a bridge that omits the list yields `seen_unknown`, never a tier.
- **Tuta mail outside INBOX, Sent and Trash is absent** from the backtest — an archive folder,
  spam, his own folders — and the report says so; nothing there is counted archived.
- **Whether an export runs "from a terminal" is stdin's tty** (`mailexport.interactive`). A
  terminal run with stdin redirected falls back, and says so; a background process with a tty
  on stdin would try Downloads.
- **The daily report quotes attacker-steered prose into a chat he acts in.** The Reader's
  one-line `meaning` reaches the report (withheld on cautioned mail), bounded, sanitised and
  de-linked, never sterile. The chat is tainted for every task while the note is there, and the
  origin-scoped cardless licence is off; the consequence-scoped ones (owner-only mail, opted-in
  note writes) stay, as in every chat.
- **The report's follow-ups do not judge whether his last message asked anything**: a closing
  *"thanks"* shows as *their turn* after 5 days, and a thread he finished by phone keeps showing
  until the settlement judge connects later mail to it, he says *done* (`inbox_items resolve`), or
  it leaves the 60-day window. The section's footer says so.
- **Mail he sends only to himself is never his "copy".** A Gmail mail whose To and Cc are all his
  own addresses is a mail TO him (a note to self across inboxes), so it is excluded from the
  own-copy law and read like any other (`test_mail_he_sends_only_to_himself_is_not_a_copy`). The
  owner addresses are `mailreport.owner_addresses`: an alias of his that never sent mail and is
  not in `recipients.OWNER_ADDRESSES` is not known to be his.
- **His own mail is recognised only Tuta-side.** A Tuta copy of his Gmail SENT mail is matched
  (§Open items and settlement); a Gmail inbound copy of mail he sent from Tuta is not — no such
  pair was observed, and none is matched.
- **Readings made blind are re-read only by their own model.** The 367 Gmail bodies that read as
  absent on 2026-11-02 were held all along — `evidence.get` failed CRLF bodies on its own hash
  (fixed). Readings made meanwhile are re-read `REREAD_PER_PASS` at a time and only for the
  pass's model; a reading by another model stays blind until a pass or `--reread-blind` runs with
  it, and a reading whose recorded bundle left the evidence store cannot be checked and stands.
  A body genuinely absent is still never judged and is counted in the footer.
- **A third party with no case history settles an item only by his vouch.** Tier (c) reads the
  case store only; a courier or payment provider on no case he transacted on (InPost on
  2026-11-02: on none) is named on the item as "not weighed", with the vouch offered as a chip,
  until he vouches for its exact domain and that kind of item (§The settler vouch).
- **An item's anchor can move.** An older asking mail indexed later becomes the first asking mail,
  so a your-turn row's anchor (and with it his resolve) moves — the row shows again: the visible
  direction.
- **The entity tie is a substring.** A counterparty's display name or domain label appearing in a
  later mail ties it as a candidate; that is a reason to ASK only — the tier rule decides, and a
  refused tie is named on the line — but it can name an unrelated later mail there.
- **The settlement judge is not admitted by this slice**, and its exam has not been run against a
  real model in the repository; until the driver admits it per model, every pair is counted *not
  yet judged* and every item stays open.
- **A forged OLD Date through Tuta can hide an answer from Follow-ups** (it sorts before his
  message). A future date is clamped to when aish first read the mail; a past one is not, since
  mail older than the watcher's first pass is legitimately first read late.
- **The Gmail link's form** (`https://mail.google.com/mail/u/0/?authuser=<account>#all/<id>`)
  was not opened against his account here; it is built from the provider id alone.
- **Retry on the report's answer** re-asks the task string — fixed words and the headline — of the
  session's MODEL, with the note in context: a model's presentation then replaces code's.
- **The Reader's admission has not been run.** The charter's central claims — that a model answers
  these cases as the exam expects — are hypotheses until `scripts/role-admission.py` runs per
  model. Nothing in this repository measured a model on it.

## What S1 deliberately does NOT do

- **Act.** No label, no archive, no trash, no push, no chip, no proposal, no draft. There is no
  code path to a mutating Gmail call (`READ_ONLY_CALLS`).
- **Judge the sender's pattern.** The Gate's judged half is S2.
- **Close cases deterministically, or snooze** — his close and the `cases` tool exist (§The case verbs), and since 2026-11-02 an ITEM can be settled from evidence (§Open items and settlement, demoted and named, never removed); a case is still closed only by him, and nothing snoozes.
- **Open any trust lane** — S4, by his explicit per-lane act, never a threshold.
- **Match interests** — S5. The Reader never sees the interest profile.
- **Run inside aish-web** — S2.
- **Read any mailbox but his Gmail account and his Tuta mailbox**, and only by the commands in
  this file (`READ_ONLY_CALLS`, `mailtuta.ALLOWED_ARGV`).
- **Notify per mail.** A failing watcher is a warning in `/mail`, not a push; #362's escalation
  pattern is for a later slice. The one push is the daily report's chat (§The daily report,
  2026-10-28, the owner's premise amendment) — a digest of counts, never a per-mail alert.

---

## For the driver: the real-inbox runs (after merge, not in this repository)

1. **Admit the Reader, per model** (real model calls; `--dry-run` first lists the cases):
   ```sh
   uv run python scripts/role-admission.py --dry-run mail-reader
   uv run python scripts/role-admission.py --model gemini:<flash spec> mail-reader
   uv run python scripts/role-admission.py --model local:<mi 35B spec> mail-reader
   ```
   Optionally first add his mined cases to `~/.config/aish/roles/mail-reader/cases.yaml` (the
   private half; a failing case there fails the admission).
2. **Backtest, 90 days, both arms** (read-only Gmail; writes only under the run directory, by
   default `~/.local/state/aish/mail-backtests/<timestamp>/`):
   ```sh
   aish-mailbacktest --account pawel@wenda.eu --owner pawel@wenda.email \
     --days 90 --arm gemini:<flash spec> --arm local:<mi 35B spec> --reader-max 300
   ```
   `report.txt` is the human report; `report.json` holds the full disagreement list for his
   adjudication. The backfill fetches every mail in the window one `gws` call each.
3. **Shadow pass** (when wanted): `aish-mailwatch --once --account pawel@wenda.eu --owner
   pawel@wenda.email --source gmail --source tuta --model <admitted spec>`, then
   `aish-mailwatch --counters`, and `/mail` in aish.
3a. **S1.5: re-admit first.** The owner cases file changed, so every earlier admission of the
   Reader no longer matches; re-run step 1 per model before the watcher's verdicts read `ok`.
   Then schedule it: `scripts/install-mailwatch-service.sh --print pawel@wenda.eu <spec>
   pawel@wenda.email` to read the plist, and without `--print` to load it.
3b. **S1.6: re-admit, then measure the teaching.** Charter v4 and the owner's 15 cases move the
   admission digest: re-run step 1 per model. A backtest resumed into an existing run directory
   re-buys every Reader call under v4 (expected). `report.txt` shows the raw counts and, beside
   them, the counts after the clamp. `aish-mailbacktest --csv <run-dir>` writes the review file
   into ~/Downloads without touching the mailbox; `/mail export` writes the live one. Both from
   his own terminal — a background context falls back to the state dir and says so.
3c. **The backtest over Tuta** (2026-10-26): add `--source gmail --source tuta` to step 2 (the
   bridge running, `TUTA_BRIDGE_PASSWORD` in aish's secret store). The report header states each
   Tuta folder's depth against the window and what is absent.
3d. **The keeper clamp** (2026-10-27) needs no re-admission (no charter change). A fresh backtest
   writes `keeper_clamped` into every record. A run directory from before it reads as no keeper
   clamp, and resuming it does not change that (a final reading is never re-read): re-run into a
   fresh `--out` to see the clamp in a report.
3e. **The daily report** (2026-10-28). Needs aish-web restarted on this build (the `/trigger`
   `report` field and `Agent.present` are server-side). Preview first:
   `aish-mailreport --dry-run --account pawel@wenda.eu` (reads only; sends and writes nothing).
   Then `scripts/install-mailreport-service.sh --print pawel@wenda.eu` to read the plist and
   without `--print` to load `com.aish.mailreport` (daily 07:30); **track the plist in mm-config
   like the watcher's**. Observed on 2026-10-28: the live index holds **0** of his sent messages
   (130 messages since 2026-10-23, Gmail 28, Tuta 102), so Follow-ups currently says it cannot be
   computed. Why the watcher indexes no sent mail is not established here — the next thing to
   look at before Follow-ups can mean anything.
3f. **The live backfill** (2026-10-31), once, with the bridge running and aish-web's env:
   ```sh
   aish-mailwatch --backfill 60 --account pawel@wenda.eu --owner pawel@wenda.email \
     --source gmail --source tuta
   ```
   No model, no Reader call; the printed JSON says what was indexed and observed. Re-run the same
   command after an interruption or a quota stop — it fetches only what the index lacks. Then
   `aish-mailreport --dry-run --account pawel@wenda.eu` shows whether Follow-ups now has sent mail
   to work from.
3g. **The Tuta authentication recheck** (2026-10-31), once, with the bridge running and
   aish-web's env (`TUTA_BRIDGE_PASSWORD` in aish's secret store):
   ```sh
   aish-mailwatch --recheck-tuta-auth --owner pawel@wenda.email
   ```
   No model, no Reader call, never `--seen`. The printed JSON says how many were rechecked, how
   many are now authenticated, still not (and why), failed, unmatched, and cases before/after.
   Re-run after an interruption or `failed` > 0; a run with nothing left to recheck makes no
   bridge call.
3h. **Inbox preferences** (2026-10-31). Needs aish-web restarted on this build (the tool, the
   report note's new heading). Seed his preferences once, then read them back and preview:
   ```sh
   aish-mailreport --seed-preferences
   aish   # then: /mail preferences
   aish-mailreport --dry-run --account pawel@wenda.eu
   ```
   The seed prints what it saved, that kuba@wenda… was NOT seeded (truncated address), and the
   `cases` hint for (d). The dry run's footer must read *Using 3 of your preferences (hide 3)*. A
   report chat delivered before the restart keeps the old heading, without the memory rule.
3i. **Open items and settlement** (2026-11-02). Restart aish-web on this build (the
   `inbox_items` tool, the report note's extended memory rule, the restore / done chips the server
   now keeps). Then admit the settlement judge per model — real model calls, `--dry-run` first
   lists the 8 cases:
   ```sh
   uv run python scripts/role-admission.py --dry-run item-settler
   uv run python scripts/role-admission.py --model gemini:gemini-3.8-flash item-settler
   ```
   Then, once, the backlog of readings made without their body (the CRLF bug; real Reader
   calls, one per such mail, oldest first — run with `--max N` to spread the cost):
   ```sh
   aish-mailwatch --reread-blind --model gemini:gemini-3.8-flash
   ```
   Nothing else changes in the jobs: every `--once` pass now runs the settlement step when the
   charter loads (unadmitted: no call, the report counts the candidates as not yet judged). To run
   the step alone over what is indexed (no mailbox read):
   `aish-mailwatch --settle --model gemini:gemini-3.8-flash [--since YYYY-MM-DD]`, then
   `aish-mailreport --dry-run --account pawel@wenda.eu`. Report chats delivered before the restart
   keep the old heading, without the extended memory rule, and their chips are broken by the old
   server.
4. **The mined replay scenario** — a real courier/store pair from his inbox — goes in the private
   tree (`~/.config/aish/evals/<name>/`, `docs/replay.md`), never here: find the pair in the
   backtest run's verdict store (`case_choice` on the courier mail), and replay it end to end
   through `mailwatch.assess` against the run's scratch state.
