# -*- coding: utf-8 -*-
"""截图 OCR 辅助：Flutter 列表里没有稳定文本节点时用来找卡名/按钮。"""
from __future__ import annotations

import re
import time

_engine = None


def _ocr():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def screenshot(ui):
    return ui.run(["exec-out", "screencap", "-p"], timeout=25, binary=True)


def items(ui):
    raw = screenshot(ui)
    result, _ = _ocr()(raw)
    found = []
    for box, text, score in result or []:
        text = (text or "").strip()
        if not text:
            continue
        xs = [point[0] for point in box]
        ys = [point[1] for point in box]
        found.append(dict(text=text, score=float(score), center=(sum(xs) / len(xs), sum(ys) / len(ys)),
                          box=box))
    return found


def _norm(value):
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value or "")


def tap_name(ui, name, attempts=2):
    """在搜索结果截图里找完全同名/最接近的卡，点开后核对详情页。"""
    wanted = _norm(name)
    for _ in range(attempts):
        rows = items(ui)
        candidates = [row for row in rows if _norm(row["text"]) == wanted
                      and "未拆封" not in row["text"] and "未开封" not in row["text"]]
        if not candidates:
            candidates = [row for row in rows if wanted and wanted in _norm(row["text"])
                          and "未拆封" not in row["text"] and "未开封" not in row["text"]]
        for row in candidates:
            ui.tap(*row["center"])
            time.sleep(3.0)
            focus = ui.current_focus() or ""
            if "CardDetailActivity" in focus:
                try:
                    actual = _norm(ui.read_detail().get("name") or "")
                except Exception:
                    actual = ""
                if actual == wanted:
                    return True
                ui.back()
                time.sleep(1.0)
            elif "SearchActivity" not in focus:
                try:
                    ui.back()
                except Exception:
                    pass
        time.sleep(1.0)
    return False
