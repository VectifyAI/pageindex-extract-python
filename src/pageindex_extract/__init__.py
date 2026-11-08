"""Page Extract API by PageIndex: turn a PDF into JSON that fits your schema."""

from pageindex_search import Document

from ._client import ExtractJob, JobPage, Jobs, PageExtract, PageExtractError, Usage, schema_of
from ._version import __version__

__all__ = ["PageExtract", "PageExtractError", "ExtractJob", "JobPage", "Jobs", "Usage", "Document", "schema_of",
           "__version__"]
