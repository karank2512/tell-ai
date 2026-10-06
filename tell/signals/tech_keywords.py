"""Tech keywords in posting descriptions (spec section 7, ``tech_adoption`` light version).

:data:`TECH_KEYWORDS` is the maintained table; ``docs/methods/tech-keywords.md``
documents it row for row (a test keeps the two in step). Each keyword has one regular
expression matched against a posting's ``descriptionPlain`` on word boundaries.

Most keywords are distinctive enough to match case-insensitively. Words that are also
ordinary English ("go", "react", "segment", "outreach", "temporal", "spark", "ray",
"clay", "rust", "airflow", "amplitude") match only in a form that reads as the
product: the capitalised proper noun, either inside a list ("Python, Go, Rust",
"Salesforce and Outreach", "experience with Clay") or with a product suffix
("golang", "React.js", "Apache Spark", "temporal.io", "Ray Serve").

:func:`extract_tech_keywords` runs inside the collector base on the description
before it is discarded (``ObservedPosting``); only the keyword names survive.
"""

from __future__ import annotations

import re

__all__ = ["TECH_KEYWORDS", "extract_tech_keywords", "keyword_names"]

_CI = re.IGNORECASE


def _word(*spellings: str) -> re.Pattern[str]:
    """Case-insensitive match of any spelling on word boundaries (``.`` allowed inside)."""
    alternatives = "|".join(re.escape(s).replace(r"\ ", r"[\s-]?") for s in spellings)
    return re.compile(rf"(?<![\w.-])(?:{alternatives})(?![\w-])", _CI)


# A capitalised proper noun counts when it sits in a list or after "with"/"using"/"in":
# at the start of a line or bullet ("- ", "\u2022 "),
# preceded by ", " / "/" / "(" / ": " or one of the words and, or, with, using, in, on,
# plus, like, including, use,
# and followed by a list separator, a closing bracket, " and ", " or " or the end of a
# sentence. Python's look-behind needs fixed widths, hence one look-behind per prefix.
_LIST_BEFORE = (
    r"(?:^|(?<=- )|(?<=\u2022 )|(?<=, )|(?<=,)|(?<=/)|(?<=\()|(?<= and )|(?<= or )|(?<= with )"
    r"|(?<= use )|(?<= using )|(?<= in )|(?<= on )|(?<= plus )|(?<= like )|(?<= including )|(?<=: ))"
)
_LIST_AFTER = r"(?=\s*[,/);:]|\s+(?:and|or|&)\s|\.(?:\s|$)|\s*$)"


def _proper(noun: str, *extra: str) -> re.Pattern[str]:
    """Case-sensitive ``noun`` in list context, or any of ``extra`` (case-insensitive)."""
    parts = [rf"{_LIST_BEFORE}{re.escape(noun)}{_LIST_AFTER}"]
    parts += [rf"(?i:(?<![\w.-]){e}(?![\w-]))" for e in extra]
    return re.compile("|".join(parts), re.MULTILINE)


TECH_KEYWORDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # infrastructure and platforms
    ("kubernetes", _word("kubernetes", "k8s")),
    ("terraform", _word("terraform")),
    ("pulumi", _word("pulumi")),
    ("docker", _word("docker")),
    ("datadog", _word("datadog")),
    ("grafana", _word("grafana")),
    ("aws", _word("aws", "amazon web services")),
    ("gcp", _word("gcp", "google cloud platform")),
    ("azure", _word("azure")),
    ("temporal", _proper("Temporal", r"temporal\.io", r"temporal cloud")),
    ("kafka", _word("kafka")),
    ("redis", _word("redis")),
    ("postgres", _word("postgres", "postgresql")),
    ("clickhouse", _word("clickhouse")),
    # data stack
    ("snowflake", _word("snowflake")),
    ("databricks", _word("databricks")),
    ("bigquery", _word("bigquery")),
    ("redshift", _word("redshift")),
    ("dbt", _word("dbt")),
    ("airflow", _proper("Airflow", r"apache airflow")),
    ("dagster", _word("dagster")),
    ("spark", _proper("Spark", r"apache spark", r"pyspark", r"spark sql", r"spark streaming")),
    ("fivetran", _word("fivetran")),
    ("looker", _word("looker")),
    ("tableau", _word("tableau")),
    ("segment", _proper("Segment", r"segment\.(?:io|com)", r"twilio segment")),
    ("amplitude", _proper("Amplitude")),
    ("mixpanel", _word("mixpanel")),
    ("hightouch", _word("hightouch")),
    # ML and AI
    ("pytorch", _word("pytorch")),
    ("tensorflow", _word("tensorflow")),
    ("vllm", _word("vllm")),
    (
        "ray",
        _proper("Ray", r"ray\.io", r"anyscale", r"kuberay", r"ray (?:serve|train|tune|data|core)"),
    ),
    ("langchain", _word("langchain")),
    ("openai", _word("openai")),
    ("anthropic", _word("anthropic")),
    ("hugging face", _word("hugging face", "huggingface")),
    ("pinecone", _word("pinecone")),
    # languages and frameworks
    ("rust", _proper("Rust", r"rustlang")),
    ("go", _proper("Go", r"golang")),
    ("typescript", _word("typescript")),
    ("react", _proper("React", r"react\.js", r"reactjs", r"react native")),
    ("nextjs", _word("next.js", "nextjs")),
    ("elixir", _word("elixir")),
    ("graphql", _word("graphql")),
    ("kotlin", _word("kotlin")),
    ("scala", _word("scala")),
    # go-to-market tools
    ("salesforce", _word("salesforce", "sfdc")),
    ("hubspot", _word("hubspot")),
    ("clay", _proper("Clay", r"clay\.com")),
    ("marketo", _word("marketo")),
    ("outreach", _proper("Outreach", r"outreach\.io")),
    ("salesloft", _word("salesloft")),
    ("gong", _word("gong", "gong.io")),
    ("apollo", _word("apollo.io", "apollo")),
    ("zoominfo", _word("zoominfo")),
    ("clari", _word("clari")),
    ("6sense", _word("6sense")),
    ("zendesk", _word("zendesk")),
    ("intercom", _word("intercom")),
    ("netsuite", _word("netsuite")),
)
"""``(keyword, pattern)`` in table order; the keyword is the name events carry."""


def keyword_names() -> tuple[str, ...]:
    """The keyword names in table order."""
    return tuple(name for name, _ in TECH_KEYWORDS)


def extract_tech_keywords(text: str | None) -> tuple[str, ...]:
    """The table keywords mentioned in ``text``, each once, in table order."""
    if not text:
        return ()
    return tuple(name for name, pattern in TECH_KEYWORDS if pattern.search(text))
