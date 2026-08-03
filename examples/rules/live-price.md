---
name: live-price
description: If you quote me a price, you must have read the seller's own page this turn.
when:
  answer:
    matches: '[0-9][0-9., ]*\s?(zł|PLN|EUR|USD|GBP)|[€$£]\s?[0-9]'
then:
  must_first: read_url
---

Prices and availability change constantly. A figure remembered from training
data, or lifted from a search-result snippet, is a guess wearing the costume of
a fact — and it is the kind of guess someone acts on.

So the condition is on the answer, not on the question. "How much is a Switch"
is obviously about a price; "what should I get my nephew" is not, and can still
end in one. By the time an answer quotes a figure, the harness can simply look
at whether anything was actually fetched this turn.

If nothing was, aish asks for the fetch rather than asking you. The question
provokes the work, the work lands in the record, and the record is what gets
checked — saying "I did check" changes nothing. If the fetch never arrives, the
answer is still delivered, carrying a line saying the figure was not verified.
