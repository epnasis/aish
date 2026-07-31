---
name: youtube-url-analysis
description: A message that is nothing but a YouTube URL means analyse THAT video — not search the web about it.
tier: 0
fail: open
trigger: message_shape
match: ^\s*<?https?://(www\.)?(youtu\.be|youtube\.com|m\.youtube\.com)/\S+\s*$
route: youtube_analyze
prohibit: web_search, read_url
unless: disclosed
disclose: transcript_unavailable
disclosure_terms: transcript, youtube_analyze
---

The user pasted a link and nothing else. That means: tell me what is IN this
video. An article about the video, a news story on the same topic, or your own
recollection of it is a different deliverable, and presenting one as the video's
content is the failure this rule exists to stop.

If youtube_analyze comes back empty or fails, say so plainly first. Disclosing a
dead end is a good answer; quietly sourcing the answer from somewhere else is not.
