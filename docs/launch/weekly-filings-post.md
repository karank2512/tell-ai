# Weekly Form D filings post (LinkedIn and X)

Status: draft, posted weekly after the Monday edition is published. Posting is Karan's. The one placeholder, `{{ digest.rows }}`, is filled by hand at posting time (see [numbers.yaml](numbers.yaml), `inputs`), and nothing ships while it is still in the text. The page is `form-d-filings.html` on the public site ([ADR 0044](../decisions/0044-public-site-form-d-digest-and-opt-out.md)).

## Rules

- **Names stay on the page.** A post never names a company and never links one company's page. Readers find the companies on the page.
- **No "not announced" or "stealth" wording anywhere public:** not in a post, a comment, a reply or an image. The page lists filings and says nothing about press. Keep it that way.
- **No mention of YC, batches or the directory.**
- **Two kinds of numbers only.** The first is the digest's row count for the week (`{{ digest.rows }}`): count the rows of the table on the page at posting time, and skip the post if there are none. The second is the one quotable press-share sentence in [README.md](README.md): "of 331 Form D filings tracked, 44 (13.3%) had verified press within 60 days". Add no amounts, totals, other percentages or lead-time figures ([ADR 0041](../decisions/0041-no-lead-time-claims.md)).
- **Where the links go.** On X, the page link goes in the first comment: post 2 replies to post 1, and post 3 replies to post 2. On LinkedIn it goes in the body, and the repo link goes in the comments.
- **No hashtags, no @-mentions of companies, no emoji.**
- **Do not repeat the opt-out line in the post.** The page already carries it.
- **Length.** Each X post stays under 280 characters. Measured with `len()` on the text below, placeholder included: post 1 is 278, post 2 is 123, post 3 is 209. A filled count of up to four digits is shorter than the 17-character placeholder (post 1 with `9999`: 265).

## LinkedIn

```text
Startups file a Form D with the SEC when they raise. Most of those rounds never make the news: of 331 filings I tracked, only 44 (13.3%) had press within 60 days.

So every week I publish the filings themselves. This week: {{ digest.rows }} filings by companies I track, each with its date, offering size and a link to the filing on sec.gov.

karankapur.com/tell-data/form-d-filings.html

Public records only, nothing about people, refreshed every night. The code is open source (link in the comments). If you sell to startups, send me 50 domains and I'll send back a scored list with receipts.
```

First comment: `github.com/karank2512/tell-ai`

## X, three posts

Post 1:

```text
Startups file a Form D with the SEC when they raise. Most of those rounds never make the news: of 331 filings I tracked, only 44 (13.3%) had press within 60 days.

So every week I publish the filings themselves: date, size, and a link to the filing. {{ digest.rows }} this week.
```

Post 2, a reply to post 1:

```text
The digest, refreshed every night: karankapur.com/tell-data/form-d-filings.html

Public records only, nothing about people.
```

Post 3, a reply to post 2:

```text
It's open source: github.com/karank2512/tell-ai

If you sell to startups, send me 50 domains and I'll send back a scored list, every row linked to the posting or filing behind it. No login, nothing to install.
```

## Checks before posting

- [ ] `{{ digest.rows }}` is replaced by the row count read off the page today, and no `{{` is left.
- [ ] The page opens and its table has that many rows.
- [ ] No company name, no "not announced", no "stealth", no YC, batch or directory, no hashtag, no @-mention, no emoji.
- [ ] Each X post is still under 280 characters.
