# issuer_sites fixtures

Company-site pages for issuer-first discovery (`tell discover issuers`, ADR 0035), hand-made
in the shape of the real pages the 2026-10-14 design pass read (homepage with a footer of
terms/privacy links, a terms page naming the operating entity). The company is fictional:
"Quillfeather Robotics, Inc.", a Delaware corporation in Austin, TX. No person, e-mail
address, phone number or street address appears.

- `normal/`: the site states the legal name on its terms page (tier A).
- `empty_board/`: the guessed domain belongs to someone else (no brand at all).
- `drift/`: the same site after a redesign that moved the legal entity line into a script
  and the page title; the contract test fails loudly (title-only is never evidence).

The real hand-checked excerpts live in `tests/resolve/issuer_evidence_20261014.json`.
