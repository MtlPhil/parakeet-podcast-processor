"""The web as Ask's second source, next to the library: passages from a web
search, given to the answer model as [W1], [W2]... and shown apart from the
podcasts' passages.

Two searchers, chosen by the shared ``web_search`` setting:

- ``gemini``: Google Search through Gemini's grounding (the Gemini API key):
  Gemini writes short notes and says which web page supports each one; each
  page becomes a passage of the notes it supports.
- ``searxng``: a SearXNG server (``searxng_url``, its JSON format enabled),
  for local models: each result's title and snippet is a passage. Use its
  local address: through a public URL its limiter lets only a couple of
  JSON requests through, and behind a proxy (Cloudflare) every client has
  the same address, so it can't be exempted by IP.

Only the question (as the question step rewrote it) is sent. Asked per
question: the page has an "Also search the web" switch, off by default."""

import errno
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from .llm import LLMClient, resolve_provider_and_model

logger = logging.getLogger(__name__)

WEB_SEARCHERS = ("gemini", "searxng")
MAX_WEB_PASSAGES = 6
_PASSAGE_CHARS = 1200
_TIMEOUT = 20.0
# SearXNG's limiter (bot detection) answers 429 to a request whose Accept
# header lacks text/html, with no Accept-Language, or from a user agent like
# curl's or python's; these pass it. Nothing in them names the user.
_SEARXNG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) P3",
    "Accept": "text/html,application/json;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

_NOTES_SYSTEM = """Search the web for the user's question and report what \
reliable, recent sources say, as short factual notes: one or two sentences \
each, every one stating who says it (the publication or the person). Give \
figures and dates exactly. No introduction and no conclusion. Write in \
{language}."""


def domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


class GeminiWebSearch:
    name = "gemini"

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def search(self, question: str, language: str = "en") -> List[Dict[str, Any]]:
        try:
            data = self.llm.gemini_web_search(
                _NOTES_SYSTEM.replace(
                    "{language}", "French" if language == "fr" else "English"
                ),
                question,
                max_tokens=900,
            )
        except Exception as e:
            if "API error: 429" in str(e):  # see LLMClient._gemini_request
                raise RuntimeError(
                    "Google Search through Gemini is over its quota on this API "
                    "key (grounding has its own quota; it may need billing turned "
                    "on in Google AI Studio)"
                ) from e
            raise
        return grounded_passages(data)


def grounded_passages(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Gemini's grounded reply as passages: one per web page, holding the
    sentences of the reply that page supports, in the order pages are first
    used. A reply with no grounding gives none: an unsourced note isn't a
    web passage."""
    try:
        meta = data["candidates"][0].get("groundingMetadata") or {}
    except (KeyError, IndexError):
        return []
    chunks = meta.get("groundingChunks") or []
    texts: Dict[int, List[str]] = {}
    for support in meta.get("groundingSupports") or []:
        text = " ".join(str((support.get("segment") or {}).get("text") or "").split())
        if not text:
            continue
        for i in support.get("groundingChunkIndices") or []:
            if 0 <= i < len(chunks) and text not in texts.setdefault(i, []):
                texts[i].append(text)
    passages = []
    for i, sentences in texts.items():
        web = chunks[i].get("web") or {}
        url = web.get("uri")
        if not url:
            continue
        passages.append(
            {
                "title": web.get("title") or domain(url),
                "url": url,
                # Gemini's links go through a Google redirect; its title is
                # the page's site.
                "site": web.get("title") or domain(url),
                "text": " ".join(sentences)[:_PASSAGE_CHARS],
            }
        )
    return passages[:MAX_WEB_PASSAGES]


def _host_unreachable(error: BaseException) -> bool:
    """Whether ``error`` comes from "No route to host", which is also how
    macOS refuses a process without Local Network permission (P3 started by
    launchd gets none), though the host answers from Terminal."""
    seen: Optional[BaseException] = error
    while seen is not None:
        if isinstance(seen, OSError) and seen.errno == errno.EHOSTUNREACH:
            return True
        seen = seen.__cause__ or seen.__context__
    return False


class SearxngWebSearch:
    name = "searxng"

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def search(self, question: str, language: str = "en") -> List[Dict[str, Any]]:
        try:
            response = httpx.get(
                f"{self.base_url}/search",
                params={"q": question, "format": "json", "language": language},
                headers={**_SEARXNG_HEADERS, "Accept-Language": language},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as e:
            if _host_unreachable(e):
                raise RuntimeError(
                    f"SearXNG didn't answer: {e}. If this Mac reaches it otherwise, "
                    "macOS is blocking P3 from the local network: allow it in "
                    "System Settings > Privacy & Security > Local Network"
                ) from e
            raise RuntimeError(f"SearXNG didn't answer: {e}") from e
        if response.status_code == 403:
            raise RuntimeError(
                "SearXNG refused JSON: add json to search.formats in its settings.yml"
            )
        if response.status_code == 429:
            raise RuntimeError(
                "SearXNG's limiter refused the request (429): set searxng_url "
                "to the SearXNG server's local address, which its limiter can "
                "exempt, rather than a public URL"
            )
        try:
            response.raise_for_status()
            results = response.json().get("results") or []
        except (httpx.HTTPError, ValueError) as e:
            raise RuntimeError(f"SearXNG didn't answer: {e}") from e
        passages = []
        for r in results:
            url, text = r.get("url"), " ".join(str(r.get("content") or "").split())
            if not url or not text:
                continue
            passages.append(
                {
                    "title": r.get("title") or domain(url),
                    "url": url,
                    "site": domain(url),
                    "text": text[:_PASSAGE_CHARS],
                }
            )
            if len(passages) >= MAX_WEB_PASSAGES:
                break
        return passages


def make_web_searcher(settings: Dict[str, Any]):
    """The searcher the shared ``web_search`` setting names, or None when it
    is off or can't work (no Gemini key, no SearXNG address)."""
    choice = (settings.get("web_search") or "").lower()
    if choice == "gemini":
        _, model = resolve_provider_and_model({**settings, "llm_provider": "gemini"})
        llm = LLMClient(provider="gemini", model=model)
        if not llm.is_configured:
            logger.warning("Web search through Gemini needs a Gemini API key")
            return None
        return GeminiWebSearch(llm)
    if choice == "searxng":
        url = (settings.get("searxng_url") or "").strip()
        return SearxngWebSearch(url) if url else None
    return None


def web_source(n: int, passage: Dict[str, Any], cited: bool) -> Dict[str, Any]:
    """A web passage as the page shows it, numbered W``n``."""
    return {
        "n": n,
        "title": passage["title"],
        "url": passage["url"],
        "site": passage.get("site") or domain(passage["url"]),
        "text": passage["text"],
        "cited": cited,
    }


def format_web_passages(passages: List[Dict[str, Any]]) -> Optional[str]:
    """The [W1]... block of the answer prompt; None without passages."""
    from .ask import _FENCE_RE

    if not passages:
        return None
    return "\n\n".join(
        f"[W{n}] {p['site']}: {p['title']}\n<<<\n{_FENCE_RE.sub('', p['text'])}\n>>>"
        for n, p in enumerate(passages, start=1)
    )
