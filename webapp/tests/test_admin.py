"""管理后台（CodeArts 容器路径、知识库）功能测试。"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from webapp import auth
from webapp.routers import admin
from webapp.routers.admin import (
    _parse_tree_section,
    _tree_to_json,
    _json_to_tree_text,
    _parse_markdown_toc,
    _kb_files,
)


def test_parse_tree_section_extracts_text_block(tmp_path):
    """解析 modules.md §2 树块（前置 + 树文本分离）。"""
    md = tmp_path / "modules.md"
    md.write_text(
        "# WMS\n\n## 2. CodeArts 用例存放目录树\n\n说明\n\n```text\n"
        "├─ web\n│  └─ 设置管理\n├─ POS\n```\n\n## 3. 后续\n",
        encoding="utf-8",
    )
    prefix, tree_lines = _parse_tree_section(md)
    assert any("## 2." in L for L in prefix)
    assert tree_lines == ["├─ web", "│  └─ 设置管理", "├─ POS"]


def test_tree_to_json_parses_indented_structure(tmp_path):
    """树文本 → JSON 嵌套（含 children）。"""
    lines = ["├─ web", "   ├─ 设置管理", "   └─ 标品管理", "├─ POS"]
    nodes = _tree_to_json(lines)
    assert len(nodes) == 2
    assert nodes[0]["text"] == "web"
    assert len(nodes[0]["children"]) == 2
    assert nodes[0]["children"][0]["text"] == "设置管理"
    assert nodes[1]["text"] == "POS"


def test_json_to_tree_text_restores_format():
    """JSON → 树文本（还原 ├─ └─ 格式）。"""
    nodes = [
        {"text": "web", "children": [{"text": "设置", "children": []}]},
        {"text": "POS", "children": []},
    ]
    lines = _json_to_tree_text(nodes)
    assert "├─ web" in lines
    assert "└─ POS" in lines
    assert any("设置" in L for L in lines)


def test_parse_markdown_toc_extracts_headers(tmp_path):
    """解析 markdown 标题（# ## ###）→ TOC。"""
    md = tmp_path / "rules.md"
    md.write_text("# 规则\n\n正文\n\n## 模块1\n\n### 细节", encoding="utf-8")
    toc = _parse_markdown_toc(md)
    assert len(toc) == 3
    assert toc[0]["level"] == 1 and toc[0]["title"] == "规则"
    assert toc[1]["level"] == 2 and toc[1]["title"] == "模块1"
    assert toc[2]["level"] == 3 and toc[2]["title"] == "细节"


def test_roundtrip_modules_tree_json(tmp_path):
    """modules.md 树块 roundtrip（parse → json → text → parse 不变）。"""
    orig_lines = ["├─ web", "   ├─ 设置", "├─ POS"]
    nodes = _tree_to_json(orig_lines)
    rebuilt = _json_to_tree_text(nodes)
    nodes2 = _tree_to_json(rebuilt)
    # 文本可能略有格式差异（└─ vs ├─），比较结构
    assert len(nodes2) == 2
    assert nodes2[0]["text"] == "web"
    assert nodes2[1]["text"] == "POS"


def test_kb_files_excludes_bulk_index():
    """知识库管理只暴露 curated KB，不把 _bulk-index 原始中间产物列入编辑范围。"""
    files = _kb_files("wms")
    all_paths = [f["path"] for group in files.values() for f in group]
    assert all_paths
    assert all("_bulk-index" not in p for p in all_paths)


class FakeStore:
    def __init__(self):
        self.saved = []
        self.exported = []

    def read_text(self, rel, fallback=None):
        if rel == "_kb/projects/dbonly/modules.md":
            return "# DB\n\n## 2. CodeArts 用例存放目录树\n\n```text\n├─ old\n```\n\n## 3. 后续\nDB tail\n"
        if rel == "_kb/projects/dbonly/rules.md":
            return "# Rules\n\n## 1. DB Only\n"
        return ""

    def upsert_text(self, rel, content, **kwargs):
        self.saved.append((rel, content, kwargs))

    def export_documents(self, product):
        self.exported.append(product)

    def classify_path(self, path):
        return "dbonly", "product", "rules"

    def search(self, q, product=None):
        return []

    def list_products(self):
        return ["dbonly"]


@pytest.mark.parametrize("bad", ["../_kb/projects/wms/rules.md", "_kb2/projects/wms/rules.md"])
def test_safe_kb_path_rejects_boundary_bypass(bad):
    with pytest.raises(HTTPException):
        admin._safe_kb_path(bad, require_exists=False)


def test_list_products_prefers_db(monkeypatch):
    monkeypatch.setattr(admin, "_kb_store", lambda: FakeStore())
    assert admin._list_products() == ["dbonly"]


def test_kb_view_allows_db_only_document(monkeypatch):
    monkeypatch.setattr(admin, "_kb_store", lambda: FakeStore())
    resp = asyncio.run(admin.kb_view_file("_kb/projects/dbonly/rules.md", user=object()))
    data = json.loads(resp.body.decode("utf-8"))
    assert data["content"].startswith("# Rules")
    assert data["name"] == "rules.md"


def test_modules_save_uses_db_content_and_preserves_tail(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(admin, "_kb_store", lambda: store)
    nodes = [{"text": "web", "children": []}]
    resp = asyncio.run(admin.modules_save("dbonly", json.dumps(nodes, ensure_ascii=False), user=object()))
    data = json.loads(resp.body.decode("utf-8"))
    assert data["store"] == "postgres"
    assert store.saved
    rel, content, kwargs = store.saved[0]
    assert rel == "_kb/projects/dbonly/modules.md"
    assert "web" in content and "old" not in content
    assert "DB tail" in content


def test_kb_search_db_empty_does_not_fallback_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "_kb_store", lambda: FakeStore())
    kb = tmp_path / "_kb"
    (kb / "_global").mkdir(parents=True)
    (kb / "_global" / "x.md").write_text("needle", encoding="utf-8")
    monkeypatch.setattr(admin, "_kb_root", lambda: kb)
    resp = asyncio.run(admin.kb_search("needle", user=object()))
    data = json.loads(resp.body.decode("utf-8"))
    assert data["results"] == []


def _login_cookie(client: TestClient, monkeypatch, tmp_path, username="u", role="测试工程师"):
    store = auth.UserStore(tmp_path / f"{username}.json")
    salt, pwd_hash = auth.hash_password("pw")
    store.upsert(auth.User(username=username, display_name=username, role=role, salt=salt, pwd_hash=pwd_hash))
    monkeypatch.setattr(auth, "store", store)
    client.get("/login")
    token = client.cookies.get("qa_csrf")
    resp = client.post("/login", data={"username": username, "password": "pw", "csrf_token": token}, follow_redirects=False)
    assert resp.status_code == 303
    return client.cookies.get("qa_csrf")


def test_admin_kb_data_routes_require_login():
    from webapp.main import app

    client = TestClient(app)
    assert client.get("/admin/kb/view?file=_kb/projects/wms/rules.md", follow_redirects=False).status_code in {303, 401}
    assert client.get("/admin/kb/search?q=规则", follow_redirects=False).status_code in {303, 401}
    assert client.post("/admin/kb/save", data={"file": "_kb/projects/wms/rules.md", "content": "x"}, follow_redirects=False).status_code in {303, 401, 403}
    assert client.post("/admin/modules/save", data={"product": "wms", "tree_json": "[]"}, follow_redirects=False).status_code in {303, 401, 403}


def test_admin_routes_require_admin_role(monkeypatch, tmp_path):
    from webapp.main import app

    client = TestClient(app)
    _login_cookie(client, monkeypatch, tmp_path, role="测试工程师")
    assert client.get("/admin/kb?product=wms").status_code == 403
    assert client.get("/admin/modules?product=wms").status_code == 403


def test_admin_post_requires_csrf_for_admin(monkeypatch, tmp_path):
    from webapp.main import app

    client = TestClient(app)
    token = _login_cookie(client, monkeypatch, tmp_path, role="admin")
    assert token
    resp = client.post("/admin/kb/save", data={"file": "_kb/projects/wms/rules.md", "content": "x"})
    assert resp.status_code == 403

    class Store(FakeStore):
        def read_text(self, rel, fallback=None):
            if rel == "_kb/projects/wms/rules.md":
                return "# Rules\n"
            return super().read_text(rel, fallback)

        def classify_path(self, path):
            return "wms", "product", "rules"

    monkeypatch.setattr(admin, "_kb_store", lambda: Store())
    ok = client.post(
        "/admin/kb/save",
        data={"file": "_kb/projects/wms/rules.md", "content": "# New\n", "csrf_token": token},
    )
    assert ok.status_code == 200
    assert ok.json()["ok"] is True


def test_logout_revokes_old_session(monkeypatch, tmp_path):
    from webapp.main import app

    monkeypatch.setattr(auth.config, "REVOKED_SESSIONS_FILE", tmp_path / "revoked.json")
    client = TestClient(app)
    _login_cookie(client, monkeypatch, tmp_path, role="admin")
    old_session = client.cookies.get("qa_session")
    assert old_session
    assert client.get("/logout", follow_redirects=False).status_code == 303
    client.cookies.set("qa_session", old_session)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_audit_redacts_secret_values(monkeypatch, tmp_path):
    from webapp import config
    from webapp.deps import audit

    monkeypatch.setattr(config, "AUDIT_LOG", tmp_path / "audit.log")
    audit(None, "test", "api_key=abc123 postgresql://u:secret@db/app token:xyz")
    text = (tmp_path / "audit.log").read_text(encoding="utf-8")
    assert "abc123" not in text
    assert "secret@db" not in text
    assert "token:xyz" not in text
    assert "[REDACTED]" in text
