# -*- coding: utf-8 -*-
"""集换社结算页邮费探测：加购最低卖家 → 结算预览 → 读邮费 → 清理。

只做预览，不付款、不下单；调用方负责串行、冷却和缓存。
"""
from __future__ import annotations

import re
import time

from .price_shipping import parse_checkout, ui_nodes


class CheckoutError(RuntimeError):
    pass


def _nodes(ui):
    return ui_nodes(ui.dump_xml())


def _find(nodes, text, exact=True):
    for node in nodes:
        value = node.get("text") or ""
        if (value == text) if exact else (text in value):
            return node
    return None


def _tap(ui, node, wait=1.0):
    ui.tap(*node["center"])
    time.sleep(wait)


class CheckoutProbe:
    def __init__(self, ui, card_name: str):
        self.ui = ui
        self.card_name = card_name

    def add_to_cart(self, seller: str):
        nodes = _nodes(self.ui)
        node = _find(nodes, seller)
        if node is None:
            raise CheckoutError(f"找不到卖家：{seller}")
        _tap(self.ui, node, 2.0)
        nodes = _nodes(self.ui)
        button = _find(nodes, "加入购物车")
        if button is None:
            raise CheckoutError("卖家页没有加入购物车按钮")
        _tap(self.ui, button, 1.5)
        self.ui.back()
        time.sleep(0.8)
        self.ui.back()
        time.sleep(0.8)

    def open_cart(self):
        # ShopCarActivity 不导出，不能 am start；退回主界面再点“个人 → 购物车”。
        for _ in range(6):
            if "MainActivity" in (self.ui.current_focus() or ""):
                break
            self.ui.back()
            time.sleep(0.7)
        nodes = _nodes(self.ui)
        personal = _find(nodes, "个人")
        if personal is None:
            raise CheckoutError("找不到个人页入口")
        _tap(self.ui, personal, 1.5)
        nodes = _nodes(self.ui)
        cart = _find(nodes, "购物车")
        if cart is None:
            raise CheckoutError("个人页找不到购物车")
        _tap(self.ui, cart, 2.0)

    def checkout(self, seller: str):
        nodes = _nodes(self.ui)
        card = _find(nodes, self.card_name, exact=False)
        if card is None:
            raise CheckoutError("购物车里没有刚加的商品")
        # 勾选该行左侧的圆圈；购物车行高约 220，商品名左侧 x 约 55。
        self.ui.tap(55, card["center"][1])
        time.sleep(0.8)
        nodes = _nodes(self.ui)
        settle = _find(nodes, "去结算") or _find(nodes, "结算")
        if settle is None:
            raise CheckoutError("购物车没有结算按钮")
        _tap(self.ui, settle, 3.0)
        result = parse_checkout(self.ui.dump_xml())
        if result.get("shipping") is None:
            raise CheckoutError("结算页没有读到邮费")
        return result

    def cleanup(self, seller: str):
        """尽力删除刚加的那件；失败只返回 False，不抛异常。"""
        try:
            self.ui.back()
            time.sleep(1.0)
            nodes = _nodes(self.ui)
            manage = _find(nodes, "管理")
            if manage is None:
                return False
            _tap(self.ui, manage, 0.8)
            nodes = _nodes(self.ui)
            card = _find(nodes, self.card_name, exact=False)
            if card is None:
                return False
            self.ui.tap(55, card["center"][1])
            time.sleep(0.5)
            nodes = _nodes(self.ui)
            delete = _find(nodes, "删除") or _find(nodes, "删除所选")
            if delete is None:
                return False
            _tap(self.ui, delete, 0.8)
            nodes = _nodes(self.ui)
            confirm = _find(nodes, "确定") or _find(nodes, "确认")
            if confirm is not None:
                _tap(self.ui, confirm, 1.0)
            return True
        except Exception:
            return False

    def probe(self, seller: str):
        self.add_to_cart(seller)
        self.open_cart()
        try:
            result = self.checkout(seller)
        finally:
            self.cleanup(seller)
        result["seller"] = seller
        return result
