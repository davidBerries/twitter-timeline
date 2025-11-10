import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .utils_time import to_iso8601

log = logging.getLogger("twitter_parser")

@dataclass
class TimelineFetcher:
    """
    Fetch public timelines via Nitter HTML with resilient parsing.
    Falls back to a local synthetic generator when network fails.
    """
    nitter_base: str = "https://nitter.net"
    timeout_seconds: float = 20.0
    proxy: Optional[str] = None
    max_posts: int = 50

    def _client(self) -> httpx.AsyncClient:
        proxies = None
        if self.proxy:
            proxies = {"all://": self.proxy}
        return httpx.AsyncClient(
            timeout=self.timeout_seconds,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; TimelineFetcher/1.0; +https://bitbash.dev)"
            },
            proxies=proxies,
            follow_redirects=True,
        )

    @retry(
        reraise=True,
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.8, min=1, max=6),
        retry=retry_if_exception_type(httpx.HTTPError),
    )
    async def _get_html(self, client: httpx.AsyncClient, url: str) -> str:
        res = await client.get(url)
        res.raise_for_status()
        return res.text

    async def fetch_username(self, screen_name: str) -> List[Dict[str, Any]]:
        """
        Attempt strategies in order:
        1) Nitter HTML page parse
        2) Nitter RSS-json endpoint (_format=json) if supported
        3) Synthetic fallback (no network)
        """
        screen_name = screen_name.lstrip("@").strip()
        try:
            async with self._client() as client:
                # Strategy 1: HTML scrape
                url = f"{self.nitter_base.rstrip('/')}/{screen_name}"
                html = await self._get_html(client, url)
                items = self._parse_nitter_html(html, screen_name)
                if items:
                    return items[: self.max_posts]

                # Strategy 2: _format=json (not every instance supports this)
                json_url = f"{self.nitter_base.rstrip('/')}/{screen_name}?_format=json"
                try:
                    txt = await self._get_html(client, json_url)
                    data = json.loads(txt)
                    candidate = self._parse_nitter_json(data, screen_name)
                    if candidate:
                        return candidate[: self.max_posts]
                except Exception as e:
                    log.debug("JSON strategy failed for @%s: %s", screen_name, e)
        except Exception as e:
            log.warning("Network strategies failed for @%s: %s", screen_name, e)

        # Strategy 3: synthetic
        log.info("Using synthetic data for @%s", screen_name)
        return self._synthetic_posts(screen_name)

    # --------- Parsers ---------

    def _parse_nitter_html(self, html: str, screen_name: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        timeline = soup.select("div.timeline > div.timeline-item")
        results: List[Dict[str, Any]] = []
        for item in timeline:
            # Tweet id from links like /user/status/12345
            sid = None
            link = item.select_one("a.tweet-link")
            if link and link.get("href"):
                m = re.search(r"/status/(\d+)", link.get("href"))
                if m:
                    sid = m.group(1)

            # Content
            content = item.select_one(".tweet-content")
            text = content.get_text(" ", strip=True) if content else None

            # Date
            date_node = item.select_one("span.tweet-date > a")
            created_at = date_node.get("title") if date_node else None
            created_at_iso = to_iso8601(created_at) if created_at else None

            # Counts (may be present in aria-label or in count nodes)
            stats = {
                "replies": 0,
                "retweets": 0,
                "quotes": 0,
                "favorites": 0,
                "bookmarks": None,
                "views": None,
            }
            for s in item.select("div.tweet-stats > span"):
                label = s.get("title") or s.get_text("", strip=True)
                if not label:
                    continue
                # e.g., "12 Likes", "3 Retweets", "4 Replies"
                m_count = re.search(r"(\d[\d,\.]*)\s+(\w+)", label)
                if not m_count:
                    continue
                val = int(m_count.group(1).replace(",", "").replace(".", ""))
                kind = m_count.group(2).lower()
                if "like" in kind:
                    stats["favorites"] = val
                elif "retweet" in kind:
                    stats["retweets"] = val
                elif "repl" in kind:
                    stats["replies"] = val
                elif "quote" in kind:
                    stats["quotes"] = val

            # Media (basic)
            media = []
            for img in item.select(".attachments .attachment.image img"):
                if img.get("src"):
                    media.append({"type": "photo", "url": img.get("src")})
            for video in item.select(".attachments .attachment.video"):
                poster = video.get("data-poster")
                if poster:
                    media.append({"type": "video", "url": poster})

            # Author
            avatar = soup.select_one("a.profile-card-avatar img")
            display = soup.select_one("a.profile-card-fullname")
            verified = bool(soup.select_one("span.profile-bio .icon-verified")) or bool(
                soup.select_one(".profile-card .icon-verified")
            )
            author = {
                "rest_id": None,
                "name": display.get_text(strip=True) if display else screen_name,
                "screen_name": screen_name,
                "avatar": avatar.get("src") if avatar and avatar.get("src") else None,
                "blue_verified": verified,
            }

            obj = {
                "tweet_id": sid,
                "created_at": created_at or created_at_iso,
                "text": text,
                "lang": None,
                "views": stats["views"],
                "favorites": stats["favorites"],
                "replies": stats["replies"],
                "retweets": stats["retweets"],
                "quotes": stats["quotes"],
                "bookmarks": stats["bookmarks"],
                "conversation_id": sid,  # best-effort without API
                "media": media,
                "author": author,
            }
            if obj["tweet_id"] or obj["text"]:
                results.append(obj)
            if len(results) >= self.max_posts:
                break
        return results

    def _parse_nitter_json(self, data: Any, screen_name: str) -> List[Dict[str, Any]]:
        # Some instances return a JSON array of tweets with minimal fields
        results: List[Dict[str, Any]] = []
        if isinstance(data, dict) and "statuses" in data:
            items = data.get("statuses") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []

        for tw in items:
            tid = str(tw.get("id")) if tw.get("id") is not None else None
            created_at = tw.get("date") or tw.get("created_at")
            obj = {
                "tweet_id": tid,
                "created_at": created_at or to_iso8601(created_at) if created_at else None,
                "text": tw.get("text"),
                "lang": tw.get("lang"),
                "views": tw.get("views"),
                "favorites": tw.get("likes") or tw.get("favorites") or 0,
                "replies": tw.get("replies") or 0,
                "retweets": tw.get("retweets") or 0,
                "quotes": tw.get("quotes") or 0,
                "bookmarks": tw.get("bookmarks"),
                "conversation_id": tw.get("conversation_id") or tid,
                "media": tw.get("media") or [],
                "author": {
                    "rest_id": tw.get("user", {}).get("id"),
                    "name": tw.get("user", {}).get("name") or screen_name,
                    "screen_name": tw.get("user", {}).get("screen_name") or screen_name,
                    "avatar": tw.get("user", {}).get("profile_image_url"),
                    "blue_verified": bool(tw.get("user", {}).get("verified")),
                },
            }
            results.append(obj)
            if len(results) >= self.max_posts:
                break
        return results

    def _synthetic_posts(self, screen_name: str) -> List[Dict[str, Any]]:
        # Deterministic pseudo data for offline/blocked network scenarios
        base_id = abs(hash(screen_name)) % 10_000_000_000
        out: List[Dict[str, Any]] = []
        for i in range(min(self.max_posts, 10)):
            tid = str(base_id + i)
            obj = {
                "tweet_id": tid,
                "created_at": to_iso8601(None),
                "text": f"SYNTHETIC: Hello from @{screen_name} #{i}",
                "lang": "en",
                "views": str(1000 + i * 7),
                "favorites": 10 + i,
                "replies": i // 2,
                "retweets": i // 3,
                "quotes": i // 5,
                "bookmarks": None,
                "conversation_id": tid,
                "media": [],
                "author": {
                    "rest_id": None,
                    "name": screen_name,
                    "screen_name": screen_name,
                    "avatar": None,
                    "blue_verified": False,
                },
            }
            out.append(obj)
        return out