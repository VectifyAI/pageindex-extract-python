"""The Page Extract API client: turn a PDF into JSON that fits your schema."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Union

import httpx
from pageindex_search import Document, PageSearch

from ._version import __version__

DEFAULT_BASE_URL = "https://api.extract.pageindex.ai/v1"
API_KEY_ENV = "PAGEINDEX_SEARCH_API_KEY"  # one key for Page Search and Page Extract
# Retried, after a short wait that doubles: the server was busy or briefly unreachable.
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Starting an extraction is retried only when the request surely did not start one: it was turned away before it
# reached the service, so a retry cannot run (or charge) the same extraction twice.
CREATE_RETRY_STATUSES = {429, 502, 503}
FINISHED = ("COMPLETED", "FAILED", "CANCELLED")

FileInput = Union[str, os.PathLike, bytes, BinaryIO]
DocumentInput = Union[FileInput, Document]


class PageExtractError(Exception):
    """An error the API returned, or an extraction that failed or was cancelled (then job holds it)."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None,
                 request_id: str | None = None, job: "ExtractJob | None" = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.request_id = request_id
        self.job = job

    def __str__(self) -> str:
        parts = [f"{self.code}: {self.message}" if self.code else self.message]
        if self.status:
            parts.append(f"(HTTP {self.status})")
        if self.request_id:
            parts.append(f"[{self.request_id}]")
        return " ".join(parts)


def _make(cls, data: dict[str, Any]):
    """A dataclass from a response, ignoring fields this version does not know."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Usage:
    pages: int  # pages extracted
    credits: float  # US dollars charged


@dataclass
class ExtractJob:
    id: str
    status: str  # "PENDING", "RUNNING", "COMPLETED", "FAILED" or "CANCELLED"
    document: str  # the document's id
    configuration: dict[str, Any] = field(default_factory=dict)  # data_schema, tier, target_pages, ...
    created_at: str | None = None
    updated_at: str | None = None
    result: Any = None  # the JSON that fits the schema, once COMPLETED
    # With cite_sources: the page each list item is on, by its JSON Pointer ("/rows/0": {"page": 3})
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)
    usage: Usage | None = None  # once COMPLETED: what was charged
    error: str | None = None  # why it FAILED

    @property
    def done(self) -> bool:
        return self.status in FINISHED

    def parse(self, model: Any) -> Any:
        """The result as an instance of a Pydantic model (model.model_validate(result))."""
        if self.status != "COMPLETED":
            raise PageExtractError(f"Extract job {self.id} is {self.status}, not COMPLETED.", job=self)
        return model.model_validate(self.result)

    @classmethod
    def _from(cls, data: dict[str, Any]) -> "ExtractJob":
        out = _make(cls, data)
        out.result = data.get("extract_result")
        out.sources = (data.get("extract_metadata") or {}).get("field_metadata") or {}
        out.usage = _make(Usage, data["usage"]) if data.get("usage") else None
        out.error = data.get("error_message")
        return out


class JobPage:
    """One page of your extractions, newest first: data and next (the id to pass as after for the page after it).
    Iterating it goes on through the pages after it, so a for loop over client.jobs.list() sees every extraction.
    Listed jobs come without their results: get(id) for one's result."""

    def __init__(self, jobs: "Jobs", data: list[ExtractJob], next: str | None, limit: int):
        self._jobs, self.data, self.next, self._limit = jobs, data, next, limit

    @property
    def has_more(self) -> bool:
        return self.next is not None

    def next_page(self) -> JobPage | None:
        return self._jobs.list(limit=self._limit, after=self.next) if self.next else None

    def __iter__(self) -> Iterator[ExtractJob]:
        page: JobPage | None = self
        while page is not None:
            yield from page.data
            page = page.next_page()


def schema_of(schema: Any) -> dict[str, Any]:
    """A JSON Schema from a dict, or from a Pydantic model (its model_json_schema())."""
    if isinstance(schema, dict):
        return schema
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    raise TypeError("schema is a JSON Schema (a dict) or a Pydantic model")


def _pages(pages: str | int | tuple[int, int] | None) -> str | None:
    """target_pages as the API takes it: "3", or "3-9"."""
    if pages is None or isinstance(pages, str):
        return pages
    if isinstance(pages, int):
        return str(pages)
    first, last = pages
    return f"{first}-{last}"


class Jobs:
    """Your extractions: client.jobs.create(document, schema), .get(id), .list(), .wait(job), .cancel(job),
    .delete(job)."""

    def __init__(self, client: "PageExtract"):
        self._client = client

    def create(self, document: Document | str, schema: Any, *, tier: str = "standard",
               pages: str | int | tuple[int, int] | None = None, cite_sources: bool = False,
               system_prompt: str | None = None) -> ExtractJob:
        """Start extracting from an uploaded document (or its id); returns at once, PENDING. client.extract() also
        uploads a file and waits for the result."""
        configuration: dict[str, Any] = {"data_schema": schema_of(schema), "tier": tier}
        if pages is not None:
            configuration["target_pages"] = _pages(pages)
        if cite_sources:
            configuration["cite_sources"] = True
        if system_prompt:
            configuration["system_prompt"] = system_prompt
        body = {"document": _id(document), "configuration": configuration}
        return ExtractJob._from(self._client._request("POST", "/extract", json=body, retry=CREATE_RETRY_STATUSES))

    def get(self, job: ExtractJob | str) -> ExtractJob:
        return ExtractJob._from(self._client._request("GET", f"/extract/{_id(job)}"))

    def list(self, *, limit: int = 20, after: str | None = None) -> JobPage:
        """Your extractions, newest first: limit (1 to 100) of them, after the job whose id is after.

            for job in client.jobs.list():    # every extraction, a page at a time
        """
        params: dict[str, Any] = {"limit": limit}
        if after:
            params["after"] = after
        page = self._client._request("GET", "/extract", params=params)
        return JobPage(self, [ExtractJob._from(j) for j in page["jobs"]], page.get("next"), limit)

    def wait(self, job: ExtractJob | str, *, timeout: float = 3600.0, interval: float = 3.0) -> ExtractJob:
        """The job once COMPLETED. Raises PageExtractError if it failed, was cancelled, or is not done within
        timeout (it goes on running then: wait again, or cancel it)."""
        current = job if isinstance(job, ExtractJob) else self.get(job)
        deadline = time.monotonic() + timeout
        while not current.done:
            if time.monotonic() > deadline:
                raise PageExtractError(f"Extract job {current.id} is still {current.status} after {timeout:.0f} s.",
                                       code="timeout", job=current)
            time.sleep(interval)
            current = self.get(current.id)
        if current.status != "COMPLETED":
            message = current.error or f"Extract job {current.id} was {current.status.lower()}."
            raise PageExtractError(message, code=f"job_{current.status.lower()}", job=current)
        return current

    def cancel(self, job: ExtractJob | str) -> ExtractJob:
        """Stop a job that is PENDING or RUNNING; nothing is charged."""
        return ExtractJob._from(self._client._request("POST", f"/extract/{_id(job)}/cancel"))

    def delete(self, job: ExtractJob | str) -> None:
        """Delete a job and its result."""
        self._client._request("DELETE", f"/extract/{_id(job)}")


class PageExtract:
    """A client for the Page Extract API.

        client = PageExtract()  # reads PAGEINDEX_SEARCH_API_KEY: the same key as Page Search
        job = client.extract("invoice.pdf", schema)  # a JSON Schema, or a Pydantic model
        print(job.result)

    Documents are Page Search's: client.documents is a Page Search client's, with the same key.
    """

    def __init__(self, api_key: str | None = None, *, base_url: str | None = None, timeout: float = 120.0,
                 max_retries: int = 2, http_client: httpx.Client | None = None, search: PageSearch | None = None):
        api_key = api_key or os.environ.get(API_KEY_ENV)
        if not api_key:
            raise PageExtractError(f"No API key: pass api_key or set {API_KEY_ENV}. "
                                   "Create one at https://search.pageindex.ai/console/keys.")
        self.base_url = (base_url or os.environ.get("PAGEINDEX_EXTRACT_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.max_retries = max_retries
        self._http = http_client or httpx.Client(timeout=timeout)
        self._headers = {"Authorization": f"Bearer {api_key}", "User-Agent": f"pageindex-extract-python/{__version__}"}
        self.search = search or PageSearch(api_key, timeout=timeout, max_retries=max_retries)
        self.documents = self.search.documents
        self.jobs = Jobs(self)

    def extract(self, document: DocumentInput, schema: Any, *, tier: str = "standard",
                pages: str | int | tuple[int, int] | None = None, cite_sources: bool = False,
                system_prompt: str | None = None, wait: bool = True, timeout: float = 3600.0) -> ExtractJob:
        """Extract JSON that fits schema (a JSON Schema, or a Pydantic model) from a PDF.

        document is a file (a path, bytes or an open binary file), uploaded first (a file uploaded before is not
        uploaded again), or an uploaded document or its id ("doc_..."). tier is "standard" or "pro"; pages keeps
        the extraction to some pages ("3-9", (3, 9) or 3), and only they are charged; cite_sources gives the page
        each list item is on (job.sources); system_prompt adds your own instructions.

        With wait (the default), returns the job once COMPLETED (job.result), or raises PageExtractError if it
        failed; without it, returns the job at once, PENDING: client.jobs.wait(job) for its result.
        """
        doc = self._document(document)
        for _ in range(60):  # a document just uploaded elsewhere may still be processing
            try:
                job = self.jobs.create(doc, schema, tier=tier, pages=pages, cite_sources=cite_sources,
                                       system_prompt=system_prompt)
                break
            except PageExtractError as error:
                if error.code != "document_not_ready":
                    raise
                doc = self.documents.wait(_id(doc))
        else:
            raise PageExtractError(f"Document {_id(doc)} is not ready.", code="document_not_ready")
        return self.jobs.wait(job, timeout=timeout) if wait else job

    def _document(self, document: DocumentInput) -> Document | str:
        """An uploaded document, or an id: a file is uploaded (and waited for)."""
        if isinstance(document, Document):
            return document
        if isinstance(document, str) and not os.path.exists(document) and not document.lower().endswith(".pdf"):
            return document  # an id
        if isinstance(document, (str, os.PathLike)) and not Path(document).exists():
            raise FileNotFoundError(str(document))
        return self.documents.upload(document)

    # Plumbing

    def _request(self, method: str, path: str, *, retry: set[int] = RETRY_STATUSES, **kwargs: Any) -> Any:
        """The response's JSON. An error raises PageExtractError."""
        for attempt in range(self.max_retries + 1):
            try:
                response = self._http.request(method, self.base_url + path, headers=self._headers, **kwargs)
            except httpx.TransportError as error:
                # Starting a job is retried only when the request surely was not sent.
                unsent = isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout))
                if attempt == self.max_retries or (method == "POST" and path == "/extract" and not unsent):
                    raise PageExtractError(f"Could not reach the API: {error}") from error
            else:
                if response.status_code not in retry or attempt == self.max_retries:
                    return _json(response)
            time.sleep(0.5 * 2 ** attempt)
        raise AssertionError("unreachable")

    def close(self) -> None:
        self._http.close()
        self.search.close()

    def __enter__(self) -> "PageExtract":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _id(item: Any) -> str:
    return item if isinstance(item, str) else item.id


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        data = None
    if response.is_success and isinstance(data, dict):
        return data
    request_id = response.headers.get("x-request-id")
    error = data.get("error") if isinstance(data, dict) else None
    error = error if isinstance(error, dict) else {}
    raise PageExtractError(error.get("message") or response.text[:200] or response.reason_phrase,
                           status=response.status_code, code=error.get("code"),
                           request_id=error.get("request_id") or request_id)
