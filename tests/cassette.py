"""Recorded HTTP exchanges ("cassettes") for the integration tests.

Every HTTP request P3 sends through httpx, the ollama library's included,
ends in ``httpx.HTTPTransport.handle_request``. A cassette takes its place:
it answers each request with the next exchange recorded in
``tests/fixtures/http/<name>.json``. With ``P3_RECORD=1`` it sends the
requests for real instead and writes what they got to that file::

    P3_RECORD=1 PYTHONPATH=. pytest tests/test_recorded.py

Requests are matched in order by method and URL (without its query), so a
change in what P3 asks for fails the test, saying to record it again,
instead of replaying an answer to another question. Request headers are
never stored (they carry the API keys); of the response's, only the content
type is.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import httpx

FIXTURES = Path(__file__).parent / "fixtures" / "http"
RECORDING = bool(os.environ.get("P3_RECORD"))


def _url(url: httpx.URL) -> str:
    return str(url.copy_with(query=None))


def _decode(content: bytes, content_type: str) -> Dict[str, Any]:
    """A body as stored: parsed when it is JSON, else as text."""
    if not content:
        return {"text": ""}
    text = content.decode("utf-8")
    if "json" in content_type or not content_type:
        try:
            return {"json": json.loads(text)}
        except ValueError:
            pass
    return {"text": text}


def _encode(body: Dict[str, Any]) -> bytes:
    if "json" in body:
        return json.dumps(body["json"]).encode()
    return body["text"].encode()


class Cassette:
    """``with Cassette("name") as tape:`` answers the block's HTTP requests
    from the recording; ``tape.requests`` holds what was sent, parsed, for
    the test to check."""

    def __init__(self, name: str):
        self.path = FIXTURES / f"{name}.json"
        self.exchanges: List[Dict[str, Any]] = []
        if not RECORDING:
            self.exchanges = json.loads(self.path.read_text())["exchanges"]
        self.requests: List[Dict[str, Any]] = []
        self._played = 0

    def __enter__(self) -> "Cassette":
        self._real = httpx.HTTPTransport.handle_request
        tape = self

        def handle_request(transport, request):
            return tape._handle(transport, request)

        httpx.HTTPTransport.handle_request = handle_request  # type: ignore
        return self

    def __exit__(self, exc_type, exc, tb):
        httpx.HTTPTransport.handle_request = self._real  # type: ignore
        if exc_type is not None:
            return
        if RECORDING:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"exchanges": self.exchanges}, indent=1, ensure_ascii=False)
                + "\n"
            )
        elif self._played < len(self.exchanges):
            raise AssertionError(
                f"{self.path.name}: {len(self.exchanges) - self._played} recorded "
                "requests were never sent; record it again with P3_RECORD=1"
            )

    def _handle(self, transport, request: httpx.Request) -> httpx.Response:
        sent = {
            "method": request.method,
            "url": _url(request.url),
            "body": _decode(request.read(), request.headers.get("content-type", "")),
        }
        self.requests.append(sent)
        if RECORDING:
            response = self._real(transport, request)
            content = response.read()
            response.close()
            content_type = response.headers.get("content-type", "")
            self.exchanges.append(
                {
                    "request": sent,
                    "response": {
                        "status": response.status_code,
                        "content_type": content_type,
                        "body": _decode(content, content_type),
                    },
                }
            )
            return httpx.Response(
                response.status_code,
                headers={"content-type": content_type},
                content=content,
                request=request,
            )

        if self._played == len(self.exchanges):
            raise AssertionError(
                f"{self.path.name}: no recording for {sent['method']} {sent['url']} "
                f"(request {self._played + 1}); record it again with P3_RECORD=1"
            )
        recorded = self.exchanges[self._played]
        self._played += 1
        expected = (recorded["request"]["method"], recorded["request"]["url"])
        if expected != (sent["method"], sent["url"]):
            raise AssertionError(
                f"{self.path.name}: request {self._played} is "
                f"{sent['method']} {sent['url']}, recorded {' '.join(expected)}; "
                "record it again with P3_RECORD=1"
            )
        answer = recorded["response"]
        return httpx.Response(
            answer["status"],
            headers={"content-type": answer["content_type"]},
            content=_encode(answer["body"]),
            request=request,
        )
