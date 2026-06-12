"""PostgreSQL KB store helpers: parsing/rendering and safe migration boundaries."""

import importlib.util
import os
import sys

import pytest

from webapp import config

if str(config.SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(config.SCRIPTS_DIR))

import kb_store  # noqa: E402


def test_curated_files_excludes_bulk_index():
    files = [p.relative_to(config.REPO_ROOT).as_posix() for p in kb_store.curated_files()]
    assert files
    assert all("/_bulk-index/" not in f for f in files)
    assert "_kb/projects/wms/rules.md" in files
    assert "_kb/projects/wms/modules.md" in files
    assert "_kb/projects/wms/terms.md" in files


def test_modules_tree_roundtrip_helpers():
    lines = ["├─ web", "│  ├─ 标品管理", "│  │  └─ 业务规则", "├─ POS"]
    nodes = kb_store.tree_lines_to_nodes(lines)
    assert nodes[0]["text"] == "web"
    assert nodes[0]["children"][0]["children"][0]["text"] == "业务规则"
    rebuilt = kb_store.nodes_to_tree_lines(nodes)
    assert rebuilt[0] == "├─ web"
    assert any("业务规则" in line for line in rebuilt)


def test_replace_modules_tree_keeps_surrounding_sections():
    content = "# M\n\n## 2. CodeArts 用例存放目录树\n\n```text\n├─ old\n```\n\n## 3. 后续\n正文\n"
    out = kb_store.replace_modules_tree(content, [{"text": "web", "children": []}])
    assert "web" in out and "old" not in out
    assert "## 3. 后续" in out and "正文" in out


def test_upsert_text_forces_global_scope(monkeypatch):
    calls = []

    class FakeRow(dict):
        pass

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            calls.append((sql, params))
            if "RETURNING id" in sql:
                return FakeResult({"id": "doc-1"})
            return FakeResult({})

        def commit(self):
            calls.append(("commit", None))

    class FakeResult:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return FakeRow(self.row)

    monkeypatch.setattr(kb_store, "connect", lambda: FakeConn())
    monkeypatch.setattr(kb_store, "refresh_structured", lambda *args: calls.append(("refresh", args)))

    kb_store.upsert_text("_kb/_global/case-writing-spec.md", "# Spec", product="wms", kind="spec")

    upsert = next(params for sql, params in calls if "INSERT INTO kb_documents" in sql)
    assert upsert[1] is None
    assert upsert[2] == "global"


def test_upsert_text_rejects_product_mismatch():
    with pytest.raises(kb_store.KBStoreError):
        kb_store.upsert_text("_kb/projects/wms/rules.md", "# Rules", product="erp", kind="rules")


def test_extract_source_accepts_non_wms_ticket_key():
    source, ticket_key = kb_store._extract_source("- 来源：关联工单 OMS-1234 评论原文")

    assert source == "来源：关联工单 OMS-1234 评论原文"
    assert ticket_key == "OMS-1234"


def test_search_maps_chunk_match_to_document_line(monkeypatch):
    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            return FakeResult([
                {
                    "path": "_kb/projects/wms/rules.md",
                    "ref": "§2.1",
                    "title": "业务规则",
                    "body": "规则正文含 needle。",
                    "document_content": "# R\n\n## 1. 其他\n\n## 2. 业务规则\n\n规则正文含 needle。\n",
                    "score": 0.8,
                }
            ])

    class FakeResult:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    monkeypatch.setattr(kb_store, "connect", lambda: FakeConn())

    results = kb_store.search("needle", "wms")

    assert results[0]["line"] == 7


def test_export_product_excludes_global_documents(monkeypatch, tmp_path):
    docs = [
        {"path": "_kb/_global/spec.md", "product": None, "scope": "global", "content": "# Global"},
        {"path": "_kb/projects/wms/rules.md", "product": "wms", "scope": "product", "content": "# WMS"},
    ]

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            selected = []
            for doc in docs:
                if params[0] is None:
                    selected.append(doc)
                elif doc["product"] == params[2]:
                    selected.append(doc)
            return FakeResult(selected)

    class FakeResult:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    monkeypatch.setattr(kb_store, "connect", lambda: FakeConn())
    monkeypatch.setattr(kb_store, "REPO_ROOT", tmp_path)

    assert kb_store.export_documents("wms") == 1
    assert not (tmp_path / "_kb" / "_global" / "spec.md").exists()
    assert (tmp_path / "_kb" / "projects" / "wms" / "rules.md").read_text(encoding="utf-8") == "# WMS\n"


def test_validate_containers_reads_modules_from_db(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "validate_containers_test", config.SCRIPTS_DIR / "validate-containers.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    class FakeKBStore:
        @staticmethod
        def read_text(rel, fallback=None):
            assert rel == "_kb/projects/dbonly/modules.md"
            return "# M\n\n## 2. CodeArts 用例存放目录树\n\n```text\n├─ web\n   └─ DB节点\n```\n"

    monkeypatch.setattr(mod, "kb_store", FakeKBStore)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)

    edges, roots = mod.parse_modules_tree("dbonly")
    assert roots == {"web"}
    assert ("web", "DB节点") in edges


def test_migrate_fails_in_strict_mode_without_curated_files(monkeypatch):
    monkeypatch.setenv("QA_KB_STRICT", "1")
    monkeypatch.setattr(kb_store, "init_schema", lambda: None)
    monkeypatch.setattr(kb_store, "curated_files", lambda: [])

    with pytest.raises(kb_store.KBStoreError):
        kb_store.migrate()


def test_strict_db_mode_rejects_markdown_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("QA_KB_STRICT", "1")

    class BrokenConn:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, exc_type, exc, tb):
            return False

    fallback = tmp_path / "rules.md"
    fallback.write_text("# should not be read\n", encoding="utf-8")
    monkeypatch.setattr(kb_store, "connect", lambda: BrokenConn())

    with pytest.raises(kb_store.KBStoreError):
        kb_store.read_text("_kb/projects/wms/rules.md", fallback)


def test_database_dsn_uses_webapp_url_and_strips_sqlalchemy_dialect(monkeypatch):
    monkeypatch.delenv("QA_KB_DATABASE_URL", raising=False)
    monkeypatch.delenv("QA_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("QA_WEBAPP_DATABASE_URL", "postgresql+psycopg://u:p@postgres:5432/qa_workflow")

    assert kb_store.database_dsn() == "postgresql://u:p@postgres:5432/qa_workflow"


def test_load_raw_config_treats_system_exit_as_optional(monkeypatch):
    scripts_dir = str(config.REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import _load_env  # noqa: E402

    monkeypatch.setattr(_load_env, "load_raw_config", lambda: (_ for _ in ()).throw(SystemExit(1)))
    assert kb_store._load_raw_config() == {}


def test_database_is_available_and_migrated():
    if os.environ.get("QA_KB_TEST_DB") != "1":
        pytest.skip("set QA_KB_TEST_DB=1 to run real PostgreSQL KB smoke test")
    ok, reason = kb_store.available()
    assert ok, reason
    status = kb_store.status()
    assert status["kb_documents"] >= 14
    assert status["kb_rule_sections"] >= 200
    assert status["kb_module_nodes"] >= 100
