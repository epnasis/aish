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

**Parsing records, it never resolves.** Amounts become integer minor units plus a currency (default PLN, said so in `currency_source`); `496.33`, `496,33`, `3 264,00`, `3264,00 PLN` are accepted, and anything ambiguous (`1,234`) or not an amount (`TBD`) is null with the raw text kept, capped. `paid` takes any spelling of true/false; an empty one is `unknown`. Frontmatter and the body's bullets (`Kwota`, `Termin płatności`, `Rachunek`, `Faktura`) fill each other's gaps; where both are present and DISAGREE the field is null, its `_source` says `conflict`, and both values are in `conflicts` — which one is right is the owner's call. A paid line beside `paid: false` is a conflict too. A paid line is "Zapłacono" in any case after a heading marker, a ✅, or both — plain prose mentioning it is not one. An invoice number is accepted only when the WHOLE value is one token (`FV/123/2026`, or `nr 900000000123` in the body); `FV 123/2026` is unparsed with its raw text, never cut to `FV`. The month comes from frontmatter, else the file name's `YYYY-MM`.

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

## Not verified (the index)

Nothing here has run against the real vault: the formats above come from the plugin's source. In particular, what an offloaded note's listed `modified` and `size` are (the placeholder's, by the plugin's `stat` of the `.icloud` file) and whether every one of the ~500 notes follows the bullet labels above are read from code and from the owner's description, not observed.

# Matching bills to payments

`paysig.py`, `aish paysig`, `tests/test_paysig.py`. The owner, 2026-11-12: *"I usually pay exactly the same way. Once you figure out how to match each invoice for a given provider and type of payment, it would repeat every single time."* And, binding, 2026-11-13: *"Account or org name is to be matched, not names in Obsidian. The garage and the flat have different accounts and transfer titles. A telecom has three accounts and three contracts."*

So **the payee account is the bill.** A note's wording never identifies a bill, and a bill exists whether or not a note does: the bills are learned by code from the bank ledger alone, and each note attaches to one. Nothing the owner sees changes except the read-only `aish paysig`; bills in the brief are a later slice.

## Scars: what identified a bill before, and failed

Three matchers built on the note's words were measured on his data and dropped:

- **Bills keyed by the note's file-name words, then an account learned per key** (2026-11-12). Notes are free writing, so one bill splits into many keys (`prad lipowa`, `rozliczeniowa lipowa`, `lipowa`; a misspelling of the street; `Gazex Gazex` apart from `Gazex Lipowa` — the street and provider here invented), and few keys keep 3 note-months. Measured: 3 of 165 keys learned (131 of 494 notes).
- **Payee profiles with word overlap** (a prototype, never built). Measured on 183 paid notes: 92 attached to one account, 72 tied (42 of them the electricity supplier's notes across its accounts), 19 shared no word with the payee; at least 4 attachments were wrong.
- **Word-keyed associations** (built 2026-11-12, removed 2026-11-13). The invoice matcher learned `(provider words, purpose words) → account` from its A pairings and used it for notes with no invoice account. Measured on the full data (596 invoices read, 465 paid notes): usable 5, too few 31, stale 12, conflicting 2 — the same bill fragmented by free-text note names (one electricity bill of one property appeared as several one-month entries; the flat and the garage "conflicted" because their notes share words). Its `the invoice names a new account`, keyed the same way, fired 54 times across history. The notes-by-account measurement below shows why: 27 invoice accounts carry the 359 paid notes that have one, and up to 18 distinct note spellings land on ONE account.

## The bills, from the bank alone

`learn_bills`, pure and deterministic. A payment's bill is its payee account; a card payment (no account) belongs to its **merchant**: the title before the first `/` (the import era writes `MERCHANT /CITY … Data waluty: …`), digits dropped, folded and cut to 19 characters like a payee name.

- **An account is a bill** when it was paid at least `BILL_MIN_PAYMENTS` (3) times, or when a note's invoice prints it and it was paid at all.
- **A card merchant is a bill** only when it looks like one by the bank's own record: at least 3 payments, a regular cadence, an amount `always the same` or `a few values`, and at most `CARD_MAX_PER_MONTH` (1.3) payments per month it is paid in. Measured: of 1,210 merchant keys in 36 months, 296 had 3+ payments and 54 a regular cadence at about one payment a month — but most of those were shops (clothing, pizza, a cinema); the amount rule leaves the subscriptions (video, music, cloud services). Every rule here is a fence against a measured rival, not a classifier: a parking operator paid every two months is still a "bill" by it.
- **Its profile is its own last 36 months** (`BILL_HISTORY_MONTHS`, counted back from its last payment, so an ended bill keeps the profile it had): cadence, usual day-of-month range (the extreme tenth on each side dropped), amount profile (`always the same` / `a few values`, the top three covering 80 % / `varies`), recurring title words (words with a letter in at least half its titles), first and last payment month.
- **Its names are over ALL its history**, newest first, each with its first and last month: a payee renamed on an unchanged account (renamed twice) is one bill with three names, and a note written under any of them finds it.
- **Cadence** is the median gap between the months it was paid in: `monthly`, `every 2 months`, `quarterly` (3–4), `half-yearly` (5–7), `yearly` (11–13), when at least 60 % of the gaps sit at that median (±1 beyond monthly); otherwise `irregular`; fewer than 3 payment months is `too few payments`.
- **Its id names no account**: `account:` + 12 hex of the account's SHA-256, or `card:` + the merchant. The account itself is in `bills.json` only (0600), never logged or printed.

### Expected, not yet seen — and ended?

Per bill, as far as the ledger reaches (`min(today, synced_to)`; an unsynced ledger says `the bank ledger does not reach this month` and expects nothing):

- **`ended?`** when its last payment is older than two cadence lengths — or, with no cadence (irregular, too few), older than `UNCADENCED_ENDED_MONTHS` (12, a stated choice). It expects nothing.
- A `monthly`, `every 2 months` or `quarterly` bill with at least `EXPECT_MIN_PAYMENTS` (6) payments expects its next payment by the **end of its usual day range in the month one cadence after its last payment**. One day past that it is `expected, not yet seen`, with `days_late`. Stored per bill and as a bill finding; **not rendered** — the brief slice will use it.

## A note attaches to a bill

A note is an occurrence of some bill, never its identity; its id is its vault path. Its invoice evidence is the union over the note's attachments read `read` or `partial`, except a record flagged `fetched_elsewhere` or `shared_file`, which is evidence for neither note; an account that is one of his own (the bank's accounts table) is no payee. Amounts with `currency_seen: false` or `ambiguous_grouping: true` are WEAK.

**The route** (`NoteResult.route`), one per note:

1. **`invoice account`** — the account its invoice prints is its bill (tier A or B evidence, below).
2. For a note with no usable invoice account, the bank decides — **never another note's name**:
   - the bills whose names, over time, carry EVERY word of the note's `paid_to` (folded; words with a letter only) — of those, the ones paid in the note's window are the candidates (all of them when none was);
   - one candidate → **`payee name, one bill`**;
   - several → the one whose recurring title words carry a word of the note's file name (its words after the month, minus the payee's), while no word of the note is ANOTHER candidate's own title word (one no other candidate's titles recur) → **`payee name and transfer title`**;
   - otherwise **`several bills of this payee`**, with how many — what a later slice asks the owner about;
   - no bill carries the payee's words → `no bill of this payee`; no `paid_to` → `the note names no payee`.

The refutation clause is what lets a garage note reach the garage when the flat's titles carry the street and the garage's carry `garaz`; without it the note's street word reaches both. Four rival rules (any overlap; most words; covers every relevant word; this one) gave the same answers on his data; this one is the one whose fixture they fail.

**Measured, before building it on the notes** (2026-11-13, frozen copy): for the notes whose invoice prints exactly one account that is a bill, the C route was run with the invoice removed and compared with the invoice's account: **207 reached the same bill (42 by name, 165 by name and title), 0 another bill**; 73 were `several bills of this payee` (61 of them the electricity supplier's notes whose one word reaches two bills' titles equally — the flat's and the garage's, whose notes do not say what tells the titles apart), 67 `no bill of this payee` (a housing administrator's notes, paid to the community's account under another name).

## The window

From the first day of the second month before the note's month — or the invoice's earliest read date, when that is LATER — to a due date read on a due-date line plus 30 days (never before the end of the month after), else the last day of the second month after. **A read date only narrows**: invoices print dates from years back, and using one as a start made a 2026 bill take a 2021 payment. Stated choices: of several due-line dates, the one nearest the note's month; a note with no invoice due date uses its own `date_due`.

## The bank side

`read_payments` reads the ledger through `mailbank.read_ledger(keep_raw=True)` — the one read-only opening — and normalises both eras by the raw record's field names: the CSV import (to 2025-08-31: `Numer konta`, `Nadawca/Odbiorca`, `Tytuł`, `Opis operacji`; no creditor-account field) and the bank API (`creditor_account.other.identification` or `.iban`, `creditor.name`, `remittance_information` = title lines then the operation phrase). Left out as payments to nobody: transfers to his own accounts, and the counted phrases `PRZELEW NA TWOJE CELE` / `PRZELEW WŁASNY` (`docs/vocabularies.md`).

## Evidence, strongest first

- **A — account and amount**: a payment to one of the invoice's accounts, of one of its amounts. Weak amounts count only when no strong amount matches, and the pairing is marked `weak amount`. Never an amount with no account.
- **B — account only**: a payment to the invoice's account in the window, no amount agreeing. **A finding, never "paid", and it spends nothing.**
- **C — the note's bill by the route above**, for a note with no usable invoice account: the unspent payment of that bill in the window nearest the note's own month (a note amount, when present, picks first; one that differs is marked). Nearest, not earliest: measured, 221 of 251 A pairings were paid in the note's own month, and the earliest in the window is a cycle before (13 of the first 29 C pairings took the payment two months early before this was changed).

**Several invoices on one note** — more than one payee account across its usable attachments — is a finding of its own and pairs nothing. **Several invoices of ONE account on a note** (last cycle's invoice attached beside this one — a measured residual of the first build, where any amount on any attachment could price the note): tier A takes amounts only from the attachment whose read dates sit nearest the note's month (ties: the newest), and the pairing is marked `amounts on the note's other invoices of this account were not used` when another attachment printed amounts. A bundled last-cycle invoice therefore never lets last cycle's payment pay this note. **The invoice number only breaks a tie**: when a candidate's title carries one, it is chosen over otherwise equal candidates (marked).

### Why B stays B — the 90 "amount differs", measured (2026-11-13)

Every one of the 90 notes with a payment to its invoice's account in the window and no amount agreeing was compared with everything its invoice facts hold:

- **0** were a matcher rule problem: no payment equals an amount read from the invoice that A did not consider (weak amounts are already considered when no strong one matches; there are no page-only, currency or grouping misses).
- **55** paid an amount printed on ANOTHER note's invoice of the same account — the neighbouring cycle: in 27 the payment is that neighbour's own pairing, in 27 the neighbour is paired to another payment, 1 neighbour is unpaired; in 8 of the 55 the bill's own printed amount was there too, taken by a neighbour printing the same (a repeated forecast amount). 50 of the 55 are the electricity supplier's.
- **35** no neighbour explains — 25 with no amount near, 6 within 10 % and 4 within 1 % of a printed amount: a housing administrator 22 (its monthly payments are a fixed advance that matches no printed amount), insurance 8, the electricity supplier 3, a developer 2.
- No instalment sums (2–3 payments in the window adding to a printed amount) and 2 forecast schedules (3+ to-pay amounts) among them. 78 were read from a text layer, 12 by OCR.

So nothing is loosened: most of B is the neighbour's payment, and an amount tolerance would take it. Of the 7 "account known, no payment in the window": 3 had no invoice account (an association), 2 an account never paid in the ledger, 1 payments just outside the window (1 of 5 with the amount), 1 the nearest payment 6 months away.

## Assignment

Joint over all notes, deterministic, spent once. Recorded A pairs stand (one whose transaction left the ledger settles nothing, and its note takes no other). Then **A, with every claim evaluated once up front**: each unspent payment that satisfies A for exactly one note goes to that note first; then notes in month order — his notes before notes aish made, within a month — take the earliest unspent A payment. Then **C** over what is left. **B** is computed last and only reported. A note whose month is after the current one pairs nothing. One transaction pays one bill across this ledger and the mail bills' (`mailbank.assign` skips note pairs; `match` skips mail pairs).

**Only A is recorded** (note, transaction, tier, flags, `checked_to`) and never reassigned. **C is recomputed every run and gives way to any later A claim**: a C pairing frozen today would take the payment a neighbour's invoice matches exactly next week.

## Findings, one per note, and marks

Observations only: `note says paid, payment found` (tier A or C); `note says unpaid, payment found (the note is behind)`; `a payment to the invoice's account exists in the window, amount differs` / `…, no amount read` (B); `several invoices on one note`; `several bills of this payee` (with how many); `note says paid, no payment found:` `no invoice read yet, no bill of this payee` / `the invoice shows no account, no bill of this payee` / `bill known, no payment in the window`; `note says paid, and its payment is already paired to a mail bill`; `note says unpaid, no payment,` due date ahead / passed (the invoice's due line, else the note's), or no due date known, month ahead / passed; `the note's paid field is unknown or contradictory`; `the bank ledger does not reach this month`; `one document attached to several notes`; `the invoice was found outside the note's own folders`.

Marks: **`the invoice names a new account`** — only on a note NOT yet paid (no payment found, not marked paid) whose month is the current one or later, when its invoice prints an account that is no bill of its payee (the bills whose names carry its `paid_to` words); it carries, as an observation only, the payee name the newest of those bills is paid to. **History never raises it**, and neither does a payee with no bill to differ from. In practice it can fire only while a note is unpaid and in its own month (or a later one) with its invoice already read — once the note is marked paid or its payment is found, it is never raised. `weak amount`, `amount differs` (C), `chosen by the invoice number in the title`, `paired to an aish-made note while an owner note of the same bill shares the window`, `name disagrees`, `amounts on the note's other invoices of this account were not used`. Per paired account: payments to it that pair with no note.

Each note also stores, for the next slice, what its invoice offers a reminder: exactly one strong to-pay amount, several, or none read; a due date read on a due line, or only the month. Stored, not rendered.

## Stores, the one writer, the command

`<state>/payments/bills.json` — per bill: `account` (null for a card merchant), `kind` (`account` | `no account`), `names` (`{name, first, last}`, newest first), `name_words`, `title_words`, `cadence`, `cadence_months`, `day_range`, `amounts`, `payments`, `first_month`, `last_month`, `last_paid`, `status` (`active` | `ended?`), `expectation` (`state`, `expected_by`, `days_late`), `notes`, `spellings` (the note names that attached to it, without words carrying a digit), `senders` (empty; a later slice fills it) — and `findings` (each bill `expected, not yet seen`, with its days late). `<state>/payments/signatures.json` — per note: finding, tier, marks, `bill`, `route`, `candidates`, `observations`, reminder offer. `note-pairs.jsonl` — A pairs only, keyed by the note's path. All written whole through `atomic_write.publish`, 0600. **Only `learn_and_record` writes them**, under the item acts' lock; the report job calls it after the bank sync and the mail bills' pairs (`mailreport._learn_payment_signatures`), its outcome counts in the ledger entry's `paysig` part, and an exception is recorded by type and never costs the brief. **Could not check**: no readable notes index, invoice facts or ledger writes nothing; any of them stale (the index or facts older than 2 days, the ledger past `mailbank.FRESH_DAYS`) records the outcomes and NO new pairing.

`aish paysig` and `--backtest` recompute for display and write nothing. The command prints the bills table (the payee as the bank writes it, digit runs elided; cadence; payments; notes attached; status), tiers, routes, findings and marks as counts, the bills `expected, not yet seen` with their days late, and the new-account marks with the usual payee — never an amount, an account number or a title. `--backtest YYYY-MM` learns the bills from the ledger before the cut (and the invoice accounts of the notes before it) and reports, for paid notes from the cut on, tier A / B / C / none, the routes of those with no invoice account, how many with no read invoice C matched, whether C alone picks the same payment as A, and unpaid notes' payments found anyway.

`tests/test_paysig.py`: `TestTheBill`, `TestTierA`, `TestTheBills`, `TestExpectations`, `TestTierC`, `TestFindings`, `TestTheBacktest`, `TestTheBankSide`, `TestTheStoresAndTheWriter`, `TestTheCommand`, `TestTheReportJob`. A mutation run (no refutation clause, C earliest, the new account on history, the card per-month or amount rule off, the ledger reach ignored, names or name words from the last 36 months only, every named bill a candidate, `ended?` off) fails at least one test each.

## Measured (2026-11-13; a fresh frozen copy of the notes index, the invoice facts and a `.backup` of the ledger)

**Bills from the bank alone (Part 1, last 36 months)**: 7,371 outgoing payments, 1,101 to an account and 6,270 by card. 106 accounts, 48 with 3+ payments: cadence monthly 27, every 2 months 8, quarterly 1, yearly 2, irregular 10; amount always the same 3, a few values 22, varies 23; 36 kept one payee-name prefix, 12 two or three. 12 of the 48 are on a read invoice; 36 never — the most regular of those a tax office, a telecom's three accounts, a cable operator, housing and sports clubs and family transfers. Payee names with several accounts: an electricity supplier 5 (all linked to notes; their titles differ by words and an 11-digit number), housing communities 4, a club 6, a telecom 3 — **the telecom's titles carry only a per-invoice 14-digit number, nothing recurring: its three accounts are told apart by the account alone.**

**Notes by account**: of 465 paid notes, 359 have an invoice account, 106 none (66 with no invoice read). 27 distinct accounts; notes per account 1 (5 accounts), 2 (5), 3–6 (8), 10+ (9); distinct note spellings per account 1 (9 accounts) up to 13, 15 and 18; 11 notes print several accounts.

**After this slice**, against BEFORE (the invoice matcher with word-keyed associations; 494 notes, 465 paid):

| | before | after |
|---|---|---|
| payment found (paid notes) | 264 (A 251, C 13) | 280 (A 251, C 29) |
| amount differs / no amount read | 90 / 3 | 90 / 3 |
| several invoices on one note | 11 | 11 |
| several bills of this payee | — | 26 |
| no invoice read (and no bill of the payee) | 52 | 29 |
| invoice shows no account (and no bill) | 29 | 10 |
| bill known, no payment in the window | 7 | 10 |
| ledger does not reach (all notes) | 21 | 20 |
| the invoice names a new account | 54 | 0 |

Pricing a note only by its nearest invoice of one account (above), re-measured 2026-11-15 on a fresh copy: tier A 251 → 248, amount differs 90 → 93; 50 A pairings carry the mark.

Paid notes by route: invoice account 359, payee name one bill 18 (C paired 16), name and title 19 (13), several bills 27, no bill of the payee 42. Bills: 241 (188 `ended?`); of the 53 not ended — monthly 27, every 2 months 11, quarterly 1, yearly 4, irregular 8, too few payments 2; 39 accounts, 14 card merchants; 17 with notes, 36 without. `expected, not yet seen` now: 1 (a parking operator paid by card, 48 days); 33 next payment not yet due, 19 no expectation. Hold-out: at 2026-05, A 10, B 6, C 3, none 8 (C alone the same payment as A 4, another 0); at 2025-11, A 20, B 6, C 4, none 12 (same 12, another 0); unpaid notes with a payment found 0 at both.

## Residuals

- **The flat and the garage are mostly `several bills of this payee`.** Their notes' words (the street) reach both bills' transfer titles; what tells the titles apart is not in the notes. The route reports it — a later slice asks the owner which bill — rather than letting a note's name decide.
- **A bill is anything the bank shows paid regularly**: family transfers, a tax office, payment gateways (one account per payment operator, not per merchant) and a parking operator are bills by this rule. The rule has no notion of "a bill he cares about"; that is the owner's to say.
- **A merchant can split across the two ledger eras** where the import and the API write its card title differently. Not measured per merchant.
- **Weak amounts dominate A** (190 marks) and **A crosses cycles** where consecutive invoices print the same forecast amount; neither is changed here.
