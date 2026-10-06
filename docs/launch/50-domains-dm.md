# The "50 domains" message (SPEC 15.1)

Status: draft. Sending is Karan's; the agent never contacts anyone. Nothing ships with a `{{ placeholder }}` still in it (see [README.md](README.md)). Under 80 words.

Channel: LinkedIn DM or email, cold or warm. Send to people Karan picks from his own conversations.

## Cold

> Hi {{ recipient.first_name }}, I'm Karan, a CS grad out of UW–Madison. I built tell: it watches public job boards and SEC filings and turns them into dated, source-linked buying signals.
>
> I'd like to run it on your accounts. Send me 50 domains and I'll send back a scored CSV by Friday, every row linked to the posting or filing behind it. No login, nothing to install.
>
> If it's useful, tell me what's wrong with it. If it isn't, that's useful too.

## Warm variant

Replace the first line (the "Hi ..., I'm Karan ..." sentence pair) with one sentence about the one thing that person told Karan in their own earlier conversation: `{{ warm.one_thing }}`. Never use a detail that came from someone else's conversation. Then the same ask, unchanged:

> I'd like to run it on your accounts. Send me 50 domains and I'll send back a scored CSV by Friday, every row linked to the posting or filing behind it. No login, nothing to install.
>
> If it's useful, tell me what's wrong with it. If it isn't, that's useful too.

## Follow-up

Once, after 5 days, one line:

> Still happy to run this on 50 domains if you send them over.

No third message.

## Before sending

- Replace `{{ recipient.first_name }}` (and `{{ warm.one_thing }}` for the warm variant) by hand; do not store either in this repo.
- Keep "by Friday" true: the CSV comes from `tell publish` (see the CSV export in `docs/methods/publishing.md`) for the recipient's watchlist; the [delivery message](delivery-message.md) goes with it.
