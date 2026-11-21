"""Refresh the local card data snapshot used by the public MyCard Super Pre page."""

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen


SOURCE = "https://sapi.moecube.com:444/cdn/ygopro-super-pre/data/test-release-v2.json"
SNAPSHOT = Path(__file__).resolve().parents[1] / "data" / "cache" / "superpre-cards.json"
MAX_BYTES = 8 * 1024 * 1024


def refresh():
    with urlopen(SOURCE, timeout=20) as response:
        payload = response.read(MAX_BYTES + 1)
    if len(payload) > MAX_BYTES:
        raise ValueError("超先行卡表体积异常")

    cards = json.loads(payload)
    if not isinstance(cards, list) or not cards:
        raise ValueError("超先行卡表为空或格式异常")

    by_id = {}
    for card in cards:
        if not isinstance(card, dict):
            raise ValueError("超先行卡片格式异常")
        card_id = str(card.get("id", ""))
        name = card.get("name")
        description = card.get("desc")
        types = card.get("overallString")
        picture = card.get("picUrl")
        if (not card_id.isascii() or not card_id.isdigit() or int(card_id) <= 0
                or card_id in by_id or not isinstance(name, str) or not name.strip()
                or not isinstance(description, str) or not isinstance(types, str)
                or not types.strip() or not isinstance(picture, str)
                or urlsplit(picture).scheme != "https" or not urlsplit(picture).hostname):
            raise ValueError(f"超先行卡片字段异常，卡号 {card_id!r}")
        by_id[card_id] = {
            "id": int(card_id), "name": name, "desc": description, "types": types, "picUrl": picture,
        }

    contents = json.dumps(by_id, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if SNAPSHOT.is_file() and SNAPSHOT.read_text(encoding="utf-8") == contents:
        print(f"超先行卡表未变化，共 {len(by_id)} 张")
        return

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=SNAPSHOT.parent, delete=False, mode="w",
                                     encoding="utf-8", newline="\n") as temporary:
        temporary_path = Path(temporary.name)
        try:
            temporary.write(contents)
            temporary.close()
            os.replace(temporary_path, SNAPSHOT)
        finally:
            temporary_path.unlink(missing_ok=True)
    print(f"超先行卡表已刷新，共 {len(by_id)} 张")


if __name__ == "__main__":
    refresh()
