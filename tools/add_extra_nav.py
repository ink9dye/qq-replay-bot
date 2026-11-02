# -*- coding: utf-8 -*-
"""W77：给所有网页导航加“额外卡”入口，并同步源码到维护目录。"""
import pathlib
import shutil
import time

PROD = pathlib.Path(r'D:\pythons\qq-replay-bot\bot')
MAINT = pathlib.Path(r'C:\Users\12783\Documents\ChatGPT\qq机器人\feature-stage-20261026-1529\bot')
stamp = time.strftime('%Y%m%d-%H%M%S')
changed = []
for path in PROD.glob('*.html'):
    text = path.read_text(encoding='utf-8')
    if 'cards-report' not in text or '>额外卡</a>' in text:
        continue
    marker = '<a href="/statistics/cards-report"'
    start = text.find(marker)
    if start < 0:
        continue
    end = text.find('</a>', start)
    if end < 0:
        continue
    text = text[:end + 4] + '<a href="/statistics/cards-extra">额外卡</a>' + text[end + 4:]
    path.write_text(text, encoding='utf-8', newline='')
    changed.append(path.name)
print('changed', changed)
for name in changed:
    src = PROD / name
    dst = MAINT / name
    if dst.exists():
        shutil.copy2(dst, dst.with_name(dst.name + '.bak-' + stamp))
    shutil.copy2(src, dst)
for name in ('statistics_extra_report.py', 'statistics_cards_extra.html'):
    src = PROD / name
    if src.exists():
        dst = MAINT / name
        if dst.exists():
            shutil.copy2(dst, dst.with_name(dst.name + '.bak-' + stamp))
        shutil.copy2(src, dst)
