# -*- coding: utf-8 -*-
"""只读探测萌卡两个接口是否可用（不登录、不改任何文件）。

用法：python tools/check_mycard_api.py
输出：sapi 榜单接口、accounts 鉴权接口的 HTTP 状态与耗时；两者都通说明"接口恢复"。
"""
import time
import urllib.error
import urllib.request

TARGETS = (
    ("sapi 榜单接口", "https://sapi.moecube.com:444/ygopro/arena/users?o=pt"),
    ("accounts 登录接口", "https://api.moecube.com/accounts/authUser"),
)


def probe(name, url, timeout=8.0):
    start = time.time()
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(80)
            return f"{name}: HTTP {response.status}，{time.time() - start:.2f}s（通）"
    except urllib.error.HTTPError as exc:
        # 401/403/404 也说明主机可达（只是没带凭证/路径不对）
        return f"{name}: HTTP {exc.code}，{time.time() - start:.2f}s（主机可达）"
    except Exception as exc:  # noqa: BLE001
        return f"{name}: 失败 {type(exc).__name__}，{time.time() - start:.2f}s（不通）"


def main():
    for name, url in TARGETS:
        print(probe(name, url))
    print("提示：watch 每 2 分钟会刷新前100；capture-status.json 里 "
          "top100_stale=false、sapi_error 为空、top100_updated_at 很新 = 已恢复。")


if __name__ == "__main__":
    main()
