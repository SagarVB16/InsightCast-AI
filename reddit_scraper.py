import asyncio
import os
import requests
from typing import List
from datetime import datetime, timedelta
from dotenv import load_dotenv
from aiolimiter import AsyncLimiter
from tenacity import retry, stop_after_attempt, wait_exponential
from langchain_groq import ChatGroq
from langchain_core.messages import SystemMessage, HumanMessage

load_dotenv()

# ── Date filter ───────────────────────────────────────────────────────────────
two_weeks_ago     = datetime.today() - timedelta(days=14)
two_weeks_ago_str = two_weeks_ago.strftime('%Y-%m-%d')

# ── Rate limiter ──────────────────────────────────────────────────────────────
rate_limiter = AsyncLimiter(5, 1)   # max 5 requests / second

FIRECRAWL_BASE = "https://api.firecrawl.dev/v2"


# ─────────────────────────────────────────────────────────────────────────────
# STEP 1 — Search Reddit via Firecrawl Search API
# Firecrawl queries Google/Bing for site:reddit.com results (not blocked)
# ─────────────────────────────────────────────────────────────────────────────
def search_reddit(topic: str, limit: int = 2) -> list[dict]:
    """
    Search for Reddit posts about a topic using Firecrawl's /v2/search.
    Firecrawl queries Google/Bing for site:reddit.com results and returns
    matching URLs with titles and descriptions.

    Returns a list of dicts: {url, title, description}
    """
    api_key = os.getenv("FIRECRAWL_API_KEY")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "query"          : f'site:reddit.com "{topic}" after:{two_weeks_ago_str}',
        "limit"          : limit,
        "includeDomains" : ["reddit.com"]
    }

    resp = requests.post(f"{FIRECRAWL_BASE}/search", json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success"):
        raise RuntimeError(f"Firecrawl search error: {data.get('error', 'Unknown')}")

    # Response: {"success": true, "data": {"web": [{url, title, description}, ...]}}
    search_data = data.get("data", {})
    if isinstance(search_data, dict):
        return search_data.get("web", [])
    return search_data or []


# ─────────────────────────────────────────────────────────────────────────────
# STEP 2 — Scrape each Reddit post via RSS feed
# Fetches the post body + comments as XML, no API keys needed
# ───────────────────────────────────────────────────────────────────────────────────
def scrape_reddit_url(url: str) -> str:
    """
    Scrape a Reddit post and its comments using Reddit's RSS feed (.rss).
    Bypasses Cloudflare blocks without requiring API keys or headless browsers.
    """
    import xml.etree.ElementTree as ET
    from bs4 import BeautifulSoup

    rss_url = f"{url.rstrip('/')}.rss"
    # Use a standard, clean user agent to prevent rate limiting
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    resp = requests.get(rss_url, headers=headers, timeout=15)
    resp.raise_for_status()
    
    root = ET.fromstring(resp.content)
    ns = {'atom': 'http://www.w3.org/2005/Atom'}
    
    feed_title_elem = root.find('atom:title', ns)
    feed_title = feed_title_elem.text if feed_title_elem is not None else "Reddit Thread"
    
    entries = root.findall('atom:entry', ns)
    if not entries:
        return "No content found in Reddit RSS feed."
        
    # The first entry in the RSS feed represents the original post submission
    post_entry = entries[0]
    post_author_elem = post_entry.find('atom:author/atom:name', ns)
    post_author = post_author_elem.text if post_author_elem is not None else "[Unknown]"
    
    post_content_elem = post_entry.find('atom:content', ns)
    post_content = ""
    if post_content_elem is not None and post_content_elem.text:
        soup = BeautifulSoup(post_content_elem.text, "html.parser")
        post_content = soup.get_text().strip()
        
    formatted_text = [
        f"Thread Title: {feed_title}",
        f"Author: {post_author}",
        f"Post Body:\n{post_content}",
        "\n--- Comments ---"
    ]
    
    # Subsequent entries in the feed represent comments
    for entry in entries[1:]:
        author_elem = entry.find('atom:author/atom:name', ns)
        author = author_elem.text if author_elem is not None else "[Unknown]"
        
        content_elem = entry.find('atom:content', ns)
        content_text = ""
        if content_elem is not None and content_elem.text:
            soup = BeautifulSoup(content_elem.text, "html.parser")
            content_text = soup.get_text().strip()
            
        formatted_text.append(f"Comment by {author}:\n{content_text}\n")
        
    return "\n".join(formatted_text)



# ─────────────────────────────────────────────────────────────────────────────
# STEP 3 — Format posts into a clean text block for the LLM
# ─────────────────────────────────────────────────────────────────────────────
def format_posts_for_llm(posts: list[dict]) -> str:
    sections = []
    for i, post in enumerate(posts, 1):
        section = (
            f"POST {i}:\n"
            f"Title: {post['title']}\n"
            f"URL:   {post['url']}\n\n"
            f"Content:\n{post['content']}"
        )
        sections.append(section)
    return "\n\n---\n\n".join(sections)


# ─────────────────────────────────────────────────────────────────────────────
# STEP 4 — Analyse one topic (async, rate-limited, auto-retried)
# ─────────────────────────────────────────────────────────────────────────────
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True
)
async def process_topic(topic: str) -> str:
    async with rate_limiter:
        loop = asyncio.get_event_loop()

        # ── Search Reddit via Firecrawl Search API ─────────────────────────────
        try:
            results = await loop.run_in_executor(
                None, lambda: search_reddit(topic, limit=2)
            )
        except Exception as e:
            return f"Error searching Reddit: {str(e)}"

        if not results:
            return f"No recent Reddit posts found for '{topic}' in the last two weeks."

        # ── Scrape each Reddit URL via RSS feed ───────────────────────────────
        posts = []
        for item in results:
            url         = item.get("url", "")
            title       = item.get("title", "No title")
            description = item.get("description", "")   # fallback if scrape fails
            if not url:
                continue
            try:
                rss_text = await loop.run_in_executor(
                    None, lambda u=url: scrape_reddit_url(u)
                )
                # Trim to keep token count reasonable
                content = rss_text[:4000] if rss_text else description
            except Exception:
                # RSS scraper sometimes can't scrape a page — fall back to description
                content = description or "[Content not available]"

            posts.append({"title": title, "url": url, "content": content})

        if not posts:
            return f"Could not retrieve content from Reddit posts about '{topic}'."

        scraped_content = format_posts_for_llm(posts)

        # ── Groq analysis using the original prompt structure ─────────────────
        messages = [
            SystemMessage(content=(
                f"You are a Reddit analysis expert. Use available tools to:\n"
                f"1. Find top 2 posts about the given topic BUT only after {two_weeks_ago_str}, NOTHING before this date strictly!\n"
                f"2. Analyze their content and sentiment\n"
                f"3. Create a summary of discussions and overall sentiment"
            )),
            HumanMessage(content=(
                f"Analyze Reddit posts about '{topic}'.\n"
                f"Provide a comprehensive summary including:\n"
                f"- Main discussion points\n"
                f"- Key opinions expressed\n"
                f"- Any notable trends or patterns\n"
                f"- Summarize the overall narrative, discussion points and also quote interesting comments without mentioning names\n"
                f"- Overall sentiment (positive/neutral/negative)\n\n"
                f"Here are the Reddit posts and comments to analyze:\n\n"
                f"{scraped_content}"
            ))
        ]

        try:
            llm = ChatGroq(
                model="llama-3.3-70b-versatile",
                api_key=os.getenv("GROQ_API_KEY"),
                temperature=0.3,
                max_tokens=2000
            )
            response = await llm.ainvoke(messages)
            return response.content
        except Exception as e:
            return f"Error generating summary via Groq: {str(e)}"


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API — called by backend.py
# ─────────────────────────────────────────────────────────────────────────────
async def scrape_reddit_topics(topics: List[str]) -> dict:
    """Process a list of topics and return Reddit analysis results."""
    reddit_results = {}
    for topic in topics:
        summary = await process_topic(topic)
        reddit_results[topic] = summary
        await asyncio.sleep(1)   # politeness delay between topics

    return {"reddit_analysis": reddit_results}
