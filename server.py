"""
FoundersMail MCP Server
-----------------------
Standalone MCP server — deploy on Railway, Render, or any VPS.
Connects directly to the Neon PostgreSQL database.

Local test:
    pip install -r requirements.txt
    python server.py

Deployed (Railway sets PORT automatically):
    MCP_TRANSPORT=sse DATABASE_URL=<neon-url> python server.py

Your MCP URL:  https://<your-host>/sse
"""

import json
import re
import os
from datetime import datetime, timedelta

import psycopg2
import psycopg2.extras
from mcp.server.fastmcp import FastMCP

# ── config ─────────────────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is not set.")

mcp = FastMCP("FoundersMail CMS")


# ── helpers ────────────────────────────────────────────────────────────────────

def _conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")

def _fmt(dt):
    return dt.isoformat() if dt else None


# ── Landing page tools ─────────────────────────────────────────────────────────

@mcp.tool()
def list_pages(type: str = "all") -> str:
    """
    List pages on the site.

    Args:
        type: "landing" | "blog" | "all"  (default: "all")

    Returns JSON list with id, slug, title, status, url, created_at for each page.
    """
    rows = []
    with _conn() as conn:
        with conn.cursor() as cur:
            if type in ("landing", "all"):
                cur.execute("""
                    SELECT id, slug, title, status, created_at, updated_at
                    FROM landing_page ORDER BY created_at DESC
                """)
                for r in cur.fetchall():
                    rows.append({
                        **dict(r),
                        "type": "landing",
                        "url": f"/p/{r['slug']}",
                        "created_at": _fmt(r["created_at"]),
                        "updated_at": _fmt(r["updated_at"]),
                    })
            if type in ("blog", "all"):
                cur.execute("""
                    SELECT id, slug, title, published AS status, created_at, updated_at
                    FROM blog_post ORDER BY created_at DESC
                """)
                for r in cur.fetchall():
                    rows.append({
                        **dict(r),
                        "type": "blog",
                        "status": "published" if r["status"] else "draft",
                        "url": f"/blog/{r['slug']}",
                        "created_at": _fmt(r["created_at"]),
                        "updated_at": _fmt(r["updated_at"]),
                    })
    return json.dumps(rows, indent=2)


@mcp.tool()
def get_page(slug: str, type: str = "landing") -> str:
    """
    Get the full content of a page.

    Args:
        slug: page slug, e.g. "why-founders-love-us"
        type: "landing" | "blog"
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            table = "landing_page" if type == "landing" else "blog_post"
            cur.execute(f"SELECT * FROM {table} WHERE slug = %s", (slug,))
            row = cur.fetchone()
    if not row:
        return json.dumps({"error": f"{type} page '{slug}' not found"})
    result = dict(row)
    for k in ("created_at", "updated_at"):
        if k in result:
            result[k] = _fmt(result[k])
    return json.dumps(result, indent=2)


@mcp.tool()
def create_landing_page(
    title: str,
    content_html: str,
    slug: str = "",
    meta_title: str = "",
    meta_description: str = "",
    og_image: str = "",
    status: str = "draft",
) -> str:
    """
    Create a new landing page on FoundersMail.

    Args:
        title:            Page heading (required)
        content_html:     Full HTML body — include hero, CTAs, sections.
                          Use Tailwind CSS classes for styling.
        slug:             URL path, e.g. "why-founders-love-us" (auto-generated if blank)
        meta_title:       SEO <title> tag (defaults to title)
        meta_description: SEO meta description, max 160 chars
        og_image:         Open Graph image URL
        status:           "draft" (default) | "published"
    """
    if not slug:
        slug = _slugify(title)
    if not meta_title:
        meta_title = title
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM landing_page WHERE slug = %s", (slug,))
            if cur.fetchone():
                return json.dumps({"error": f"Slug '{slug}' already exists. Pick a different slug."})
            cur.execute("""
                INSERT INTO landing_page
                    (slug, title, content_html, meta_title, meta_description, og_image, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (slug, title, content_html, meta_title, meta_description, og_image, status))
            page_id = cur.fetchone()["id"]
        conn.commit()
    return json.dumps({"ok": True, "id": page_id, "slug": slug,
                       "url": f"/p/{slug}", "status": status})


@mcp.tool()
def update_landing_page(
    slug: str,
    title: str = None,
    content_html: str = None,
    new_slug: str = None,
    meta_title: str = None,
    meta_description: str = None,
    og_image: str = None,
    status: str = None,
) -> str:
    """
    Update fields on an existing landing page. Only provided fields are changed.

    Args:
        slug:             Current slug of the page to update (required)
        title, content_html, new_slug, meta_title, meta_description, og_image, status: fields to update
    """
    updates = {}
    if title            is not None: updates["title"]            = title
    if content_html     is not None: updates["content_html"]     = content_html
    if new_slug         is not None: updates["slug"]             = new_slug
    if meta_title       is not None: updates["meta_title"]       = meta_title
    if meta_description is not None: updates["meta_description"] = meta_description
    if og_image         is not None: updates["og_image"]         = og_image
    if status           is not None: updates["status"]           = status
    if not updates:
        return json.dumps({"error": "No fields to update."})
    updates["updated_at"] = datetime.utcnow()
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [slug]
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE landing_page SET {set_clause} WHERE slug = %s RETURNING id",
                values,
            )
            if not cur.fetchone():
                return json.dumps({"error": f"Page '{slug}' not found."})
        conn.commit()
    final_slug = updates.get("slug", slug)
    return json.dumps({"ok": True, "slug": final_slug, "url": f"/p/{final_slug}"})


@mcp.tool()
def delete_landing_page(slug: str) -> str:
    """
    Permanently delete a landing page.

    Args:
        slug: slug of the page to delete
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM landing_page WHERE slug = %s RETURNING id", (slug,))
            if not cur.fetchone():
                return json.dumps({"error": f"Page '{slug}' not found."})
        conn.commit()
    return json.dumps({"ok": True, "deleted_slug": slug})


# ── Blog post tools ────────────────────────────────────────────────────────────

@mcp.tool()
def create_blog_post(
    title: str,
    content: str,
    slug: str = "",
    meta_title: str = "",
    meta_description: str = "",
    featured_image: str = "",
    published: bool = False,
) -> str:
    """
    Create a new blog post.

    Args:
        title:            Post title (required)
        content:          Post body HTML (required)
        slug:             URL slug (auto-generated if blank)
        meta_title:       SEO title (defaults to title)
        meta_description: SEO meta description
        featured_image:   Cover image URL
        published:        True to publish immediately, False for draft
    """
    if not slug:
        slug = _slugify(title)
    if not meta_title:
        meta_title = title
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM blog_post WHERE slug = %s", (slug,))
            if cur.fetchone():
                return json.dumps({"error": f"Slug '{slug}' already exists."})
            cur.execute("""
                INSERT INTO blog_post
                    (title, slug, content, meta_title, meta_description, featured_image, published)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (title, slug, content, meta_title, meta_description, featured_image, published))
            post_id = cur.fetchone()["id"]
        conn.commit()
    return json.dumps({"ok": True, "id": post_id, "slug": slug,
                       "url": f"/blog/{slug}", "published": published})


@mcp.tool()
def update_blog_post(
    slug: str,
    title: str = None,
    content: str = None,
    new_slug: str = None,
    meta_title: str = None,
    meta_description: str = None,
    featured_image: str = None,
    published: bool = None,
) -> str:
    """
    Update an existing blog post. Only provided fields are changed.

    Args:
        slug: current slug of the post (required)
        title, content, new_slug, meta_title, meta_description, featured_image, published: fields to update
    """
    updates = {}
    if title            is not None: updates["title"]            = title
    if content          is not None: updates["content"]          = content
    if new_slug         is not None: updates["slug"]             = new_slug
    if meta_title       is not None: updates["meta_title"]       = meta_title
    if meta_description is not None: updates["meta_description"] = meta_description
    if featured_image   is not None: updates["featured_image"]   = featured_image
    if published        is not None: updates["published"]        = published
    if not updates:
        return json.dumps({"error": "No fields to update."})
    updates["updated_at"] = datetime.utcnow()
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + [slug]
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE blog_post SET {set_clause} WHERE slug = %s RETURNING id",
                values,
            )
            if not cur.fetchone():
                return json.dumps({"error": f"Post '{slug}' not found."})
        conn.commit()
    final_slug = updates.get("slug", slug)
    return json.dumps({"ok": True, "slug": final_slug, "url": f"/blog/{final_slug}"})


@mcp.tool()
def delete_blog_post(slug: str) -> str:
    """
    Permanently delete a blog post.

    Args:
        slug: slug of the post to delete
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM blog_post WHERE slug = %s RETURNING id", (slug,))
            if not cur.fetchone():
                return json.dumps({"error": f"Post '{slug}' not found."})
        conn.commit()
    return json.dumps({"ok": True, "deleted_slug": slug})


# ── Analytics tools ────────────────────────────────────────────────────────────

@mcp.tool()
def get_page_analytics(slug: str, type: str = "landing", days: int = 30) -> str:
    """
    Get detailed analytics for a landing page or blog post.

    Args:
        slug: page slug
        type: "landing" | "blog"
        days: lookback window in days (default 30)
    """
    url   = f"/p/{slug}" if type == "landing" else f"/blog/{slug}"
    since = datetime.utcnow() - timedelta(days=days)

    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*)                                        AS total_views,
                    COUNT(DISTINCT session_id)                      AS unique_visitors,
                    ROUND(AVG(NULLIF(time_spent, 0))::numeric, 1)  AS avg_time_seconds
                FROM page_view
                WHERE page = %s AND visited_at >= %s
            """, (url, since))
            summary = dict(cur.fetchone())

            cur.execute("""
                SELECT source, COUNT(*) AS cnt
                FROM page_view
                WHERE page = %s AND visited_at >= %s
                GROUP BY source ORDER BY cnt DESC
            """, (url, since))
            sources = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT DATE(visited_at) AS date,
                       COUNT(*) AS views,
                       COUNT(DISTINCT session_id) AS unique_visitors
                FROM page_view
                WHERE page = %s AND visited_at >= %s
                GROUP BY DATE(visited_at) ORDER BY date
            """, (url, since))
            daily = [{"date": str(r["date"]), "views": r["views"],
                      "unique_visitors": r["unique_visitors"]} for r in cur.fetchall()]

            cur.execute("""
                SELECT referrer, COUNT(*) AS cnt
                FROM page_view
                WHERE page = %s AND visited_at >= %s
                  AND referrer IS NOT NULL AND referrer != ''
                GROUP BY referrer ORDER BY cnt DESC LIMIT 10
            """, (url, since))
            referrers = [dict(r) for r in cur.fetchall()]

    return json.dumps({
        "slug": slug, "type": type, "url": url, "days": days,
        "total_views":      int(summary.get("total_views") or 0),
        "unique_visitors":  int(summary.get("unique_visitors") or 0),
        "avg_time_seconds": float(summary.get("avg_time_seconds") or 0),
        "sources":   sources,
        "referrers": referrers,
        "daily":     daily,
    }, indent=2)


@mcp.tool()
def get_all_pages_analytics(days: int = 30) -> str:
    """
    Traffic summary for every landing page and blog post side by side.

    Args:
        days: lookback window (default 30)
    """
    since = datetime.utcnow() - timedelta(days=days)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    pv.page,
                    COUNT(*)                                       AS views,
                    COUNT(DISTINCT session_id)                     AS unique_visitors,
                    ROUND(AVG(NULLIF(time_spent, 0))::numeric, 1) AS avg_time
                FROM page_view pv
                WHERE visited_at >= %s
                  AND (pv.page LIKE '/p/%%' OR pv.page LIKE '/blog/%%')
                GROUP BY pv.page
                ORDER BY views DESC
            """, (since,))
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if r.get("avg_time"):
            r["avg_time"] = float(r["avg_time"])
    return json.dumps({"days": days, "pages": rows}, indent=2)


# ── entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    from starlette.requests import Request

    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    port = int(os.environ.get("PORT", 8000))

    if transport == "sse":
        # Build the Starlette SSE app manually — avoids FastMCP's TrustedHostMiddleware
        # which blocks Railway/Render proxy Host headers
        sse = SseServerTransport("/messages/")
        # FastMCP stores the underlying Server as _mcp_server (newer) or _server (older)
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
