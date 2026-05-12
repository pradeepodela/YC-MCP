#!/usr/bin/env python3
"""Y Combinator MCP Server — HN news, YC companies, jobs, and batches."""

import asyncio
import json
import re
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("YCombinator")

HN_API = "https://hacker-news.firebaseio.com/v0"
HN_SEARCH = "https://hn.algolia.com/api/v1"
YC_COMPANIES_URL = "https://www.ycombinator.com/companies"

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get_json(url: str, params: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


async def _hn_item(item_id: int) -> dict | None:
    return await _get_json(f"{HN_API}/item/{item_id}.json")


async def _hn_items(ids: list[int], limit: int) -> list[dict]:
    capped = ids[: min(limit, len(ids))]
    results = await asyncio.gather(*[_hn_item(i) for i in capped], return_exceptions=True)
    return [r for r in results if isinstance(r, dict) and r]


async def _yc_page_companies() -> list[dict]:
    """
    Fetch the YC companies directory page and extract embedded Next.js data.
    Returns a list of company dicts, or [] if parsing fails.
    """
    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": _BROWSER_UA},
    ) as client:
        r = await client.get(YC_COMPANIES_URL)
        r.raise_for_status()

    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL
    )
    if not m:
        return []

    page_data = json.loads(m.group(1))
    return (
        page_data.get("props", {})
        .get("pageProps", {})
        .get("companies", [])
    )


# ---------------------------------------------------------------------------
# Hacker News — story feeds
# ---------------------------------------------------------------------------

@mcp.tool()
async def hn_top_stories(limit: int = 10) -> list[dict]:
    """Fetch the current top stories from Hacker News (max 30)."""
    ids = await _get_json(f"{HN_API}/topstories.json")
    return await _hn_items(ids, min(limit, 30))


@mcp.tool()
async def hn_new_stories(limit: int = 10) -> list[dict]:
    """Fetch the newest stories from Hacker News (max 30)."""
    ids = await _get_json(f"{HN_API}/newstories.json")
    return await _hn_items(ids, min(limit, 30))


@mcp.tool()
async def hn_best_stories(limit: int = 10) -> list[dict]:
    """Fetch the best-ranked stories from Hacker News (max 30)."""
    ids = await _get_json(f"{HN_API}/beststories.json")
    return await _hn_items(ids, min(limit, 30))


@mcp.tool()
async def hn_ask_stories(limit: int = 10) -> list[dict]:
    """Fetch current Ask HN posts (max 30)."""
    ids = await _get_json(f"{HN_API}/askstories.json")
    return await _hn_items(ids, min(limit, 30))


@mcp.tool()
async def hn_show_stories(limit: int = 10) -> list[dict]:
    """Fetch current Show HN posts (max 30)."""
    ids = await _get_json(f"{HN_API}/showstories.json")
    return await _hn_items(ids, min(limit, 30))


# ---------------------------------------------------------------------------
# Hacker News — item & user lookups
# ---------------------------------------------------------------------------

@mcp.tool()
async def hn_get_item(item_id: int) -> dict:
    """
    Fetch a specific Hacker News item by its numeric ID.

    Returns the full item payload: title, URL, score, author, time,
    comment kids, and text for Ask/Show/Job posts.
    """
    item = await _hn_item(item_id)
    return item or {"error": f"Item {item_id} not found or deleted"}


@mcp.tool()
async def hn_get_user(username: str) -> dict:
    """Fetch a Hacker News user profile by username."""
    return await _get_json(f"{HN_API}/user/{username}.json")


@mcp.tool()
async def hn_get_discussion(
    item_id: int,
    max_comments: int = 50,
    max_depth: int = 3,
) -> dict:
    """
    Fetch a Hacker News post together with its full comment thread.

    Args:
        item_id: The story or Ask HN post ID.
        max_comments: Total comments to fetch across the whole tree (max 200).
        max_depth: How many reply levels deep to recurse (max 5).

    Returns a dict with the story fields plus a 'comments' list where each
    comment may itself have a nested 'comments' list.
    """
    max_comments = min(max_comments, 200)
    max_depth = min(max_depth, 5)
    counter = {"n": 0}

    async def fetch_thread(kids: list[int], depth: int) -> list[dict]:
        if depth > max_depth or not kids or counter["n"] >= max_comments:
            return []
        remaining = max_comments - counter["n"]
        batch = kids[:remaining]
        items = await asyncio.gather(*[_hn_item(i) for i in batch], return_exceptions=True)
        result = []
        for item in items:
            if not isinstance(item, dict) or not item or item.get("deleted") or item.get("dead"):
                continue
            counter["n"] += 1
            comment = {k: v for k, v in item.items() if k != "kids"}
            if item.get("kids") and depth < max_depth and counter["n"] < max_comments:
                comment["comments"] = await fetch_thread(item["kids"], depth + 1)
            result.append(comment)
        return result

    story = await _hn_item(item_id)
    if not story:
        return {"error": f"Item {item_id} not found"}

    story = dict(story)
    story["comments"] = await fetch_thread(story.pop("kids", []) or [], depth=1)
    story["comments_fetched"] = counter["n"]
    return story


# ---------------------------------------------------------------------------
# Hacker News — Algolia full-text search
# ---------------------------------------------------------------------------

@mcp.tool()
async def hn_search(
    query: str,
    search_type: str = "story",
    limit: int = 10,
) -> dict:
    """
    Full-text search of Hacker News via Algolia, ranked by relevance.

    Args:
        query: Search terms.
        search_type: One of 'story', 'comment', 'job', 'ask_hn', 'show_hn', 'poll'.
        limit: Number of results (max 50).
    """
    return await _get_json(
        f"{HN_SEARCH}/search",
        {"query": query, "tags": search_type, "hitsPerPage": min(limit, 50)},
    )


@mcp.tool()
async def hn_search_by_date(query: str, limit: int = 10) -> dict:
    """
    Full-text search of Hacker News sorted by date, most recent first.

    Args:
        query: Search terms.
        limit: Number of results (max 50).
    """
    return await _get_json(
        f"{HN_SEARCH}/search_by_date",
        {"query": query, "hitsPerPage": min(limit, 50)},
    )


# ---------------------------------------------------------------------------
# YC Jobs
# ---------------------------------------------------------------------------

@mcp.tool()
async def yc_jobs(limit: int = 20) -> list[dict]:
    """
    Fetch the latest YC startup job postings from Hacker News.

    These are official HN job listings submitted by YC-backed companies.
    Each item contains the job title, company URL, and posting text.
    """
    ids = await _get_json(f"{HN_API}/jobstories.json")
    return await _hn_items(ids, min(limit, 50))


# ---------------------------------------------------------------------------
# YC Companies
# ---------------------------------------------------------------------------

@mcp.tool()
async def yc_search_companies(
    query: str = "",
    batch: str = "",
    limit: int = 20,
) -> dict:
    """
    Search YC-funded companies from ycombinator.com.

    Args:
        query: Filter by company name or one-liner description (case-insensitive).
        batch: Filter by YC batch cohort, e.g. 'W24', 'S23', 'W22', 'S21'.
               Leave blank to search all batches.
        limit: Maximum companies to return (max 100).

    Returns a dict with 'total_matched', 'returned', and 'companies' list.
    Each company includes name, one_liner, batch, website, tags, and description.
    """
    companies = await _yc_page_companies()

    if not companies:
        return {
            "error": (
                "Could not load YC companies data from ycombinator.com. "
                "The page may have changed structure. "
                "Try hn_search with your company name as a fallback."
            ),
            "companies": [],
        }

    if query:
        q = query.lower()
        companies = [
            c for c in companies
            if q in (c.get("name") or "").lower()
            or q in (c.get("one_liner") or "").lower()
            or q in (c.get("long_description") or "").lower()
        ]

    if batch:
        b = batch.upper()
        companies = [c for c in companies if (c.get("batch") or "").upper() == b]

    total = len(companies)
    return {
        "total_matched": total,
        "returned": min(total, limit),
        "companies": companies[:limit],
    }


@mcp.tool()
async def yc_get_company(identifier: str) -> dict:
    """
    Look up a specific YC company by its slug or exact name.

    Args:
        identifier: The company slug (e.g. 'airbnb') or exact name (e.g. 'Airbnb').

    Returns the full company record including batch, description, tags, and website.
    """
    companies = await _yc_page_companies()
    key = identifier.lower().strip()

    for c in companies:
        if (c.get("slug") or "").lower() == key:
            return c
        if (c.get("name") or "").lower() == key:
            return c

    return {"error": f"Company '{identifier}' not found in YC directory"}


# ---------------------------------------------------------------------------
# YC Batches
# ---------------------------------------------------------------------------

@mcp.tool()
async def yc_list_batches() -> list[str]:
    """
    List all YC batch cohorts in reverse chronological order.

    Returns strings like ['W24', 'S24', 'W23', 'S23', ...].
    Each batch label starts with W (Winter/January) or S (Summer/June).
    """
    companies = await _yc_page_companies()
    batches = sorted(
        {c.get("batch", "") for c in companies if c.get("batch")},
        reverse=True,
    )
    return batches or ["Could not fetch batch list — ycombinator.com page structure may have changed"]


@mcp.tool()
async def yc_batch_companies(batch: str, limit: int = 50) -> dict:
    """
    Get all companies from a specific YC batch.

    Args:
        batch: Batch label, e.g. 'W24', 'S23', 'W22'.
        limit: Maximum companies to return (max 200).
    """
    companies = await _yc_page_companies()
    b = batch.upper()
    matched = [c for c in companies if (c.get("batch") or "").upper() == b]

    return {
        "batch": b,
        "total": len(matched),
        "returned": min(len(matched), limit),
        "companies": matched[:limit],
    }


if __name__ == "__main__":
    import os
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.routing import Mount, Route

    transport = "sse"
    port = int(os.environ.get("PORT", 8000))

    if transport == "sse":
        sse = SseServerTransport("/messages/")
        _server = getattr(mcp, "_mcp_server", None) or getattr(mcp, "_server", None)

        async def handle_sse(request: Request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await _server.run(
                    streams[0], streams[1],
                    _server.create_initialization_options(),
                )

        app = Starlette(routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ])

        uvicorn.run(app, host="0.0.0.0", port=port)
    else:
        mcp.run()
