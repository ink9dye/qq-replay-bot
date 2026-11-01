# -*- coding: utf-8 -*-
"""OCR 主导的集换社查价：截图 + 文本坐标，不依赖 Flutter 节点。"""
from __future__ import annotations

import re
import time


class OcrQuoteError(RuntimeError):
    pass


class OcrQuote:
    def __init__(self, ui, log=None):
        self.ui = ui
        self.log = log or (lambda message: None)
        self._rapid = None

    def _ocr(self):
        if self._rapid is None:
            from rapidocr_onnxruntime import RapidOCR
            self._rapid = RapidOCR()
        return self._rapid

    def _items(self):
        raw = self.ui.run(["exec-out", "screencap", "-p"], timeout=25, binary=True)
        result, _ = self._ocr()(raw)
        rows = []
        for box, text, score in result or []:
            text = (text or "").strip()
            if not text:
                continue
            xs = [point[0] for point in box]
            ys = [point[1] for point in box]
            rows.append(dict(text=text, score=float(score),
                             center=(sum(xs) / len(xs), sum(ys) / len(ys))))
        return rows

    @staticmethod
    def _norm(value):
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value or "")

    def _find(self, text, contains=False, exclude=()):
        wanted = self._norm(text)
        hits = []
        for row in self._items():
            value = self._norm(row["text"])
            if any(bad in row["text"] for bad in exclude):
                continue
            if (value == wanted) if not contains else (wanted and wanted in value):
                hits.append(row)
        return hits

    def _tap(self, row):
        self.ui.tap(*row["center"])

    def _focus(self):
        return self.ui.current_focus() or ""

    def _wake(self):
        self.ui.run(["shell", "input", "keyevent", "224"], check=False)
        self.ui.run(["shell", "svc", "power", "stayon", "true"], check=False)
        time.sleep(0.5)

    def _reset(self):
        self._wake()
        self.ui.run(["shell", "am", "force-stop", "com.jihuanshe"], check=False)
        time.sleep(1.0)
        self.ui.run(["shell", "am", "start", "-S", "--activity-clear-task", "-n",
                     "com.jihuanshe/com.jihuanshe.ui.page.main.MainActivity"], check=False)
        time.sleep(6.0)
        for _ in range(8):
            focus = self._focus().lower()
            if "mainactivity" in focus:
                break
            if "com.jihuanshe" in focus:
                self.ui.back()
            else:
                self.ui.run(["shell", "am", "start", "-S", "--activity-clear-task", "-n",
                             "com.jihuanshe/com.jihuanshe.ui.page.main.MainActivity"], check=False)
            time.sleep(1.0)
        if "chrom" in self._focus().lower():
            self.ui.back()
            time.sleep(1.0)

    def _open_search(self):
        self._reset()
        for _ in range(4):
            rows = self._items()
            hit = next((row for row in rows if "同音字" in row["text"] or "拼音" in row["text"]), None)
            if hit is None:
                hit = next((row for row in rows if "搜索" in row["text"]), None)
            if hit is not None:
                self._tap(hit)
            else:
                # 首页搜索框固定在顶部中间
                self.ui.tap(540, 95)
            time.sleep(1.8)
            if "SearchActivity" in self._focus():
                return
            if "chrom" in self._focus().lower():
                self.ui.back()
                time.sleep(1.0)
        raise OcrQuoteError("没有进入集换社搜索页")

    def _search(self, name):
        # 清空：搜索框右侧的 ×
        for _ in range(2):
            self.ui.tap(844, 94)
            time.sleep(0.6)
        typed = False
        for _ in range(3):
            self.ui.tap(300, 94)
            time.sleep(0.3)
            self.ui.tap(844, 94)
            time.sleep(0.6)
            self.ui.tap(300, 94)
            time.sleep(0.3)
            self.ui.key(123)
            for _ in range(40):
                self.ui.key(67)
            self.ui.text(name)
            time.sleep(0.8)
            top = [row["text"] for row in self._items()
                   if 50 <= row["center"][1] <= 120 and "取消" not in row["text"]]
            if any(self._norm(value) == self._norm(name) for value in top):
                typed = True
                break
        if not typed:
            raise OcrQuoteError("搜索框没有输入目标卡名")
        self.ui.key(66)
        time.sleep(2.5)
        wanted = self._norm(name)
        for _ in range(10):
            time.sleep(1.2)
            rows = self._items()
            text = " ".join(row["text"] for row in rows)
            if any(wanted and wanted in self._norm(row["text"]) for row in rows):
                return
            if "搜索历史" in text:
                self.ui.key(66)
        raise OcrQuoteError("结果页没有出现目标卡名")

    def _open_card(self, name):
        wanted = self._norm(name)
        for _ in range(3):
            rows = self._items()
            exact = [row for row in rows if self._norm(row["text"]) == wanted
                     and "未拆封" not in row["text"] and "未开封" not in row["text"]]
            near = [row for row in rows if wanted and wanted in self._norm(row["text"])
                    and "未拆封" not in row["text"] and "未开封" not in row["text"]]
            for row in exact + near:
                self._tap(row)
                time.sleep(3.0)
                if "CardDetailActivity" not in self._focus():
                    if "SearchActivity" not in self._focus():
                        self.ui.back()
                        time.sleep(0.8)
                    continue
                detail = self._items()
                title = any(self._norm(item["text"]) == wanted for item in detail)
                if title:
                    return
                self.ui.back()
                time.sleep(0.8)
            time.sleep(1.0)
        raise OcrQuoteError("没能打开同名卡片")

    def _choose_rarity(self, rarity):
        wanted = (rarity or "").strip().upper()
        if not wanted:
            return
        for _ in range(3):
            rows = self._items()
            button = next((row for row in rows if "共有" in row["text"] or "切换版本" in row["text"]), None)
            if button is None:
                return
            self._tap(button)
            time.sleep(2.0)
            for _ in range(10):
                rows = self._items()
                hit = next((row for row in rows if row["text"].strip().upper() == wanted), None)
                if hit is not None:
                    self._tap(hit)
                    time.sleep(2.5)
                    return
                self.ui.swipe(540, 1500, 540, 700)
                time.sleep(1.0)
        raise OcrQuoteError("没有找到目标罕贵")

    @staticmethod
    def _money(row):
        match = re.search(r"\d+(?:\.\d+)?", row["text"].replace(",", ""))
        return float(match.group()) if match else None

    def _prices(self):
        rows = self._items()
        result = {}
        for row in rows:
            text = row["text"]
            if "集换价" in text:
                target_y = row["center"][1]
                candidates = [other for other in rows if abs(other["center"][1] - target_y) <= 40
                              and self._money(other) is not None]
                if candidates:
                    chosen = min(candidates, key=lambda item: abs(item["center"][0] - row["center"][0]))
                    result["price"] = self._money(chosen)
            if "最低价" in text:
                target_y = row["center"][1]
                candidates = [other for other in rows if abs(other["center"][1] - target_y) <= 70
                              and self._money(other) is not None]
                if candidates:
                    chosen = min(candidates, key=lambda item: abs(item["center"][0] - row["center"][0]))
                    result["lowest"] = self._money(chosen)
        return result

    def quote(self, name, rarity="", language="日文"):
        try:
            self._open_search()
            self._search(name)
            self._open_card(name)
            self._choose_rarity(rarity)
            prices = self._prices()
            if prices.get("price") is None:
                try:
                    detail = self.ui.read_detail()
                    prices.setdefault("price", detail.get("price"))
                    prices.setdefault("lowest", detail.get("lowest"))
                except Exception:
                    pass
            if prices.get("price") is None:
                raise OcrQuoteError("无法读取集换价")
            return dict(name=name, rarity=rarity, language=language,
                        price=prices.get("price"), lowest=prices.get("lowest"),
                        versions=[])
        finally:
            pass
