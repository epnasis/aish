---
name: always-use-show-image
description: Any picture in an answer comes from show_image, never a raw markdown link.
when: always
then:
  answer_must_not_include:
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

This one is a pattern rather than a named thing, because it is about what must
NOT be in the text: a remote image URL pasted straight into the answer, which
renders as a dead box. The other half — "a picture actually got shown" — needs
no rule at all. It is what the word "picture" means anywhere it is used: aish
fetched it, and the reader can see it.
