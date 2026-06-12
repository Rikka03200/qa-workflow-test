"""数据归属：共享底层 tickets/，但每个用户只看到自己创建/生成的 Sprint。

账本 webapp/data/ownership.json：{ "<product>": { "<sprint>": "<owner_username>" } }。
- 用户在 app 里新增/生成的 Sprint → 归该用户。
- 首启播种：账本不存在时，若系统中【恰好一个用户】，把磁盘上已有的所有 Sprint 归给 TA
  （linzixuan 首次启动即接管历史数据）；多用户则留空、由管理员用 CLI 认领，避免错认。
- 看板/工单访问按归属校验；非归属者看不到、也进不去。

CLI：python -m webapp.services.ownership claim <用户名> <产品> <Sprint日期>
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .. import config
from . import tickets

_LEDGER = config.DATA_DIR / "ownership.json"


def _load() -> dict:
    if not _LEDGER.exists():
        return {}
    try:
        return json.loads(_LEDGER.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _save(data: dict) -> None:
    config._ensure_data_dir()
    tmp = _LEDGER.with_name(_LEDGER.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _LEDGER)


def _user_count() -> int:
    try:
        from .. import auth
        return auth.store.count()
    except Exception:  # noqa: BLE001
        return 0


def _sole_user() -> Optional[str]:
    try:
        from .. import auth
        users = auth.store.all()
        return users[0].username if len(users) == 1 else None
    except Exception:  # noqa: BLE001
        return None


def ensure_seeded() -> None:
    """首启播种：账本不存在 → 把磁盘已有 Sprint 归给唯一用户（多用户则留空）。"""
    if _LEDGER.exists():
        return
    data: dict = {}
    sole = _sole_user()
    if sole:
        for product_dir in (config.TICKETS_DIR.glob("*") if config.TICKETS_DIR.exists() else []):
            if not product_dir.is_dir():
                continue
            product = product_dir.name
            for sprint in tickets.sprint_dates(product):
                data.setdefault(product, {})[sprint] = sole
    _save(data)  # 即便为空也落盘，标记“已播种”，避免每次重扫


def owner_of(product: str, sprint: str) -> Optional[str]:
    return (_load().get(product) or {}).get(sprint)


def set_owner(product: str, sprint: str, user: str, *, overwrite: bool = False) -> None:
    data = _load()
    prod = data.setdefault(product, {})
    if overwrite or sprint not in prod:
        prod[sprint] = user
        _save(data)


def remove(product: str, sprint: str) -> None:
    """从归属账本移除某 Sprint 条目（删除 Sprint 时调用）。"""
    data = _load()
    prod = data.get(product)
    if prod and sprint in prod:
        prod.pop(sprint, None)
        if not prod:
            data.pop(product, None)
        _save(data)


def can_view(user: str, product: str, sprint: str) -> bool:
    return owner_of(product, sprint) == user


def sprints_owned(user: str, product: str) -> list[str]:
    ensure_seeded()
    prod = _load().get(product) or {}
    owned = [s for s, o in prod.items() if o == user]
    return sorted(owned, reverse=True)


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="数据归属管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("claim", help="把某 Sprint 归到某用户")
    c.add_argument("user")
    c.add_argument("product")
    c.add_argument("sprint")
    sub.add_parser("list", help="列出归属账本")
    a = ap.parse_args()
    if a.cmd == "list":
        print(json.dumps(_load(), ensure_ascii=False, indent=2))
        return 0
    if a.cmd == "claim":
        set_owner(a.product, a.sprint, a.user, overwrite=True)
        print(f"已将 {a.product}/{a.sprint} 归属到 {a.user}")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
