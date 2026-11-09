# Page Extract Python SDK

Python client for [Page Extract](https://extract.pageindex.ai) by PageIndex: extract the data you need out of any PDF.

```bash
pip install pageindex-extract
```

Create an API key in the [console](https://search.pageindex.ai/console/keys?for=extract). Page Extract and Page Search share one key, one credit balance and one set of documents.

```bash
export PAGEINDEX_EXTRACT_API_KEY=pie_live_...
```

The client reads `PAGEINDEX_EXTRACT_API_KEY`, or `PAGEINDEX_SEARCH_API_KEY` if you already keep the key there.

## Extract

```python
from pageindex_extract import PageExtract

client = PageExtract()

schema = {
    "type": "object",
    "properties": {
        "invoice_number": {"type": "string"},
        "total": {"type": "number", "description": "Amount due, in dollars, without the currency sign"},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"item": {"type": "string"}, "amount": {"type": "number"}},
            },
        },
    },
}

job = client.extract("invoice.pdf", schema)
print(job.result)
```

`extract` uploads the file (a file uploaded before is not uploaded again), waits for the extraction and returns it. `job.result` is JSON that fits your schema; a field the document does not give is `null`.

The schema can also be a Pydantic model:

```python
from pydantic import BaseModel

class Line(BaseModel):
    item: str
    amount: float

class Invoice(BaseModel):
    invoice_number: str
    total: float
    lines: list[Line]

job = client.extract("invoice.pdf", Invoice)
invoice = job.parse(Invoice)
```

### Options

```python
job = client.extract(
    "report.pdf",
    schema,
    tier="pro",            # "standard" ($0.03 a page) or "pro" ($0.05 a page)
    pages="3-9",           # only these pages, and only they are charged; also (3, 9) or 3
    cite_sources=True,     # the page each list item is on: job.sources
    system_prompt="Amounts are in thousands of dollars.",
)
print(job.usage.pages, job.usage.credits)
```

`document` can be a path, bytes, an open binary file, an uploaded document, or a document id (`"doc_..."`). A file is uploaded to the **Page Extract** collection (made on first use), where it is kept apart from documents you upload to search; pass `collection="..."` (a name or an id) for another, or `collection=None` for none. A file already in that collection is not uploaded again.

You pay only for extractions that complete. A failed extraction raises `PageExtractError` (its `job` holds the job) and costs nothing.

## Jobs

Extractions run in the background. To start one and come back for it later:

```python
job = client.extract("report.pdf", schema, wait=False)   # PENDING
job = client.jobs.wait(job)                              # COMPLETED, or raises

client.jobs.get(job.id)          # the job, with its result once COMPLETED
client.jobs.cancel(job.id)       # stop it; nothing is charged
client.jobs.delete(job.id)       # delete it and its result

for job in client.jobs.list():   # every extraction, newest first
    print(job.id, job.status)
```

## Documents

`client.documents` is the Page Search client's, with the same key:

```python
doc = client.documents.upload("report.pdf")
job = client.extract(doc, schema)
```

## Errors

Errors raise `PageExtractError` with `status`, `code`, `message` and `request_id`.

| Code | When |
| --- | --- |
| `unauthorized` | The API key is missing or not valid. |
| `insufficient_credit` | Your credit does not cover the pages. Add credit in the console. |
| `invalid_schema` | The schema is not valid JSON Schema, or its top is not an object. |
| `schema_too_large` | The schema is larger than 1 MB. |
| `invalid_target_pages` | `pages` is not within the document. |
| `document_not_found` | No such document. |
| `job_not_found` | No such job. |
| `job_failed` / `job_cancelled` | The extraction failed or was cancelled. Nothing is charged. |
| `timeout` | `wait` gave up; the job goes on running. |

## Links

- [Page Extract](https://extract.pageindex.ai)
- [API docs](https://extract.pageindex.ai/docs)
- [Page Search Python SDK](https://github.com/VectifyAI/pageindex-search-python)

## License

MIT
