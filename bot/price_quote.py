# -*- coding: utf-8 -*-
"""单张卡查价：集换价（不含邮费）+ 最低价（商品价+邮费，华东）。"""
from __future__ import annotations

from pathlib import Path

from .price_checkout import CheckoutProbe
from .price_shipping import REGION, ShippingCache, landed_price


def name_variants(name):
    """百鸽与集换社的卡名常有“之/的”、空格差异；依次尝试。"""
    values = [name]
    if "之" in name:
        values.append(name.replace("之", "的"))
    if "的" in name:
        values.append(name.replace("的", "之"))
    values.append(name.replace(" ", ""))
    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


import re
from difflib import SequenceMatcher


def normalize_name(value):
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", value or "")


JHS_ALIASES = {"增殖的G": "增殖的Z", "墓穴的指名者": "墓穴指名者", "羽毛扫": "神鹰羽毛扫", "剑指": "抹杀之指名者"}


def jhs_name(name):
    wanted = normalize_name(name)
    for source, target in JHS_ALIASES.items():
        if normalize_name(source) == wanted:
            return target
    return name


def name_score(left, right):
    a, b = normalize_name(left), normalize_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        # “决斗者之力”不能和“决斗者之力未拆封”同分；多出的字要扣分。
        ratio = max(ratio, 0.9 - 0.02 * abs(len(a) - len(b)))
    return ratio


class PriceQuoteError(RuntimeError):
    pass


class PriceQuote:
    def __init__(self, cache, ui_factory=None, probe_factory=None, log=None):
        self.cache = cache
        self.log = log or (lambda message: None)
        self.ui_factory = ui_factory or self._default_ui
        self.probe_factory = probe_factory or (lambda ui, name: CheckoutProbe(ui, name))

    @staticmethod
    def _default_ui():
        import sys
        price_dir = Path(r'C:\Users\12783\Documents\ChatGPT\qq机器人\price-watch')
        if str(price_dir) not in sys.path:
            sys.path.insert(0, str(price_dir))
        import json
        import jhs_ui
        cfg = json.loads((price_dir / 'config.json').read_text(encoding='utf-8'))
        return jhs_ui.JhsUI(cfg)

    def _open_best(self, ui, name, _retry=True):
        """同名文字优先；卡片行没有卡名文本时点价格行，进详情核对卡名后再返回。"""
        import time
        import jhs_ui
        wanted = normalize_name(name)
        # 搜索结果可能停在旧的滚动位置；先把列表扫回顶部，卡本体通常在第一条。
        for _ in range(2):
            ui.swipe(540, 500, 540, 1600)
            time.sleep(0.5)
        for _ in range(1):
            nodes = [n for n in jhs_ui.parse_nodes(ui.dump_xml()) if n.get("center") and n.get("text")]
            centers = []
            for n in nodes:
                if n["cls"].endswith("TextView") and normalize_name(n["text"]) == wanted:
                    centers.append(n["center"])
            price_count = 0
            for n in nodes:
                if n["text"].strip() == "¥" and n["center"][1] > 200:
                    centers.append(n["center"])
                    price_count += 1
                    if price_count >= 2:
                        break
            seen = set()
            for center in centers:
                if center in seen:
                    continue
                seen.add(center)
                ui.tap(*center)
                time.sleep(2.5)
                self._dismiss_ads(ui)
                focus = ui.current_focus() or ""
                if "CardDetailActivity" not in focus:
                    # 还在搜索页说明只是没点开，不要 back，否则会退回首页把候选清空。
                    if "SearchActivity" not in focus and "SearchResult" not in focus:
                        try: ui.back()
                        except Exception: pass
                    continue
                actual = ""
                try: actual = ui.read_detail().get("name") or ""
                except Exception: pass
                if normalize_name(actual) == wanted and "未拆封" not in actual and "未开封" not in actual:
                    return
                try: ui.back()
                except Exception: pass
                time.sleep(0.8)
            try: ui.swipe(540, 500, 540, 1600)
            except Exception: pass
            time.sleep(0.8)
        texts = " ".join((n.get("text") or "") for n in nodes)
        if "纳尼" in texts or "没有找到" in texts or "暂无结果" in texts:
            if _retry:
                try:
                    ui.run(["shell", "am", "force-stop", "com.jihuanshe"], check=False)
                    time.sleep(1.0)
                    ui.run(["shell", "am", "start", "-S", "--activity-clear-task", "-n",
                            "com.jihuanshe/com.jihuanshe.ui.page.main.MainActivity"], check=False)
                    time.sleep(8.0)
                    for _ in range(6):
                        page = self._page(ui)
                        if page == "home":
                            break
                        if page == "outside" and "chrom" in (ui.current_focus() or "").lower():
                            ui.back()
                        else:
                            ui.back()
                        time.sleep(0.7)
                    self._dismiss_ads(ui)
                    ui.open_search()
                    ui.type_search(name)
                    for _ in range(8):
                        nodes2 = jhs_ui.parse_nodes(ui.dump_xml())
                        texts2 = " ".join((n.get("text") or "") for n in nodes2)
                        if "纳尼" in texts2 or any((n.get("text") or "").strip() == "¥" for n in nodes2):
                            break
                        ui.key(66)
                        time.sleep(1.0)
                    return self._open_best(ui, name, _retry=False)
                except Exception:
                    pass
            raise PriceQuoteError("集换社没有找到「%s」，请换集换社全名或卡包编号。" % name)
        # 自定义点行失败时，回退到 jhs_ui.open_card 的精确选卡。
        try:
            ui.open_card(name, "", "")
            time.sleep(2.0)
            if "CardDetailActivity" in (ui.current_focus() or ""):
                actual = ui.read_detail().get("name") or ""
                if normalize_name(actual) == wanted:
                    return
        except Exception:
            pass
        try:
            from .jhs_ocr import tap_name
            if tap_name(ui, name):
                return
        except Exception:
            pass
        raise PriceQuoteError("没能打开「%s」的卡片详情页，请稍后再试。" % name)

    def _show_products(self, ui):
        """详情页可能停在上次的“效果”tab；切回“商品”才有价格和卖家。"""
        import time
        import jhs_ui
        for _ in range(3):
            nodes = [n for n in jhs_ui.parse_nodes(ui.dump_xml()) if n.get("center") and n.get("text")]
            tab = next((n for n in nodes if n["text"] == "商品"), None)
            if tab is None:
                time.sleep(1)
                continue
            ui.tap(*tab["center"])
            time.sleep(2)
            return

    def _dismiss_ads(self, ui):
        import time
        import jhs_ui
        for _ in range(6):
            nodes = jhs_ui.parse_nodes(ui.dump_xml())
            target = next((n for n in nodes if n.get("center") and n.get("text") and
                           ("跳过" in n["text"] or n["text"] in ("关闭", "跳过"))), None)
            if target is None:
                return
            ui.tap(*target["center"])
            time.sleep(1.2)

    def _page(self, ui):
        focus = ui.current_focus() or ""
        if "CardDetailActivity" in focus:
            return "detail"
        if "SearchActivity" in focus or "SearchResult" in focus:
            import jhs_ui
            nodes = jhs_ui.parse_nodes(ui.dump_xml())
            return "search" if any("EditText" in (n.get("cls") or "") for n in nodes) else "results"
        if "MainActivity" in focus:
            return "home"
        if "com.jihuanshe" in focus:
            return "other"
        return "outside"

    def _collect(self, ui, name, rarity):
        """显式重置到主界面再搜索，避免 start_app 的循环在异常页面卡住。"""
        import time
        self.log("查价: 重置集换社")
        search_name = name
        # 连续查价可能跨过熄屏时间；先唤醒并保持常亮。
        ui.run(["shell", "input", "keyevent", "224"], check=False)
        ui.run(["shell", "svc", "power", "stayon", "true"], check=False)
        time.sleep(0.5)
        for attempt in range(5):
            ui.run(["shell", "am", "force-stop", "com.jihuanshe"], check=False)
            time.sleep(1)
            ui.run(["shell", "am", "start", "-S", "--activity-clear-task", "-n",
                    "com.jihuanshe/com.jihuanshe.ui.page.main.MainActivity"], check=False)
            time.sleep(3)
            for _ in range(8):
                page = self._page(ui)
                if page == "home":
                    break
                if page == "outside":
                    focus = ui.current_focus() or ""
                    if "chrom" in focus.lower() or "browser" in focus.lower():
                        ui.back()
                        time.sleep(1.8)
                    else:
                        ui.run(["shell", "am", "start", "-S", "--activity-clear-task", "-n",
                                "com.jihuanshe/com.jihuanshe.ui.page.main.MainActivity"], check=False)
                        time.sleep(2.5)
                else:
                    ui.back()
                    time.sleep(0.6)
            self._dismiss_ads(ui)
            try:
                ui.open_search()
                ui.type_search(search_name)
                import jhs_ui as _jhs_wait
                for _wait in range(10):
                    _nodes = _jhs_wait.parse_nodes(ui.dump_xml())
                    _texts = " ".join((n.get("text") or "") for n in _nodes)
                    if "纳尼" in _texts or "没有找到" in _texts or "暂无结果" in _texts or any(
                            (n.get("text") or "").strip() == "¥" for n in _nodes):
                        break
                    ui.key(66)
                    time.sleep(1.0)
                focus = ui.current_focus() or ""
                if "SearchActivity" in focus or "SearchResult" in focus:
                    break
                self.log("查价: 未进入搜索结果，第 %d 次重开" % (attempt + 1))
            except Exception as exc:
                self.log("查价: 搜索失败 %s，第 %d 次重开" % (type(exc).__name__, attempt + 1))
            time.sleep(1)
        else:
            raise PriceQuoteError("集换社搜索页没有响应，已复位，请稍后再试。")
        self.log("查价: 打开卡页")
        self._open_best(ui, search_name)
        self._show_products(ui)
        self.log("查价: 读取详情")
        data = ui.read_detail()
        actual_name = data.get("name") or ""
        if normalize_name(actual_name) not in (normalize_name(name), normalize_name(name)):
            raise PriceQuoteError("集换社显示的是「%s」，不是「%s」，请用集换社里的全名再查。"
                                  % (actual_name or "未知", name))
        want_rarity = (rarity or "").strip().upper()
        if want_rarity and (data.get("rarity") or "").strip().upper() != want_rarity:
            self.log("查价: 切换罕贵 %s" % want_rarity)
            selected = False
            for attempt in range(3):
                if ui.select_version(want_rarity):
                    selected = True
                    break
                self.log("查价: 版本选择失败，第 %d 次重试" % (attempt + 1))
                time.sleep(1.5)
                try:
                    ui.back()
                except Exception:
                    pass
                time.sleep(0.8)
            if not selected:
                raise PriceQuoteError("集换社没有找到「%s」的 %s 版本。" % (name, want_rarity))
            self._show_products(ui)
            data = ui.read_detail()
            if (data.get("rarity") or "").strip().upper() != want_rarity:
                raise PriceQuoteError("集换社没有找到「%s」的 %s 版本。" % (name, want_rarity))
        import jhs_ui
        sellers = []
        for _ in range(4):
            sellers = jhs_ui.parse_sellers(ui.dump_xml())[:10]
            if sellers:
                break
            ui.swipe(540, 1500, 540, 700)
            time.sleep(1.2)
        data["sellers"] = sellers
        data["lowest_seller"] = sellers[0] if sellers else None
        self.log("查价: 卖家 %d 个" % len(data["sellers"]))
        return data

    def quote(self, name: str, rarity: str = '', language: str = '日文'):
        ui = self.ui_factory()
        ui.connect()
        if hasattr(ui, 'open_search'):
            from .price_ui_lock import PriceUiLock
            with PriceUiLock(timeout=300):
                data = self._collect(ui, name, rarity)
        else:
            ui.start_app()
            data = ui.collect_card({'name': name, 'rarity': rarity}, restart=True)
        actual_language = data.get('language') or ''
        try:
            from .commands import parse_language
            actual_language = parse_language(actual_language) or actual_language
            language = parse_language(language) or language
        except Exception:
            pass
        if language and actual_language and actual_language != language:
            raise PriceQuoteError(f"页面语言是{actual_language}，不是{language}")
        sellers = data.get('sellers') or []
        lowest = data.get('lowest')
        if lowest is None and sellers:
            lowest = min(item['price'] for item in sellers if item.get('price') is not None)
        shipping = None
        landed = None
        low_seller = sellers[0] if sellers else None
        return {
            'name': data.get('name') or name,
            'rarity': data.get('rarity') or rarity,
            'language': actual_language or language,
            'price': data.get('price'),
            'lowest': lowest,
            'seller': low_seller,
            'shipping': shipping,
            'landed': landed,
            'region': REGION,
        }


def format_quote(result):
    name = result['name']
    label = f"{name} {result.get('rarity') or ''}（{result.get('language') or ''}）".strip()
    price = result.get('price')
    lowest = result.get('lowest')
    return "\n".join([
        label,
        f"集换价：{'¥%g' % price if price is not None else '暂无'}（不含邮费）",
        f"最低价：{'¥%g' % lowest if lowest is not None else '暂无'}（不含邮费）",
    ])
