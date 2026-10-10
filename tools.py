"""tools.py - STUDENT IMPLEMENTS.  Source tools for the research agents.   Guide: GUIDE.md, part 1.

Rules for every tool:
  * runs on the HOST (not in the sandbox): API keys must never enter the sandbox;
  * returns a STRING (JSON text of compact records) and NEVER raises:
        "NO RESULTS"  when the source answers with nothing,
        "ERROR: ..."  when the source keeps failing after the retries (the agent then tries another source);
  * the docstring is the tool description the LLM reads: keep it precise (what it does, what it returns, when to use it).
Try your tools without any agent:   python tools.py
"""
import json  # noqa: F401
import os  # noqa: F401
import time  # noqa: F401
import xml.etree.ElementTree as ET # noqa: F401  (arXiv answers with Atom XML)
import random
import re

import httpx  # noqa: F401
from langchain_core.tools import tool

# ---- constants (given) ----
ARXIV_URL = "https://export.arxiv.org/api/query"  # https only: http answers 301
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"
HF_SEARCH_URL = "https://huggingface.co/api/papers/search"
EXA_URL = "https://mcp.exa.ai/mcp"


class RetryableError(Exception):
    """Given. Raise it inside a call to ask with_retry to wait and try again (retry_after in seconds, optional)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_ARXIV_LAST_CALL = 0.0  # time.time() of the last arXiv request (etiquette: >= 3 s apart)


def _http_get(url, params, timeout=20):
    """GET url; turn network errors and HTTP 429/5xx into RetryableError so with_retry tries again."""
    try:
        response = httpx.get(url, params=params, timeout=timeout)
    except httpx.TransportError as e:  # timeout, connection reset -> retry
        raise RetryableError(f"{type(e).__name__}: {e}")
    if response.status_code in (429, 500, 502, 503, 504):
        retry_after = response.headers.get("Retry-After")
        raise RetryableError(
            f"HTTP {response.status_code}",
            retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
        )
    response.raise_for_status()  # other 4xx -> not retryable
    return response


def with_retry(fn, *, attempts=5, base=1.0, cap=30.0):
    """Call fn(); when it raises RetryableError, wait and call it again.

    PSEUDO-CODE:
      for attempt in 0 .. attempts-1:
          try: return fn()
          except RetryableError as e:
              if this was the last attempt: raise
              delay = e.retry_after if the server told us, else exponential backoff base * 2**attempt
              cap the delay at `cap` seconds; add random jitter to the exponential case
              sleep(delay)
    Use it to wrap EVERY network call below. Also treat these as retryable: HTTP 429/500/502/503/504,
    httpx.TransportError (timeouts, connection resets). Read the Retry-After header when present.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except RetryableError as e:
            if attempt == attempts - 1:
                raise
            delay = e.retry_after if e.retry_after is not None else base * (2**attempt)
            delay = min(delay, cap) + random.uniform(0, 1)  # add random jitter
            time.sleep(delay)


@tool
def arxiv_search(query: str, max_results: int = 10) -> str:
    """Search arXiv papers by keywords, newest first. Returns a JSON list of {id, url, published, title, summary}."""
    # PSEUDO-CODE:
    #   keep only word characters of `query` -> terms; no terms -> "NO RESULTS" (do not call the network)
    #   respect arXiv etiquette: at least 3 seconds between two arXiv calls (remember the time of the last call)
    #   GET ARXIV_URL params: search_query="all:t1 AND all:t2 ...", sortBy=submittedDate, sortOrder=descending,
    #       max_results=clamp(max_results, 1, 30)           (wrap in with_retry)
    #   parse the Atom XML: each <entry> -> {id (last part of <id> after /abs/), url, published[:10], title, summary}
    #       collapse whitespace/newlines in title and summary; cut summary to ~600 chars
    #   no entries -> "NO RESULTS"; else json.dumps(records, ensure_ascii=False)
    #   any exception -> "ERROR: <type>: <message>"
    terms = [t.strip("-") for t in re.sub(r"[^\w\s-]", " ", query).split()]
    terms = [t for t in terms if t and t.upper() not in ("AND", "OR", "ANDNOT")]
    if not terms:
        return "NO RESULTS"

    params = {
        "search_query": " AND ".join(f"all:{t}" for t in terms),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": max(1, min(int(max_results), 30)),
        "start": 0,
    }

    def _call_arxiv():
        global _ARXIV_LAST_CALL
        wait = 3.0 - (time.time() - _ARXIV_LAST_CALL)  # arXiv etiquette: >= 3 s between calls
        if wait > 0:
            time.sleep(wait)
        _ARXIV_LAST_CALL = time.time()
        return _http_get(ARXIV_URL, params, timeout=30).text

    try:
        # arXiv rate-limits whole classrooms behind one IP: retry longer than other sources
        root = ET.fromstring(with_retry(_call_arxiv, attempts=6, cap=60.0))

        records = []
        for entry in root.findall("atom:entry", ATOM_NS):
            raw_id = entry.findtext("atom:id", "", ATOM_NS)
            if "/abs/" not in raw_id:
                continue
            arxiv_id = re.sub(r"v\d+$", "", raw_id.split("/abs/")[-1])  # 2501.00001v1 -> 2501.00001
            summary = " ".join(entry.findtext("atom:summary", "", ATOM_NS).split())
            if len(summary) > 600:
                summary = summary[:600] + "..."
            records.append(
                {
                    "id": arxiv_id,
                    "url": f"https://arxiv.org/abs/{arxiv_id}",
                    "published": entry.findtext("atom:published", "", ATOM_NS)[:10],
                    "title": " ".join(entry.findtext("atom:title", "", ATOM_NS).split()),
                    "summary": summary,
                }
            )

        if not records:
            return "NO RESULTS"
        return json.dumps(records, ensure_ascii=False)
    except Exception as e:  # includes RetryableError after the last attempt: never raise
        return f"ERROR: {type(e).__name__}: {e}"


@tool
def hf_daily_papers(limit: int = 30, date: str = "", keyword: str = "") -> str:
    """Hugging Face Daily Papers = what is trending in AI research. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars} sorted by upvotes. `date` is YYYY-MM-DD (empty = latest).
    `keyword` filters title/summary; there is no topic search on this endpoint (use hf_search_papers for a topic)."""
    # PSEUDO-CODE:
    #   GET HF_DAILY_URL params: limit (clamp 1..100) and date (only when given)      (with_retry)
    #   response = list of items {"paper": {id, title, summary, upvotes, githubRepo, githubStars, publishedAt}, ...}
    #   map every item to the record shape above (skip items without paper.id); url = https://huggingface.co/papers/<id>
    #   keyword -> keep records whose title+summary contains it (case-insensitive); sort by upvotes descending
    params = {"limit": max(1, min(int(limit), 100))}
    if date:
        params["date"] = date

    try:
        items = with_retry(lambda: _http_get(HF_DAILY_URL, params).json())

        records = []
        for item in items or []:
            paper = item.get("paper") or {}
            paper_id = paper.get("id")
            if not paper_id:
                continue
            summary = " ".join((paper.get("summary") or item.get("summary") or "").split())
            if len(summary) > 600:
                summary = summary[:600] + "..."
            records.append(
                {
                    "id": paper_id,
                    "url": f"https://huggingface.co/papers/{paper_id}",
                    "published": (paper.get("publishedAt") or item.get("publishedAt") or "")[:10],
                    "title": " ".join((paper.get("title") or item.get("title") or "").split()),
                    "summary": summary,
                    "upvotes": paper.get("upvotes") or 0,
                    "github": paper.get("githubRepo") or "",
                    "stars": paper.get("githubStars") or 0,
                }
            )

        if keyword:
            kw = keyword.lower()
            records = [r for r in records if kw in (r["title"] + " " + r["summary"]).lower()]
        records.sort(key=lambda r: r["upvotes"], reverse=True)

        if not records:
            return "NO RESULTS"
        return json.dumps(records, ensure_ascii=False)
    except Exception as e:  # includes RetryableError after the last attempt: never raise
        return f"ERROR: {type(e).__name__}: {e}"


@tool
def hf_search_papers(query: str, limit: int = 10) -> str:
    """Search Hugging Face papers by topic. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars}."""
    # PSEUDO-CODE:
    #   GET HF_SEARCH_URL params: q=query, limit (clamp 1..50)                         (with_retry)
    #   same item shape as the daily endpoint; prefer paper["ai_summary"] over paper["summary"] when present
    params = {"q": query, "limit": max(1, min(int(limit), 50))}

    try:
        items = with_retry(lambda: _http_get(HF_SEARCH_URL, params).json())

        records = []
        for item in items or []:
            paper = item.get("paper") or {}
            paper_id = paper.get("id")
            if not paper_id:
                continue
            # prefer ai_summary when present
            summary = paper.get("ai_summary") or paper.get("summary") or item.get("summary") or ""
            summary = " ".join(summary.split())
            if len(summary) > 600:
                summary = summary[:600] + "..."
            records.append(
                {
                    "id": paper_id,
                    "url": f"https://huggingface.co/papers/{paper_id}",
                    "published": (paper.get("publishedAt") or item.get("publishedAt") or "")[:10],
                    "title": " ".join((paper.get("title") or item.get("title") or "").split()),
                    "summary": summary,
                    "upvotes": paper.get("upvotes") or 0,
                    "github": paper.get("githubRepo") or "",
                    "stars": paper.get("githubStars") or 0,
                }
            )

        if not records:
            return "NO RESULTS"
        return json.dumps(records, ensure_ascii=False)
    except Exception as e:  # includes RetryableError after the last attempt: never raise
        return f"ERROR: {type(e).__name__}: {e}"


def _exa_call(tool_name, arguments):
    """Call one Exa MCP tool over plain HTTP (JSON-RPC "tools/call") and return its text.
    Retries on network errors, HTTP 429/5xx and Exa's free-tier rate limit; raises after the last attempt."""
    key = os.environ.get("EXA_API_KEY", "")
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments}}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def _call():
        try:
            response = httpx.post(EXA_URL, params={"exaApiKey": key} if key else None,
                                  json=body, headers=headers, timeout=60)
        except httpx.TransportError as e:  # timeout, connection reset -> retry
            raise RetryableError(f"{type(e).__name__}: {e}")
        if response.status_code in (429, 500, 502, 503, 504):  # keyless rate limit comes back as 429
            retry_after = response.headers.get("Retry-After")
            raise RetryableError(
                f"HTTP {response.status_code}",
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        response.raise_for_status()

        # the answer is server-sent events ("data: {...}" lines) or, sometimes, plain JSON
        if "text/event-stream" in response.headers.get("content-type", ""):
            data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
            if not data:
                raise ValueError("Exa answered with no data line")
            message = json.loads(data[-1])
        else:
            message = response.json()

        if "error" in message:  # JSON-RPC error instead of a result
            err = message["error"].get("message", str(message["error"]))
            if "rate limit" in err.lower():
                raise RetryableError("Exa rate limited")
            raise RuntimeError(f"Exa error: {err}")

        result = message.get("result") or {}
        text = "\n".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
        # free tier may answer HTTP 200 with a notice as "content" and a flag in result._meta: never treat that as a page
        meta = result.get("_meta") or {}
        if any("rate" in k.lower() and v for k, v in meta.items()) or (
            result.get("isError") and "rate limit" in text.lower()
        ):
            raise RetryableError("Exa rate limited")
        if result.get("isError"):
            raise RuntimeError(f"Exa tool error: {text[:300]}")
        return text.strip()

    return with_retry(_call, attempts=6, cap=60.0)


@tool
def web_search(query: str, objective: str = "", num_results: int = 5) -> str:
    """Search the web (Exa). Describe the ideal page in natural language. Returns clean text of the top results with URLs."""
    # PSEUDO-CODE:
    #   call the MCP tool "web_search_exa" with arguments {query, objective, numResults}
    #       (objective is REQUIRED by Exa: when empty, build one from the query)
    #   see GUIDE.md part 1.4 for how to call an MCP server over plain HTTP (JSON-RPC "tools/call") and read the answer
    #   read optional env EXA_API_KEY; when present it is sent to the Exa endpoint.
    #       (see GUIDE.md 1.4 for where it goes) => the key then appears in exception text: redact it before returning "ERROR: ..."
    #   WATCH OUT: read GUIDE.md 1.4 about how Exa signals "rate limited" on the free tier, and retry on it
    query = " ".join(query.split())
    if not query:
        return "NO RESULTS"
    arguments = {
        "query": query,
        "objective": objective.strip() or f"Find authoritative web pages about: {query}",
        "numResults": max(1, min(int(num_results), 10)),
    }
    try:
        text = _exa_call("web_search_exa", arguments)
        if not text:
            return "NO RESULTS"
        return text[:12000] + ("..." if len(text) > 12000 else "")
    except Exception as e:  # the key travels in the URL, so httpx errors may contain it: redact
        key = os.environ.get("EXA_API_KEY", "")
        msg = str(e).replace(key, "***") if key else str(e)
        return f"ERROR: {type(e).__name__}: {msg}"


@tool
def web_fetch(url: str) -> str:
    """Read the full content of one web page (e.g. an arXiv abstract page) as markdown. Long pages are truncated."""
    # PSEUDO-CODE: MCP tool "web_fetch_exa" with arguments {"urls": [url]}; truncate the text to ~12000 chars
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return "ERROR: ValueError: url must start with http:// or https://"
    try:
        text = _exa_call("web_fetch_exa", {"urls": [url]})
        if not text:
            return "NO RESULTS"
        return text[:12000] + ("..." if len(text) > 12000 else "")
    except Exception as e:  # the key travels in the URL, so httpx errors may contain it: redact
        key = os.environ.get("EXA_API_KEY", "")
        msg = str(e).replace(key, "***") if key else str(e)
        return f"ERROR: {type(e).__name__}: {msg}"


# ---- TODO 5: registry (the researcher subagent gets exactly these) ----
SOURCE_TOOLS = [arxiv_search, hf_daily_papers, hf_search_papers, web_search, web_fetch]


if __name__ == "__main__":
    for name, fn, args in [
        ("arxiv_search", arxiv_search, {"query": "world model", "max_results": 3}),
        ("hf_daily_papers", hf_daily_papers, {"limit": 20}),
        ("hf_search_papers", hf_search_papers, {"query": "world model", "limit": 3}),
        ("web_search", web_search, {"query": "survey paper on world models", "num_results": 2}),
        ("web_fetch", web_fetch, {"url": "https://arxiv.org/abs/1803.10122"}),
    ]:
        try:
            print(f"== {name}\n{fn.invoke(args)[:400]}\n")
        except NotImplementedError as exc:
            print(f"== {name}: not implemented yet ({exc})\n")
