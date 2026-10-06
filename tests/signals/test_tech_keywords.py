"""The tech keyword table: matching rules and the methods page that documents it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tell.signals.tech_keywords import TECH_KEYWORDS, extract_tech_keywords, keyword_names

METHODS_PAGE = Path(__file__).resolve().parents[2] / "docs" / "methods" / "tech-keywords.md"

SPEC_KEYWORDS = [
    "kubernetes",
    "snowflake",
    "salesforce",
    "hubspot",
    "clay",
    "dbt",
    "rust",
    "pytorch",
    "vllm",
    "temporal",
    "databricks",
    "bigquery",
    "redshift",
    "airflow",
    "dagster",
    "terraform",
    "datadog",
    "segment",
    "marketo",
    "outreach",
    "gong",
    "apollo",
    "zoominfo",
    "clari",
    "langchain",
    "openai",
    "anthropic",
    "ray",
    "kafka",
    "spark",
    "react",
    "nextjs",
    "typescript",
    "go",
    "elixir",
]

POSITIVE: dict[str, str] = {
    "kubernetes": "Deploys on k8s.",
    "terraform": "Infrastructure as code with Terraform.",
    "pulumi": "Pulumi stacks.",
    "docker": "Docker images.",
    "datadog": "Observability in Datadog.",
    "grafana": "Grafana dashboards.",
    "aws": "Runs on AWS.",
    "gcp": "Google Cloud Platform credits.",
    "azure": "Azure tenants.",
    "temporal": "Workflows on temporal.io",
    "kafka": "Event streams in Kafka.",
    "redis": "Redis caches.",
    "postgres": "PostgreSQL 16.",
    "clickhouse": "ClickHouse analytics.",
    "snowflake": "Snowflake warehouse.",
    "databricks": "Databricks notebooks.",
    "bigquery": "BigQuery datasets.",
    "redshift": "Amazon Redshift.",
    "dbt": "Models in dbt.",
    "airflow": "Apache Airflow DAGs.",
    "dagster": "Dagster assets.",
    "spark": "PySpark jobs.",
    "fivetran": "Fivetran connectors.",
    "looker": "Looker explores.",
    "tableau": "Tableau reports.",
    "segment": "Events through Segment.io today.",
    "amplitude": "Product analytics with Amplitude.",
    "mixpanel": "Mixpanel funnels.",
    "hightouch": "Reverse ETL with Hightouch.",
    "pytorch": "PyTorch training.",
    "tensorflow": "TensorFlow serving.",
    "vllm": "Inference on vLLM.",
    "ray": "Distributed training with Ray Train.",
    "langchain": "LangChain agents.",
    "openai": "OpenAI APIs.",
    "anthropic": "Anthropic models.",
    "hugging face": "Models from Hugging Face.",
    "pinecone": "Pinecone indexes.",
    "rust": "Services in Rust.",
    "go": "Backend in Golang.",
    "typescript": "TypeScript everywhere.",
    "react": "UI in React.js.",
    "nextjs": "Next.js frontend.",
    "elixir": "Elixir services.",
    "graphql": "GraphQL gateway.",
    "kotlin": "Kotlin on Android.",
    "scala": "Scala pipelines.",
    "salesforce": "CRM is SFDC.",
    "hubspot": "HubSpot workflows.",
    "clay": "Enrichment in clay.com tables.",
    "marketo": "Marketo programs.",
    "outreach": "Sequences in Outreach.io.",
    "salesloft": "Salesloft cadences.",
    "gong": "Calls recorded in Gong.",
    "apollo": "Prospects from Apollo.io.",
    "zoominfo": "ZoomInfo data.",
    "clari": "Forecasts in Clari.",
    "6sense": "Intent from 6sense.",
    "zendesk": "Tickets in Zendesk.",
    "intercom": "Chat on Intercom.",
    "netsuite": "Books in NetSuite.",
}


def test_table_has_at_least_forty_keywords_and_every_spec_example() -> None:
    names = keyword_names()
    assert len(names) >= 40
    assert len(set(names)) == len(names)
    assert set(SPEC_KEYWORDS) <= set(names)
    assert set(POSITIVE) == set(names)


@pytest.mark.parametrize(("keyword", "text"), sorted(POSITIVE.items()))
def test_every_keyword_matches_its_example(keyword: str, text: str) -> None:
    assert keyword in extract_tech_keywords(text)


@pytest.mark.parametrize(
    "text",
    [
        "Go above and beyond to help customers go-to-market.",
        "React quickly to change.",
        "Market segment leaders and community outreach programs.",
        "Spark joy; temporal data; airflow in HVAC systems.",
        "Shape clay into pots.",
        "Rust-proof coating for the Rust Belt factory floor.",
        "The amplitude of the signal.",
        "Mr. Ray is great.",
        "A file.dbt-like name, kafkaesque prose, snowflakes.",
    ],
)
def test_common_words_do_not_match(text: str) -> None:
    assert extract_tech_keywords(text) == ()


def test_list_context_and_order() -> None:
    text = "Stack: Python, Go, Rust and React; we use Clay, Outreach and Segment.\n- Temporal"
    assert extract_tech_keywords(text) == (
        "temporal",
        "segment",
        "rust",
        "go",
        "react",
        "clay",
        "outreach",
    )
    assert extract_tech_keywords(None) == ()
    assert extract_tech_keywords("") == ()
    assert extract_tech_keywords("kubernetes Kubernetes K8S") == ("kubernetes",)


def test_methods_page_documents_the_table_row_for_row() -> None:
    text = METHODS_PAGE.read_text(encoding="utf-8")
    table = text.split("## The table", 1)[1].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `([^`]+)` \|", table, re.MULTILINE)
    assert tuple(rows) == keyword_names()
    assert len(TECH_KEYWORDS) == len(rows)
