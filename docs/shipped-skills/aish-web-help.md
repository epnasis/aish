---
name: aish-web-help
description: Use when the user asks how the aish web UI works — approval cards, the chat menu, pinning, deleting or restoring a chat, offline reading, attachments, PDF export, slash commands, keyboard shortcuts, the interactive shell, /watch or /fork.
keywords: aish, web, interface, approval, card, menu, chat, pin, delete, restore, offline, attachment, export, pdf, shortcut, slash, command, shell, watch, fork
---
How the aish web UI works, for answering the user's questions about it. Answer from this page; where it is silent, say so rather than guessing.

## Approval cards
- Every command aish proposes appears as a card with **Approve / This chat / Always / Deny** buttons and a pencil beside the command to edit it before it runs. File writes show a unified diff first.
- A card has an optional comment field; its text arrives with whichever button the user presses.
- Read-only commands auto-approve inside the chat's trusted folders. **This chat** auto-approves that command's prefixes until the chat closes (memory only). **Always** saves the same prefixes to the persistent allowlist (`~/.config/aish/allow.txt`).
- A command or file read that reaches outside the trusted folders gets a warning on its card and a **Trust dir** button: one tap adds that directory to this chat's trusted folders (memory only).

## Typing in the composer
- A message starting with `!` runs directly as a shell command — the user's own action, no aish and no card. `!cd <dir>` is the `/cd` alias that moves the project directory.
- Slash commands: `/model /resume /delete /new /fork /cd /add-dir /jobs /watch /help`, plus `/learn` (distils the conversation into saved skills/memory; an optional hint follows, e.g. `/learn the gh flow`; `/learn lessons` migrates the legacy lessons file), `/objective` and `/plan`.
- `/fork` (or `/branch`) copies the whole conversation so far into a NEW chat and switches there, leaving this one untouched — for a tangent or an alternative approach.
- `/watch` opens a live, read-only window on the page aish is driving with browse in this chat. It shows the page and changes nothing — no clicking, typing or scrolling, and it never interrupts aish.
- The ＋ button opens: attach file / reference a path (@) / slash command (/) / photo / send feedback / terminal mode (multi-command shell, prefix `!`) / interactive shell.
- The **interactive shell** is a real terminal for programs that prompt for input (gcloud auth, ssh host-key prompts, sudo). aish has NO access to it: its input and output stay private unless the user selects text and taps **Share**. URLs printed there are tappable, even when wrapped across lines; a tap opens the link in the device's chosen browser, and HOLDING it offers their own browser, aish's remote browser (where they can see the page and sign in, as `/browser` does) or copy, plus a row that changes what a plain tap does. A sign-in URL should be held and opened in aish's browser, since that is the browser aish later reads that site as.

## Keyboard (desktop)
- Cmd/Ctrl+K opens the chat list (again moves down a row, with Shift up, Enter opens).
- Cmd/Ctrl+Shift+M opens the model list (again moves down, Enter picks).
- Cmd/Ctrl+M switches between local and cloud — to whichever model on the other side a chat used most recently.

## The header and the chat menu
- **Chats** (top left, badge when a background chat needs attention) opens the chat list: it slides over the conversation on a narrow screen and is a sidebar on a wide one. Swiping the transcript sideways pages through recent chats.
- A chat is named automatically — after the first answer, when a fork first differs from its parent, and occasionally when the subject really changes. A name the user sets by hand is final and never rewritten.
- Tapping the chat title opens the chat menu: **New chat, Rename chat…, Switch model…, Change directory…, Pin chat, Line wrap, Text size, Export to PDF, Workspace & jobs…, Copy log path, Reconnect, Delete chat**. The compose pencil (top right) also starts a new chat.
- The context bar under the title shows the working directory (tap for a folder picker) and the model (tap to switch); the model chip also shows how full the context was on the last model call, as a percentage.

## Deleting, and getting it back
- **Delete chat is not final.** The chat moves to **Recently deleted** at the bottom of the chat list and can be restored for **30 days** before aish purges it.
- Deleted at once, and not brought back by a restore: the chat's throwaway working files and the stored copies of the requests its steps sent. The copy mirrored to the user's devices also goes at once and returns by syncing after a restore.
- Permanently deleting from Recently deleted is the one step that cannot be undone.
- Each of the user's own prompts has a row: the time it was sent, a trash chip, a pencil that puts the prompt back in the composer, and copy. The trash chip DELETES that whole exchange (their prompt, aish's work and answer) after a dialog naming what is lost: the text leaves the chat log and aish's context and every device's offline copy at the next sync; a "Message deleted" marker stays. It is the ONLY way to take something back — a message sent to the wrong chat, half-typed, or a pasted secret — and it cannot be undone.

## Answers and exports
- Every finished answer has a row of chips: copy, export that answer to PDF, and read-aloud where available.
- When a task the user sent went the long way round — very many tool calls, the same call repeated, steps refused, two or more comments on its cards, or stopped for looping — its answer also offers **Save this as a lesson?**. One tap sends `/learn`; nothing is saved without it, and every file `/learn` writes is approved first. It is never offered twice in a row in one chat, nor on a chat aish started itself.
- Both PDF exports render markdown locally and download the file. The whole-chat export holds only final answers, not thinking or steps, and is named after the chat; a single-answer PDF is titled after the ANSWER (aish writes that title when asked).
- PDFs embed pictures: local images inside the chat's trusted folders, web images, Google Maps snapshots (needs GOOGLE_MAPS_API_KEY) and YouTube thumbnails; anything unavailable becomes a captioned link card.
- Tool activity (thinking, recalled knowledge, commands and output) is grouped into one collapsible activity trace per turn.

## Offline reading and pinning
- The web UI works OFFLINE for reading: past chats are mirrored to the device automatically (newest first, including chats started on the user's other devices), so with no connection the app opens, past chats open, and search works over their contents.
- **Pin chat** in the chat menu is a one-tap toggle: the chat floats under "Pinned" in the chat list and is never dropped from the local copy however old it gets. The pin is the USER's, not the device's — pinning on one browser or phone pins it on all of them. To keep a conversation for reference while travelling, pin it.
- There is nothing else to manage and no cache to clear: the mirror caps its own size and drops the least useful copies first.
- SENDING is paused while offline, by design: a prompt queued for later would run commands with nobody there to approve them.

## Attachments
- Upload with the ＋ button, a PASTE into the composer (Cmd/Ctrl+V on a desktop, the paste button on a phone), or a DRAG onto the window. From an iPhone, the share sheet can hand aish a file or a link through a Shortcut (README: "Share to aish from iOS"); a shared file is attached to the user's next message and reaches aish only when they send it.
- The user can TAP an attached picture or PDF to look at it (a PDF opens page by page) and save any attachment to their device from there, so aish never needs to copy a file out for them.
- In the user's own copy of a message an attachment appears as `![[cat.png]]` (wiki-link style, as in Obsidian) — a message quoted back with `![[…]]` in it refers to an attached file.

## Chats and history
- Every chat (conversation + command audit trail) is logged under `~/.local/state/aish/` in the same format as terminal aish, so a chat started here can be resumed in the terminal and vice versa.

## Doing something later
- "Check after 10pm" and the like become a one-shot automation: aish proposes it with schedule_task, and the user approves a card that states when it runs, the whole instruction, the commands that may run without asking and the sites it may act on as them.
- At that time it runs once in a NEW automated chat (the automation glyph on the row); anything else that changes something waits there on a card, and the usual hold/finish pushes apply. A run that cannot start within 24 hours is given up and reported once.
- Saved as a plain JSON file in `~/.config/aish/automations/`; editing its `at` re-arms it, deleting the file cancels it. Automated chats cannot create automations, and aish refuses any other write into that folder.
- Renaming and deleting happen in the chat menu above.
