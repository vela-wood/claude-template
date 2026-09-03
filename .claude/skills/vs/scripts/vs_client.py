"""Version Story REST API driver.

Encapsulates auth, multipart upload, async job polling, download, and error
mapping. No CLI concerns live here; vs.py is the only intended caller.

    POST /v1/<kind>  ──202──▶  job id
    GET  /v1/<kind>/<id>?format=a,b  ──▶  processing (Retry-After) ▶ ready ▶ downloads[]
                                       └▶  failed (job-level code)  ▶ JobFailed
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

import requests

BASE_URL = "https://api.versionstory.com"
ENV_KEY = "VERSION_STORY_API_KEY"
KEY_PREFIX = "vs_"
MAX_FILE_BYTES = 100 * 1024 * 1024
SUPPORTED_SUFFIXES = {".docx", ".doc", ".pdf"}
DEFAULT_TIMEOUT_S = 600
DEFAULT_RETRY_AFTER_S = 1
INTERNAL_ERROR_RETRIES = 3
INTERNAL_ERROR_BACKOFF_S = 2
HTTP_TIMEOUT_S = 120
# Authenticated, quota-free listing endpoint used to validate a key.
KEY_CHECK_PATH = "/v1/documents"

HTTP_ACCEPTED = 202
HTTP_UNAUTHORIZED = 401
HTTP_INTERNAL_ERROR = 500

STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

COMPARE_FORMATS = ("docx", "pdf", "pdf_changed_pages_only", "md", "json")
MERGE_FORMATS = ("docx", "md", "json")
EDIT_FORMATS = ("docx", "redline")
MIN_MERGE_REVISIONS = 2

# Edit operations accepted by POST /v1/edit and their required fields.
EDIT_OPS: dict[str, tuple[str, ...]] = {
    "replace": ("unid", "text"),
    "delete": ("unid",),
    "insert": ("unid", "position", "text"),
    "accept_revision": ("revision_id",),
    "reject_revision": ("revision_id",),
}
INSERT_POSITIONS = ("before", "after")


class JobKind(Enum):
    COMPARE = "compare"
    MERGE = "merge"
    EDIT = "edit"

    @property
    def path(self) -> str:
        return f"/v1/{self.value}"

    @property
    def id_field(self) -> str:
        return JOB_ID_FIELDS[self]


# Response key holding the job id for each POST endpoint (not derivable: compare → comparison_id).
JOB_ID_FIELDS = {
    JobKind.COMPARE: "comparison_id",
    JobKind.MERGE: "merge_id",
    JobKind.EDIT: "edit_id",
}


def _job_id(body: dict, kind: JobKind) -> str:
    for field in (kind.id_field, "id"):
        if field in body:
            return body[field]
    raise ApiError("UNEXPECTED_RESPONSE", f"no {kind.id_field} in response: {sorted(body)}", None, HTTP_ACCEPTED)


class ApiError(Exception):
    """HTTP-level error carrying the API's error envelope."""

    def __init__(self, code: str, message: str, request_id: str | None, http_status: int):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.request_id = request_id
        self.http_status = http_status


class JobFailed(Exception):
    """Async job reached status=failed."""

    def __init__(self, code: str, upstream_code: str | None = None, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.upstream_code = upstream_code


@dataclass(frozen=True)
class Download:
    format: str
    url: str
    file_name: str
    expires_at: str | None


def redact_key(key: str) -> str:
    """`vs_live_9f3c…` — enough to recognize a key, never enough to use it."""
    prefix_len = len("vs_live_") + 4
    return key[:prefix_len] + "…"


def validate_input(path: Path) -> None:
    """Client-side checks so a bad file does not burn quota."""
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")

    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        allowed = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ValueError(f"Unsupported file type {path.suffix!r} for {path.name}; allowed: {allowed}")

    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"File exceeds 100 MB limit: {path.name}")


def validate_edits(edits: Any) -> None:
    """Shape-check the edits array before uploading."""
    if not isinstance(edits, list) or not edits:
        raise ValueError("edits must be a non-empty JSON array")

    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise ValueError(f"edits[{index}] is not an object")

        op = edit.get("type")
        if op not in EDIT_OPS:
            raise ValueError(f"edits[{index}] has unknown type {op!r}; allowed: {', '.join(EDIT_OPS)}")

        missing = [field for field in EDIT_OPS[op] if field not in edit]
        if missing:
            raise ValueError(f"edits[{index}] ({op}) missing field(s): {', '.join(missing)}")

        if op == "insert" and edit["position"] not in INSERT_POSITIONS:
            raise ValueError(f"edits[{index}] position must be one of {', '.join(INSERT_POSITIONS)}")


def _parse_error(response: requests.Response) -> ApiError:
    try:
        envelope = response.json().get("error", {})
    except ValueError:
        envelope = {}

    return ApiError(
        code=envelope.get("code", f"HTTP_{response.status_code}"),
        message=envelope.get("message", response.text[:200] or response.reason),
        request_id=envelope.get("request_id"),
        http_status=response.status_code,
    )


def _open_files(paths: list[tuple[str, Path]]) -> Iterator[tuple[str, tuple[str, Any]]]:
    for field, path in paths:
        yield field, (path.name, path.open("rb"))


class Client:
    def __init__(self, api_key: str, session: requests.Session | None = None, base_url: str = BASE_URL):
        self._session = session or requests.Session()
        self._session.headers["Authorization"] = f"Bearer {api_key}"
        self._base_url = base_url

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        """Single chokepoint: error envelope → ApiError; retry only 500 INTERNAL_ERROR."""
        url = self._base_url + path
        attempt = 0

        while True:
            response = self._session.request(method, url, timeout=HTTP_TIMEOUT_S, **kwargs)
            if response.ok:
                return response

            error = _parse_error(response)
            retryable = error.http_status == HTTP_INTERNAL_ERROR and attempt < INTERNAL_ERROR_RETRIES
            if not retryable:
                raise error

            attempt += 1
            time.sleep(INTERNAL_ERROR_BACKOFF_S * attempt)

    def _post_files(self, path: str, files: list[tuple[str, Path]], data: dict[str, str]) -> dict:
        handles = list(_open_files(files))
        try:
            response = self._request("POST", path, files=handles, data=data)
        finally:
            for _field, (_name, handle) in handles:
                handle.close()
        return response.json()

    # -- key -----------------------------------------------------------------

    def check_key(self) -> None:
        self._request("GET", KEY_CHECK_PATH, params={"limit": 1})

    # -- job submission ------------------------------------------------------

    def compare(self, original: Path, modified: Path, author: str | None = None) -> str:
        data = {"author": author} if author else {}
        body = self._post_files(JobKind.COMPARE.path, [("original", original), ("modified", modified)], data)
        return _job_id(body, JobKind.COMPARE)

    def merge(self, original: Path, revisions: list[Path]) -> str:
        if len(revisions) < MIN_MERGE_REVISIONS:
            raise ValueError(f"merge needs at least {MIN_MERGE_REVISIONS} revisions")

        files = [("original", original)] + [("revisions", rev) for rev in revisions]
        body = self._post_files(JobKind.MERGE.path, files, {})
        return _job_id(body, JobKind.MERGE)

    def edit_initiate(self, document: Path) -> str:
        body = self._post_files("/v1/edit/initiate", [("document", document)], {})
        return body["file_id"]

    def edit_markdown(self, file_id: str, timeout_s: int = DEFAULT_TIMEOUT_S) -> dict:
        """Poll the initiate endpoint until the markdown is ready."""
        deadline = time.monotonic() + timeout_s

        while True:
            response = self._request("GET", f"/v1/edit/initiate/{file_id}")
            body = response.json()
            status = body.get("status")
            if status == STATUS_READY or body.get("markdown"):
                return body
            if status == STATUS_FAILED:
                raise JobFailed(body.get("error_code", "EDIT_INITIATE_FAILED"), message=body.get("error", ""))

            self._sleep_until_retry(response, deadline, f"edit initiate {file_id}")

    def edit(
        self,
        edits_json: str,
        *,
        file_id: str | None = None,
        document: Path | None = None,
        description: str | None = None,
    ) -> str:
        if bool(file_id) == bool(document):
            raise ValueError("edit needs exactly one of file_id or document")

        data = {"edits": edits_json}
        if file_id:
            data["file_id"] = file_id
        if description:
            data["edit_description"] = description

        files = [("document", document)] if document else []
        body = self._post_files(JobKind.EDIT.path, files, data)
        return _job_id(body, JobKind.EDIT)

    # -- polling / download --------------------------------------------------

    def wait(self, kind: JobKind, job_id: str, formats: list[str], timeout_s: int = DEFAULT_TIMEOUT_S) -> list[Download]:
        """Poll until every requested format is downloadable."""
        deadline = time.monotonic() + timeout_s
        params = {"format": ",".join(formats)}

        while True:
            response = self._request("GET", f"{kind.path}/{job_id}", params=params)
            body = response.json()
            status = body.get("status")

            if status == STATUS_FAILED:
                error = body.get("error") or {}
                if isinstance(error, str):
                    error = {"code": error}
                raise JobFailed(
                    error.get("code", f"{kind.value.upper()}_FAILED"),
                    upstream_code=error.get("upstream_code"),
                    message=error.get("message", ""),
                )

            downloads = [
                Download(d["format"], d["url"], d.get("file_name", ""), d.get("expires_at"))
                for d in body.get("downloads", [])
            ]
            have = {d.format for d in downloads}
            if status == STATUS_READY and not body.get("pending_formats") and have >= set(formats):
                return downloads

            self._sleep_until_retry(response, deadline, f"{kind.value} {job_id}")

    def download(self, url: str) -> bytes:
        response = self._session.get(url, timeout=HTTP_TIMEOUT_S)
        if not response.ok:
            raise _parse_error(response)
        return response.content

    def _sleep_until_retry(self, response: requests.Response, deadline: float, label: str) -> None:
        try:
            delay = float(response.headers.get("Retry-After", DEFAULT_RETRY_AFTER_S))
        except ValueError:
            delay = DEFAULT_RETRY_AFTER_S

        if time.monotonic() + delay > deadline:
            raise TimeoutError(f"Timed out waiting for {label}")
        time.sleep(delay)
