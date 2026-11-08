"""The client against a stand-in for the API. test_live.py runs it against the real one."""
import json

import httpx
import pytest
from pageindex_search import PageSearch

from pageindex_extract import ExtractJob, PageExtract, PageExtractError

SCHEMA = {"type": "object", "properties": {"total": {"type": "number"}}}


def job(status="PENDING", **extra):
    return {"id": "ext_1", "status": status, "document": "doc_1", "configuration": {"data_schema": SCHEMA},
            "created_at": "2026-11-08T00:00:00+00:00", "updated_at": "2026-11-08T00:00:01+00:00", **extra}


def client(handler, search_handler=None) -> PageExtract:
    search = PageSearch(api_key="pis_live_x", max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(search_handler or handler)))
    return PageExtract(api_key="pis_live_x", max_retries=2, search=search,
                       http_client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


def test_extract_by_id_waits_for_the_result():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            body = json.loads(request.content)
            assert body == {"document": "doc_1", "configuration": {
                "data_schema": SCHEMA, "tier": "pro", "target_pages": "3-9", "cite_sources": True}}
            assert request.headers["authorization"] == "Bearer pis_live_x"
            return httpx.Response(200, json=job())
        if len(seen) < 4:
            return httpx.Response(200, json=job("RUNNING"))
        return httpx.Response(200, json=job("COMPLETED", extract_result={"total": 12.5},
                                            extract_metadata={"field_metadata": {"/rows/0": {"page": 3}}},
                                            usage={"pages": 7, "credits": 0.35}))

    out = client(handler).extract("doc_1", SCHEMA, tier="pro", pages=(3, 9), cite_sources=True)
    assert out.status == "COMPLETED" and out.result == {"total": 12.5}
    assert out.sources == {"/rows/0": {"page": 3}} and out.usage.pages == 7 and out.usage.credits == 0.35
    assert seen[0] == ("POST", "/v1/extract") and seen[-1] == ("GET", "/v1/extract/ext_1")


def test_failed_job_raises_with_the_job():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json=job())
        return httpx.Response(200, json=job("FAILED", error_message="The document could not be finished."))

    with pytest.raises(PageExtractError) as error:
        client(handler).extract("doc_1", SCHEMA)
    assert error.value.code == "job_failed" and error.value.job.status == "FAILED"
    assert error.value.message == "The document could not be finished."


def test_no_wait_returns_at_once():
    out = client(lambda request: httpx.Response(200, json=job())).extract("doc_1", SCHEMA, wait=False)
    assert out.status == "PENDING" and not out.done


def test_api_error_raises():
    body = {"error": {"code": "insufficient_credit", "message": "Extracting 9 pages takes $0.45 of credit."}}
    with pytest.raises(PageExtractError) as error:
        client(lambda request: httpx.Response(402, json=body)).jobs.create("doc_1", SCHEMA)
    assert (error.value.status, error.value.code) == (402, "insufficient_credit")


def test_create_is_not_retried_after_it_may_have_started():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, json={"error": {"code": "error", "message": "boom"}})

    with pytest.raises(PageExtractError):
        client(handler).jobs.create("doc_1", SCHEMA)
    assert len(calls) == 1  # a 500 may have started the job: retrying could run it twice


def test_create_is_retried_when_turned_away():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, json=job())

    assert client(handler).jobs.create("doc_1", SCHEMA).id == "ext_1" and len(calls) == 2


def test_get_is_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500) if len(calls) == 1 else httpx.Response(200, json=job("RUNNING"))

    assert client(handler).jobs.get("ext_1").status == "RUNNING" and len(calls) == 2


def test_file_is_uploaded_then_extracted(tmp_path):
    pdf = tmp_path / "invoice.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    def search(request):
        assert request.method == "POST" and request.url.path == "/v1/documents"
        return httpx.Response(200, json={"id": "doc_7", "status": "ready", "filename": "invoice.pdf", "pages": 2})

    def extract(request):
        assert json.loads(request.content)["document"] == "doc_7"
        return httpx.Response(200, json=job())

    assert client(extract, search).extract(str(pdf), SCHEMA, wait=False).id == "ext_1"


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        client(lambda request: httpx.Response(200, json=job())).extract(str(tmp_path / "nope.pdf"), SCHEMA)


def test_document_not_ready_waits_then_retries():
    calls = []

    def extract(request):
        calls.append(request.method)
        if request.method == "POST" and calls.count("POST") == 1:
            return httpx.Response(409, json={"error": {"code": "document_not_ready", "message": "processing"}})
        return httpx.Response(200, json=job())

    def search(request):
        return httpx.Response(200, json={"id": "doc_1", "status": "ready"})

    assert client(extract, search).extract("doc_1", SCHEMA, wait=False).id == "ext_1" and calls.count("POST") == 2


def test_pydantic_schema_and_parse():
    pydantic = pytest.importorskip("pydantic")

    class Invoice(pydantic.BaseModel):
        total: float

    def handler(request):
        if request.method == "POST":
            assert json.loads(request.content)["configuration"]["data_schema"]["properties"]["total"]["type"] == "number"
            return httpx.Response(200, json=job())
        return httpx.Response(200, json=job("COMPLETED", extract_result={"total": 3}))

    assert client(handler).extract("doc_1", Invoice).parse(Invoice) == Invoice(total=3)


def test_list_pages_through_every_job():
    def handler(request):
        after = request.url.params.get("after")
        if after is None:
            return httpx.Response(200, json={"jobs": [job(id="ext_1"), job(id="ext_2")], "next": "ext_2"})
        return httpx.Response(200, json={"jobs": [job(id="ext_3")], "next": None})

    page = client(handler).jobs.list(limit=2)
    assert page.has_more and [j.id for j in page] == ["ext_1", "ext_2", "ext_3"]


def test_cancel_and_delete():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.method == "DELETE":
            return httpx.Response(200, json={"id": "ext_1", "deleted": True})
        return httpx.Response(200, json=job("CANCELLED"))

    c = client(handler)
    assert c.jobs.cancel("ext_1").status == "CANCELLED"
    c.jobs.delete(ExtractJob._from(job()))
    assert seen == [("POST", "/v1/extract/ext_1/cancel"), ("DELETE", "/v1/extract/ext_1")]


def test_no_key_raises(monkeypatch):
    monkeypatch.delenv("PAGEINDEX_SEARCH_API_KEY", raising=False)
    with pytest.raises(PageExtractError):
        PageExtract()
