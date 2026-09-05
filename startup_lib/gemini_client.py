"""Gemini API layer for cloud OCR: one page image in, Markdown out.

The only module that imports google.genai. Everything above it works with
PageText/PageFailed and never sees the SDK's request or response shapes.
"""

from dataclasses import dataclass
from enum import Enum

from google import genai
from google.genai import errors, types

MODEL = "gemini-3.8-flash"
ENV_KEY = "GEMINI_API_KEY"
IMAGE_MIME = "image/jpeg"

# Gemini 3 charges a fixed token count per image part regardless of its
# pixel size (high = 1120 tokens); Google documents `high` for dense text.
_MEDIA_RESOLUTION = types.PartMediaResolutionLevel.MEDIA_RESOLUTION_HIGH

# A dense page transcribes to ~1-2k tokens; headroom for wide tables.
_MAX_OUTPUT_TOKENS = 16_384
_HTTP_TIMEOUT_MS = 180_000

# Transport-level retries live in the SDK; 429 is the one that matters
# at high concurrency.
_RETRY_OPTIONS = types.HttpRetryOptions(
    attempts=5,
    initial_delay=1.0,
    max_delay=30.0,
    exp_base=2.0,
    jitter=1.0,
    http_status_codes=[408, 429, 500, 502, 503, 504],
)

# A bad key fails every page the same way; abort the run instead of
# paying for and reporting hundreds of identical failures.
_FATAL_HTTP_CODES = frozenset({401, 403})

# App-level retry for MAX_TOKENS, blocked, or blank responses.
_PAGE_ATTEMPTS = 2

# Promotional pricing at GA (2026-09); used for the run totals only.
USD_PER_M_INPUT = 0.75
USD_PER_M_OUTPUT = 3.75

SYSTEM_INSTRUCTION = """You are an OCR engine transcribing one scanned page of a legal document.

Rules:
- Transcribe every visible character in natural reading order as Markdown.
- Preserve structure: headings, numbered and bulleted lists, indentation, and
  tables (render tables as Markdown tables).
- Output the transcription only. No summary, no commentary, no preamble, and
  no code fence around the output.
- Write [illegible] for a run of text you cannot read.
- Write [signature], [handwritten: <text>], or [stamp: <text>] for marks that
  are not typed text.
- If nothing is printed on the page, output exactly: [blank page]
- Never invent, complete, or correct text that is not on the page."""

PAGE_PROMPT = "Transcribe this page to Markdown."

_FENCE = "```"


class ThinkingLevel(Enum):
    """CLI-facing reasoning depth; maps onto the SDK's enum."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    def to_sdk(self) -> types.ThinkingLevel:
        return types.ThinkingLevel(self.value.upper())


@dataclass(frozen=True)
class PageText:
    markdown: str
    prompt_tokens: int
    output_tokens: int  # candidates + thoughts


class PageFailed(Exception):
    """One page could not be transcribed; `fatal` aborts the whole run."""

    def __init__(self, detail: str, fatal: bool = False):
        super().__init__(detail)
        self.detail = detail
        self.fatal = fatal


def make_client(api_key: str) -> genai.Client:
    """Test seam: monkeypatched to return a fake SDK client."""
    http_options = types.HttpOptions(
        timeout=_HTTP_TIMEOUT_MS, retry_options=_RETRY_OPTIONS
    )
    return genai.Client(api_key=api_key, http_options=http_options)


def _request_config(level: ThinkingLevel) -> types.GenerateContentConfig:
    # gemini-3.8-flash rejects temperature/top_p/top_k; thinking_level is
    # the only sampling knob.
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        thinking_config=types.ThinkingConfig(thinking_level=level.to_sdk()),
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        # No tools are passed; disabling AFC silences the SDK's async warning.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


def _image_part(data: bytes) -> types.Part:
    return types.Part.from_bytes(
        data=data, mime_type=IMAGE_MIME, media_resolution=_MEDIA_RESOLUTION
    )


def _strip_fence(text: str) -> str:
    """Drop one outer ```markdown fence if the model added it anyway."""
    stripped = text.strip()
    if not stripped.startswith(_FENCE) or not stripped.endswith(_FENCE):
        return stripped
    first_newline = stripped.find("\n")
    if first_newline == -1:
        return stripped
    return stripped[first_newline + 1 : -len(_FENCE)].strip()


def _api_error_detail(exc: errors.APIError) -> str:
    return f"{exc.code} {exc.status}: {exc.message}"


class GeminiPageOcr:
    """Transcribes page images with gemini-3.8-flash."""

    def __init__(self, api_key: str, level: ThinkingLevel):
        self._client = make_client(api_key)
        self._config = _request_config(level)

    async def ocr_page(self, image: bytes) -> PageText:
        last_reason = "empty response"
        for _ in range(_PAGE_ATTEMPTS):
            try:
                response = await self._client.aio.models.generate_content(
                    model=MODEL,
                    contents=[PAGE_PROMPT, _image_part(image)],
                    config=self._config,
                )
            except errors.APIError as exc:
                raise PageFailed(
                    _api_error_detail(exc), fatal=exc.code in _FATAL_HTTP_CODES
                ) from exc

            page = _parse_response(response)
            if page is not None:
                return page
            last_reason = _finish_reason(response)

        raise PageFailed(
            f"no usable text after {_PAGE_ATTEMPTS} attempts ({last_reason})"
        )


def _finish_reason(response) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "no candidates"
    reason = getattr(candidates[0], "finish_reason", None)
    return getattr(reason, "name", None) or str(reason)


def _parse_response(response) -> PageText | None:
    """The only place that touches the SDK response shape."""
    if _finish_reason(response) != types.FinishReason.STOP.name:
        return None
    text = _strip_fence(getattr(response, "text", None) or "")
    if not text:
        return None

    usage = getattr(response, "usage_metadata", None)
    prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
    output_tokens = (getattr(usage, "candidates_token_count", 0) or 0) + (
        getattr(usage, "thoughts_token_count", 0) or 0
    )
    return PageText(text, prompt_tokens, output_tokens)
