"""Against the real API, when PAGEINDEX_SEARCH_API_KEY and PAGE_EXTRACT_LIVE_PDF are set: one page, standard tier
(about $0.03). The PDF is uploaded (or found, if uploaded before) and left in place."""
import os

import pytest

from pageindex_extract import PageExtract, PageExtractError

PDF = os.environ.get("PAGE_EXTRACT_LIVE_PDF")
pytestmark = pytest.mark.skipif(not (PDF and os.environ.get("PAGEINDEX_SEARCH_API_KEY")),
                                reason="set PAGEINDEX_SEARCH_API_KEY and PAGE_EXTRACT_LIVE_PDF")

SCHEMA = {"type": "object", "properties": {
    "title": {"type": ["string", "null"], "description": "The document's title, as printed on its first page"},
    "page_count_printed": {"type": ["integer", "null"], "description": "Total pages, if the page says (Page 1 of N)"},
}}


def test_extract_one_page():
    with PageExtract() as client:
        job = client.extract(PDF, SCHEMA, pages=1)
        assert job.status == "COMPLETED" and set(job.result) == {"title", "page_count_printed"}
        assert job.usage.pages == 1
        assert client.jobs.get(job.id).result == job.result
        assert any(j.id == job.id for j in client.jobs.list(limit=10))


def test_bad_schema_is_refused():
    with PageExtract() as client, pytest.raises(PageExtractError) as error:
        client.jobs.create("doc_does_not_matter", {"type": "string"})
    assert error.value.status == 400 and error.value.code == "invalid_schema"
