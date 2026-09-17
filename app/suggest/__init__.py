"""
The assistant proposing a service the firm sells.

Two parts, kept apart so each can be tested alone:

  * `catalog` — what is for sale, read from the product backend's public
    list and cached. The only place this service knows that backend exists.
  * `classify` — given a client's message and that list, which service (if
    any) they actually need from a lawyer. A small model call, strict JSON,
    and rules around it so it helps instead of nagging.

`propose()` in `engine` ties them together for a turn.
"""
