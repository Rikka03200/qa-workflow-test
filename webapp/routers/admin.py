"""管理后台路由：CodeArts 容器路径配置、知识库编辑等系统管理功能。"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.status import HTTP_303_SEE_OTHER

from .. import config
from ..deps import require_role, templates
from ..services import scripts_loader
from core.productcfg import DEFAULT_PRODUCT

router = APIRouter(prefix="/admin", tags=["admin"])


def _kb_store():
    try:
        return scripts_loader.load_normal("kb_store")
    except Exception:  # noqa: BLE001
        return None


def _nav(active: str, product: str) -> dict:
    """构造导航上下文（复用 pages.py 的结构）。"""
    return {
        "active": active,
        "product": product,
        "product_display": config.product_display(product),
        "products": config.products(),
    }


def _product_modules_md(product: str) -> Path:
    """产品 modules.md 路径。"""
    return config.REPO_ROOT / "_kb" / "projects" / product / "modules.md"


def _parse_tree_section(md_path: Path) -> tuple[list[str], list[str]]:
    """解析 modules.md §2 CodeArts 用例存放目录树文本块。
    返回 (前置行列表, 树文本行列表)——树文本保留缩进，便于重建。"""
    store = _kb_store()
    try:
        rel = str(md_path.relative_to(config.REPO_ROOT)).replace("\\", "/")
    except ValueError:
        rel = ""
    if store and rel.startswith("_kb/"):
        text = store.read_text(rel, md_path)
    elif md_path.exists():
        text = md_path.read_text(encoding="utf-8")
    else:
        return ([], [])
    if not text:
        return ([], [])
    lines = text.splitlines()
    # 找 §2 开头
    start = next((i for i, L in enumerate(lines) if L.startswith("## 2. CodeArts")), None)
    if start is None:
        return ([], [])
    # 找树块开头（```text）和结尾（```）
    tree_start = next((i for i in range(start, len(lines)) if lines[i].strip().startswith("```text")), None)
    if tree_start is None:
        return (lines[: start + 1], [])  # 保留到 §2 标题
    tree_end = next((i for i in range(tree_start + 1, len(lines)) if lines[i].strip() == "```"), None)
    if tree_end is None:
        tree_end = len(lines)
    prefix = lines[: tree_start + 1]  # 保留 §2 标题 + 说明 + ```text
    tree_lines = lines[tree_start + 1 : tree_end]  # 纯树文本
    return (prefix, tree_lines)


def _tree_to_json(tree_lines: list[str]) -> list[dict]:
    """把树文本（│├─ └─ 缩进）解析成 [{text, children, level}, ...] 嵌套结构。
    level=缩进深度（根=0）；children 递归嵌套。"""
    stack: list[tuple[int, dict]] = []  # [(level, node), ...]
    root_nodes = []

    for line in tree_lines:
        if not line.strip():
            continue
        # 计算缩进层级（│├─ └─ 每组3字符，中文2字符当1字符处理——实测 modules.md 是混合的）
        stripped = line.lstrip("│├─└ \t│")
        indent = len(line) - len(stripped)
        level = indent // 3  # 粗略估计
        text = stripped.strip()
        node = {"text": text, "children": [], "level": level}

        # 栈回退到父级
        while stack and stack[-1][0] >= level:
            stack.pop()

        if not stack:
            root_nodes.append(node)
        else:
            stack[-1][1]["children"].append(node)
        stack.append((level, node))

    return root_nodes


def _json_to_tree_text(nodes: list[dict], level: int = 0) -> list[str]:
    """把 JSON 树还原成树文本（│├─ └─ 格式）。"""
    lines = []
    for i, node in enumerate(nodes):
        is_last = i == len(nodes) - 1
        prefix = "   " * level
        marker = "└─ " if is_last else "├─ "
        lines.append(f"{prefix}{marker}{node['text']}")
        if node.get("children"):
            lines.extend(_json_to_tree_text(node["children"], level + 1))
    return lines


@router.get("/modules", response_class=HTMLResponse)
async def modules_page(request: Request, product: str = DEFAULT_PRODUCT, user=Depends(require_role("admin"))):
    """CodeArts 容器路径管理页面。"""
    md_path = _product_modules_md(product)
    prefix, tree_lines = _parse_tree_section(md_path)
    tree_json = _tree_to_json(tree_lines)
    return templates.TemplateResponse(request, "admin_modules.html", {
        "nav": _nav("admin", product),
        "user": user,
        "product": product,
        "tree_json": json.dumps(tree_json, ensure_ascii=False),
    })


@router.post("/modules/save")
async def modules_save(product: str = Form(...), tree_json: str = Form(...), user=Depends(require_role("admin"))):
    """保存 CodeArts 容器路径树（JSON → markdown）。"""
    md_path = _product_modules_md(product)

    try:
        nodes = json.loads(tree_json)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"JSON 格式错误：{e}")

    store = _kb_store()
    rel = str(md_path.relative_to(config.REPO_ROOT)).replace("\\", "/")
    current = ""
    if store:
        current = store.read_text(rel, md_path)
    if not current and md_path.exists():
        current = md_path.read_text(encoding="utf-8")
    if not current:
        raise HTTPException(404, f"产品 {product} 的 modules.md 不存在")

    # 备份当前有效内容（DB 优先），避免 DB 与 Markdown 暂不同步时备份错版本。
    bak = md_path.with_suffix(f".md.bak-{datetime.now():%Y%m%d-%H%M%S}")
    bak.parent.mkdir(parents=True, exist_ok=True)
    bak.write_text(current, encoding="utf-8")

    if store and hasattr(store, "replace_modules_tree"):
        try:
            content = store.replace_modules_tree(current, nodes)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"modules.md 树块替换失败：{e}")
    else:
        # 重建 modules.md §2 树块；全程基于当前有效内容，避免旧 Markdown 尾段覆盖 DB 新内容。
        lines = current.splitlines()
        start = next((i for i, line in enumerate(lines) if re.match(r"^##\s*2[.\s]", line)), None)
        if start is None:
            raise HTTPException(400, "modules.md 未找到 §2 标题")
        tree_start = next((i for i in range(start, len(lines)) if lines[i].strip().startswith("```text")), None)
        if tree_start is None:
            raise HTTPException(400, "modules.md §2 未找到 ```text 树块")
        tree_end = next((i for i in range(tree_start + 1, len(lines)) if lines[i].strip() == "```"), None)
        if tree_end is None:
            raise HTTPException(400, "modules.md §2 树块未闭合")
        content = "\n".join(lines[:tree_start + 1] + _json_to_tree_text(nodes) + lines[tree_end:]) + "\n"

    if store:
        try:
            store.upsert_text(rel, content, product=product, kind="modules")
            store.export_documents(product)
        except Exception:  # noqa: BLE001
            md_path.write_text(content, encoding="utf-8")
        else:
            return JSONResponse({"ok": True, "backup": bak.name, "store": "postgres"})
    md_path.write_text(content, encoding="utf-8")

    return JSONResponse({"ok": True, "backup": bak.name, "store": "markdown"})


# ========== 知识库管理 ==========

def _kb_root() -> Path:
    return config.REPO_ROOT / "_kb"


def _list_products() -> list[str]:
    """列出所有产品（DB 优先；本地 _kb/projects 兜底）。"""
    store = _kb_store()
    if store and hasattr(store, "list_products"):
        products = store.list_products()
        if products:
            return products
    projects = _kb_root() / "projects"
    if not projects.exists():
        return []
    return [d.name for d in projects.iterdir() if d.is_dir() and not d.name.startswith("_")]


def _kb_files(product: str | None) -> dict[str, list[dict]]:
    """列出知识库文件清单（分全局规范、产品文件）。
    返回 {category: [{name, path, size, mtime}, ...], ...}"""
    store = _kb_store()
    if store:
        files = store.list_files(product)
        if files["global"] or files["product"]:
            return files
    result = {"global": [], "product": []}

    # 全局规范
    glob_dir = _kb_root() / "_global"
    if glob_dir.exists():
        for f in glob_dir.glob("*.md"):
            result["global"].append({
                "name": f.name,
                "path": str(f.relative_to(config.REPO_ROOT)).replace("\\", "/"),
                "size": f.stat().st_size,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })

    # 产品文件（只列 curated 根级 Markdown，_bulk-index 不进入管理清单）
    if product:
        prod_dir = _kb_root() / "projects" / product
        if prod_dir.exists():
            for f in prod_dir.glob("*.md"):
                result["product"].append({
                    "name": f.name,
                    "path": str(f.relative_to(config.REPO_ROOT)).replace("\\", "/"),
                    "size": f.stat().st_size,
                    "mtime": datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                })

    return result


def _safe_kb_path(file: str, *, require_exists: bool = True) -> Path:
    rel = Path(file)
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(404, "文件不存在或路径非法")
    p = (config.REPO_ROOT / rel).resolve()
    try:
        p.relative_to(_kb_root().resolve())
    except ValueError:
        raise HTTPException(404, "文件不存在或路径非法")
    if "_bulk-index" in p.parts:
        raise HTTPException(404, "_bulk-index 为原始中间产物，不在知识库编辑范围")
    if p.suffix.lower() not in {".md", ".json"}:
        raise HTTPException(404, "仅允许编辑知识库 Markdown/JSON 文件")
    if require_exists and not p.exists():
        raise HTTPException(404, "文件不存在或路径非法")
    return p


def _safe_kb_file(file: str) -> Path:
    return _safe_kb_path(file, require_exists=True)


def _parse_toc_text(content: str) -> list[dict]:
    """解析 markdown 文本目录（# ## ### 标题 → {level, title, line}）。"""
    lines = content.splitlines()
    toc = []
    for i, line in enumerate(lines, start=1):
        m = re.match(r"^(#{1,6})\s+(.+)", line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            toc.append({"level": level, "title": title, "line": i})
    return toc


def _parse_markdown_toc(md_path: Path) -> list[dict]:
    """解析 markdown 章节目录（# ## ### 标题 → {level, title, line}）。"""
    if not md_path.exists():
        return []
    return _parse_toc_text(md_path.read_text(encoding="utf-8"))


@router.get("/kb", response_class=HTMLResponse)
async def kb_index(request: Request, product: str = DEFAULT_PRODUCT, user=Depends(require_role("admin"))):
    """知识库管理首页。"""
    products = _list_products()
    files = _kb_files(product)
    return templates.TemplateResponse(request, "kb_index.html", {
        "nav": _nav("kb", product),
        "user": user,
        "products": products,
        "current_product": product,
        "files": files,
    })


@router.get("/kb/view")
async def kb_view_file(file: str, user=Depends(require_role("admin"))):
    """查看知识库文件内容（DB 优先返回 markdown 原文 + TOC）。"""
    p = _safe_kb_path(file, require_exists=False)
    store = _kb_store()
    content = store.read_text(file, p) if store else ""
    if not content and p.exists():
        content = p.read_text(encoding="utf-8")
    if not content:
        raise HTTPException(404, "文件不存在或路径非法")
    toc = _parse_toc_text(content)

    return JSONResponse({
        "content": content,
        "toc": toc,
        "path": file,
        "name": p.name,
    })


@router.post("/kb/save")
async def kb_save_file(file: str = Form(...), content: str = Form(...), user=Depends(require_role("admin"))):
    """保存知识库文件（先备份 .bak；DB 优先，随后导出 markdown 兼容文件）。"""
    p = _safe_kb_path(file, require_exists=False)
    store = _kb_store()

    current = store.read_text(file, p) if store else ""
    if not current and p.exists():
        current = p.read_text(encoding="utf-8")
    if not current and not store:
        raise HTTPException(404, "文件不存在或路径非法")

    bak = p.with_suffix(f".md.bak-{datetime.now():%Y%m%d-%H%M%S}")
    if current:
        bak.parent.mkdir(parents=True, exist_ok=True)
        bak.write_text(current, encoding="utf-8")

    if store:
        try:
            product, _scope, kind = store.classify_path(p)
            store.upsert_text(file, content, product=product, kind=kind)
            store.export_documents(product)
        except Exception:  # noqa: BLE001
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return JSONResponse({"ok": True, "backup": bak.name if current else "", "store": "markdown"})
        return JSONResponse({"ok": True, "backup": bak.name if current else "", "store": "postgres"})

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")

    return JSONResponse({"ok": True, "backup": bak.name if current else "", "store": "markdown"})


@router.get("/kb/search")
async def kb_search(q: str, product: str = "", user=Depends(require_role("admin"))):
    """搜索知识库（DB pg_trgm 优先，失败时回退文件子串搜索）。"""
    if not q.strip():
        return JSONResponse({"results": []})

    store = _kb_store()
    if store:
        try:
            results = store.search(q, product or None)
        except Exception:  # noqa: BLE001
            results = None
        else:
            return JSONResponse({"results": results[:50]})

    search_dirs = [_kb_root() / "_global"]
    if product:
        search_dirs.append(_kb_root() / "projects" / product)

    results = []
    for d in search_dirs:
        if not d.exists():
            continue
        for f in d.rglob("*.md"):
            if "_bulk-index" in f.parts:
                continue
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
                for i, line in enumerate(lines, start=1):
                    if q.lower() in line.lower():
                        ctx_start = max(0, i - 2)
                        ctx_end = min(len(lines), i + 2)
                        ctx = lines[ctx_start:ctx_end]
                        results.append({
                            "file": str(f.relative_to(config.REPO_ROOT)).replace("\\", "/"),
                            "line": i,
                            "text": line.strip(),
                            "context": "\n".join(ctx),
                        })
            except Exception:
                continue

    return JSONResponse({"results": results[:50]})  # 限制返回前 50 条
