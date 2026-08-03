---
name: say-when-the-link-failed
description: If a page you were given comes back empty, say so — do not answer around it.
when:
  result:
    of: read_url
    was: empty
then:
  answer_must_include:
    like:
      - I could not read that page, so this is not based on it
      - the link came back empty and I have not been able to open it
      - nie udało mi się otworzyć tego linku
---

A page that came back empty is a fact I need, and the failure mode is that it
gets quietly worked around: the answer arrives sounding as confident as one
built on something real.

This is the disclosure half of the bounded-material rule, and it lives here
rather than there because it is conditional. Requiring every answer about a
link to say "I could not read it" would be nonsense — it is only required once
the read has actually failed.
