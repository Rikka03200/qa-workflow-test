#!/usr/bin/env python
"""PostgreSQL-backed knowledge-base store with Markdown compatibility.

Design constraints:
- PostgreSQL is the structured source after migration.
- Existing qa-workflow consumers still expect Markdown/text blocks.
- Every DB read has a file fallback so generation does not fail hard when DB is offline.
- `_kb/projects/*/_bulk-index/` is scratch/raw evidence and is intentionally excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
KB_ROOT = REPO_ROOT / "_kb"

from core.productcfg import DEFAULT_PRODUCT

try:  # Optional until webapp requirements are installed.
    import psycopg
    from psycopg.rows import dict_row
except Exception:  # noqa: BLE001
    psycopg = None
    dict_row = None


class KBStoreError(RuntimeError):
    """Raised for database-backed KB failures."""


@dataclass(frozen=True)
class KBDocument:
    path: str
    product: str | None
    kind: str
    scope: str
    title: str
    content: str


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_raw_config() -> dict:
    config_file = REPO_ROOT / "config" / "config.local.yaml"
    if not config_file.exists():
        return {}
    try:
        if str(REPO_ROOT / "scripts") not in sys.path:
            sys.path.insert(0, str(REPO_ROOT / "scripts"))
        import _load_env  # type: ignore
        return _load_env.load_raw_config() or {}
    except BaseException:
        return {}


def _psycopg_dsn(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def _postgres_dsn_or_none(value: str) -> str | None:
    raw = value.strip()
    lowered = raw.lower()
    if "://" not in lowered:
        return raw
    if lowered.startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
        return _psycopg_dsn(raw)
    return None


def database_dsn() -> str:
    """Resolve KB database DSN without exposing secrets.

    Priority:
    1. QA_KB_DATABASE_URL.
    2. QA_WEBAPP_DATABASE_URL / QA_DATABASE_URL / DATABASE_URL env.
    3. config.local.yaml database.knowledge_url / database.url.
    4. config.local.yaml database.{host,port,name,user,password}.
    5. Local developer default: qa_knowledge/postgres on localhost:5432.
    """
    for key in ("QA_KB_DATABASE_URL", "QA_WEBAPP_DATABASE_URL", "QA_DATABASE_URL", "DATABASE_URL"):
        value = os.environ.get(key)
        if value:
            dsn = _postgres_dsn_or_none(value)
            if dsn:
                return dsn

    cfg = _load_raw_config()
    db = cfg.get("database") or cfg.get("postgres") or {}
    url = db.get("knowledge_url") or db.get("url") or db.get("dsn")
    if url:
        dsn = _postgres_dsn_or_none(str(url))
        if dsn:
            return dsn

    name = db.get("knowledge_db") or db.get("dbname") or db.get("database") or "qa_knowledge"
    user = db.get("user") or db.get("username") or "postgres"
    host = db.get("host") or "localhost"
    port = db.get("port") or 5432
    parts = [f"dbname={name}", f"user={user}", f"host={host}", f"port={port}"]
    if db.get("password"):
        parts.append(f"password={db['password']}")
    return " ".join(parts)


def _require_driver() -> None:
    if psycopg is None:
        raise KBStoreError("缺少 psycopg；请安装 webapp/requirements-web.txt 中的 PostgreSQL 驱动")


def connect():
    _require_driver()
    return psycopg.connect(database_dsn(), row_factory=dict_row)


def strict_db_mode() -> bool:
    """Return True when KB reads must come from PostgreSQL without Markdown fallback."""
    value = os.environ.get("QA_KB_STRICT", "") or os.environ.get("QA_KB_DB_REQUIRED", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def available() -> tuple[bool, str]:
    try:
        with connect() as conn:
            conn.execute("select 1")
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE TABLE IF NOT EXISTS kb_products (
    product text PRIMARY KEY,
    display_name text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    path text NOT NULL UNIQUE,
    product text REFERENCES kb_products(product) ON DELETE CASCADE,
    scope text NOT NULL CHECK (scope IN ('global', 'product')),
    kind text NOT NULL,
    title text NOT NULL DEFAULT '',
    content text NOT NULL,
    content_hash text NOT NULL,
    source_mtime timestamptz,
    imported_from text NOT NULL DEFAULT 'markdown',
    is_curated boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_rule_sections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product text NOT NULL REFERENCES kb_products(product) ON DELETE CASCADE,
    chapter_no integer NOT NULL,
    title text NOT NULL,
    intro text NOT NULL DEFAULT '',
    sort_order integer NOT NULL DEFAULT 0,
    source_path text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (product, chapter_no)
);

CREATE TABLE IF NOT EXISTS kb_rules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product text NOT NULL REFERENCES kb_products(product) ON DELETE CASCADE,
    section_id uuid REFERENCES kb_rule_sections(id) ON DELETE CASCADE,
    chapter_no integer NOT NULL,
    rule_no integer,
    title text NOT NULL,
    body text NOT NULL,
    source_text text NOT NULL DEFAULT '',
    source_ear text NOT NULL DEFAULT '',
    conflict text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'active',
    content_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_terms (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product text NOT NULL REFERENCES kb_products(product) ON DELETE CASCADE,
    category text NOT NULL,
    term text NOT NULL,
    definition text NOT NULL DEFAULT '',
    note text NOT NULL DEFAULT '',
    raw_row jsonb NOT NULL DEFAULT '{}'::jsonb,
    sort_order integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (product, category, term)
);

CREATE TABLE IF NOT EXISTS kb_module_nodes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product text NOT NULL REFERENCES kb_products(product) ON DELETE CASCADE,
    parent_id uuid REFERENCES kb_module_nodes(id) ON DELETE CASCADE,
    label text NOT NULL,
    depth integer NOT NULL,
    sort_order integer NOT NULL DEFAULT 0,
    path_text text NOT NULL,
    source_section text NOT NULL DEFAULT 'codearts',
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (product, source_section, path_text)
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id uuid REFERENCES kb_documents(id) ON DELETE CASCADE,
    product text REFERENCES kb_products(product) ON DELETE CASCADE,
    path text NOT NULL,
    kind text NOT NULL,
    ref text NOT NULL DEFAULT '',
    title text NOT NULL DEFAULT '',
    body text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding_model text,
    embedding_dim integer,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_audit_events (
    id bigserial PRIMARY KEY,
    actor text NOT NULL DEFAULT '',
    action text NOT NULL,
    product text,
    object_kind text NOT NULL DEFAULT '',
    object_id text NOT NULL DEFAULT '',
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS kb_documents_product_kind_idx ON kb_documents(product, kind);
CREATE INDEX IF NOT EXISTS kb_documents_content_trgm_idx ON kb_documents USING gin (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS kb_rules_body_trgm_idx ON kb_rules USING gin (body gin_trgm_ops);
CREATE INDEX IF NOT EXISTS kb_rules_title_trgm_idx ON kb_rules USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS kb_terms_term_trgm_idx ON kb_terms USING gin (term gin_trgm_ops);
CREATE INDEX IF NOT EXISTS kb_chunks_body_trgm_idx ON kb_chunks USING gin (body gin_trgm_ops);
CREATE INDEX IF NOT EXISTS kb_chunks_title_trgm_idx ON kb_chunks USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS kb_module_nodes_product_path_idx ON kb_module_nodes(product, path_text);
"""

VECTOR_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS embedding vector(1536);
"""

HNSW_SQL = """
CREATE INDEX IF NOT EXISTS kb_chunks_embedding_hnsw_idx
ON kb_chunks USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);
"""


def init_vector_support() -> bool:
    """Enable optional pgvector columns/indexes when the extension is available."""
    try:
        with connect() as conn:
            for statement in [s.strip() for s in VECTOR_SQL.split(";") if s.strip()]:
                conn.execute(statement)
            try:
                conn.execute(HNSW_SQL)
            except Exception:
                # HNSW is optional even when pgvector exists.
                conn.rollback()
                with connect() as retry:
                    retry.execute("select 1")
                return True
            conn.commit()
        return True
    except Exception:
        return False


def init_schema() -> None:
    with connect() as conn:
        for statement in [s.strip() for s in SCHEMA_SQL.split(";") if s.strip()]:
            conn.execute(statement)
        conn.commit()
    init_vector_support()


def classify_path(path: Path) -> tuple[str | None, str, str]:
    rel = path.relative_to(REPO_ROOT).as_posix()
    if rel.startswith("_kb/_global/"):
        return None, "global", _kind_from_name(path)
    m = re.match(r"_kb/projects/([^/]+)/(.+)$", rel)
    if not m:
        return None, "global", _kind_from_name(path)
    product, rest = m.group(1), m.group(2)
    if rest.startswith("case-samples/gold/"):
        return product, "product", "case_gold"
    if rest.startswith("case-samples/"):
        return product, "product", "case_sample"
    return product, "product", _kind_from_name(path)


def _kind_from_name(path: Path) -> str:
    name = path.name.lower()
    if name == "rules.md":
        return "rules"
    if name == "modules.md":
        return "modules"
    if name == "terms.md":
        return "terms"
    if name.endswith(".json"):
        return "json"
    if "schema" in name:
        return "schema"
    if "spec" in name:
        return "spec"
    if "method" in name:
        return "methodology"
    if name == "readme.md":
        return "readme"
    return "markdown"


def curated_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(KB_ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "/_bulk-index/" in rel:
            continue
        if path.suffix.lower() in {".md", ".json"}:
            files.append(path)
    return files


def first_heading(content: str, fallback: str) -> str:
    for line in content.splitlines():
        m = re.match(r"^#\s+(.+?)\s*$", line)
        if m:
            return m.group(1).strip()
    return fallback


def upsert_product(conn, product: str | None) -> None:
    if not product:
        return
    conn.execute(
        """
        INSERT INTO kb_products(product, display_name)
        VALUES (%s, %s)
        ON CONFLICT (product) DO NOTHING
        """,
        (product, product.upper()),
    )


def upsert_document(conn, path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).as_posix()
    content = path.read_text(encoding="utf-8")
    product, scope, kind = classify_path(path)
    upsert_product(conn, product)
    title = first_heading(content, path.name)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    row = conn.execute(
        """
        INSERT INTO kb_documents(path, product, scope, kind, title, content, content_hash, source_mtime)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (path) DO UPDATE SET
            product = EXCLUDED.product,
            scope = EXCLUDED.scope,
            kind = EXCLUDED.kind,
            title = EXCLUDED.title,
            content = EXCLUDED.content,
            content_hash = EXCLUDED.content_hash,
            source_mtime = EXCLUDED.source_mtime,
            updated_at = now()
        RETURNING id
        """,
        (rel, product, scope, kind, title, content, _sha256(content), mtime),
    ).fetchone()
    return str(row["id"])


_CHAPTER_RE = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$", re.M)
_SUB_RE = re.compile(r"^###\s+(\d+)\.(\d+)\s+(.+?)\s*$", re.M)


def _chapter_blocks(text: str) -> list[tuple[int, str, int, int]]:
    matches = list(_CHAPTER_RE.finditer(text))
    blocks = []
    for idx, match in enumerate(matches):
        chap = int(match.group(1))
        title = match.group(2).strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        blocks.append((chap, title, start, end))
    return blocks


def _extract_source(body: str) -> tuple[str, str]:
    source = ""
    ear = ""
    for line in body.splitlines():
        if "来源" in line:
            source = line.strip().lstrip("- ").strip()
            m = re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", source)
            ear = m.group(0) if m else ""
            break
    return source, ear


def refresh_rules(conn, product: str, doc_id: str, path: str, text: str) -> None:
    conn.execute("DELETE FROM kb_rule_sections WHERE product = %s", (product,))
    conn.execute("DELETE FROM kb_chunks WHERE document_id = %s", (doc_id,))
    for order, (chap, title, start, end) in enumerate(_chapter_blocks(text)):
        if chap == 0:
            continue
        body = text[start:end].strip()
        row = conn.execute(
            """
            INSERT INTO kb_rule_sections(product, chapter_no, title, intro, sort_order, source_path)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (product, chap, title, "", order, path),
        ).fetchone()
        section_id = row["id"]
        sub_matches = list(_SUB_RE.finditer(body))
        if not sub_matches:
            source, ear = _extract_source(body)
            conn.execute(
                """
                INSERT INTO kb_rules(product, section_id, chapter_no, rule_no, title, body, source_text, source_ear, content_hash)
                VALUES (%s, %s, %s, NULL, %s, %s, %s, %s, %s)
                """,
                (product, section_id, chap, title, body, source, ear, _sha256(body)),
            )
            _insert_chunk(conn, doc_id, product, path, "rule_section", f"§{chap}", title, body, {"chapter": chap})
            continue
        intro = body[:sub_matches[0].start()].strip()
        conn.execute("UPDATE kb_rule_sections SET intro = %s WHERE id = %s", (intro, section_id))
        for sub_idx, sub in enumerate(sub_matches):
            sub_chap, rule_no, sub_title = int(sub.group(1)), int(sub.group(2)), sub.group(3).strip()
            sub_start = sub.end()
            sub_end = sub_matches[sub_idx + 1].start() if sub_idx + 1 < len(sub_matches) else len(body)
            sub_body = body[sub_start:sub_end].strip()
            source, ear = _extract_source(sub_body)
            conn.execute(
                """
                INSERT INTO kb_rules(product, section_id, chapter_no, rule_no, title, body, source_text, source_ear, content_hash)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (product, section_id, sub_chap, rule_no, sub_title, sub_body, source, ear, _sha256(sub_body)),
            )
            _insert_chunk(
                conn, doc_id, product, path, "rule", f"§{sub_chap}.{rule_no}", sub_title, sub_body,
                {"chapter": sub_chap, "rule_no": rule_no, "source": source, "ear": ear},
            )


def _modules_tree_block(text: str) -> list[str]:
    parts = re.split(r"(?m)^##\s*2[.\s]", text)
    if len(parts) < 2:
        return []
    after = re.split(r"(?m)^##\s", parts[1])[0]
    blocks = re.findall(r"```(?:text)?\s*(.*?)```", after, re.S)
    return blocks[0].splitlines() if blocks else []


def tree_lines_to_nodes(lines: Iterable[str]) -> list[dict[str, Any]]:
    stack: list[tuple[int, dict[str, Any]]] = []
    roots: list[dict[str, Any]] = []
    for raw in lines:
        if not raw.strip():
            continue
        m = re.match(r"^([│\s]*)[├└]─+\s*(.+?)\s*$", raw)
        if not m:
            continue
        depth = len(m.group(1)) // 3
        node = {"text": m.group(2).strip(), "children": []}
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if stack:
            stack[-1][1]["children"].append(node)
        else:
            roots.append(node)
        stack.append((depth, node))
    return roots


def nodes_to_tree_lines(nodes: list[dict[str, Any]], level: int = 0) -> list[str]:
    lines: list[str] = []
    for idx, node in enumerate(nodes):
        marker = "└─ " if idx == len(nodes) - 1 else "├─ "
        lines.append(f"{'   ' * level}{marker}{node.get('text', '')}")
        children = node.get("children") or []
        if children:
            lines.extend(nodes_to_tree_lines(children, level + 1))
    return lines


def refresh_modules(conn, product: str, doc_id: str, path: str, text: str) -> None:
    conn.execute("DELETE FROM kb_module_nodes WHERE product = %s AND source_section = 'codearts'", (product,))
    conn.execute("DELETE FROM kb_chunks WHERE document_id = %s", (doc_id,))
    nodes = tree_lines_to_nodes(_modules_tree_block(text))

    def walk(items: list[dict[str, Any]], parent_id, depth: int, parents: list[str]) -> None:
        for order, node in enumerate(items):
            label = str(node.get("text", "")).strip()
            if not label:
                continue
            path_parts = parents + [label]
            path_text = ">".join(path_parts)
            row = conn.execute(
                """
                INSERT INTO kb_module_nodes(product, parent_id, label, depth, sort_order, path_text, source_section)
                VALUES (%s, %s, %s, %s, %s, %s, 'codearts')
                RETURNING id
                """,
                (product, parent_id, label, depth, order, path_text),
            ).fetchone()
            _insert_chunk(conn, doc_id, product, path, "module", path_text, label, path_text, {"depth": depth})
            walk(node.get("children") or [], row["id"], depth + 1, path_parts)

    walk(nodes, None, 0, [])


def _parse_table_row(line: str) -> list[str] | None:
    if not line.strip().startswith("|") or not line.strip().endswith("|"):
        return None
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if cells and all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in cells):
        return None
    return cells


def refresh_terms(conn, product: str, doc_id: str, path: str, text: str) -> None:
    conn.execute("DELETE FROM kb_terms WHERE product = %s", (product,))
    conn.execute("DELETE FROM kb_chunks WHERE document_id = %s", (doc_id,))
    category = ""
    headers: list[str] = []
    order = 0
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            category = m.group(1).strip()
            headers = []
            continue
        cells = _parse_table_row(line)
        if not cells:
            continue
        if not headers:
            headers = cells
            continue
        raw = dict(zip(headers, cells))
        term = cells[0] if cells else ""
        definition = cells[1] if len(cells) > 1 else ""
        note = cells[2] if len(cells) > 2 else ""
        if not term or term in {"术语", "参数"}:
            continue
        order += 1
        conn.execute(
            """
            INSERT INTO kb_terms(product, category, term, definition, note, raw_row, sort_order)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (product, category, term) DO UPDATE SET
                definition = EXCLUDED.definition,
                note = EXCLUDED.note,
                raw_row = EXCLUDED.raw_row,
                sort_order = EXCLUDED.sort_order
            """,
            (product, category, term, definition, note, json.dumps(raw, ensure_ascii=False), order),
        )
        _insert_chunk(conn, doc_id, product, path, "term", category, term, f"{term}\n{definition}\n{note}", raw)


def _insert_chunk(conn, doc_id: str, product: str | None, path: str, kind: str, ref: str,
                  title: str, body: str, metadata: dict[str, Any]) -> None:
    if not body.strip():
        return
    conn.execute(
        """
        INSERT INTO kb_chunks(document_id, product, path, kind, ref, title, body, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (doc_id, product, path, kind, ref, title, body, json.dumps(metadata, ensure_ascii=False)),
    )


def refresh_generic_chunks(conn, product: str | None, doc_id: str, path: str, kind: str, text: str) -> None:
    conn.execute("DELETE FROM kb_chunks WHERE document_id = %s", (doc_id,))
    chunks = _markdown_heading_chunks(text)
    if not chunks:
        _insert_chunk(conn, doc_id, product, path, kind, "", first_heading(text, Path(path).name), text, {})
        return
    for ref, title, body in chunks:
        _insert_chunk(conn, doc_id, product, path, kind, ref, title, body, {})


def _markdown_heading_chunks(text: str) -> list[tuple[str, str, str]]:
    matches = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, re.M))
    chunks = []
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        title = match.group(2).strip()
        body = text[start:end].strip()
        ref = title
        chunks.append((ref, title, body or title))
    return chunks


def refresh_structured(conn, rel_path: str, doc_id: str, product: str | None, kind: str, content: str) -> None:
    if product and kind == "rules":
        refresh_rules(conn, product, doc_id, rel_path, content)
    elif product and kind == "modules":
        refresh_modules(conn, product, doc_id, rel_path, content)
    elif product and kind == "terms":
        refresh_terms(conn, product, doc_id, rel_path, content)
    else:
        refresh_generic_chunks(conn, product, doc_id, rel_path, kind, content)


def migrate(files: Optional[list[Path]] = None) -> dict[str, int]:
    init_schema()
    selected = curated_files() if files is None else files
    stats = {"documents": 0, "skipped": 0, "source_files": len(selected)}
    if strict_db_mode() and not selected:
        raise KBStoreError("严格 KB 数据库模式下未找到可导入的 curated 知识库文件")
    with connect() as conn:
        for path in selected:
            rel = path.relative_to(REPO_ROOT).as_posix()
            if "/_bulk-index/" in rel:
                stats["skipped"] += 1
                continue
            doc_id = upsert_document(conn, path)
            product, _scope, kind = classify_path(path)
            content = path.read_text(encoding="utf-8")
            refresh_structured(conn, rel, doc_id, product, kind, content)
            stats["documents"] += 1
        conn.execute(
            """
            INSERT INTO kb_audit_events(action, object_kind, detail)
            VALUES ('migrate', 'kb', %s)
            """,
            (json.dumps(stats, ensure_ascii=False),),
        )
        conn.commit()
    return stats


def read_text(rel_path: str, fallback: Path | None = None) -> str:
    rel = rel_path.replace("\\", "/").lstrip("/")
    try:
        with connect() as conn:
            row = conn.execute("SELECT content FROM kb_documents WHERE path = %s", (rel,)).fetchone()
            if row:
                return row["content"]
    except Exception as exc:  # noqa: BLE001
        if strict_db_mode():
            raise KBStoreError(f"知识库数据库不可用，已禁止 Markdown 兜底：{type(exc).__name__}: {exc}") from exc
    if strict_db_mode():
        raise KBStoreError(f"知识库数据库缺少文档，已禁止 Markdown 兜底：{rel}")
    path = fallback or (REPO_ROOT / rel)
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def upsert_text(rel_path: str, content: str, *, product: str | None = None,
                kind: str | None = None, actor: str = "") -> None:
    rel = rel_path.replace("\\", "/").lstrip("/")
    path = REPO_ROOT / rel
    inferred_product, scope, inferred_kind = classify_path(path)
    if scope == "global":
        product = None
    elif product is not None and product != inferred_product:
        raise KBStoreError(f"知识库路径产品不匹配：{rel} 属于 {inferred_product}，不能写入 {product}")
    else:
        product = inferred_product
    kind = kind or inferred_kind
    with connect() as conn:
        upsert_product(conn, product)
        title = first_heading(content, path.name)
        row = conn.execute(
            """
            INSERT INTO kb_documents(path, product, scope, kind, title, content, content_hash, source_mtime, imported_from)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now(), 'webapp')
            ON CONFLICT (path) DO UPDATE SET
                product = EXCLUDED.product,
                scope = EXCLUDED.scope,
                kind = EXCLUDED.kind,
                title = EXCLUDED.title,
                content = EXCLUDED.content,
                content_hash = EXCLUDED.content_hash,
                updated_at = now()
            RETURNING id
            """,
            (rel, product, scope, kind, title, content, _sha256(content)),
        ).fetchone()
        refresh_structured(conn, rel, str(row["id"]), product, kind, content)
        conn.execute(
            """
            INSERT INTO kb_audit_events(actor, action, product, object_kind, object_id, detail)
            VALUES (%s, 'document.upsert', %s, %s, %s, %s)
            """,
            (actor, product, kind, rel, json.dumps({"hash": _sha256(content)}, ensure_ascii=False)),
        )
        conn.commit()


def list_products() -> list[str]:
    try:
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT product FROM kb_products
                UNION
                SELECT DISTINCT product FROM kb_documents WHERE product IS NOT NULL
                ORDER BY product
                """
            ).fetchall()
    except Exception:
        return []
    return [row["product"] for row in rows if row.get("product")]


def document_exists(rel_path: str) -> bool:
    rel = rel_path.replace("\\", "/").lstrip("/")
    try:
        with connect() as conn:
            row = conn.execute("SELECT 1 FROM kb_documents WHERE path = %s", (rel,)).fetchone()
            return bool(row)
    except Exception:
        return False


def list_files(product: str | None = None) -> dict[str, list[dict[str, Any]]]:
    try:
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT path, product, scope, kind, title, length(content) AS size, updated_at
                FROM kb_documents
                WHERE scope = 'global' OR (%s::text IS NOT NULL AND product = %s)
                ORDER BY scope, path
                """,
                (product, product),
            ).fetchall()
    except Exception:
        return {"global": [], "product": []}
    out = {"global": [], "product": []}
    for row in rows:
        item = {
            "name": Path(row["path"]).name,
            "path": row["path"],
            "kind": row["kind"],
            "title": row["title"],
            "size": row["size"],
            "mtime": row["updated_at"].strftime("%Y-%m-%d %H:%M") if row["updated_at"] else "",
        }
        out["global" if row["scope"] == "global" else "product"].append(item)
    return out


def _document_line_for_chunk(document: str, body: str, query: str, title: str, ref: str) -> int:
    """Map a chunk match back to the original Markdown document line."""
    if not document:
        return 1
    lowered_query = query.lower()
    pos = -1
    if body:
        body_pos = document.find(body)
        if body_pos >= 0:
            query_pos = body.lower().find(lowered_query)
            pos = body_pos + query_pos if query_pos >= 0 else body_pos
    if pos < 0 and title:
        title_pos = document.lower().find(title.lower())
        if title_pos >= 0:
            query_pos = title.lower().find(lowered_query)
            pos = title_pos + query_pos if query_pos >= 0 else title_pos
    if pos < 0 and ref:
        ref_pos = document.lower().find(ref.lower())
        if ref_pos >= 0:
            pos = ref_pos
    if pos < 0:
        pos = document.lower().find(lowered_query)
    if pos < 0:
        return 1
    return document[:pos].count("\n") + 1


def search(q: str, product: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    query = (q or "").strip()
    if not query:
        return []
    try:
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT c.path, c.ref, c.title, c.body, d.content AS document_content,
                       GREATEST(similarity(c.title, %s), similarity(c.body, %s)) AS score,
                       CASE WHEN c.product = %s THEN 2 ELSE 0 END AS product_boost,
                       CASE c.kind
                           WHEN 'rule' THEN 3
                           WHEN 'rule_section' THEN 3
                           WHEN 'term' THEN 2
                           WHEN 'module' THEN 2
                           ELSE 0
                       END AS kind_boost
                FROM kb_chunks c
                JOIN kb_documents d ON d.id = c.document_id
                WHERE (%s::text IS NULL OR c.product IS NULL OR c.product = %s)
                  AND (c.title ILIKE %s OR c.body ILIKE %s OR c.title %% %s OR c.body %% %s)
                ORDER BY product_boost DESC, kind_boost DESC, score DESC, c.path, c.ref
                LIMIT %s
                """,
                (query, query, product, product, product, f"%{query}%", f"%{query}%", query, query, limit),
            ).fetchall()
    except Exception:
        return []
    results = []
    for row in rows:
        body = row["body"] or ""
        pos = body.lower().find(query.lower())
        if pos < 0:
            context = body[:300]
        else:
            start = max(0, pos - 120)
            end = min(len(body), pos + len(query) + 180)
            context = body[start:end]
        line = _document_line_for_chunk(
            row["document_content"] or "",
            body,
            query,
            row["title"] or "",
            row["ref"] or "",
        )
        results.append({
            "file": row["path"],
            "line": line,
            "text": row["title"] or row["ref"] or row["path"],
            "context": context.strip(),
            "score": float(row["score"] or 0),
        })
    return results


def export_documents(product: str | None = None, *, overwrite: bool = True) -> int:
    count = 0
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT path, content FROM kb_documents
            WHERE (%s::text IS NULL AND scope IN ('global', 'product'))
               OR (%s::text IS NOT NULL AND product = %s)
            ORDER BY path
            """,
            (product, product, product),
        ).fetchall()
    for row in rows:
        path = REPO_ROOT / row["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            continue
        path.write_text(row["content"].rstrip() + "\n", encoding="utf-8")
        count += 1
    return count


def replace_modules_tree(content: str, nodes: list[dict[str, Any]]) -> str:
    lines = content.splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"^##\s*2[.\s]", line)), None)
    if start is None:
        raise ValueError("modules.md 未找到 §2 标题")
    tree_start = next((i for i in range(start, len(lines)) if lines[i].strip().startswith("```text")), None)
    if tree_start is None:
        raise ValueError("modules.md §2 未找到 ```text 树块")
    tree_end = next((i for i in range(tree_start + 1, len(lines)) if lines[i].strip() == "```"), None)
    if tree_end is None:
        raise ValueError("modules.md §2 树块未闭合")
    return "\n".join(lines[:tree_start + 1] + nodes_to_tree_lines(nodes) + lines[tree_end:]) + "\n"


def append_backfill_rules(product: str, ear: str, rules: list[dict[str, Any]], date: str = "") -> dict[str, Any]:
    rel = f"_kb/projects/{product}/rules.md"
    text = read_text(rel, REPO_ROOT / rel)
    if not text:
        return {"ok": False, "applied": 0, "chapter": None, "notes": f"知识库文件不存在：{rel}"}
    selected = [r for r in (rules or []) if (r or {}).get("content")]
    if not selected:
        return {"ok": False, "applied": 0, "chapter": None, "notes": "没有选中可入库的规则"}
    today = date or datetime.now().strftime("%Y-%m-%d")
    chapter = _find_backfill_chapter(text)
    if chapter is None:
        chapter = _next_chapter_no(text)
        text = _insert_toc_ref(text, chapter, "知识回填（系统沉淀）")
        text = text.rstrip() + (
            f"\n\n## {chapter}. 知识回填（系统沉淀）\n\n"
            f"> 本章由控制台「知识回填」累积：从工单修改方案/已确认待确认提炼、人工审批后入库；"
            f"每条标注来源与工单，可后续人工归并到对应业务章节。\n"
        )
    start, end = _chapter_region(text, chapter)
    seq = len(re.findall(rf"^###\s+{chapter}\.\d+", text[start:end], re.M))
    blocks = []
    for rule in selected:
        seq += 1
        title = (rule.get("title") or "新增规则").strip()
        content = (rule.get("content") or "").strip()
        source = (rule.get("source") or "").strip()
        block = [f"\n### {chapter}.{seq} {title}", "", content, "", f"- 来源：{source}", f"- 入库：{ear} · {today}"]
        if rule.get("conflict"):
            block.append(f"- ⚠ 待人工裁定的冲突：{rule['conflict']}")
        blocks.append("\n".join(block) + "\n")
    start, end = _chapter_region(text, chapter)
    new_text = text[:end].rstrip() + "\n" + "".join(blocks) + "\n" + text[end:]
    ok, reason = _check_rules_toc(new_text)
    if not ok:
        return {"ok": False, "applied": 0, "chapter": chapter,
                "notes": "写入后目录校验未通过，已放弃（知识库未改动）：" + reason[-200:]}
    current_path = REPO_ROOT / rel
    if current_path.exists():
        backup = current_path.with_suffix(f".md.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(current_path, backup)
    upsert_text(rel, new_text, product=product, kind="rules")
    export_documents(product)
    return {"ok": True, "applied": len(selected), "chapter": chapter, "notes": ""}


def _check_rules_toc(text: str) -> tuple[bool, str]:
    script = REPO_ROOT / "scripts" / "kb-check-toc.py"
    with tempfile.NamedTemporaryFile("w", suffix=".rules.md", delete=False, encoding="utf-8") as handle:
        handle.write(text)
        tmp = Path(handle.name)
    try:
        proc = subprocess.run([sys.executable, str(script), "--path", str(tmp)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return False, f"目录校验执行失败：{exc}"
    finally:
        tmp.unlink(missing_ok=True)


def _find_backfill_chapter(text: str) -> int | None:
    for match in _CHAPTER_RE.finditer(text):
        if match.group(2).strip().startswith("知识回填"):
            return int(match.group(1))
    return None


def _next_chapter_no(text: str) -> int:
    nums = [int(m.group(1)) for m in _CHAPTER_RE.finditer(text) if int(m.group(1)) != 0]
    return max(nums) + 1 if nums else 1


def _chapter_region(text: str, chapter: int) -> tuple[int, int]:
    match = re.search(rf"^##\s+{chapter}\.\s+.*$", text, re.M)
    if not match:
        return len(text), len(text)
    nxt = re.search(r"^##\s+\d+\.\s+", text[match.end():], re.M)
    end = match.end() + nxt.start() if nxt else len(text)
    return match.end(), end


def _insert_toc_ref(text: str, chapter: int, title: str) -> str:
    match = re.search(r"^##\s+1\.\s+", text, re.M)
    line = f"- §{chapter} {title}（控制台知识回填累积）\n"
    if not match:
        return text
    return text[:match.start()] + line + "\n" + text[match.start():]


def status() -> dict[str, Any]:
    ok, reason = available()
    out: dict[str, Any] = {"available": ok, "reason": reason}
    if not ok:
        return out
    with connect() as conn:
        out["extensions"] = [dict(row) for row in conn.execute(
            "SELECT extname, extversion FROM pg_extension ORDER BY extname").fetchall()]
        out["vector_available"] = any(row["extname"] == "vector" for row in out["extensions"])
        out["embedding_column"] = bool(conn.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'kb_chunks' AND column_name = 'embedding'
            """
        ).fetchone())
        for table in ("kb_documents", "kb_rule_sections", "kb_rules", "kb_terms", "kb_module_nodes", "kb_chunks"):
            try:
                out[table] = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
            except Exception:
                out[table] = None
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="PostgreSQL-backed qa-workflow KB store")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("migrate")
    exp = sub.add_parser("export")
    exp.add_argument("--product", default=None)
    srch = sub.add_parser("search")
    srch.add_argument("query")
    srch.add_argument("--product", default=DEFAULT_PRODUCT)
    sub.add_parser("status")
    args = parser.parse_args()

    if args.cmd == "init":
        init_schema()
        print("OK: schema initialized")
    elif args.cmd == "migrate":
        print(json.dumps(migrate(), ensure_ascii=False, indent=2))
    elif args.cmd == "export":
        print(json.dumps({"exported": export_documents(args.product)}, ensure_ascii=False))
    elif args.cmd == "search":
        print(json.dumps(search(args.query, args.product), ensure_ascii=False, indent=2))
    elif args.cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
