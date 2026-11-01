# -*- coding: utf-8 -*-
"""集换社结算页邮费解析与按卖家缓存（钱只读，不付款）。"""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from xml.etree import ElementTree as ET

REGION = "华东"
CACHE_SECONDS = 7 * 24 * 3600
BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _nodes(xml: str):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    result = []
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        if not text:
            continue
        match = BOUNDS_RE.match(node.get("bounds") or "")
        center = None
        if match:
            x1, y1, x2, y2 = map(int, match.groups())
            center = ((x1 + x2) // 2, (y1 + y2) // 2)
        result.append({"text": text, "center": center})
    return result


def ui_nodes(xml: str):
    return [node for node in _nodes(xml) if node.get("center")]


def parse_checkout(xml: str):
    """从“提交订单”页取邮费和收件地址；找不到就返回 None/空串。"""
    nodes = _nodes(xml)
    fee = None
    address = ""
    for index, node in enumerate(nodes):
        text = node["text"]
        if text == "邮费" or text.startswith("邮费"):
            for after in nodes[index + 1:index + 6]:
                numbers = re.findall(r"\d+(?:\.\d+)?", after["text"].replace(",", ""))
                if numbers:
                    fee = float(numbers[-1])
                    break
        if "收件地址" in text:
            parts = []
            for after in nodes[index + 1:index + 5]:
                value = after["text"]
                if value and not value.startswith("137") and "收货" not in value:
                    parts.append(value)
            address = " ".join(parts)[:120]
    return {"shipping": fee, "address": address, "region": REGION}


class ShippingCache:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS shipping_cache (
                seller TEXT NOT NULL,
                region TEXT NOT NULL,
                fee REAL NOT NULL,
                address TEXT DEFAULT '',
                checked_at REAL NOT NULL,
                PRIMARY KEY (seller, region)
            )""")
            conn.commit()

    def get(self, seller: str, now: float | None = None, region: str = REGION):
        now = time.time() if now is None else float(now)
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            row = conn.execute(
                "SELECT fee,address,checked_at FROM shipping_cache WHERE seller=? AND region=?",
                (str(seller), region)).fetchone()
        if not row or now - float(row[2]) > CACHE_SECONDS:
            return None
        return {"fee": float(row[0]), "address": row[1] or "", "checked_at": float(row[2])}

    def put(self, seller: str, fee: float, address: str = "", now: float | None = None,
            region: str = REGION):
        now = time.time() if now is None else float(now)
        with closing(sqlite3.connect(self.path, timeout=5)) as conn:
            conn.execute(
                "INSERT INTO shipping_cache (seller,region,fee,address,checked_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(seller,region) DO UPDATE SET fee=excluded.fee,"
                "address=excluded.address,checked_at=excluded.checked_at",
                (str(seller), region, float(fee), address or "", now))
            conn.commit()


def landed_price(lowest, seller, cache, now=None, region=REGION):
    """最低商品价 + 该卖家缓存邮费；邮费未知时 total=None，绝不冒充含邮费。"""
    if lowest is None:
        return None
    cached = cache.get(seller, now=now, region=region) if seller else None
    if not cached:
        return {"lowest": float(lowest), "shipping": None, "total": None,
                "known": False, "address": "", "region": region}
    fee = float(cached["fee"])
    return {"lowest": float(lowest), "shipping": fee, "total": float(lowest) + fee,
            "known": True, "address": cached.get("address") or "", "region": region}
