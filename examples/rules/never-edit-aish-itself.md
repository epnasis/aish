---
name: never-edit-aish-itself
description: aish's own source changes through issues, never directly.
when:
  action:
    path_under: ~/dev/aish
then:
  never_use: [write_file, edit_file, run_command]
---

The source of the assistant you are running inside is not yours to edit. Open a
GitHub issue on epnasis/aish describing the improvement, the bug, or the fix —
that is the whole contribution, and someone else applies it with review.

Two things this does NOT cover, deliberately:

  - `~/.config/aish` — skills, memory, rules, tools. That is the owner's
    knowledge, it is yours to write, and it is backed up in its own git repo.
  - reading. You can and should read the source to answer questions about how
    aish works; the rule is about changing it.
