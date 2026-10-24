# The inbox assistant — shadow mode (#142 W5, slices S1 and S1.5)

`mailindex.py`, `mailgate.py`, `mailcases.py`, `mailreader.py`, `mailverdicts.py`,
`mailwatch.py`, `mailsource.py`, `mailtuta.py`, `mailview.py`, `mailbacktest.py`,
`aish/charters/mail-reader.md`, `scripts/install-mailwatch-service.sh`.
The binding design is the comment *"W5 REWRITTEN — the inbox assistant"* on epic #142
(2026-10-24). This file says what slice S1 built from it, what S1.5 added (the owner surface,
both inboxes, quota discipline, continuous runs — §S1.5 below), what they deliberately did not,
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
- `case` — a case he transacted on includes the address (from the case store, as of).

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

**Everything here is code.** Keys by regex after an introducing word (`_KEY_WORDS`, counted)
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
connection words when it is not connected; mail listed but not yet fetched. Each says what was
observed, never why. `TestHealth`, `TestWiring` (the slash registration; the web answers
terminal-only).

### Both inboxes — `mailtuta.py`

`TutaMailbox` sits beside the Gmail source (`mailsource.GmailSource`) behind one seam:
`listing`, `held`, `fetch`, `note`. It drives himalaya against tutabridge with the config
tutalib generates (`~/.config/aish/tools/_tuta/tutalib.py`, loaded by path; the secret
`TUTA_BRIDGE_PASSWORD` comes from aish's own store at run time, and its absence fails that
inbox's listing visibly while Gmail still runs). One shared state tree for both: the ledger
must see all of his acts whichever inbox they are in, and digest dedup makes a mail delivered to
both one entry (`TestIdentityNotUid`).

**The authenticator is declared by the adapter, never inferred from headers.** Each parser
stamps `Header.mailbox`; `mailgate.authenticate_header` reads it. Through the Tuta gateway
nothing prepends a result above the sender's, so the top-most Authentication-Results IS the
sender's, and a sender-written exact copy of Gmail's would authenticate. So
`mailindex.from_rfc822` DROPS every Authentication-Results header at parse time, and the Gate
gives the explicit ending `not_readable_via_gateway` with the reason in words — even for a
header that somehow survived. Unauthenticated, a Tuta mail is `first-contact` whatever the
history, opens no case, admits no alias and enters no case's account history; the forged
perfect-Gmail-header case is pinned end to end. `TestAuthenticationIsDeclaredByTheGateway`.

**Direction is the folder.** On Tuta a mail is `out` iff it sits in his Sent folder
(`\Sent`, else a folder named Sent). A From header naming one of his addresses is something any
sender can write; in his inbox it makes nothing his, so it can never become an owner act toward
the attacker it is addressed to (pinned). Counted, not acted on: `inbox_from_an_owner_address`,
and `sent_from_an_unlisted_address` — a mail in his Sent folder from an address not passed as
`--owner`, i.e. an alias the watcher was not told about. **Every alias he sends from must be in
`--owner`**: on Gmail it still decides direction. `at` is the sender-written Date header on Tuta
(counted per pass as `at_from_sender`).

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

**Provider ids are unstable here** (UIDs renumber on restart, UIDVALIDITY unchanged), so all
skip-by-id and top-up logic is Gmail-only. A Tuta fetch is skipped only when the envelope's own
Message-ID maps to a digest the index already holds — an identity the renumbering cannot move.
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
mail goes to the ledger. The bucket numbers are conservative CHOICES, not measurements; the only
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

### What a backtest costs

Two independent costs, and `--reader-max` bounds only one. **Reader calls** scale with
`--reader-max` (each selected mail is at most one call per arm, plus at most `READER_ASK_ROUNDS`
re-asks). **Fetch cost** scales with `--days` × his mail volume: the backfill lists the whole
window and fetches every listed mail once, one `gws` call each, under the bucket — 862 listed in
90 days on 2026-10-24 — whatever `--reader-max` is. A resumed run into the same directory re-buys
no fetch whose mail its index holds.

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
  structural stops the join.
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
- **The case store has no owner surface yet** — no list, no delete path. The design requires one
  (S3). Until then it is a store he cannot see or shred, written only by this CLI.
- **The lookalike flag needs admitted domains**, and admission now needs his act toward a sender,
  so early on it has few to compare against.
- **On Gmail a From header naming him still decides direction (found in S1.5).**
  `from_gmail` calls a mail `out` when its From is an owner address OR it carries `SENT`. A
  stranger's inbound mail forging his From, addressed to the stranger's own address, would read
  as his act toward that address; an authenticated mail from it could then grade `known`. Tuta
  closes this by deciding direction by folder alone; Gmail was left as S1 shipped it because
  changing it moves the ledger the backtest measured. Not fixed here.
- **Tuta's `at` is sender-written.** The Date header is all this gateway gives. It is bounded —
  an unauthenticated mail opens no case and moves no grade — but a forged Date reorders the
  index, and a Tuta copy of a mail that arrived first by its claimed Date is the copy the index
  keeps (first wins), which costs that mail its Gmail authentication: fail-closed, never open.
  Counted per pass as `at_from_sender`.
- **The no-\Seen guarantee rests on himalaya's documented behaviour.** The fence pins that no
  argv carries `--seen`; that `message read --raw` without it leaves the flag untouched is
  himalaya's help text ("Without this flag the read leaves the seen state untouched"), the same
  basis `tuta_read` stands on. It was not re-observed against the live bridge here — doing so
  risks marking a real mail read.
- **The himalaya envelope JSON shape** the Tuta listing reads (`messages[].uid`,
  `envelope.message_id`, `envelope.date`; STATUS `messages`) is the shape the tuta_* tools and
  their tests use; a run against the live bridge has not been made from this module.
- **The Reader's admission has not been run.** The charter's central claims — that a model answers
  these cases as the exam expects — are hypotheses until `scripts/role-admission.py` runs per
  model. Nothing in this repository measured a model on it.

## What S1 deliberately does NOT do

- **Act.** No label, no archive, no trash, no push, no chip, no proposal, no draft. There is no
  code path to a mutating Gmail call (`READ_ONLY_CALLS`).
- **Judge the sender's pattern.** The Gate's judged half is S2.
- **Close cases, follow up, snooze, or show the case store** — S3.
- **Open any trust lane** — S4, by his explicit per-lane act, never a threshold.
- **Match interests** — S5. The Reader never sees the interest profile.
- **Run inside aish-web** — S2.
- **Read any mailbox but his Gmail account and his Tuta mailbox**, and only by the commands in
  this file (`READ_ONLY_CALLS`, `mailtuta.ALLOWED_ARGV`).
- **Notify.** A failing watcher is a warning in `/mail`, not a push; #362's escalation pattern
  is for a later slice.

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
4. **The mined replay scenario** — a real courier/store pair from his inbox — goes in the private
   tree (`~/.config/aish/evals/<name>/`, `docs/replay.md`), never here: find the pair in the
   backtest run's verdict store (`case_choice` on the courier mail), and replay it end to end
   through `mailwatch.assess` against the run's scratch state.
