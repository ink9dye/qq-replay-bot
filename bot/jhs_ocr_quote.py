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
        for _ in range(3):
            focus = self._focus().lower()
            if "mainactivity" in focus:
                break
            self.ui.run(["shell", "am", "start", "-S", "--activity-clear-task", "-n",
                         "com.jihuanshe/com.jihuanshe.ui.page.main.MainActivity"], check=False)
            time.sleep(2.5)
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
        try:
            try:
                self.ui.open_search()
            except Exception:
                pass
            typed = False
            for _ in range(3):
                self.ui.tap(300, 94)
                time.sleep(0.4)
                self.ui.tap(844, 94)
                time.sleep(0.5)
                self.ui.tap(300, 94)
                time.sleep(0.4)
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
        except Exception as exc:
            raise OcrQuoteError("搜索输入失败：%s" % exc)
        time.sleep(2.0)
        rows = self._items()
        tab = next((row for row in rows if row["text"].strip() == "商品"), None)
        if tab is not None:
            self._tap(tab)
            time.sleep(2.0)
            rows = self._items()
        if "SearchActivity" not in self._focus():
            raise OcrQuoteError("结果页已离开")
        if any("搜索历史" in row["text"] for row in rows):
            try:
                self.ui.type_search(name)
            except Exception:
                pass
            time.sleep(2.0)
            rows = self._items()
        if any("搜索历史" in row["text"] for row in rows):
            raise OcrQuoteError("搜索结果仍是历史页")
        if any(self._norm(row["text"]) == self._norm(name) and row["center"][1] > 300 for row in rows):
            return
        # 再给页面 2 秒；仍没有完全同名卡就失败，不点用户/店铺
        time.sleep(2.0)
        rows = self._items()
        if any(self._norm(row["text"]) == self._norm(name) and row["center"][1] > 300 for row in rows):
            return
        raise OcrQuoteError("商品结果里没有完全同名卡")

    def _open_card(self, name):
        wanted = self._norm(name)
        for _ in range(3):
            rows = self._items()
            exact = [row for row in rows if self._norm(row["text"]) == wanted
                     and row["center"][1] > 300
                     and "未拆封" not in row["text"] and "未开封" not in row["text"]]
            near = [row for row in rows if wanted and wanted in self._norm(row["text"])
                    and "未拆封" not in row["text"] and "未开封" not in row["text"]]
            for row in exact + near:
                below = [other for other in rows
                         if row["center"][1] < other["center"][1] <= row["center"][1] + 260
                         and ("¥" in other["text"] or "￥" in other["text"] or self._money(other) is not None)]
                target = min(below, key=lambda other: other["center"][1]) if below else row
                self._tap(target)
                time.sleep(3.0)
                if "CardDetailActivity" not in self._focus():
                    raise OcrQuoteError("点击结果后没有进入卡片详情")
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
        if "CardDetailActivity" not in self._focus():
            raise OcrQuoteError("不在卡片详情页，无法选择版本")
        if not self.ui.select_version(wanted):
            raise OcrQuoteError("没有找到目标罕贵")
        time.sleep(1.0)
        if "CardDetailActivity" not in self._focus():
            raise OcrQuoteError("切换版本后离开详情页")

    @staticmethod
    def _money(row):
        text = row["text"].replace(",", "")
        match = re.search(r"[¥￥]\s*(\d+(?:\.\d+)?)", text)
        if match:
            return float(match.group(1))
        values = re.findall(r"\d+(?:\.\d+)?", text)
        return float(values[-1]) if values else None

    def _prices(self):
        if "CardDetailActivity" not in self._focus():
            raise OcrQuoteError("不在卡片详情页，不能读价")
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
        last = None
        for attempt in range(2):
            try:
                return self._quote_once(name, rarity, language)
            except Exception as exc:
                last = exc
                self.log(f"OCR 第 {attempt + 1} 次失败：{exc}")
                time.sleep(2)
        raise last

    def _quote_once(self, name, rarity, language):
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
