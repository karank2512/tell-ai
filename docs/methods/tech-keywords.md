# Method: tech keywords (`tech_mentioned_first_time`)

Spec: `docs/SPEC.md` section 7 ("Tech keywords", the light version of `tech_adoption`) and
section 8 (signal table: weight 0 in the `default` profile, half-life 90 days). Code:
`tell/signals/tech_keywords.py` (the table and the matcher) and `tell/collectors/base.py`
(`ObservedPosting`, `_tech_events`). Tests: `tests/signals/test_tech_keywords.py`,
`tests/collectors/test_base.py` (`test_tech_keywords_*`).

This page is the maintained keyword table. The code's `TECH_KEYWORDS` tuple and the table
below list the same keywords in the same order; `test_methods_page_documents_the_table_row_for_row`
fails when they drift, so a keyword is added by editing both.

## How a keyword is found

1. A collector builds an `ObservedPosting` with the provider's plain-text description
   (`descriptionPlain`, Greenhouse `content` rendered to text, and so on).
2. At construction the description is hashed into `content_hash` and scanned with
   `extract_tech_keywords`; then it is discarded. Only the hash and the keyword *names*
   survive. No description text ever reaches a table, an event payload or a log line.
3. Matching is on word boundaries. Most keywords are distinctive and match
   case-insensitively. Keywords that are also ordinary English words match only in a
   form that reads as the product:
   - the capitalised proper noun in *list context*: at the start of a line or bullet,
     or after `, ` `/` `(` `: ` or one of the words *and, or, with, use, using, in, on,
     plus, like, including*, and followed by a list separator, a closing bracket, *and*,
     *or*, `&` or the end of a sentence ("Python, Go, Rust", "we use Clay, Outreach and
     Segment"); or
   - an unambiguous product spelling (`golang`, `react.js`, `apache spark`,
     `temporal.io`, `ray serve`, `clay.com`, `outreach.io`, `segment.io`).
   This is a heuristic. "Go and see" at the start of a line still reads as Go, and
   lower-case product names in list context ("python, go") are missed.

## When an event fires

In `sync_board_postings`, the descriptions the scan has not seen before are those of new
postings and of postings whose `content_hash` changed. For each keyword in them:

- The **scope** is the board's company (`companies.id`), or `vendor:identifier` for a
  board that is not yet resolved to a company.
- If an event of type `tech_mentioned_first_time` already exists for the keyword in that
  scope, nothing happens. Otherwise one event is emitted, with the first posting of the
  poll (in provider order) that mentions the keyword as the receipt.

| Field | Value |
| --- | --- |
| `source` | the ATS vendor (`greenhouse`, `lever`, ...) |
| `type` | `tech_mentioned_first_time` |
| `external_id` | `f"{scope}:{keyword}"`, e.g. `42:kubernetes` or `greenhouse:acme:kubernetes` |
| `version` | the receipt posting's `external_id` |
| `occurred_at` | the posting's `posted_at` (else now) for a new posting; now for a re-worded one |
| `url` | the receipt posting's URL |
| `payload` | `{keyword, posting_title, posting_external_id, vendor, board_identifier, scope}` |

An unchanged rerun emits nothing whatever `events` holds, because only new or re-worded
descriptions are scanned.

Known limitations:

- The "first time" check reads `events`. Events older than 180 days move to the
  `tell-data` JSONL archive (spec section 5); a keyword whose first event was pruned
  emits again if it reappears in a new or re-worded posting.
- A board resolved to a company after its first poll keeps its board-scoped events; the
  company scope then starts fresh.
- A posting that has been listed since before this feature existed is scanned only
  when its description changes.

## The table

| Keyword | Matches | Category / notes |
| --- | --- | --- |
| `kubernetes` | `kubernetes`, `k8s` | infrastructure |
| `terraform` | `terraform` | infrastructure |
| `pulumi` | `pulumi` | infrastructure |
| `docker` | `docker` | infrastructure |
| `datadog` | `datadog` | observability |
| `grafana` | `grafana` | observability |
| `aws` | `aws`, `amazon web services` | infrastructure |
| `gcp` | `gcp`, `google cloud platform` | infrastructure |
| `azure` | `azure` | infrastructure |
| `temporal` | `Temporal` in list context; `temporal.io`, `temporal cloud` | workflow engine; "temporal" is also an adjective |
| `kafka` | `kafka` | streaming |
| `redis` | `redis` | database |
| `postgres` | `postgres`, `postgresql` | database |
| `clickhouse` | `clickhouse` | database |
| `snowflake` | `snowflake` | warehouse |
| `databricks` | `databricks` | warehouse |
| `bigquery` | `bigquery` | warehouse |
| `redshift` | `redshift` | warehouse |
| `dbt` | `dbt` | transformation |
| `airflow` | `Airflow` in list context; `apache airflow` | orchestration; "airflow" is also HVAC |
| `dagster` | `dagster` | orchestration |
| `spark` | `Spark` in list context; `apache spark`, `pyspark`, `spark sql`, `spark streaming` | compute; "spark" is also a verb |
| `fivetran` | `fivetran` | ingestion |
| `looker` | `looker` | BI |
| `tableau` | `tableau` | BI |
| `segment` | `Segment` in list context; `segment.io`, `segment.com`, `twilio segment` | CDP; "segment" is also a noun |
| `amplitude` | `Amplitude` in list context | product analytics; also a noun |
| `mixpanel` | `mixpanel` | product analytics |
| `hightouch` | `hightouch` | reverse ETL |
| `pytorch` | `pytorch` | ML framework |
| `tensorflow` | `tensorflow` | ML framework |
| `vllm` | `vllm` | LLM inference |
| `ray` | `Ray` in list context; `ray.io`, `anyscale`, `kuberay`, `ray serve|train|tune|data|core` | distributed compute; also a given name |
| `langchain` | `langchain` | LLM framework |
| `openai` | `openai` | LLM provider |
| `anthropic` | `anthropic` | LLM provider |
| `hugging face` | `hugging face`, `hugging-face`, `huggingface` | ML hub |
| `pinecone` | `pinecone` | vector database |
| `rust` | `Rust` in list context; `rustlang` | language; "rust" is also a noun |
| `go` | `Go` in list context; `golang` | language; "go" is also a verb |
| `typescript` | `typescript` | language |
| `react` | `React` in list context; `react.js`, `reactjs`, `react native` | UI library; "react" is also a verb |
| `nextjs` | `next.js`, `nextjs` | framework |
| `elixir` | `elixir` | language |
| `graphql` | `graphql` | API |
| `kotlin` | `kotlin` | language |
| `scala` | `scala` | language |
| `salesforce` | `salesforce`, `sfdc` | CRM |
| `hubspot` | `hubspot` | CRM / marketing automation |
| `clay` | `Clay` in list context; `clay.com` | GTM enrichment; also a noun |
| `marketo` | `marketo` | marketing automation |
| `outreach` | `Outreach` in list context; `outreach.io` | sales engagement; also a noun |
| `salesloft` | `salesloft` | sales engagement |
| `gong` | `gong`, `gong.io` | revenue intelligence |
| `apollo` | `apollo.io`, `apollo` | prospecting data (also matches Apollo GraphQL) |
| `zoominfo` | `zoominfo` | prospecting data |
| `clari` | `clari` | forecasting |
| `6sense` | `6sense` | intent data |
| `zendesk` | `zendesk` | support |
| `intercom` | `intercom` | support |
| `netsuite` | `netsuite` | ERP |

## Changing the table

Add or edit a row in `TECH_KEYWORDS` and in the table above, in the same position, with
a positive example in `tests/signals/test_tech_keywords.py::POSITIVE`. If the word is
also ordinary English, use the list-context form and add a negative example to
`test_common_words_do_not_match`. Profiles that care about tech adoption list the
keywords they count in `profiles/*.yaml` (`tech_keywords`); the default profile weighs
the signal 0.
