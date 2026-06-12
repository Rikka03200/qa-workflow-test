"""Product configuration merge layer.

The scale-up plan requires product-specific Jira, selection, ticket key and
output conventions to live behind one API. This module keeps backward
compatibility with the current `config/config.local.yaml` shape while accepting
newer nested product config.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


DEFAULT_PRODUCT = "wms"
DEFAULT_TICKET_KEY_REGEX = r"^[A-Z]+-\d+$"
DEFAULT_TICKET_DIR_GLOB = "*"
DEFAULT_PLATFORM_LABELS = {
    "web": "网页端",
    "仓配app": "仓配 App",
    "采配app": "采配 App",
    "零售app": "零售 App",
    "pos": "POS",
    "tms": "TMS",
    "新web": "新网页端",
    "接口": "接口",
    "公告": "公告",
}
DEFAULT_SELECTION = {
    "tester": "林子宣",
    "tester_jql_field": "cf[10020]",
    "issuetype": "提高",
    "resolution": "Unresolved",
    "resolutions_allowed": ["已修复", "Fixed"],
    "split_regex": r"^(?P<base>.+)--(?P<suffix>[^-]{1,12})$",
    "split_pick_priority": ["web", "app", "接口", "pc", "小程序", "h5", "pos"],
}


def deep_merge(*parts: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge dictionaries left-to-right without mutating inputs."""
    out: dict[str, Any] = {}
    for part in parts:
        if not part:
            continue
        for key, value in part.items():
            if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
                out[key] = deep_merge(out[key], value)
            else:
                out[key] = copy.deepcopy(value)
    return out


@dataclass(frozen=True)
class ProductConfig:
    key: str
    display_name: str
    ticket_key_regex: str = DEFAULT_TICKET_KEY_REGEX
    ticket_dir_glob: str = DEFAULT_TICKET_DIR_GLOB
    jira_project_keys: tuple[str, ...] = ("EAR",)
    jira_board_id: int | None = None
    kb_path: str = ""
    selection: dict[str, Any] = field(default_factory=dict)
    platform_labels: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ticket_key_pattern(self) -> re.Pattern[str]:
        return re.compile(self.ticket_key_regex)

    def valid_ticket_key(self, key: str) -> bool:
        return bool(self.ticket_key_pattern.fullmatch(key or ""))

    def ticket_glob(self) -> str:
        if self.ticket_dir_glob and self.ticket_dir_glob != "*":
            return self.ticket_dir_glob
        prefix = self.raw.get("ticket_key_prefix") or self.raw.get("jira_project_key")
        if prefix:
            return f"{prefix}-*"
        return "*"


def _legacy_product_defaults(product: str) -> dict[str, Any]:
    key = product or DEFAULT_PRODUCT
    return {
        "display_name": "WMS" if key == "wms" else key.upper(),
        "ticket_key_regex": DEFAULT_TICKET_KEY_REGEX,
        "ticket_dir_glob": "EAR-*" if key == "wms" else "*",
        "jira_project_keys": ["EAR"] if key == "wms" else [],
        "selection": DEFAULT_SELECTION,
        "platform_labels": DEFAULT_PLATFORM_LABELS,
    }


def from_raw_config(raw_config: Mapping[str, Any] | None, product: str = DEFAULT_PRODUCT) -> ProductConfig:
    """Build config from global defaults + top-level sections + product section.

    Supported input shapes:
    - legacy: `products.wms.jira_project_keys`, `jira_board_id`, `kb_path`
    - new: `products.wms.jira.project_keys`, `products.wms.selection.*`,
      `products.wms.output.ticket_key_regex`, `products.wms.platforms.labels`
    """
    cfg = raw_config or {}
    products = cfg.get("products") or {}
    product_raw = dict(products.get(product) or {})
    global_selection = cfg.get("selection") or {}
    global_platforms = ((cfg.get("platforms") or {}).get("labels") or {})

    merged = deep_merge(_legacy_product_defaults(product), {
        "selection": global_selection,
        "platform_labels": global_platforms,
    }, product_raw)

    jira = merged.get("jira") or {}
    output = merged.get("output") or {}
    platforms = merged.get("platforms") or {}

    project_keys = (
        jira.get("project_keys")
        or merged.get("jira_project_keys")
        or (cfg.get("jira") or {}).get("default_project_keys")
        or []
    )
    board_id = jira.get("board_id", merged.get("jira_board_id"))
    ticket_key_regex = (
        output.get("ticket_key_regex")
        or merged.get("ticket_key_regex")
        or DEFAULT_TICKET_KEY_REGEX
    )
    ticket_dir_glob = (
        output.get("ticket_dir_glob")
        or merged.get("ticket_dir_glob")
        or DEFAULT_TICKET_DIR_GLOB
    )
    labels = deep_merge(DEFAULT_PLATFORM_LABELS, merged.get("platform_labels"), platforms.get("labels"))
    selection = deep_merge(DEFAULT_SELECTION, merged.get("selection"))

    return ProductConfig(
        key=product,
        display_name=str(merged.get("display_name") or product.upper()),
        ticket_key_regex=str(ticket_key_regex),
        ticket_dir_glob=str(ticket_dir_glob),
        jira_project_keys=tuple(str(k) for k in project_keys),
        jira_board_id=int(board_id) if board_id not in (None, "") else None,
        kb_path=str(merged.get("kb_path") or (merged.get("kb") or {}).get("path") or ""),
        selection=selection,
        platform_labels={str(k).lower(): str(v) for k, v in labels.items()},
        raw=merged,
    )


def _load_webapp_raw_config() -> dict[str, Any]:
    try:
        from webapp import config as web_config
        return web_config.raw_config() or {}
    except Exception:
        return {}


@lru_cache(maxsize=128)
def get_product(product: str = DEFAULT_PRODUCT) -> ProductConfig:
    return from_raw_config(_load_webapp_raw_config(), product)


def reset_cache() -> None:
    get_product.cache_clear()


def valid_product_key(product: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{1,40}", product or ""))


def safe_product_dir(root: Path, product: str) -> Path:
    return root / product if valid_product_key(product) else root / "__invalid__"
