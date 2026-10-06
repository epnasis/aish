---
name: shop-search
description: Use when the user wants to find, buy, compare or price a product in an online shop — searching the shop itself for today's offers instead of a search engine's index.
keywords: shop, store, product, buy, price, offer, listing, search, compare, allegro
---
To search a SHOP, call read_url on the shop's own search URL — e.g. `read_url("https://allegro.pl/listing?string=zawiesie+wezowe+czarne")` — rather than web_search'ing for the shop: the search engine returns its index, while the shop's own listing returns today's offers and prices.

You MUST NOT use web_search with a `site:` filter to browse a shop (no `site:allegro.pl …`): read the shop's own listing URL instead.
