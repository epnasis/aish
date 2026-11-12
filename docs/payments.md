# The payment notes index

`paynotes.py`. The owner keeps one Obsidian note per bill in the vault folder `Payments/` (~500 notes, named `YYYY-MM <Provider> <Purpose>.md`, YAML frontmatter plus a body with paid lines and detail bullets). The mail and brief code and offline analysis need what those notes say; this module puts it where they can read it.

## Why an index exists, and who may read the vault

**Only the aish-web process can open the vault.** It holds the macOS privacy grant for the iCloud folder; any other process — the terminal client, a cron job, a Claude Code session — gets a refusal or hangs forever on it. So the readers never touch the vault: aish-web exports a compact index into the state tree, and everything else reads that.

**The vault is read only through the owner's own plugin tools**, `obsidian_find` (one listing of the folder) and `obsidian_read` (one note), run through `tool_plugins.discover` + `tool_plugins.execute` exactly as a model's call would be: validated JSON args on stdin, no shell. Repo code never opens a vault path. The plugin owns path confinement and iCloud offloading (`.icloud` placeholders, the EDEADLK read, `brctl download`); a second reader would be a second copy of those rules to keep in step. `plugin_runner` returns None — outcome `no tool` — when either tool is absent, or when either declares itself `mutating`: this runs unattended and may only read.

## The refresh

**What the plugin returns** (read from its source, not from a run). `obsidian_find` prints JSON `{"hits": [...], "total_matching": N}`; each hit is `note` (vault-relative path), `title`, `modified` (local mtime, `YYYY-MM-DD HH:MM`), `size`, and either `offloaded: true` or `tags` + `preview` (a few head lines). No frontmatter beyond tags. Hits are newest-modified first and cut to `max` (default 20), so the listing asks for `LIST_MAX`, far above the folder, and records `listing_truncated` when `total_matching` still exceeds the hits. `preview_lines` is read as `int(x or 3)`, so zero cannot disable it and one is asked. `obsidian_read` prints `{note, vault, frontmatter, tags, aish_editable, attachments, content}`; frontmatter is the plugin's YAML subset (strings with quotes stripped, `[]` for an empty key). A failure is `ERROR: …` on stderr with exit 2 — the reason recorded for an unread note is that text.

**Incremental.** Each listed note's marker is `(modified, size, offloaded)`. A note is read when it is new, its `(modified, size)` changed, its last read did not succeed, or `PARSER_VERSION` moved. An offloaded note already read is NOT downloaded again to check it. When its `(modified, size)` still differs from the read's, the record keeps the old parse and carries `stale: true` (counted as `stale`): the note may have been edited before it was evicted, or eviction alone may have moved the marker — the index cannot tell which, so it says neither. A read whose returned `note` is not the listed path is `unread` with "read resolved to a different note": the plugin's resolver falls back to a vault-wide name search when the path is gone, and a same-named note elsewhere is not this bill. At most `MAX_READS_PER_RUN` (150) reads per run — never-tried notes first, then changed ones, then ones whose last read failed, newest first within each — so a note that keeps failing cannot starve the rest. The rest are `pending`, counted in `reads_remaining`; the loop comes back after `BACKLOG_EVERY` only when the run made at least one successful read (`read_ok_this_run`), and otherwise waits the full day — iCloud offline would otherwise spend the whole budget on failures every few minutes.

**Running it.** `Refresher` is started by `WebServer.startup` beside the scheduler, as its own task, and cancelled in `shutdown`. It refreshes at start and then every `REFRESH_EVERY`. The work runs on the refresher's OWN single thread — a refresh is a chain of wrapper subprocesses that can take minutes, and the default executor is for the short calls a client needs (`docs/web-server.md`, L3). Nothing in it can delay startup or a request, and nothing it raises reaches the server: a failure is printed (exception type only) and written to `index.json` as outcome `error`. `AISH_PAYNOTES=0` starts nothing; the suite sets it everywhere (`no_background_paynotes`) and the tests of the refresher clear it.

## The files

`<state>/payments/notes.jsonl` — one record per listed note: `path`, `file`, `status` (`read` | `unread` | `pending`), `listed` and `marker` (the listing's marker now, and at the last read), `parser`, `read_at`, `unread_reason`, `has_frontmatter`, `category`, `paid_to`, `paid_for`, `created`, `updated`, `month` + `month_source`, `amount` (`minor`, `currency`, `currency_source`, `source`) + `amount_raw`, `date_due`, `invoice_number`, `account_number` (each with a `_source`), `account_valid`, `stale`, `paid` (`true` | `false` | `unknown` | `conflict`) + `paid_source`, `paid_lines`, `tags`, `attachments`, `conflicts`, `unparsed`.

`<state>/payments/index.json` — `refreshed_at` (this attempt), `outcome` (`ok`, `partial`, `no tool`, `list failed`, `error`), `error`, `counts`, `reads_remaining`, `listing_truncated`, `notes_written_at`, and `notes_sha256`, the digest of the notes file it describes.

Both are written whole through `atomic_write.publish`. A run that fails writes no notes: `index.json` records the failure and carries forward the digest and counts of the notes file that still stands, so the last good index stays readable. `load` checks the digest, so a notes file that does not match its index is refused rather than half-believed.

## The laws

**Parsing records, it never resolves.** Amounts become integer minor units plus a currency (default PLN, said so in `currency_source`); `496.33`, `496,33`, `3 264,00`, `3264,00 PLN` are accepted, and anything ambiguous (`1,234`) or not an amount (`TBD`) is null with the raw text kept, capped. `paid` takes any spelling of true/false; an empty one is `unknown`. Frontmatter and the body's bullets (`Kwota`, `Termin płatności`, `Rachunek`, `Faktura`) fill each other's gaps; where both are present and DISAGREE the field is null, its `_source` says `conflict`, and both values are in `conflicts` — which one is right is the owner's call. A paid line beside `paid: false` is a conflict too. A paid line is "Zapłacono" in any case after a heading marker, a ✅, or both — plain prose mentioning it is not one. An invoice number is accepted only when the WHOLE value is one token (`FV/123/2026`, or `nr 245750201495` in the body); `FV 123/2026` is unparsed with its raw text, never cut to `FV`. The month comes from frontmatter, else the file name's `YYYY-MM`.

**No index is not zero bills.** `load` answers `available: False` with a reason when there is no index, it cannot be read, no refresh ever succeeded, or the notes file does not match it. An empty folder is an index with zero notes.

**The files hold financial data.** They live in the state tree only (never the config tree, which is pushed to GitHub), mode 0600 in a 0700 folder, and this slice renders nothing from them to a model. Nothing logs note content or an account number; an account number the parser cannot read is recorded as unparsed WITHOUT its raw text. Account numbers go through `mailcases.normalize_coordinate`, so the index and the mail cases compare in one space (a bare 26-digit number becomes its PL IBAN).

**`aish paynotes` reads, never refreshes.** It prints the index's counts, age and the outcome WORD — no note content, and never the recorded error text, which can quote a note's name (that stays in `index.json`) — and says that only aish-web can refresh it. The same holds for its invoice-facts block: counts and outcome words only.

## Invoice facts

`paynotes.refresh_invoices` + `invoicefacts.py`. The note says WHICH bill; the invoice attached to it says what a bank payment of that bill looks like.

### Why the invoice is the evidence

Matching bills to bank payments needs two things a note almost never has. Of the 494 notes, 475 carry no amount, and almost none states the payee account. The attachments do: 443 PDFs, 113 PNGs and a few JPG/HEIC files, listed in each record's `attachments`. An invoice states the account to pay and the amount, and those match the bank ledger exactly. (These counts come from the owner's analysis of the index before this slice; this slice did not re-measure them.)

**The scar: three attempts to match from names and timing failed on real data.** As measured in those attempts, before this slice:
- a key-based learner matched **3 of 165** bills;
- account-profile word matching attached **92 of 183** paid notes, with **wrong** attachments;
- an amount alone matched **7–8 unrelated payments**.

A name is shared by every bill from one provider, and a time window by every bill of a month. An account together with an amount is not shared. So this slice collects only what the invoice itself STATES, and leaves the matching to a later slice that reads these facts beside the ledger.

### Fetch: the owner's plugin, into aish's own scratch

`obsidian_attachment` is the owner's read-only plugin (`mutating: no`, `timeout: 180`). Called with `note` and `name`, its wrapper does the following, read from its source (`~/.config/aish/tools/obsidian_attachment/wrapper`), not from a run:
1. It resolves the name the way Obsidian does: the note folder's `attachments/`, the note's folder, the vault-root `attachments/`, then a vault-wide basename search. An ambiguous name is an error naming the matches.
2. It materialises the file from iCloud (`brctl download`, waiting `wait` seconds, default 60).
3. It COPIES the file into `attachlib.download_dir()`: `AISH_TOOL_DOWNLOADS_DIR` when the runtime sets it, else a fresh `mkdtemp`. The name is sanitised, flat, and made unique.
4. It prints JSON `{filename, path, bytes, mime: null, from_vault, vault}`, with `bytes` from `stat()` on the copy. A failure is `ERROR: …` on stderr with exit 2, for example "is offloaded to iCloud and did not finish downloading within 60s".

The wrapper has no size limit. `tool_plugins.execute` sets `AISH_TOOL_DOWNLOADS_DIR` from its `downloads_dir` argument, so `PluginRunner` passes `<state>/payments/invoice-scratch/fetch`. **Without that argument the copy would land in a temp directory that nothing deletes.** `test_through_execute_the_wrapper_is_handed_the_scratch_dir` runs a real wrapper through `execute` to pin it.

The plugin's reported `path` is a claim. It is read only if it resolves INSIDE the fetch directory and is a file there; otherwise the attachment is `fetch failed`. **A stray copy is cleaned up, and nothing else is ever deleted.** When the reported path is outside the fetch directory, it is deleted only in the exact shape of the wrapper's fallback: a regular file (not a symlink) in a directory whose name starts `aish-obsidian-` (`STRAY_DIR_PREFIX`, the prefix the wrapper hands `attachlib.download_dir`) and which sits DIRECTLY in the system temp directory. That directory goes too when it is left empty, and the record says `stray_removed`. Anything else — `$TMPDIR/foo.lock`, another tool's temp dir, a deeper path, anything outside the temp dir — is left alone, and the record says `stray_copy_may_exist`. A copy over `MAX_ATTACHMENT_BYTES` (25 MB) is `too large` and not read.

**Where the plugin found the file is kept, because its last resort is a guess.** The record keeps the plugin's `from_vault` (vault-relative path). It is `fetched_elsewhere: true` when that path is not in the note's own folder, the note folder's `attachments/`, or the vault root's `attachments/`: the plugin found it by its vault-wide name search, and a same-named file elsewhere may belong to another bill. When two different notes' attachments resolve to the SAME `from_vault` file, both records are `shared_file: true`. The facts are kept under both flags, and both flags are counted in `index.json` and by `aish paynotes`. **A matcher must discount both:** an elsewhere file may not be this bill's, and a shared file is one piece of evidence, not one per note. `TestInvoiceProvenance`.

**Shutdown does not wait for a fetch.** `tool_plugins.execute` took an optional `should_stop`. No interactive caller passes it, and without it the run is the same `subprocess.run` as before. With it, the wrapper is polled every quarter second and killed when Stop is set, while the tool's own timeout still applies. The refresh passes its stop flag. `TestInvoiceStoppableFetch`.

**The scratch directory is the only place an invoice's bytes or its rendition ever exist.** It is `<state>/payments/invoice-scratch/`, mode 0700. It is emptied before and after every attachment, at the start and end of every run, and in `refresh_invoices_recorded`'s `finally`. The pipeline's rendition store is inside it (`renditions/`), not the shared document store, which lies inside the model's workspace boundary. Only the facts are kept.

### Reading: the repo's one document pipeline, in a child process

`invoicereader.py`. Each attachment is read by `python -P -m aish.invoicereader`, a child of the refresher. The pattern is the OCR child's, for the same reasons:
- A pathological PDF can take unbounded time and memory inside PyMuPDF. In aish-web's own process that would wedge the refresher thread and grow the server.
- The child runs at nice 19, and its OCR grandchild inherits that. So PDF conversion yields to the owner's work and to the local model, as well as OCR does.
- It is killed — its whole process group, the OCR grandchild included — at the attachment's deadline or on Stop.
- Its memory goes back on exit.

**The child gets a scrubbed environment**, not the server's, which holds API keys from the launchd plist while a third party's PDF is being parsed. It keeps only `PATH`, `HOME`, `TMPDIR`, `LANG`, `LC_*`, `USER`, `LOGNAME`, `AISH_OCR` and `PYTHONPATH` (`invoicereader.child_env`). The OCR grandchild needs nothing more: `ocr.py` reads only `AISH_OCR` and runs `sys.executable`. `test_the_child_gets_a_scrubbed_environment`.

**Only facts cross the process boundary:** per page, how its words were obtained and `invoicefacts.page_facts` of them. The text never does. `TestRunReader`.

The child judges the file by its BYTES, never its name:
- `%PDF` goes to `documents.convert`;
- a picture `ocr.picture_format` knows (PNG, JPEG, GIF, WebP, HEIC/HEIF/AVIF, TIFF) goes to `documents.convert_picture`, read by OCR as a one-page scan;
- anything else is `not a pdf or picture`.

**Bounded before anything is allocated:**
- A PDF with more than `MAX_PAGES` (60) pages is not read at all (`too many pages`).
- A page whose MediaBox at OCR resolution (200 dpi) would exceed `MAX_RENDER_PIXELS` (25 MP, about an A1 sheet; an A4 page is 3.9 MP) is excluded BEFORE any pixmap exists. It is recorded unread (`page too large to render`).
- Conversion and OCR together have `ATTACHMENT_SECONDS` (180 s), or what is left of the run's budget with a floor of 30 s. The child's own OCR loop stops 5 s earlier so it can still answer with what it read. The parent kills it at the deadline regardless.

**A long scan is read across calls and across visits, and no page twice.** Within one visit the child calls `recognise_scans` again and again, 12 pages a call, until the pages are done, the deadline comes, or a call makes no progress. A page already read in an earlier visit of the same file (same `sha256`, same extractor) is cut out of the PDF before conversion, so it is never converted or recognised again. Its per-page facts are kept in the record (`page_detail`), and a later visit only adds pages. A changed file starts afresh. `TestReadPages`, `test_a_long_scan_adds_pages_visit_by_visit_and_never_rereads_one`.

Per attachment the record says how its words were obtained:
- `read_as`: `text layer`, `ocr`, `mixed` or `not read`;
- the page counts: `pages`, `pages_read`, `pages_text`, `pages_ocr`, `pages_ocr_empty` (OCR ran and found nothing), `pages_unread`;
- `page_detail`: per page, `how` (`text`, `ocr`, `ocr_empty`, `unread`) and its facts or its reason.

**A hollow extraction is never a complete one.** A page whose words are not in hand makes the record `partial` (some pages read) or `unread` (none read). The record carries the reason (`reason`) and an outcome word (`reason_class`), one of:
- `ocr unavailable` (off a Mac, or `AISH_OCR=0`);
- `ocr failed`;
- `pages not read` (the deadline, or Stop);
- `page too large to render`, `too many pages`, `conversion failed`;
- `timed out`, `stopped`, `reader failed` (the child itself).

An `unread` attachment gets no new facts. `test_a_text_page_beside_an_unread_scan_is_partial`, `test_a_scan_with_nothing_read_keeps_no_facts`.

### Facts: by code, no model, no free text

`invoicefacts.page_facts` reads one page and `invoicefacts.merge` joins a document's pages. Both are pure.

- **Accounts.** A Polish NRB in one of three printed groupings: 26 digits contiguous, `2+6×4`, or `6×4+2`. A space, NBSP, hyphen or line break may separate the groups, and a `PL` prefix is optional. Also any IBAN; its greedy match is trimmed group by group until the checksum holds. Each is kept ONLY when ISO 13616 mod-97 holds, normalised by `mailcases.normalize_coordinate`. Per account, the record keeps `count` (distinct occurrences) and `near_keyword` (`line`, `adjacent` or null, for payable or account words). The checksum is what makes this reliable. It still passes a random candidate about 1 time in 97, which is why only those printed shapes are candidates at all.
- **Amounts.** An amount needs a decimal part (`1 234,56`, `1234,56`, `1.234,56`, `1,234.56`, `496.33`) or a currency beside it (`2 789 zł`). That rule keeps phone numbers, NIP, REGON, postal codes, years and account digits out. Dates (`10.11.2026`, ISO) and percentages are refused by their shape. A minus sign must touch the number; a dash or bullet before it is not one. Each amount is integer minor units with its currency. An unmarked amount takes PLN only when every currency written anywhere in the document is PLN (`currency_source: document`); otherwise its currency is null. Per amount, the record keeps:
  - `count`;
  - `payable` (`line` or `adjacent`): a payable keyword on the same or a neighbouring line;
  - `currency_seen`: whether any occurrence had its currency written beside it;
  - `ambiguous_grouping`: a number printed with a PLAIN space inside it (`2 234,56`) may be a thousands separator or a quantity followed by a price. Both readings are kept (`223456` and `23456`), both marked. A typographic space (NBSP, NNBSP, thin) or a dot is a separator the typesetter chose, so it is not ambiguous. An occurrence of the same amount elsewhere without the ambiguity clears the mark.

  **A matcher must treat `currency_seen: false` and `ambiguous_grouping: true` amounts as WEAK.** A defaulted currency can attach to a stray number such as `10.11` from a date line, and an ambiguous grouping has a rival reading in the same record.
- **Dates.** ISO, `DD.MM.YYYY`, `DD-MM-YYYY`, `DD/MM/YYYY`, and `10 listopada 2026`, validated as real dates. `due` flags a due-date keyword.
- **Invoice-number candidates.** These are taken only after an invoice-number keyword, kept with their case, from the rest of the line, a table cell, or up to a gap of 2+ spaces. The value is cut to its CODE RUN: the first token holding a digit, with an upper-case prefix of up to four letters before it (`FV 123/2026`, `KOR FV 12/2026`) and digit-bearing tokens or separators after it. A word ends the run, so `FV/1/2026 z dnia 10.11.2026` yields `FV/1/2026`, and a person's name beside a number is never stored. The value is capped at 40 characters. When the keyword ends its own line, the value comes from the next line (`where: next line`). The general `nr dokumentu` / `numer dokumentu` count only when what follows is at most 3 tokens and starts with a digit-bearing one, because a document number is as often an identity card's. For the same reason, after those two keywords a bare digit run (9–11 digits is a PESEL, NIP or ID-card shape) is no candidate: the code must hold a letter or a separator. A value with no digit is no candidate, and no candidate is ever taken from anywhere else.

**Keywords flag; they never choose.** Every amount, date and account is kept. Which flagged amount is the one to pay is for the matcher to decide against the ledger. The four keyword lists and the month names are declared and counted (`docs/vocabularies.md`). Matching folds diacritics and case on both sides, `ł` to `l` included. They are consulted in the reader child, which sends its tallies back with its answer (counts only), and the parent counts them; from there they drain into whichever session flushes next. `test_the_child_s_vocabulary_tallies_reach_the_parent`.

**Known and not handled.**
- Cell and wide-gap boundaries come from the rendition's layout.
- OCR order can separate a label from its value (`docs/documents-and-pdf.md`). That is one reason flags are `adjacent` as well as `line`, and why nothing is paired by position.
- `adjacent` does not cross a page boundary, since pages are read one at a time.

### Store

`<state>/payments/invoices.jsonl` holds one record per (note `path`, attachment `name`), in the notes file's order (newest modified first). Each record has:
- `status` (`read` | `partial` | `unread` | `pending`), `fetch`, `reason_class`, `reason`, `gave_up`, `stray_removed`;
- `from_vault`, `fetched_elsewhere`, `shared_file`;
- `kind`, `read_as`, the page counts, `page_detail`, `bytes`, `sha256` (of the fetched bytes);
- `note_marker` (the note's read marker when this was processed), `extractor` (`invoicefacts.EXTRACTOR_VERSION`), `tried_at`, `read_at`, `attempts`, `fruitless`;
- `facts`: `accounts`, `amounts`, `dates`, `invoice_numbers`, and `keyword_lines` (how many lines matched each list).

`index.json` gains an `invoices` section: `refreshed_at`, `written_at`, `outcome` (`ok` | `partial` | `deferred` | `no tool` | `no notes` | `error`), `error`, `deferred`, `counts`, `remaining`, `extractor`, `invoices_sha256`. The notes refresh carries the section forward untouched, on success and on failure. `load_invoices` checks the digest. It answers `available: False` with a reason when there are no facts, which is never the same as zero invoices. Like a note record, an `unread` record keeps an earlier read's facts if it had any — but only while they can be tied to the bytes in hand. When a visit ends without a digest (fetch failed, too large, an exception), it cannot establish that the file is still the one those facts came from. So `sha256`, `facts` and `page_detail` are dropped, and the record says `stale_facts_dropped: true`. A visit that reads different bytes replaces all three. `TestInvoiceStaleFacts`. Only `read` and `partial` records carry facts established by their latest read.

The file holds payee account numbers. It is 0600 in the 0700 folder, never logged, and never rendered to a model by this slice. The daemon log line carries counts only. A failure records the exception's TYPE, never its message, because a pipeline message can quote a file name.

### Incremental, bounded, one at a time, out of the owner's way

An attachment is processed when:
- it is new;
- `EXTRACTOR_VERSION` moved;
- its note's marker moved; or
- its last visit did not reach `read` and it has not been given up on.

When only the note moved, the attachment is re-fetched; if its `sha256` is unchanged and the extractor is the same, the earlier facts stand without a re-read. The order is the notes refresh's: never-tried first, then changed, then unfinished, newest first within each.

**Giving up, a choice.** A visit that reads no new page is fruitless. After `MAX_FRUITLESS_VISITS` (3) fruitless visits in a row, the attachment is not visited again, and the record says `gave up after 3 attempts without progress` beside its last reason. A changed note or a new extractor version gives it a fresh start. Without this, a broken PDF or a page OCR cannot read would be fetched and read on every run, for ever.

Two named gaps in this rule:
- **Giving up is permanent if the attachment's bytes are replaced without the note changing.** The marker that restarts it is the NOTE's, and a given-up attachment is never fetched again, so its new bytes are never seen.
- **A note edited every day, with a permanently broken attachment, re-fetches that attachment every day.** Each edit moves the marker and gives it a fresh start, so the give-up count never reaches 3. **Only progress counts as progress:** a record that newly reaches `read`, or one whose read page count grew. Re-reading a partial file that yields nothing new does not bring the next run sooner. `TestInvoiceRetry`.

**The bounds were chosen here, not measured against a failure:**
- `MAX_INVOICES_PER_RUN` = 25;
- `INVOICE_RUN_BUDGET` = 10 minutes of wall clock, checked BETWEEN attachments;
- `ATTACHMENT_SECONDS` = 180 per attachment's reading (floor 30);
- `INVOICE_FETCH_WAIT` = 60 s, the plugin's default.

So a run overshoots its budget by at most one attachment: its fetch (bounded by the tool's 180 s timeout, and cut short by Stop) plus its reading (bounded by the reader child's deadline, at most `ATTACHMENT_SECONDS`, conversion included, enforced by killing the child).

**Out of the owner's way, both choices:**
- **A busy chat.** An invoice run does not START while any chat session is busy (`Session.busy`, handed to the Refresher as a callable by the server). It records outcome `deferred` and comes back after the backlog gap. The same check runs between attachments: a chat that starts working stops the run early, the rest stay pending, and the section says `deferred`.
- **The backlog gap.** The gap between backlog runs, `BACKLOG_EVERY`, is 15 minutes, for the notes and the invoices alike.

The next run comes after that gap only when invoices remain AND this run made progress, or when the run was deferred. Otherwise the wait is the full day, so iCloud being offline cannot become a loop of failures. `TestInvoiceCadence`.

Attachments are processed strictly one at a time, on the Refresher's own thread, after the notes refresh of the same run. The stop flag is checked between attachments, handed to the fetch (which it kills) and to the reader child (which it kills). `AISH_PAYNOTES_INVOICES=0` turns this half off alone; `AISH_PAYNOTES=0` still starts nothing. Server start and requests never wait on it. Shutdown cancels the loop and does not wait for its thread, whose running child is killed within a quarter second of Stop.

### Not verified

Nothing here has run against the real vault or the real plugin. The plugin's arguments, output and error shapes are read from its source. Also not observed:
- how long an iCloud download of a real attachment takes, and how often 60 s is too short;
- OCR's real cost on this machine beside the local model (measured only for `read_pdf`, `docs/documents-and-pdf.md`);
- the reader child's start-up cost per attachment (it imports PyMuPDF each time);
- whether the owner's invoices print their accounts in the three groupings above;
- how many attachments will come out `partial`, `unread`, `fetched_elsewhere` or `shared_file`.

The ~560 total is the sum of the counts above; no listing confirmed it. There is no memory limit on the reader child beyond its deadline and the 60-page and 25 MP caps: macOS does not enforce an address-space limit, so none was set.

## Tests

`tests/test_paynotes.py`: `TestPaynotesAmounts` (every amount spelling, and the ones refused), `TestPaynotesNoteParsing` (paid spellings, missing frontmatter, body-only values, disagreements, unparsed raw text), `TestPaynotesRefresh` (the listing's arguments, incremental reads, offloaded notes, unread notes and their retry, the per-run bound, a failed run leaving the previous index intact, `no tool`, 0600), `TestPaynotesLoad` (no index vs an empty folder, a mismatched notes file), `TestPaynotesRefresher` (backlog cadence, a raising refresh recorded and survived), `TestPaynotesServer` (the kill switch, inert without the tools, a stuck then raising refresh never holding the server), `TestPaynotesCli` (counts only).

Invoice facts:
- `tests/test_invoicefacts.py` tests each extractor against its rival:
  - `TestInvoiceAccounts`: checksum-failing accounts, every printed form, a line break, identifiers;
  - `TestInvoiceAmounts`: every notation; identifiers, dates and percentages that are not amounts; the payable flag;
  - `TestInvoiceAmountGrouping`: both readings of a plain-space grouping, `currency_seen`, pages merged;
  - `TestInvoiceDates`: formats and the due flag;
  - `TestInvoiceNumbers`: the code run, and the short-value rule for document numbers;
  - `TestInvoiceTextIsNotKept`, `TestInvoiceVocabularies`.
- `tests/test_invoicereader.py`:
  - `TestReadPages`: the page cap, the pixel cap with no pixmap, skipped pages cut out before conversion, OCR across calls, the deadline;
  - `TestRunReader`: the real child returns facts and no text; a stuck child and its grandchild are killed; Stop; a crash; nice 19.
- In `tests/test_paynotes.py`:
  - `TestInvoiceFetch`: the plugin's real arguments and output, the scratch dir through a real `execute`, deletion on every path, a reported path outside the scratch;
  - `TestInvoiceReading`: the real reader child with OCR off — the hollow partial case, a scan with nothing read;
  - `TestInvoiceRetry`, `TestInvoiceProvenance`, `TestInvoiceStrayCopy`, `TestInvoiceStaleFacts`, `TestInvoiceCadence`, `TestInvoiceStoppableFetch`;
  - `TestInvoiceRefresh`: incremental by marker and digest, the extractor version, the per-run bound, the wall-clock budget, the stop flag, retry order, 0600 and the digest, the section surviving a notes refresh;
  - `TestInvoiceRefresher`: its own thread, the kill switch, backlog cadence, nothing from an invoice in the log;
  - `TestInvoiceServer`: a stuck fetch holds neither requests nor shutdown;
  - `TestInvoiceCli`: counts and outcome words only.

The fake plugin builds its answers in the plugin's real output shapes through the runtime's own `tool_plugins.envelope`.

## Not verified

Nothing here has run against the real vault: the formats above come from the plugin's source. In particular, what an offloaded note's listed `modified` and `size` are (the placeholder's, by the plugin's `stat` of the `.icloud` file) and whether every one of the ~500 notes follows the bullet labels above are read from code and from the owner's description, not observed.
