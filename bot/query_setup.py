"""Optional query components. A disabled or invalid query config never disables watch."""
import json
import sqlite3
import time
from pathlib import Path


def build_query(config, data_dir, replay_root, log=print):
    cfg = config.get("query")
    if cfg is None:
        try:
            root = Path(config.get("root") or Path(__file__).resolve().parents[1])
            cfg = json.loads((root / "config.json").read_text(encoding="utf-8")).get("query")
        except (OSError, ValueError):
            cfg = None
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return {}
    store = None
    try:
        from .query_auth import QueryAuth
        from .query_core import PortalClient, QueryCore
        from .share import ShareApp
        from .store import Store
        admin = str(cfg.get("admin_token") or "")
        if len(admin) < 24 or admin == str((config.get("remote") or {}).get("token") or ""):
            raise ValueError("query admin token must be separate and at least 24 characters")
        data_dir, replay_root = Path(data_dir), Path(replay_root)
        auth = QueryAuth(data_dir / "query-auth.sqlite3",
                         token_hours=cfg.get("token_hours", 24),
                         refresh_days=cfg.get("refresh_days", 180),
                         code_hours=cfg.get("code_hours", 24), log=log)
        store = Store(data_dir / "bot.sqlite3", migrate=False)
        share_cfg = config.get("share") or {}
        share = ShareApp(store, replay_root, time.time,
                         public_base_url=str(share_cfg.get("public_base_url") or ""),
                         bind_host="127.0.0.1", bind_port=int(share_cfg.get("bind_port", 8787)),
                         link_hours=float(share_cfg.get("link_hours", 5)))
        portal_port = int(cfg.get("portal_port", 8788))
        if not 1 <= portal_port <= 65535:
            raise ValueError("invalid_portal_port")
        core = QueryCore(PortalClient(f"http://127.0.0.1:{portal_port}", secret_path=data_dir / "statistics-owner.key"),
                         store=store, share=share, replay_root=replay_root,
                         database=data_dir / "bot.sqlite3")
        core._owned_store = True
        return dict(query_auth=auth, admin_token=admin, query_core=core,
                    writes_enabled=cfg.get("writes_enabled") is True)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        if store is not None:
            store.close()
        log(f"[查询] 初始化失败，仅停用查询：{type(exc).__name__}")
        return {}
