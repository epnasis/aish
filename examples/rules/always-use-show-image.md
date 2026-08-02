---
name: always-use-show-image
description: Any picture in an answer comes from show_image, never a raw markdown link.
when: always
then:
  answer_must_not: raw_image_links
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

`raw_image_links` is a join, not a phrase match: show_image hands back a LOCAL
path, so an http(s) image link in the answer demonstrably did not come from it.
The model does not author that fact and cannot talk its way past it.
