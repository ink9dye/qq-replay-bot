"""Road of the King OCG chart, fetched only on command; no scheduler or game facts."""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, ProxyHandler, build_opener

from .card_search import CardReply

HOME = "https://roadoftheking.com/"
TTL = 6 * 3600
RETRY_DELAY = 300
_FETCH_LOCK = threading.Lock()


def source_url(value):
    url = urljoin(HOME, value)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "roadoftheking.com":
        raise ValueError("非来源站地址")
    return url


class Page(HTMLParser):
    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.links, self.images, self.meta, self.paragraphs = [], [], {}, []
        self.anchor = None
        self.paragraph = None
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.anchor = [attrs.get("href", ""), ""]
        elif tag == "img":
            self.images.extend(attrs.get(key, "") for key in ("src", "data-src"))
        elif tag == "meta":
            self.meta[attrs.get("property", attrs.get("name", ""))] = attrs.get("content", "")
        elif tag == "p":
            self.paragraph = ""

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor[1] += data
        if self.paragraph is not None:
            self.paragraph += data

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            self.links.append(tuple(self.anchor))
            self.anchor = None
        elif tag == "p" and self.paragraph is not None:
            self.paragraphs.append(" ".join(self.paragraph.split()))
            self.paragraph = None


def latest_report(content):
    for url, title in Page(content).links:
        title = " ".join(title.split())
        if title.casefold().startswith("ocg ") and "metagame report" in title.casefold():
            return source_url(url), title
    raise ValueError("来源首页未找到OCG环境报告")


def report_details(content):
    page = Page(content)
    for value in page.images:
        # Select the chart itself, excluding covers, sample deck lists and thumbnails.
        if not re.search(r"(?:^|[-_])pie(?:[-_.]|$)", urlsplit(value).path.rsplit("/", 1)[-1], re.I):
            continue
        url = source_url(re.sub(r"-\d+x\d+(?=\.[a-z]+(?:\?|$))", "", value, flags=re.I))
        if "/wp-content/uploads/" not in urlsplit(url).path:
            continue
        coverage = next((text for text in page.paragraphs if "this report tabulates" in text.casefold()), "")
        return {
            "image_url": url,
            "published": page.meta.get("article:published_time", "")[:10],
            "coverage": coverage[:800],
        }
    raise ValueError("报告中未找到饼图，保留原缓存")


def download(url, limit):
    request = Request(source_url(url), headers={"User-Agent": "Mozilla/5.0 (compatible; YGOStatistics/1.0)"})
    # This source is public HTTPS; retain certificate verification, no proxy dependency.
    with build_opener(ProxyHandler({})).open(request, timeout=12) as response:
        source_url(response.geturl())
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ValueError("来源内容过大")
        return body


class OcgPie:
    def __init__(self, data_dir, *, fetch=download, clock=time.time):
        self.path = Path(data_dir) / "cache" / "ocg-pie.json"
        self.fetch, self.clock = fetch, clock
        self.retry_at = 0

    def cached(self):
        try:
            if self.path.stat().st_size > 3_000_000:
                return None
            data = json.loads(self.path.read_text(encoding="utf-8"))
            blob = base64.b64decode(data["image_base64"], validate=True)
            if not blob.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
                return None
            source_url(data["url"])
            float(data["checked_at"])
            if not isinstance(data["title"], str):
                return None
            return data
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)

    def reply(self):
        data = self.cached()
        now = self.clock()
        if data and 0 <= now - float(data["checked_at"]) < TTL:
            return self.format(data)
        if now < self.retry_at:
            return self.format(data, stale=True) if data else "OCG饼图来源暂时不可用，请几分钟后再试。"
        if not _FETCH_LOCK.acquire(blocking=False):
            return self.format(data, stale=True) if data else "OCG饼图正在获取中，请稍后再发“OCG饼图”。"
        try:
            # Another request may have refreshed the cache before this lock was acquired.
            data = self.cached()
            if data and 0 <= self.clock() - float(data["checked_at"]) < TTL:
                return self.format(data)
            url, title = latest_report(self.fetch(HOME, 2_000_000).decode("utf-8"))
            if data and data["url"] == url:
                fresh = dict(data, title=title, checked_at=self.clock())
            else:
                details = report_details(self.fetch(url, 2_000_000).decode("utf-8"))
                image = self.fetch(details["image_url"], 2_000_000)
                if not image.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
                    raise ValueError("来源返回的不是饼图图片")
                fresh = dict(details, title=title, url=url, checked_at=self.clock(),
                             image_base64=base64.b64encode(image).decode("ascii"))
            self.save(fresh)
            return self.format(fresh)
        except Exception:
            self.retry_at = self.clock() + RETRY_DELAY
            return self.format(data, stale=True) if data else "OCG饼图获取失败，请几分钟后再试。"
        finally:
            _FETCH_LOCK.release()

    @staticmethod
    def format(data, stale=False):
        lines = ["OCG赛事环境饼图｜Road of the King", data["title"]]
        if data.get("published"):
            lines.append("报告发布日期：" + data["published"])
        coverage = data.get("coverage", "")
        match = re.search(r"tabulates\s+(\d+).*?from\s+(\d+)\s+tournaments", coverage, re.I)
        if match:
            lines.append(f"样本：{match[2]}场赛事、{match[1]}份上位构筑")
        period = re.search(r"during\s+(.+?)(?:\.$|$)", coverage, re.I)
        if period:
            lines.append("统计时间：" + period[1])
        elif coverage:
            lines.append("统计范围：" + coverage)
        lines.append("外部赛事上位样本；与本机器人萌卡前百统计分开。")
        if stale:
            lines.append("最新报告暂未获取，以下为上次成功保存的饼图。")
        lines.append("原文：" + data["url"])
        return CardReply("\n".join(lines), "base64://" + data["image_base64"])
