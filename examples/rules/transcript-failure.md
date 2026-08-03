---
name: transcript-failure
description: If the transcript comes back empty, tell me — don't quietly use something else.
when:
  result:
    of: youtube_analyze
    was: empty
then:
  never_use: [web_search]
  answer_must_include:
    like:
      - the transcript came back empty so I could not read the video
      - I could not get the transcript, so I have not watched this
      - nie udało się pobrać transkrypcji tego filmu
---

This is the rule the whole rules engine was built for, and it is worth knowing
why it could not be written for so long.

A video was analysed, the transcript came back empty with its error channel
populated, and the exit code was zero — so nothing anywhere said it had failed.
Six web searches later, a long, confident answer was delivered describing the
video's contents, sourced entirely from news articles about it. Nothing in the
answer said the video had never been read.

The condition is not about the question. It is about what a tool came back
with, and the person asking has no way to know that in advance. Written as a
memory it was never recalled on the turn it mattered: a pasted link has nothing
in it to match on.

While the transcript is fine, this rule restricts nothing at all. Once it comes
back empty, say so plainly and ask whether to look elsewhere — a disclosed dead
end is a good answer, and someone else's material presented as the video's is
not.
