---
name: always-use-show-image
description: Any picture in an answer comes from show_image, never a raw markdown link.
when: always
then:
  answer_must_show_if_used: show_image
  answer_must_not:
    pattern: '!\[[^\]]*\]\(https?://'
---

Whenever a picture belongs in the answer — a map, a Wikipedia photo, a video
thumbnail, anything — call show_image and use the markdown it returns. An
external image link does not render in the UI.

Note the trigger: this is NOT "when the user asks for a picture". The prompt
can be entirely generic and the answer can still want an image, so the
condition is on every turn and the obligation is on the answer. That is the
whole reason this rule had to wait for the Verify point: at the gate there is
nothing to check, because the mistake is not a call anyone made — it is a call
nobody made.

Two halves, and both are needed. The credit half is a JOIN: show_image hands
back a local path, and the check is that this exact path is in the answer — so
a picture fetched and then dropped fails. The pattern half catches the opposite
mistake, a remote image URL pasted straight into the answer, which renders as a
dead box. Neither half is a phrase the model can talk its way past.
