# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Iterator


ISSUE_STATUSES = {
    "pending",
    "triaged",
    "in_progress",
    "resolved",
    "wont_fix",
    "duplicate",
    "archived",
}
REVIEW_DECISIONS = {"approved", "todo", "deferred", "rejected"}
REVIEW_ACTIONS = {
    "approve": "approved",
    "approved": "approved",
    "todo": "todo",
    "defer": "deferred",
    "deferred": "deferred",
    "reject": "rejected",
    "rejected": "rejected",
}
TODO_STATUSES = {"open", "completed"}
ISSUE_KINDS = {"bug", "question", "optimization"}
ISSUE_SEVERITIES = {"low", "medium", "high", "critical"}
ISSUE_CATEGORIES = {
    "known_bug",
    "fixed_bug",
    "faq",
    "compatibility",
    "optimization",
    "pending_feedback",
}
ISSUE_DOMAINS = {"plugin", "astrbot", "model", "other"}
PLUGIN_NAME_LIMIT = 120
VERIFICATION_STATUSES = {
    "unverified",
    "confirmed",
    "fix_pending",
    "verified",
    "not_reproducible",
}
CONFIRMED_VERIFICATION_STATUSES = {"confirmed", "verified"}
JSON_FIELDS = {"tags", "platforms", "versions"}
ISSUE_TEXT_LIMITS = {
    "title": 160,
    "summary": 2000,
    "symptoms": 4000,
    "reproduction": 4000,
    "expected": 2000,
    "actual": 2000,
    "environment": 2000,
    "impact": 2000,
    "cause": 4000,
    "solution": 6000,
    "workaround": 4000,
    "reporter_name": 100,
    "introduced_version": 120,
    "fixed_version": 120,
    "applicable_scope": 2000,
}
ISSUE_FIELD_LABELS = {
    "title": "标题",
    "summary": "摘要",
    "symptoms": "现象",
    "reproduction": "复现条件",
    "expected": "期望行为",
    "actual": "实际行为",
    "environment": "实际环境",
    "impact": "影响",
    "cause": "原因",
    "solution": "解决方案",
    "workaround": "临时方案",
    "reporter_name": "反馈人",
    "introduced_version": "出现版本",
    "fixed_version": "修复版本",
    "applicable_scope": "适用范围",
}
MERGE_PRESERVED_TEXT_FIELDS = tuple(
    field for field in ISSUE_TEXT_LIMITS if field not in {"title", "reporter_name"}
)
MERGE_REUSABLE_CATEGORIES = {"known_bug", "fixed_bug", "faq", "compatibility"}
MERGE_TRUSTED_TEXT_FIELDS = {
    "cause",
    "solution",
    "workaround",
    "fixed_version",
    "applicable_scope",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def utc_now_precise() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json_load(value: Any, fallback: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return parsed if isinstance(parsed, type(fallback)) else fallback


def _clean_terms(value: Any) -> list[str]:
    result: list[str] = []
    for item in value if isinstance(value, list) else []:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if text and text not in result:
            result.append(text[:80])
    return result[:20]


def normalize_plugin_name(value: Any, *, truncate: bool = True) -> str:
    """Return the shared config/issue representation for one plugin name."""
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    return normalized[:PLUGIN_NAME_LIMIT] if truncate else normalized


def _knowledge_is_reusable(
    category: str,
    status: str,
    verification: str,
    solution: str,
    workaround: str,
) -> bool:
    if not (solution.strip() or workaround.strip()):
        return False
    if category == "fixed_bug":
        return status == "resolved" and verification == "verified"
    if category == "faq":
        return status == "resolved" and verification in CONFIRMED_VERIFICATION_STATUSES
    if category in {"known_bug", "compatibility"}:
        return status in {"triaged", "in_progress", "resolved", "wont_fix"} and verification in {
            "confirmed",
            "fix_pending",
            "verified",
        }
    return False


def _row_has_reusable_knowledge(row: sqlite3.Row) -> bool:
    if (
        str(row["review_decision"] or "") != "approved"
        or not row["knowledge_published_at"]
    ):
        return False
    return _knowledge_is_reusable(
        str(row["category"] or ""),
        str(row["status"] or ""),
        str(row["verification_status"] or ""),
        str(row["solution"] or ""),
        str(row["workaround"] or ""),
    )


def _merge_state_promotion(
    target: sqlite3.Row,
    sources: dict[str, sqlite3.Row],
    source_ids: list[str],
) -> tuple[dict[str, Any], dict[str, list[str]], str] | None:
    """Return a coherent single-source promotion, or None when it is unsafe."""
    candidates = [
        (source_id, sources[source_id])
        for source_id in source_ids
        if _row_has_reusable_knowledge(sources[source_id])
    ]
    if not candidates:
        return {}, {}, ""

    if _row_has_reusable_knowledge(target):
        return {}, {}, ""

    target_category = str(target["category"] or "pending_feedback")
    target_domain = str(target["issue_domain"] or "other")
    candidate_domains = {
        str(row["issue_domain"] or "other") for _, row in candidates
    }
    if target_domain == "other":
        if len(candidate_domains) != 1:
            return None
        promoted_domain = candidate_domains.pop()
    elif candidate_domains == {target_domain}:
        promoted_domain = target_domain
    else:
        # A verified solution for another owner must not turn this target into
        # reusable knowledge for the wrong component.
        return None
    categories = {str(row["category"] or "") for _, row in candidates}
    if target_category == "pending_feedback":
        if len(categories) != 1:
            return None
        promoted_category = categories.pop()
    elif target_category in MERGE_REUSABLE_CATEGORIES and categories == {target_category}:
        promoted_category = target_category
    else:
        return None

    target_status = str(target["status"] or "pending")
    target_verification = str(target["verification_status"] or "unverified")
    target_solution = str(target["solution"] or "").strip()
    target_workaround = str(target["workaround"] or "").strip()
    for witness_id, witness in candidates:
        witness_status = str(witness["status"] or "")
        witness_verification = str(witness["verification_status"] or "")
        witness_solution = str(witness["solution"] or "").strip()
        witness_workaround = str(witness["workaround"] or "").strip()
        if target_status not in {"pending", witness_status}:
            continue
        if target_verification not in {"unverified", witness_verification}:
            continue
        if target_solution and witness_solution and target_solution != witness_solution:
            continue
        if target_workaround and witness_workaround and target_workaround != witness_workaround:
            continue

        updates: dict[str, Any] = {}
        provenance: dict[str, list[str]] = {}
        promoted_values = {
            "issue_domain": promoted_domain,
            "category": promoted_category,
            "status": witness_status,
            "verification_status": witness_verification,
        }
        defaults = {
            "issue_domain": "other",
            "category": "pending_feedback",
            "status": "pending",
            "verification_status": "unverified",
        }
        for field, value in promoted_values.items():
            if str(target[field] or defaults[field]) == defaults[field]:
                updates[field] = value
                provenance[field] = [witness_id]
        if (
            target_category == "pending_feedback"
            and witness["category"] == "faq"
            and target["kind"] == "bug"
            and witness["kind"] == "question"
        ):
            updates["kind"] = "question"
            provenance["kind"] = [witness_id]
        for field in ("resolved_at", "last_confirmed_at"):
            if not target[field] and witness[field]:
                updates[field] = str(witness[field])
                provenance[field] = [witness_id]
        return updates, provenance, witness_id
    return None


def _merge_trusted_source_ids(
    target: sqlite3.Row,
    sources: dict[str, sqlite3.Row],
    source_ids: list[str],
    witness_id: str,
    effective_plugin_name: str = "",
) -> list[str] | None:
    """Choose coherent reusable sources for fields that can drive later answers."""
    target_reusable = _row_has_reusable_knowledge(target)
    if witness_id:
        anchor = sources[witness_id]
        ordered = [witness_id] + [
            source_id
            for source_id in source_ids
            if source_id != witness_id
            and _row_has_reusable_knowledge(sources[source_id])
        ]
    elif target_reusable:
        anchor = target
        ordered = [
            source_id
            for source_id in source_ids
            if _row_has_reusable_knowledge(sources[source_id])
        ]
    else:
        return []

    anchor_category = str(anchor["category"] or "")
    anchor_domain = str(anchor["issue_domain"] or "other")
    anchor_plugin_name = (
        normalize_plugin_name(effective_plugin_name or anchor["plugin_name"])
        if anchor_domain == "plugin"
        else ""
    )
    known_values = {
        field: str(anchor[field] or "").strip()
        for field in MERGE_TRUSTED_TEXT_FIELDS
    }
    for source_id in ordered:
        source = sources[source_id]
        if str(source["issue_domain"] or "other") != anchor_domain:
            return None
        source_plugin_name = normalize_plugin_name(source["plugin_name"])
        if anchor_domain == "plugin" and source_plugin_name:
            if anchor_plugin_name and source_plugin_name != anchor_plugin_name:
                return None
            anchor_plugin_name = anchor_plugin_name or source_plugin_name
        if str(source["category"] or "") != anchor_category:
            return None
        for field in MERGE_TRUSTED_TEXT_FIELDS:
            value = str(source[field] or "").strip()
            if known_values[field] and value and known_values[field] != value:
                return None
            if value and not known_values[field]:
                known_values[field] = value
    return ordered


def _merge_plugin_name_promotion(
    target: sqlite3.Row,
    sources: dict[str, sqlite3.Row],
    source_ids: list[str],
    effective_domain: str,
) -> tuple[dict[str, Any], dict[str, list[str]]] | None:
    """Promote one consistent concrete plugin only from reusable knowledge."""
    if effective_domain != "plugin":
        return {}, {}
    target_name = normalize_plugin_name(target["plugin_name"])
    candidates = [
        (source_id, normalize_plugin_name(sources[source_id]["plugin_name"]))
        for source_id in source_ids
        if _row_has_reusable_knowledge(sources[source_id])
        and str(sources[source_id]["issue_domain"] or "other") == "plugin"
    ]
    known_names = {name for _, name in candidates if name}
    if target_name:
        if any(name != target_name for name in known_names):
            return None
        return {}, {}
    if len(known_names) > 1:
        return None
    if not known_names:
        return {}, {}
    promoted_name = next(iter(known_names))
    provenance = [source_id for source_id, name in candidates if name == promoted_name]
    return {"plugin_name": promoted_name}, {"plugin_name": provenance}


def issue_change_error(
    changes: Any,
    *,
    creating: bool = False,
    enforce_limits: bool = False,
) -> str:
    """Return a user-facing validation error for page-authored issue fields."""
    if not isinstance(changes, dict):
        return "问题数据格式无效"
    if creating and not str(changes.get("title") or "").strip():
        return "请填写问题标题"
    if "title" in changes and not str(changes.get("title") or "").strip():
        return "问题标题不能为空"
    enum_fields = {
        "kind": ISSUE_KINDS,
        "category": ISSUE_CATEGORIES,
        "issue_domain": ISSUE_DOMAINS,
        "severity": ISSUE_SEVERITIES,
        "status": ISSUE_STATUSES,
        "verification_status": VERIFICATION_STATUSES,
    }
    for field, allowed in enum_fields.items():
        if field in changes and changes.get(field) not in allowed:
            return f"字段 {field} 的值无效"
    if changes.get("status") == "duplicate":
        return "不能直接标记为重复，请使用合并功能建立目标关系"
    plugin_name = normalize_plugin_name(changes.get("plugin_name"), truncate=False)
    if plugin_name and changes.get("issue_domain") not in {None, "plugin"}:
        return "只有插件问题可以填写具体插件名称"
    for field in JSON_FIELDS:
        if field in changes and not isinstance(changes.get(field), list):
            return f"字段 {field} 必须是列表"
    if enforce_limits:
        if len(plugin_name) > PLUGIN_NAME_LIMIT:
            return f"具体插件名称不能超过 {PLUGIN_NAME_LIMIT} 个字符"
        for field, limit in ISSUE_TEXT_LIMITS.items():
            if field not in changes:
                continue
            if len(str(changes.get(field) or "")) > limit:
                label = ISSUE_FIELD_LABELS.get(field, field)
                return f"{label}不能超过 {limit} 个字符"
        for field in JSON_FIELDS:
            if field not in changes:
                continue
            values = changes.get(field) or []
            if len(values) > 20:
                return f"字段 {field} 最多填写 20 项"
            if any(len(str(value or "")) > 80 for value in values):
                return f"字段 {field} 的单项内容不能超过 80 个字符"
    return ""


def _search_surface(issue: dict[str, Any]) -> str:
    parts = [
        issue.get("title"),
        issue.get("summary"),
        issue.get("symptoms"),
        issue.get("reproduction"),
        issue.get("expected"),
        issue.get("actual"),
        issue.get("environment"),
        issue.get("impact"),
        issue.get("cause"),
        issue.get("solution"),
        issue.get("workaround"),
        issue.get("introduced_version"),
        issue.get("fixed_version"),
        issue.get("applicable_scope"),
        " ".join(issue.get("tags") or []),
    ]
    return " ".join(str(part or "") for part in parts).lower()


def _char_bigrams(text: str) -> set[str]:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", text.lower())
    if len(normalized) < 2:
        return {normalized} if normalized else set()
    return {normalized[index : index + 2] for index in range(len(normalized) - 1)}


def similarity_score(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_title = str(left.get("title") or "").lower()
    right_title = str(right.get("title") or "").lower()
    title_score = SequenceMatcher(None, left_title, right_title).ratio()
    left_terms = _char_bigrams(_search_surface(left))
    right_terms = _char_bigrams(_search_surface(right))
    union = left_terms | right_terms
    jaccard = len(left_terms & right_terms) / len(union) if union else 0.0
    same_kind = 0.04 if left.get("kind") == right.get("kind") else 0.0
    return min(1.0, (title_score * 0.48) + (jaccard * 0.48) + same_kind)


class IssueStore:
    """SQLite-backed issue, evidence and answer audit store."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "bug_companion.sqlite3"
        self._lock = threading.RLock()
        self._transaction_local = threading.local()
        self._init_schema()

    @staticmethod
    def _open_connection(db_path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(db_path, timeout=15, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._transaction_local, "connection", None)
        if active is not None:
            yield active
            return
        connection = self._open_connection(self.db_path)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Run synchronous store calls as one transaction."""
        with self._lock:
            active = getattr(self._transaction_local, "connection", None)
            if active is not None:
                yield
                return
            connection = self._open_connection(self.db_path)
            self._transaction_local.connection = connection
            try:
                yield
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                self._transaction_local.connection = None
                connection.close()

    def _init_schema(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    group_key TEXT PRIMARY KEY,
                    platform_id TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL DEFAULT '',
                    umo TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    discovered_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                -- A page/command toggle is an explicit operator decision. Keep it
                -- separate from the discovered/configured state so a later message
                -- cannot silently undo a deliberate disable.
                CREATE TABLE IF NOT EXISTS group_overrides (
                    group_key TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(group_key) REFERENCES groups(group_key) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS raw_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_key TEXT NOT NULL,
                    platform_id TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL DEFAULT '',
                    umo TEXT NOT NULL DEFAULT '',
                    message_id TEXT NOT NULL DEFAULT '',
                    sender_id TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'live_group',
                    import_run_id TEXT NOT NULL DEFAULT '',
                    processing_approved INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    process_error TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(group_key) REFERENCES groups(group_key) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_raw_pending
                    ON raw_messages(group_key, processed_at, id);
                CREATE INDEX IF NOT EXISTS idx_raw_created
                    ON raw_messages(created_at);

                CREATE TABLE IF NOT EXISTS issues (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL DEFAULT 'bug',
                    category TEXT NOT NULL DEFAULT 'pending_feedback',
                    issue_domain TEXT NOT NULL DEFAULT 'other',
                    plugin_name TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    severity TEXT NOT NULL DEFAULT 'medium',
                    group_key TEXT NOT NULL DEFAULT '',
                    reporter_id TEXT NOT NULL DEFAULT '',
                    reporter_name TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    symptoms TEXT NOT NULL DEFAULT '',
                    reproduction TEXT NOT NULL DEFAULT '',
                    expected TEXT NOT NULL DEFAULT '',
                    actual TEXT NOT NULL DEFAULT '',
                    environment TEXT NOT NULL DEFAULT '',
                    impact TEXT NOT NULL DEFAULT '',
                    cause TEXT NOT NULL DEFAULT '',
                    solution TEXT NOT NULL DEFAULT '',
                    workaround TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]',
                    platforms TEXT NOT NULL DEFAULT '[]',
                    versions TEXT NOT NULL DEFAULT '[]',
                    introduced_version TEXT NOT NULL DEFAULT '',
                    fixed_version TEXT NOT NULL DEFAULT '',
                    applicable_scope TEXT NOT NULL DEFAULT '',
                    verification_status TEXT NOT NULL DEFAULT 'unverified',
                    last_confirmed_at TEXT,
                    confidence REAL NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT 'model',
                    duplicate_of TEXT NOT NULL DEFAULT '',
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    resolved_at TEXT,
                    reviewed_at TEXT,
                    review_decision TEXT NOT NULL DEFAULT '',
                    review_note TEXT NOT NULL DEFAULT '',
                    knowledge_published_at TEXT,
                    pre_review_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(group_key) REFERENCES groups(group_key) ON DELETE SET DEFAULT
                );
                CREATE INDEX IF NOT EXISTS idx_issues_status_updated
                    ON issues(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_issues_group
                    ON issues(group_key, updated_at DESC);

                CREATE TABLE IF NOT EXISTS issue_todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    due_at TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT 'page',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    FOREIGN KEY(issue_id) REFERENCES issues(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_issue_todos_issue
                    ON issue_todos(issue_id, status, id);
                CREATE INDEX IF NOT EXISTS idx_issue_todos_status
                    ON issue_todos(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS issue_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id TEXT NOT NULL,
                    raw_message_id INTEGER,
                    message_id TEXT NOT NULL DEFAULT '',
                    sender_id TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    quote TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(issue_id) REFERENCES issues(id) ON DELETE CASCADE,
                    FOREIGN KEY(raw_message_id) REFERENCES raw_messages(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_evidence_issue
                    ON issue_evidence(issue_id, id);

                CREATE TABLE IF NOT EXISTS issue_relations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_issue_id TEXT NOT NULL,
                    target_issue_id TEXT NOT NULL,
                    relation_type TEXT NOT NULL DEFAULT 'similar',
                    score REAL NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(source_issue_id, target_issue_id, relation_type),
                    FOREIGN KEY(source_issue_id) REFERENCES issues(id) ON DELETE CASCADE,
                    FOREIGN KEY(target_issue_id) REFERENCES issues(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT 'system',
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_issue
                    ON audit_log(issue_id, id DESC);

                CREATE TABLE IF NOT EXISTS answer_audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_key TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    user_name TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL,
                    issue_ids TEXT NOT NULL DEFAULT '[]',
                    answer TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    sent INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_answers_created
                    ON answer_audits(created_at DESC);

                -- Repository notification cursors survive restarts so a periodic
                -- check does not announce the same commit or release twice.
                CREATE TABLE IF NOT EXISTS repository_cursors (
                    repository TEXT PRIMARY KEY,
                    latest_commit TEXT NOT NULL DEFAULT '',
                    latest_release TEXT NOT NULL DEFAULT '',
                    initialized INTEGER NOT NULL DEFAULT 0,
                    initialized_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_repository_cursors_updated
                    ON repository_cursors(updated_at DESC);
                """
            )
            self._ensure_raw_message_columns(connection)
            self._ensure_issue_columns(connection)
            self._ensure_group_override_migration(connection)
            self._ensure_issue_domain_migration(connection)
            self._ensure_plugin_name_migration(connection)
            self._ensure_review_workflow_migration(connection)
            self._ensure_model_review_reapproval_migration(connection)
            self._ensure_todo_publication_migration(connection)
            self._ensure_approved_publication_migration(connection)
            now = utc_now()
            connection.execute(
                """
                INSERT OR IGNORE INTO groups(
                    group_key, platform_id, group_id, umo, display_name,
                    enabled, discovered_at, last_seen_at, updated_at
                ) VALUES ('', '', '', '', '手动记录', 0, ?, ?, ?)
                """,
                (now, now, now),
            )

    @staticmethod
    def _ensure_raw_message_columns(connection: sqlite3.Connection) -> None:
        """Add history provenance fields to databases created before imports existed."""
        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(raw_messages)").fetchall()
        }
        definitions = {
            "source": "TEXT NOT NULL DEFAULT 'live_group'",
            "import_run_id": "TEXT NOT NULL DEFAULT ''",
            "processing_approved": "INTEGER NOT NULL DEFAULT 1",
        }
        approval_added = "processing_approved" not in existing
        for column, definition in definitions.items():
            if column not in existing:
                connection.execute(
                    f"ALTER TABLE raw_messages ADD COLUMN {column} {definition}"
                )
        if approval_added:
            connection.execute(
                """
                UPDATE raw_messages
                SET processing_approved = CASE
                    WHEN source = 'qq_history' AND processed_at IS NULL THEN 0
                    ELSE 1
                END
                """
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_import_run ON raw_messages(import_run_id, id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_ready ON raw_messages(group_key, processing_approved, processed_at, id)"
        )

    @staticmethod
    def _ensure_issue_columns(connection: sqlite3.Connection) -> None:
        """Add v2 issue metadata without rebuilding existing SQLite databases."""
        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(issues)").fetchall()
        }
        definitions = {
            "category": "TEXT NOT NULL DEFAULT 'pending_feedback'",
            "introduced_version": "TEXT NOT NULL DEFAULT ''",
            "fixed_version": "TEXT NOT NULL DEFAULT ''",
            "applicable_scope": "TEXT NOT NULL DEFAULT ''",
            "verification_status": "TEXT NOT NULL DEFAULT 'unverified'",
            "last_confirmed_at": "TEXT",
        }
        category_added = "category" not in existing
        for column, definition in definitions.items():
            if column not in existing:
                connection.execute(f"ALTER TABLE issues ADD COLUMN {column} {definition}")

        if category_added:
            connection.execute(
                """
                UPDATE issues SET category = CASE
                    WHEN kind = 'optimization' THEN 'optimization'
                    WHEN kind = 'bug' AND status = 'resolved' THEN 'fixed_bug'
                    WHEN kind = 'bug' AND status IN ('triaged', 'in_progress', 'wont_fix')
                        THEN 'known_bug'
                    WHEN kind = 'question' AND status = 'resolved'
                        AND (TRIM(COALESCE(solution, '')) != '' OR TRIM(COALESCE(workaround, '')) != '')
                        THEN 'faq'
                    ELSE 'pending_feedback'
                END
                """
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_issues_category_updated ON issues(category, updated_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_issues_verification_updated "
            "ON issues(verification_status, updated_at DESC)"
        )
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version < 2:
            connection.execute("PRAGMA user_version = 2")

    @staticmethod
    def _ensure_group_override_migration(connection: sqlite3.Connection) -> None:
        """Preserve explicit-looking legacy disables without granting old rows access."""
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version >= 3:
            return
        now = utc_now()
        connection.execute(
            """
            INSERT OR IGNORE INTO group_overrides(group_key, enabled, updated_at)
            SELECT group_key, 0, COALESCE(NULLIF(updated_at, ''), ?)
            FROM groups
            WHERE group_key != '' AND enabled = 0
            """,
            (now,),
        )
        connection.execute("PRAGMA user_version = 3")

    @staticmethod
    def _ensure_issue_domain_migration(connection: sqlite3.Connection) -> None:
        """Add the independent issue-owner dimension without guessing old data."""
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(issues)").fetchall()
        }
        if "issue_domain" not in existing:
            connection.execute(
                "ALTER TABLE issues ADD COLUMN issue_domain TEXT NOT NULL DEFAULT 'other'"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_issues_domain_updated "
            "ON issues(issue_domain, updated_at DESC)"
        )
        if current_version < 4:
            connection.execute("PRAGMA user_version = 4")

    @staticmethod
    def _ensure_plugin_name_migration(connection: sqlite3.Connection) -> None:
        """Add the optional concrete plugin name without rewriting old issues."""
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(issues)").fetchall()
        }
        if "plugin_name" not in existing:
            connection.execute(
                "ALTER TABLE issues ADD COLUMN plugin_name TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_issues_plugin_name_updated "
            "ON issues(plugin_name, updated_at DESC)"
        )
        if current_version < 5:
            connection.execute("PRAGMA user_version = 5")

    @staticmethod
    def _ensure_review_workflow_migration(connection: sqlite3.Connection) -> None:
        """Add human review/publication metadata without reopening legacy work."""
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(issues)").fetchall()
        }
        definitions = {
            "reviewed_at": "TEXT",
            "review_decision": "TEXT NOT NULL DEFAULT ''",
            "review_note": "TEXT NOT NULL DEFAULT ''",
            "knowledge_published_at": "TEXT",
            "pre_review_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column, definition in definitions.items():
            if column not in existing:
                connection.execute(f"ALTER TABLE issues ADD COLUMN {column} {definition}")

        if current_version < 6:
            now = utc_now()
            connection.execute(
                """
                UPDATE issues SET
                    reviewed_at = COALESCE(reviewed_at, NULLIF(updated_at, ''), NULLIF(created_at, ''), ?),
                    review_decision = CASE
                        WHEN TRIM(COALESCE(review_decision, '')) = '' THEN 'approved'
                        ELSE review_decision
                    END,
                    knowledge_published_at = CASE
                        WHEN status = 'archived' THEN NULL
                        ELSE COALESCE(
                            knowledge_published_at,
                            NULLIF(updated_at, ''),
                            NULLIF(created_at, ''),
                            ?
                        )
                    END,
                    pre_review_json = CASE
                        WHEN TRIM(COALESCE(pre_review_json, '')) = '' THEN '{}'
                        ELSE pre_review_json
                    END
                """,
                (now, now),
            )
            connection.execute("PRAGMA user_version = 6")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_issues_review_queue "
            "ON issues(reviewed_at, created_at, id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_issues_knowledge_published "
            "ON issues(knowledge_published_at, updated_at DESC)"
        )

    @staticmethod
    def _ensure_model_review_reapproval_migration(
        connection: sqlite3.Connection,
    ) -> None:
        """Reopen only model records auto-approved by the v6 legacy migration."""
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version >= 7:
            return
        issue_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(issues)").fetchall()
        }
        audit_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(audit_log)").fetchall()
        }
        if "source" in issue_columns and {"issue_id", "action"} <= audit_columns:
            connection.execute(
                """
                UPDATE issues
                SET reviewed_at = NULL,
                    review_decision = '',
                    review_note = '',
                    knowledge_published_at = NULL
                WHERE source = 'model'
                  AND review_decision = 'approved'
                  AND status NOT IN ('archived', 'duplicate')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM audit_log AS audit
                      WHERE audit.issue_id = issues.id
                        AND audit.action = 'review_decide'
                  )
                """
            )
        connection.execute("PRAGMA user_version = 7")

    @staticmethod
    def _ensure_todo_publication_migration(connection: sqlite3.Connection) -> None:
        """Keep assigned work out of the published answer knowledge set."""
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version >= 8:
            return
        issue_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(issues)").fetchall()
        }
        if {"review_decision", "knowledge_published_at"} <= issue_columns:
            connection.execute(
                """
                UPDATE issues
                SET knowledge_published_at = NULL
                WHERE review_decision = 'todo'
                """
            )
        connection.execute("PRAGMA user_version = 8")

    @staticmethod
    def _ensure_approved_publication_migration(
        connection: sqlite3.Connection,
    ) -> None:
        """Enforce that only explicitly approved records remain published."""
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version >= 9:
            return
        issue_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(issues)").fetchall()
        }
        if {"review_decision", "knowledge_published_at"} <= issue_columns:
            connection.execute(
                """
                UPDATE issues
                SET knowledge_published_at = NULL
                WHERE TRIM(COALESCE(review_decision, '')) != 'approved'
                """
            )
        connection.execute("PRAGMA user_version = 9")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for field in JSON_FIELDS:
            if field in item:
                item[field] = _json_load(item[field], [])
        if "pre_review_json" in item:
            item["pre_review_json"] = _json_load(item["pre_review_json"], {})
        for field in ("enabled", "sent", "initialized"):
            if field in item:
                item[field] = bool(item[field])
        return item

    def get_repository_cursor(self, repository: str) -> dict[str, Any] | None:
        """Return the persisted notification cursor for one repository."""
        repository = str(repository or "").strip()
        if not repository:
            return None
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM repository_cursors WHERE repository = ?",
                (repository,),
            ).fetchone()
        return self._row(row)

    def list_repository_cursors(self) -> list[dict[str, Any]]:
        """Return all repository cursors, newest writes first."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM repository_cursors ORDER BY updated_at DESC, repository ASC"
            ).fetchall()
        return [self._row(row) or {} for row in rows]

    def set_repository_cursor(
        self,
        repository: str,
        latest_commit: str | None = None,
        latest_release: str | None = None,
        *,
        initialized: bool | None = None,
    ) -> dict[str, Any] | None:
        """Create or update a repository notification cursor.

        ``None`` keeps an existing field unchanged, which lets commit and release
        checks advance their own cursor independently.  A new cursor is marked as
        initialized only when explicitly requested (or when a value is supplied).
        """
        repository = str(repository or "").strip()
        if not repository:
            return None
        commit = None if latest_commit is None else str(latest_commit).strip()
        release = None if latest_release is None else str(latest_release).strip()
        now = utc_now_precise()
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM repository_cursors WHERE repository = ?",
                (repository,),
            ).fetchone()
            if existing is None:
                initialized_value = (
                    bool(initialized)
                    if initialized is not None
                    else bool(commit or release)
                )
                initialized_at = now if initialized_value else None
                connection.execute(
                    """
                    INSERT INTO repository_cursors(
                        repository, latest_commit, latest_release, initialized,
                        initialized_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        repository,
                        commit or "",
                        release or "",
                        int(initialized_value),
                        initialized_at,
                        now,
                    ),
                )
            else:
                initialized_value = (
                    bool(existing["initialized"])
                    if initialized is None
                    else bool(initialized)
                )
                initialized_at = existing["initialized_at"]
                if initialized_value and not initialized_at:
                    initialized_at = now
                connection.execute(
                    """
                    UPDATE repository_cursors
                    SET latest_commit = ?,
                        latest_release = ?,
                        initialized = ?,
                        initialized_at = ?,
                        updated_at = ?
                    WHERE repository = ?
                    """,
                    (
                        existing["latest_commit"] if commit is None else commit,
                        existing["latest_release"] if release is None else release,
                        int(initialized_value),
                        initialized_at,
                        now,
                        repository,
                    ),
                )
            row = connection.execute(
                "SELECT * FROM repository_cursors WHERE repository = ?",
                (repository,),
            ).fetchone()
        return self._row(row)

    def record_group(
        self,
        *,
        group_key: str,
        platform_id: str,
        group_id: str,
        umo: str,
        display_name: str = "",
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connection() as connection:
            existing = connection.execute(
                "SELECT enabled, discovered_at FROM groups WHERE group_key = ?",
                (group_key,),
            ).fetchone()
            override = connection.execute(
                "SELECT enabled FROM group_overrides WHERE group_key = ?",
                (group_key,),
            ).fetchone()
            if override is not None:
                enabled_value = int(bool(override["enabled"]))
            else:
                enabled_value = int(enabled if enabled is not None else bool(existing and existing["enabled"]))
            discovered_at = str(existing["discovered_at"]) if existing else now
            connection.execute(
                """
                INSERT INTO groups(
                    group_key, platform_id, group_id, umo, display_name, enabled,
                    discovered_at, last_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_key) DO UPDATE SET
                    platform_id = excluded.platform_id,
                    group_id = excluded.group_id,
                    umo = excluded.umo,
                    display_name = CASE
                        WHEN excluded.display_name = '' THEN groups.display_name
                        ELSE excluded.display_name
                    END,
                    enabled = excluded.enabled,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (
                    group_key,
                    platform_id,
                    group_id,
                    umo,
                    display_name,
                    enabled_value,
                    discovered_at,
                    now,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM groups WHERE group_key = ?", (group_key,)).fetchone()
        return self._row(row) or {}

    def get_group_override(self, group_key: str) -> bool | None:
        """Return an explicit page/command override, or None if undisposed."""
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT enabled FROM group_overrides WHERE group_key = ?",
                (group_key,),
            ).fetchone()
        if row is None:
            return None
        return bool(row["enabled"])

    def set_group_enabled(self, group_key: str, enabled: bool, display_name: str = "") -> bool:
        now = utc_now()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE groups SET enabled = ?,
                    display_name = CASE WHEN ? = '' THEN display_name ELSE ? END,
                    updated_at = ?
                WHERE group_key = ?
                """,
                (int(enabled), display_name, display_name, now, group_key),
            )
            if cursor.rowcount <= 0:
                platform_id = group_key.split(":", 1)[0] if ":" in group_key else ""
                group_id = group_key.rsplit(":", 1)[-1]
                connection.execute(
                    """
                    INSERT INTO groups(
                        group_key, platform_id, group_id, umo, display_name,
                        enabled, discovered_at, last_seen_at, updated_at
                    ) VALUES (?, ?, ?, '', ?, ?, ?, ?, ?)
                    """,
                    (group_key, platform_id, group_id, display_name, int(enabled), now, now, now),
                )
                created = True
            else:
                created = False
            connection.execute(
                """
                INSERT INTO group_overrides(group_key, enabled, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(group_key) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (group_key, int(enabled), now),
            )
            return created or cursor.rowcount > 0

    def is_group_enabled(self, group_key: str) -> bool:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT enabled FROM groups WHERE group_key = ?", (group_key,)).fetchone()
        return bool(row and row["enabled"])

    def get_group(self, group_key: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM groups WHERE group_key = ?",
                (str(group_key or "")[:300],),
            ).fetchone()
        return self._row(row)

    def list_groups(self) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT g.*,
                    COUNT(DISTINCT i.id) AS issue_count,
                    SUM(CASE WHEN i.status IN ('pending', 'triaged', 'in_progress') THEN 1 ELSE 0 END) AS open_count
                FROM groups g
                LEFT JOIN issues i ON i.group_key = g.group_key AND i.status != 'archived'
                WHERE g.group_key != ''
                GROUP BY g.group_key
                ORDER BY g.enabled DESC, g.last_seen_at DESC
                """
            ).fetchall()
        return [self._row(row) or {} for row in rows]

    def add_message(self, message: dict[str, Any]) -> int:
        with self._lock, self._connection() as connection:
            if message.get("message_id"):
                duplicate = connection.execute(
                    "SELECT id FROM raw_messages WHERE group_key = ? AND message_id = ? LIMIT 1",
                    (message.get("group_key", ""), message.get("message_id", "")),
                ).fetchone()
                if duplicate:
                    if bool(message.get("processing_approved", True)):
                        connection.execute(
                            "UPDATE raw_messages SET processing_approved = 1 WHERE id = ? AND processed_at IS NULL",
                            (int(duplicate["id"]),),
                        )
                    return int(duplicate["id"])
            cursor = connection.execute(
                """
                INSERT INTO raw_messages(
                    group_key, platform_id, group_id, umo, message_id,
                    sender_id, sender_name, content, source, import_run_id,
                    processing_approved, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.get("group_key", ""),
                    message.get("platform_id", ""),
                    message.get("group_id", ""),
                    message.get("umo", ""),
                    message.get("message_id", ""),
                    message.get("sender_id", ""),
                    message.get("sender_name", ""),
                    message.get("content", ""),
                    str(message.get("source") or "live_group")[:40],
                    str(message.get("import_run_id") or "")[:120],
                    int(bool(message.get("processing_approved", True))),
                    message.get("created_at") or utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def existing_message_ids(
        self,
        group_key: str,
        message_ids: Iterable[str],
    ) -> list[str]:
        values = [
            str(value)[:200]
            for value in dict.fromkeys(message_ids)
            if str(value).strip()
        ]
        if not values:
            return []
        existing: set[str] = set()
        with self._lock, self._connection() as connection:
            for offset in range(0, len(values), 500):
                chunk = values[offset : offset + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT message_id FROM raw_messages "
                    f"WHERE group_key = ? AND message_id IN ({placeholders})",
                    (group_key, *chunk),
                ).fetchall()
                existing.update(str(row["message_id"]) for row in rows)
        return [value for value in values if value in existing]

    def pending_messages(
        self,
        group_key: str,
        limit: int = 40,
        *,
        include_unapproved: bool = False,
    ) -> list[dict[str, Any]]:
        approval_clause = "" if include_unapproved else "AND processing_approved = 1"
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM raw_messages
                WHERE group_key = ? AND processed_at IS NULL
                  {approval_clause}
                ORDER BY id ASC LIMIT ?
                """,
                (group_key, max(1, min(int(limit), 200))),
            ).fetchall()
        return [self._row(row) or {} for row in rows]

    def approve_messages(self, ids: Iterable[int]) -> int:
        id_list = list(dict.fromkeys(int(item) for item in ids))
        if not id_list:
            return 0
        placeholders = ",".join("?" for _ in id_list)
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE raw_messages SET processing_approved = 1 WHERE id IN ({placeholders}) AND processed_at IS NULL",
                tuple(id_list),
            )
            return cursor.rowcount

    def approve_pending_group(self, group_key: str) -> int:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE raw_messages SET processing_approved = 1 WHERE group_key = ? AND processed_at IS NULL",
                (str(group_key or "")[:300],),
            )
            return cursor.rowcount

    def prepare_pending_messages(
        self,
        ids: Iterable[int],
        *,
        expected_group_key: str = "",
    ) -> tuple[int, list[str], bool]:
        """Approve selected messages only while they are still pending."""
        id_list = list(dict.fromkeys(int(item) for item in ids if int(item) > 0))
        if not id_list:
            return 0, [], True
        placeholders = ",".join("?" for _ in id_list)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT id, group_key FROM raw_messages
                WHERE id IN ({placeholders}) AND processed_at IS NULL
                ORDER BY id
                """,
                tuple(id_list),
            ).fetchall()
            pending_ids = [int(row["id"]) for row in rows]
            group_keys = list(
                dict.fromkeys(
                    str(row["group_key"] or "")
                    for row in rows
                    if str(row["group_key"] or "")
                )
            )
            expected = str(expected_group_key or "")[:300]
            if expected and (len(group_keys) != 1 or group_keys[0] != expected):
                return 0, group_keys, False
            if pending_ids:
                pending_placeholders = ",".join("?" for _ in pending_ids)
                connection.execute(
                    f"UPDATE raw_messages SET processing_approved = 1 "
                    f"WHERE id IN ({pending_placeholders}) AND processed_at IS NULL",
                    tuple(pending_ids),
                )
        return len(pending_ids), group_keys, True

    def list_pending_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT r.*, g.display_name AS group_name
                FROM raw_messages r
                LEFT JOIN groups g ON g.group_key = r.group_key
                WHERE r.processed_at IS NULL
                ORDER BY r.id DESC LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [self._row(row) or {} for row in rows]

    def pending_message_count(self) -> int:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM raw_messages WHERE processed_at IS NULL"
            ).fetchone()
        return int(row["count"] if row else 0)

    def pending_message_group_keys(self) -> list[str]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT group_key FROM raw_messages
                WHERE processed_at IS NULL AND group_key != ''
                ORDER BY group_key
                """
            ).fetchall()
        return [str(row["group_key"] or "") for row in rows]

    def mark_messages_processed(self, ids: Iterable[int], error: str = "") -> None:
        id_list = [int(item) for item in ids]
        if not id_list:
            return
        placeholders = ",".join("?" for _ in id_list)
        with self._lock, self._connection() as connection:
            connection.execute(
                f"UPDATE raw_messages SET processed_at = ?, process_error = ? WHERE id IN ({placeholders})",
                (utc_now(), str(error or "")[:500], *id_list),
            )

    def mark_messages_error(self, ids: Iterable[int], error: str) -> None:
        id_list = [int(item) for item in ids]
        if not id_list:
            return
        placeholders = ",".join("?" for _ in id_list)
        with self._lock, self._connection() as connection:
            connection.execute(
                f"UPDATE raw_messages SET process_error = ? WHERE id IN ({placeholders})",
                (str(error or "")[:500], *id_list),
            )

    def delete_messages(self, ids: Iterable[int]) -> int:
        id_list = [int(item) for item in ids]
        if not id_list:
            return 0
        placeholders = ",".join("?" for _ in id_list)
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM raw_messages WHERE id IN ({placeholders}) AND id NOT IN (SELECT raw_message_id FROM issue_evidence WHERE raw_message_id IS NOT NULL)",
                tuple(id_list),
            )
            return cursor.rowcount

    def reset_messages(self, ids: Iterable[int]) -> int:
        id_list = [int(item) for item in ids]
        if not id_list:
            return 0
        placeholders = ",".join("?" for _ in id_list)
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE raw_messages SET processed_at = NULL, process_error = '' WHERE id IN ({placeholders})",
                tuple(id_list),
            )
            return cursor.rowcount

    def group_keys_for_messages(self, ids: Iterable[int]) -> list[str]:
        id_list = [int(item) for item in ids]
        if not id_list:
            return []
        placeholders = ",".join("?" for _ in id_list)
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                f"SELECT DISTINCT group_key FROM raw_messages WHERE id IN ({placeholders})",
                tuple(id_list),
            ).fetchall()
        return [str(row["group_key"] or "") for row in rows if str(row["group_key"] or "")]

    def _next_issue_id(self, connection: sqlite3.Connection) -> str:
        prefix = datetime.now().strftime("BUG-%Y%m%d")
        pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
        sequence = 0
        for row in connection.execute(
            "SELECT id FROM issues WHERE id LIKE ?",
            (f"{prefix}-%",),
        ).fetchall():
            match = pattern.fullmatch(str(row["id"] or ""))
            if match:
                sequence = max(sequence, int(match.group(1)))
        sequence += 1
        return f"{prefix}-{sequence:03d}"

    def create_issue(
        self,
        payload: dict[str, Any],
        evidence: list[dict[str, Any]] | None = None,
        *,
        actor: str = "model",
        requires_review: bool = False,
    ) -> dict[str, Any]:
        now = utc_now()
        kind = str(payload.get("kind") or "bug").lower()
        category = str(payload.get("category") or "").lower()
        issue_domain = str(payload.get("issue_domain") or "other").lower()
        plugin_name = normalize_plugin_name(payload.get("plugin_name"))
        severity = str(payload.get("severity") or "medium").lower()
        status = str(payload.get("status") or "pending").lower()
        verification_status = str(
            payload.get("verification_status") or "unverified"
        ).lower()
        kind = kind if kind in ISSUE_KINDS else "bug"
        if category not in ISSUE_CATEGORIES:
            category = "optimization" if kind == "optimization" else "pending_feedback"
        issue_domain = issue_domain if issue_domain in ISSUE_DOMAINS else "other"
        if issue_domain != "plugin":
            plugin_name = ""
        severity = severity if severity in ISSUE_SEVERITIES else "medium"
        status = status if status in ISSUE_STATUSES else "pending"
        verification_status = (
            verification_status
            if verification_status in VERIFICATION_STATUSES
            else "unverified"
        )
        title = re.sub(r"\s+", " ", str(payload.get("title") or "待整理问题")).strip()[:160]
        evidence = evidence or []
        raw_pre_review = payload.get("pre_review_json", payload.get("pre_review", {}))
        if isinstance(raw_pre_review, str):
            pre_review = _json_load(raw_pre_review, {})
        else:
            pre_review = raw_pre_review if isinstance(raw_pre_review, dict) else {}
        if requires_review:
            reviewed_at = None
            review_decision = ""
            review_note = ""
            knowledge_published_at = None
        else:
            reviewed_at = str(payload.get("reviewed_at") or now)[:80]
            requested_decision = str(
                payload.get("review_decision") or "approved"
            ).lower()
            review_decision = (
                requested_decision
                if requested_decision in REVIEW_DECISIONS
                else "approved"
            )
            review_note = str(payload.get("review_note") or "").strip()[:4000]
            knowledge_published_at = (
                str(payload.get("knowledge_published_at") or now)[:80]
                if review_decision == "approved"
                else None
            )
        with self._lock, self._connection() as connection:
            # IDs are store-owned. Page/model payloads must not influence the
            # sequence or inject malformed identifiers.
            issue_id = self._next_issue_id(connection)
            values = {
                "id": issue_id,
                "kind": kind,
                "category": category,
                "issue_domain": issue_domain,
                "plugin_name": plugin_name,
                "title": title,
                "status": status,
                "severity": severity,
                "group_key": str(payload.get("group_key") or "")[:300],
                "reporter_id": str(payload.get("reporter_id") or "")[:100],
                "reporter_name": str(payload.get("reporter_name") or "")[:100],
                "summary": str(payload.get("summary") or "").strip()[:2000],
                "symptoms": str(payload.get("symptoms") or "").strip()[:4000],
                "reproduction": str(payload.get("reproduction") or "").strip()[:4000],
                "expected": str(payload.get("expected") or "").strip()[:2000],
                "actual": str(payload.get("actual") or "").strip()[:2000],
                "environment": str(payload.get("environment") or "").strip()[:2000],
                "impact": str(payload.get("impact") or "").strip()[:2000],
                "cause": str(payload.get("cause") or "").strip()[:4000],
                "solution": str(payload.get("solution") or "").strip()[:6000],
                "workaround": str(payload.get("workaround") or "").strip()[:4000],
                "tags": json.dumps(_clean_terms(payload.get("tags")), ensure_ascii=False),
                "platforms": json.dumps(_clean_terms(payload.get("platforms")), ensure_ascii=False),
                "versions": json.dumps(_clean_terms(payload.get("versions")), ensure_ascii=False),
                "introduced_version": str(payload.get("introduced_version") or "").strip()[:120],
                "fixed_version": str(payload.get("fixed_version") or "").strip()[:120],
                "applicable_scope": str(payload.get("applicable_scope") or "").strip()[:2000],
                "verification_status": verification_status,
                "last_confirmed_at": (
                    str(payload.get("last_confirmed_at") or "").strip()[:80]
                    or (now if verification_status in CONFIRMED_VERIFICATION_STATUSES else None)
                ),
                "confidence": max(0.0, min(float(payload.get("confidence") or 0), 1.0)),
                "source": str(actor if actor == "page" else (payload.get("source") or actor))[:40],
                "duplicate_of": "" if actor == "page" else str(payload.get("duplicate_of") or "")[:80],
                "reviewed_at": reviewed_at,
                "review_decision": review_decision,
                "review_note": review_note,
                "knowledge_published_at": knowledge_published_at,
                "pre_review_json": json.dumps(
                    pre_review,
                    ensure_ascii=False,
                    default=str,
                ),
            }
            connection.execute(
                """
                INSERT INTO issues(
                    id, kind, category, issue_domain, plugin_name, title, status, severity, group_key, reporter_id,
                    reporter_name, summary, symptoms, reproduction, expected,
                    actual, environment, impact, cause, solution, workaround,
                    tags, platforms, versions, introduced_version, fixed_version,
                    applicable_scope, verification_status, last_confirmed_at,
                    confidence, source, duplicate_of, occurrence_count,
                    evidence_count, created_at, updated_at, last_seen_at, resolved_at,
                    reviewed_at, review_decision, review_note,
                    knowledge_published_at, pre_review_json
                ) VALUES (
                    :id, :kind, :category, :issue_domain, :plugin_name, :title, :status, :severity, :group_key, :reporter_id,
                    :reporter_name, :summary, :symptoms, :reproduction, :expected,
                    :actual, :environment, :impact, :cause, :solution, :workaround,
                    :tags, :platforms, :versions, :introduced_version, :fixed_version,
                    :applicable_scope, :verification_status, :last_confirmed_at,
                    :confidence, :source, :duplicate_of, 1, 0,
                    :created_at, :updated_at, :last_seen_at, :resolved_at,
                    :reviewed_at, :review_decision, :review_note,
                    :knowledge_published_at, :pre_review_json
                )
                """,
                {
                    **values,
                    "created_at": now,
                    "updated_at": now,
                    "last_seen_at": now,
                    "resolved_at": now if status == "resolved" else None,
                },
            )
            for item in evidence:
                quote = str(item.get("quote") or item.get("content") or "").strip()
                if not quote:
                    continue
                connection.execute(
                    """
                    INSERT INTO issue_evidence(
                        issue_id, raw_message_id, message_id, sender_id,
                        sender_name, quote, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        issue_id,
                        item.get("id") or item.get("raw_message_id"),
                        item.get("message_id", ""),
                        item.get("sender_id", ""),
                        item.get("sender_name", ""),
                        quote[:6000],
                        item.get("created_at") or now,
                    ),
                )
            evidence_count = connection.execute(
                "SELECT COUNT(*) AS count FROM issue_evidence WHERE issue_id = ?", (issue_id,)
            ).fetchone()["count"]
            connection.execute(
                "UPDATE issues SET evidence_count = ? WHERE id = ?",
                (int(evidence_count), issue_id),
            )
            self._audit(connection, issue_id, "create", actor, {"title": title, "source": values["source"]})
        return self.get_issue(issue_id) or {}

    def add_evidence(self, issue_id: str, evidence: list[dict[str, Any]], actor: str = "system") -> int:
        now = utc_now()
        added = 0
        with self._lock, self._connection() as connection:
            for item in evidence:
                quote = str(item.get("quote") or item.get("content") or "").strip()
                if not quote:
                    continue
                duplicate = connection.execute(
                    """
                    SELECT id FROM issue_evidence
                    WHERE issue_id = ? AND raw_message_id IS ? AND quote = ? LIMIT 1
                    """,
                    (issue_id, item.get("id") or item.get("raw_message_id"), quote[:6000]),
                ).fetchone()
                if duplicate:
                    continue
                connection.execute(
                    """
                    INSERT INTO issue_evidence(
                        issue_id, raw_message_id, message_id, sender_id,
                        sender_name, quote, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        issue_id,
                        item.get("id") or item.get("raw_message_id"),
                        item.get("message_id", ""),
                        item.get("sender_id", ""),
                        item.get("sender_name", ""),
                        quote[:6000],
                        item.get("created_at") or now,
                    ),
                )
                added += 1
            if added:
                connection.execute(
                    """
                    UPDATE issues SET
                        evidence_count = (SELECT COUNT(*) FROM issue_evidence WHERE issue_id = ?),
                        occurrence_count = occurrence_count + 1,
                        last_seen_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (issue_id, now, now, issue_id),
                )
                self._audit(connection, issue_id, "add_evidence", actor, {"added": added})
        return added

    def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            issue = self._row(connection.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone())
            if not issue:
                return None
            evidence_rows = connection.execute(
                "SELECT * FROM issue_evidence WHERE issue_id = ? ORDER BY id ASC", (issue_id,)
            ).fetchall()
            audit_rows = connection.execute(
                "SELECT * FROM audit_log WHERE issue_id = ? ORDER BY id DESC LIMIT 50", (issue_id,)
            ).fetchall()
            relation_rows = connection.execute(
                """
                SELECT r.*, i.title AS target_title, i.status AS target_status
                FROM issue_relations r
                JOIN issues i ON i.id = r.target_issue_id
                WHERE r.source_issue_id = ?
                ORDER BY r.score DESC
                """,
                (issue_id,),
            ).fetchall()
            todo_rows = connection.execute(
                "SELECT * FROM issue_todos WHERE issue_id = ? "
                "ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, id ASC",
                (issue_id,),
            ).fetchall()
        issue["evidence"] = [self._row(row) or {} for row in evidence_rows]
        issue["audit"] = [
            {**(self._row(row) or {}), "detail": _json_load(row["detail"], {})}
            for row in audit_rows
        ]
        issue["relations"] = [self._row(row) or {} for row in relation_rows]
        issue["todos"] = [self._row(row) or {} for row in todo_rows]
        return issue

    def list_review_queue(self, limit: int = 100) -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        with self._lock, self._connection() as connection:
            total = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM issues "
                    "WHERE reviewed_at IS NULL AND status NOT IN ('archived', 'duplicate')"
                ).fetchone()["count"]
            )
            rows = connection.execute(
                """
                SELECT * FROM issues
                WHERE reviewed_at IS NULL AND status NOT IN ('archived', 'duplicate')
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return {
            "items": [self._row(row) or {} for row in rows],
            "total": total,
        }

    def get_todo(self, todo_id: int) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT t.*, i.title AS issue_title, i.status AS issue_status
                FROM issue_todos t
                JOIN issues i ON i.id = t.issue_id
                WHERE t.id = ?
                """,
                (int(todo_id),),
            ).fetchone()
        return self._row(row)

    def list_todos_page(
        self,
        *,
        status: str = "",
        issue_id: str = "",
        page: int = 1,
        limit: int = 100,
    ) -> dict[str, Any]:
        where = ["1 = 1"]
        params: list[Any] = []
        if status in TODO_STATUSES:
            where.append("t.status = ?")
            params.append(status)
        if issue_id:
            where.append("t.issue_id = ?")
            params.append(str(issue_id)[:80])
        open_where = ["t.status = 'open'"]
        open_params: list[Any] = []
        if issue_id:
            open_where.append("t.issue_id = ?")
            open_params.append(str(issue_id)[:80])
        limit = max(1, min(int(limit), 500))
        page = max(1, int(page))
        with self._lock, self._connection() as connection:
            total = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM issue_todos t
                    JOIN issues i ON i.id = t.issue_id
                    WHERE {' AND '.join(where)}
                    """,
                    tuple(params),
                ).fetchone()["count"]
            )
            open_count = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*) AS count
                    FROM issue_todos t
                    JOIN issues i ON i.id = t.issue_id
                    WHERE {' AND '.join(open_where)}
                    """,
                    tuple(open_params),
                ).fetchone()["count"]
            )
            pages = max(1, (total + limit - 1) // limit)
            page = min(page, pages)
            rows = connection.execute(
                f"""
                SELECT t.*, i.title AS issue_title, i.status AS issue_status
                FROM issue_todos t
                JOIN issues i ON i.id = t.issue_id
                WHERE {' AND '.join(where)}
                ORDER BY CASE t.status WHEN 'open' THEN 0 ELSE 1 END,
                         t.updated_at DESC, t.id DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, (page - 1) * limit),
            ).fetchall()
        return {
            "items": [self._row(row) or {} for row in rows],
            "total": total,
            "open": open_count,
            "page": page,
            "pages": pages,
        }

    def list_todos(
        self,
        *,
        status: str = "",
        issue_id: str = "",
        limit: int = 100,
        page: int | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        result = self.list_todos_page(
            status=status,
            issue_id=issue_id,
            page=page or 1,
            limit=limit,
        )
        return result if page is not None else result["items"]

    def create_todo(
        self,
        issue_id: str,
        content: str,
        *,
        due_at: str = "",
        created_by: str = "page",
    ) -> dict[str, Any] | None:
        issue_id = str(issue_id or "").strip()[:80]
        content = str(content or "").strip()[:4000]
        if not issue_id or not content:
            return None
        now = utc_now_precise()
        with self._lock, self._connection() as connection:
            if not connection.execute(
                "SELECT 1 FROM issues WHERE id = ?", (issue_id,)
            ).fetchone():
                return None
            cursor = connection.execute(
                """
                INSERT INTO issue_todos(
                    issue_id, content, status, due_at, created_by,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, 'open', ?, ?, ?, ?, NULL)
                """,
                (
                    issue_id,
                    content,
                    str(due_at or "").strip()[:80],
                    str(created_by or "page")[:100],
                    now,
                    now,
                ),
            )
            todo_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE issues SET updated_at = ? WHERE id = ?",
                (now, issue_id),
            )
            self._audit(
                connection,
                issue_id,
                "todo_create",
                str(created_by or "page"),
                {"todo_id": todo_id, "content": content, "due_at": str(due_at or "")[:80]},
            )
        return self.get_todo(todo_id)

    def update_todo(
        self,
        todo_id: int,
        changes: dict[str, Any],
        *,
        actor: str = "page",
    ) -> dict[str, Any] | None:
        if not isinstance(changes, dict):
            return None
        review_reopened = False
        with self._lock, self._connection() as connection:
            before = connection.execute(
                "SELECT * FROM issue_todos WHERE id = ?", (int(todo_id),)
            ).fetchone()
            if not before:
                return None
            updates: dict[str, Any] = {}
            if "content" in changes:
                content = str(changes.get("content") or "").strip()[:4000]
                if not content:
                    return None
                updates["content"] = content
            if "due_at" in changes:
                updates["due_at"] = str(changes.get("due_at") or "").strip()[:80]
            if "status" in changes:
                status = str(changes.get("status") or "").lower()
                if status not in TODO_STATUSES:
                    return None
                updates["status"] = status
            if not updates:
                return self._row(before)
            changed = any(before[key] != value for key, value in updates.items())
            if not changed:
                return self._row(before)
            now = utc_now_precise()
            if updates.get("status") == "completed":
                updates["completed_at"] = before["completed_at"] or now
            elif updates.get("status") == "open":
                updates["completed_at"] = None
            updates["updated_at"] = now
            assignments = ", ".join(f"{field} = ?" for field in updates)
            connection.execute(
                f"UPDATE issue_todos SET {assignments} WHERE id = ?",
                (*updates.values(), int(todo_id)),
            )
            issue_id = str(before["issue_id"])
            connection.execute(
                "UPDATE issues SET updated_at = ? WHERE id = ?",
                (now, issue_id),
            )
            self._audit(
                connection,
                issue_id,
                "todo_update",
                actor,
                {"todo_id": int(todo_id), **updates},
            )
            if updates.get("status") == "completed":
                remaining_open = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM issue_todos "
                        "WHERE issue_id = ? AND status = 'open'",
                        (issue_id,),
                    ).fetchone()["count"]
                )
                issue = connection.execute(
                    "SELECT review_decision FROM issues WHERE id = ?",
                    (issue_id,),
                ).fetchone()
                if (
                    remaining_open == 0
                    and issue is not None
                    and str(issue["review_decision"] or "") == "todo"
                ):
                    connection.execute(
                        """
                        UPDATE issues SET
                            reviewed_at = NULL,
                            review_decision = '',
                            review_note = '',
                            knowledge_published_at = NULL,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (now, issue_id),
                    )
                    self._audit(
                        connection,
                        issue_id,
                        "todo_complete_reopen",
                        actor,
                        {"todo_id": int(todo_id)},
                    )
                    review_reopened = True
        result = self.get_todo(int(todo_id))
        if result is not None:
            result["review_reopened"] = review_reopened
        return result

    def review_decide(
        self,
        issue_id: str,
        action: str,
        note: str = "",
        todo: Any = None,
        expected_updated_at: str = "",
        *,
        actor: str = "page",
    ) -> dict[str, Any]:
        issue_id = str(issue_id or "").strip()[:80]
        decision = REVIEW_ACTIONS.get(str(action or "").strip().lower(), "")
        if not issue_id:
            return {"ok": False, "error": "missing_issue_id"}
        if not decision:
            return {"ok": False, "error": "invalid_action"}
        note = str(note or "").strip()[:4000]
        todo_data = todo if isinstance(todo, dict) else {"content": todo}
        todo_content = str(todo_data.get("content") or "").strip()[:4000]
        todo_due_at = str(todo_data.get("due_at") or "").strip()[:80]
        if decision == "todo" and not todo_content:
            return {"ok": False, "error": "todo_required"}

        publish = decision == "approved"
        result: dict[str, Any]
        with self.atomic():
            with self._connection() as connection:
                before_row = connection.execute(
                    "SELECT * FROM issues WHERE id = ?", (issue_id,)
                ).fetchone()
                if not before_row:
                    return {"ok": False, "error": "not_found"}
                before = self._row(before_row) or {}
                matching_todo_row = None
                if decision == "todo":
                    matching_todo_row = connection.execute(
                        """
                        SELECT * FROM issue_todos
                        WHERE issue_id = ? AND content = ? AND due_at = ?
                          AND status = 'open'
                        ORDER BY id DESC LIMIT 1
                        """,
                        (issue_id, todo_content, todo_due_at),
                    ).fetchone()
                already_applied = (
                    bool(before.get("reviewed_at"))
                    and before.get("review_decision") == decision
                    and str(before.get("review_note") or "") == note
                    and bool(before.get("knowledge_published_at")) == publish
                    and (decision != "todo" or matching_todo_row is not None)
                )
                expected = str(expected_updated_at or "").strip()
                if expected and expected != str(before.get("updated_at") or ""):
                    if already_applied:
                        result = {
                            "ok": True,
                            "idempotent": True,
                            "issue": self.get_issue(issue_id),
                            "todo": self._row(matching_todo_row),
                        }
                        return result
                    return {"ok": False, "error": "conflict", "issue": before}
                if already_applied:
                    return {
                        "ok": True,
                        "idempotent": True,
                        "issue": self.get_issue(issue_id),
                        "todo": self._row(matching_todo_row),
                    }

                now = utc_now_precise()
                published_at = (
                    before.get("knowledge_published_at") or now
                    if publish
                    else None
                )
                connection.execute(
                    """
                    UPDATE issues SET
                        reviewed_at = ?, review_decision = ?, review_note = ?,
                        knowledge_published_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, decision, note, published_at, now, issue_id),
                )
                todo_row = matching_todo_row
                if decision == "todo" and todo_row is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO issue_todos(
                            issue_id, content, status, due_at, created_by,
                            created_at, updated_at, completed_at
                        ) VALUES (?, ?, 'open', ?, ?, ?, ?, NULL)
                        """,
                        (issue_id, todo_content, todo_due_at, actor[:100], now, now),
                    )
                    todo_id = int(cursor.lastrowid)
                    todo_row = connection.execute(
                        "SELECT * FROM issue_todos WHERE id = ?", (todo_id,)
                    ).fetchone()
                    self._audit(
                        connection,
                        issue_id,
                        "todo_create",
                        actor,
                        {"todo_id": todo_id, "content": todo_content, "due_at": todo_due_at},
                    )
                self._audit(
                    connection,
                    issue_id,
                    "review_decide",
                    actor,
                    {
                        "decision": decision,
                        "note": note,
                        "published": publish,
                        "todo_id": int(todo_row["id"]) if todo_row else None,
                    },
                )
                result = {
                    "ok": True,
                    "idempotent": False,
                    "issue": self.get_issue(issue_id),
                    "todo": self._row(todo_row),
                }
        return result

    def review_reopen(
        self,
        issue_id: str,
        expected_updated_at: str = "",
        *,
        actor: str = "page",
    ) -> dict[str, Any]:
        issue_id = str(issue_id or "").strip()[:80]
        if not issue_id:
            return {"ok": False, "error": "missing_issue_id"}
        result: dict[str, Any]
        with self.atomic():
            with self._connection() as connection:
                before_row = connection.execute(
                    "SELECT * FROM issues WHERE id = ?", (issue_id,)
                ).fetchone()
                if not before_row:
                    return {"ok": False, "error": "not_found"}
                before = self._row(before_row) or {}
                already_reopened = (
                    not before.get("reviewed_at")
                    and not before.get("review_decision")
                    and not before.get("review_note")
                    and not before.get("knowledge_published_at")
                )
                expected = str(expected_updated_at or "").strip()
                if expected and expected != str(before.get("updated_at") or ""):
                    if already_reopened:
                        return {
                            "ok": True,
                            "idempotent": True,
                            "issue": self.get_issue(issue_id),
                        }
                    return {
                        "ok": False,
                        "error": "conflict",
                        "issue": self.get_issue(issue_id),
                    }
                if already_reopened:
                    return {
                        "ok": True,
                        "idempotent": True,
                        "issue": self.get_issue(issue_id),
                    }

                now = utc_now_precise()
                connection.execute(
                    """
                    UPDATE issues SET
                        reviewed_at = NULL,
                        review_decision = '',
                        review_note = '',
                        knowledge_published_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (now, issue_id),
                )
                self._audit(
                    connection,
                    issue_id,
                    "review_reopen",
                    actor,
                    {
                        "previous_decision": before.get("review_decision") or "",
                        "was_published": bool(before.get("knowledge_published_at")),
                    },
                )
                result = {
                    "ok": True,
                    "idempotent": False,
                    "issue": self.get_issue(issue_id),
                }
        return result

    def list_issues(
        self,
        *,
        query: str = "",
        status: str = "",
        kind: str = "",
        category: str = "",
        issue_domain: str = "",
        plugin_name: str = "",
        review_decision: str = "",
        verification_status: str = "",
        severity: str = "",
        group_key: str = "",
        sort: str = "updated_desc",
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        where = ["1 = 1"]
        params: list[Any] = []
        if status:
            statuses = [item for item in status.split(",") if item in ISSUE_STATUSES]
            if statuses:
                where.append(f"status IN ({','.join('?' for _ in statuses)})")
                params.extend(statuses)
            else:
                where.append("status != 'archived'")
        else:
            where.append("status != 'archived'")
        if kind in ISSUE_KINDS:
            where.append("kind = ?")
            params.append(kind)
        if category in ISSUE_CATEGORIES:
            where.append("category = ?")
            params.append(category)
        if issue_domain in ISSUE_DOMAINS:
            where.append("issue_domain = ?")
            params.append(issue_domain)
        normalized_plugin_name = normalize_plugin_name(plugin_name)
        if normalized_plugin_name:
            where.append("plugin_name = ?")
            params.append(normalized_plugin_name)
        if review_decision == "pending":
            where.append("reviewed_at IS NULL")
        elif review_decision == "published":
            where.append(
                "review_decision = 'approved' AND knowledge_published_at IS NOT NULL"
            )
        elif review_decision in REVIEW_DECISIONS:
            where.append("review_decision = ?")
            params.append(review_decision)
        if verification_status in VERIFICATION_STATUSES:
            where.append("verification_status = ?")
            params.append(verification_status)
        if severity in ISSUE_SEVERITIES:
            where.append("severity = ?")
            params.append(severity)
        if group_key:
            where.append("group_key = ?")
            params.append(group_key)
        query = re.sub(r"\s+", " ", query).strip()
        if query:
            needle = f"%{query}%"
            search_fields = (
                "id",
                "title",
                "summary",
                "symptoms",
                "reproduction",
                "environment",
                "cause",
                "solution",
                "tags",
                "versions",
                "introduced_version",
                "fixed_version",
                "applicable_scope",
                "plugin_name",
                "review_note",
                "pre_review_json",
            )
            where.append(
                "(" + " OR ".join(
                    f"{field} LIKE ?" for field in search_fields
                ) + ")"
            )
            params.extend([needle] * len(search_fields))
        order_map = {
            "created_desc": "created_at DESC",
            "severity_desc": "CASE severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC, updated_at DESC",
            "occurrence_desc": "occurrence_count DESC, updated_at DESC",
            "updated_asc": "updated_at ASC",
            "updated_desc": "updated_at DESC",
        }
        order_by = order_map.get(sort, order_map["updated_desc"])
        page_size = max(1, min(int(page_size), 100))
        page = max(1, int(page))
        where_sql = " AND ".join(where)
        with self._lock, self._connection() as connection:
            total = int(connection.execute(f"SELECT COUNT(*) AS count FROM issues WHERE {where_sql}", params).fetchone()["count"])
            pages = max(1, (total + page_size - 1) // page_size)
            page = min(page, pages)
            rows = connection.execute(
                f"SELECT * FROM issues WHERE {where_sql} ORDER BY {order_by} LIMIT ? OFFSET ?",
                (*params, page_size, (page - 1) * page_size),
            ).fetchall()
        return {
            "items": [self._row(row) or {} for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }

    def update_issue(self, issue_id: str, changes: dict[str, Any], actor: str = "page") -> dict[str, Any] | None:
        if issue_change_error(changes):
            return None
        updates: dict[str, Any] = {}
        for field, limit in ISSUE_TEXT_LIMITS.items():
            if field in changes:
                updates[field] = str(changes.get(field) or "").strip()[:limit]
        if changes.get("kind") in ISSUE_KINDS:
            updates["kind"] = changes["kind"]
        if changes.get("category") in ISSUE_CATEGORIES:
            updates["category"] = changes["category"]
        if changes.get("issue_domain") in ISSUE_DOMAINS:
            updates["issue_domain"] = changes["issue_domain"]
        if "plugin_name" in changes:
            updates["plugin_name"] = normalize_plugin_name(changes.get("plugin_name"))
        if changes.get("severity") in ISSUE_SEVERITIES:
            updates["severity"] = changes["severity"]
        if changes.get("status") in ISSUE_STATUSES:
            updates["status"] = changes["status"]
        if changes.get("verification_status") in VERIFICATION_STATUSES:
            updates["verification_status"] = changes["verification_status"]
        if "last_confirmed_at" in changes:
            updates["last_confirmed_at"] = (
                str(changes.get("last_confirmed_at") or "").strip()[:80] or None
            )
        for field in JSON_FIELDS:
            if field in changes:
                updates[field] = json.dumps(_clean_terms(changes.get(field)), ensure_ascii=False)
        if not updates:
            return self.get_issue(issue_id)
        now = utc_now()
        updates["updated_at"] = now
        with self._lock, self._connection() as connection:
            before = self._row(connection.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone())
            if not before:
                return None
            effective_domain = str(
                updates.get("issue_domain") or before.get("issue_domain") or "other"
            )
            requested_plugin_name = normalize_plugin_name(changes.get("plugin_name"))
            if requested_plugin_name and effective_domain != "plugin":
                return None
            if effective_domain != "plugin":
                updates["plugin_name"] = ""
            next_status = updates.get("status")
            previous_status = str(before.get("status") or "")
            if (
                (previous_status == "duplicate" or bool(before.get("duplicate_of")))
                and next_status is not None
                and next_status != previous_status
            ):
                return None
            if updates.get("verification_status") in CONFIRMED_VERIFICATION_STATUSES:
                updates["last_confirmed_at"] = (
                    updates.get("last_confirmed_at")
                    or before.get("last_confirmed_at")
                    or now
                )
            if next_status == "resolved" and previous_status != "resolved":
                updates["resolved_at"] = now
            elif next_status in {"pending", "triaged", "in_progress", "wont_fix"} and before.get("resolved_at"):
                updates["resolved_at"] = None
            assignments = ", ".join(f"{field} = ?" for field in updates)
            connection.execute(
                f"UPDATE issues SET {assignments} WHERE id = ?",
                (*updates.values(), issue_id),
            )
            visible_changes = {
                key: value for key, value in updates.items() if key not in {"updated_at", "resolved_at"}
            }
            self._audit(connection, issue_id, "update", actor, visible_changes)
        return self.get_issue(issue_id)

    def archive_issue(self, issue_id: str, actor: str = "page") -> bool:
        return self.update_issue(issue_id, {"status": "archived"}, actor=actor) is not None

    def bulk_update(self, issue_ids: Iterable[str], changes: dict[str, Any], actor: str = "page") -> int:
        return len(self.bulk_update_ids(issue_ids, changes, actor=actor))

    def bulk_update_ids(
        self,
        issue_ids: Iterable[str],
        changes: dict[str, Any],
        actor: str = "page",
    ) -> list[str]:
        if issue_change_error(changes):
            return []
        has_valid_change = (
            any(field in changes for field in set(ISSUE_TEXT_LIMITS) | JSON_FIELDS)
            or changes.get("kind") in ISSUE_KINDS
            or changes.get("category") in ISSUE_CATEGORIES
            or changes.get("issue_domain") in ISSUE_DOMAINS
            or "plugin_name" in changes
            or changes.get("severity") in ISSUE_SEVERITIES
            or changes.get("status") in ISSUE_STATUSES
            or changes.get("verification_status") in VERIFICATION_STATUSES
            or "last_confirmed_at" in changes
        )
        if not has_valid_change:
            return []
        updated_ids: list[str] = []
        for issue_id in dict.fromkeys(str(item) for item in issue_ids):
            if self.update_issue(issue_id, changes, actor=actor):
                updated_ids.append(issue_id)
        return updated_ids

    def merge_issues(self, source_ids: Iterable[str], target_id: str, actor: str = "page") -> dict[str, Any] | None:
        target_id = str(target_id or "").strip()
        source_ids = [
            item
            for item in dict.fromkeys(str(value or "").strip() for value in source_ids)
            if item and item != target_id
        ]
        if not target_id or not source_ids:
            return None
        now = utc_now()
        with self._lock, self._connection() as connection:
            target_row = connection.execute(
                "SELECT * FROM issues WHERE id = ?", (target_id,)
            ).fetchone()
            if (
                not target_row
                or target_row["status"] in {"duplicate", "archived"}
                or target_row["duplicate_of"]
            ):
                return None
            placeholders = ",".join("?" for _ in source_ids)
            source_rows = connection.execute(
                f"SELECT * FROM issues WHERE id IN ({placeholders})", source_ids
            ).fetchall()
            if len(source_rows) != len(source_ids):
                return None
            sources = {str(row["id"]): row for row in source_rows}
            target_group = str(target_row["group_key"] or "")
            if any(
                str(sources[source_id]["group_key"] or "") != target_group
                or sources[source_id]["status"] == "duplicate"
                or bool(sources[source_id]["duplicate_of"])
                for source_id in source_ids
            ):
                return None
            promotion = _merge_state_promotion(target_row, sources, source_ids)
            if promotion is None:
                return None
            promoted_updates, promoted_fields, witness_id = promotion
            effective_domain = str(
                promoted_updates.get("issue_domain")
                or target_row["issue_domain"]
                or "other"
            )
            plugin_promotion = _merge_plugin_name_promotion(
                target_row,
                sources,
                source_ids,
                effective_domain,
            )
            if plugin_promotion is None:
                return None
            plugin_updates, plugin_fields = plugin_promotion
            trusted_source_ids = _merge_trusted_source_ids(
                target_row,
                sources,
                source_ids,
                witness_id,
                normalize_plugin_name(
                    plugin_updates.get("plugin_name") or target_row["plugin_name"]
                ),
            )
            if trusted_source_ids is None:
                return None
            reusable_after_merge = _row_has_reusable_knowledge(target_row) or bool(
                witness_id
            )
            preservation_source_ids = (
                [witness_id]
                + [source_id for source_id in source_ids if source_id != witness_id]
                if witness_id
                else source_ids
            )
            preserved_updates: dict[str, Any] = {}
            preserved_fields: dict[str, list[str]] = {}
            for field in MERGE_PRESERVED_TEXT_FIELDS:
                if str(target_row[field] or "").strip():
                    continue
                field_source_ids = (
                    trusted_source_ids
                    if reusable_after_merge and field in MERGE_TRUSTED_TEXT_FIELDS
                    else preservation_source_ids
                )
                for source_id in field_source_ids:
                    value = str(sources[source_id][field] or "").strip()
                    if value:
                        preserved_updates[field] = value
                        preserved_fields[field] = [source_id]
                        break
            for field in JSON_FIELDS:
                combined = _clean_terms(_json_load(target_row[field], []))
                original = list(combined)
                contributing_sources: list[str] = []
                for source_id in preservation_source_ids:
                    contributed = False
                    for term in _clean_terms(_json_load(sources[source_id][field], [])):
                        if term in combined or len(combined) >= 20:
                            continue
                        combined.append(term)
                        contributed = True
                    if contributed:
                        contributing_sources.append(source_id)
                if combined != original:
                    preserved_updates[field] = json.dumps(combined, ensure_ascii=False)
                    preserved_fields[field] = contributing_sources
            preserved_updates.update(promoted_updates)
            preserved_updates.update(plugin_updates)
            promoted_fields.update(plugin_fields)
            merged: list[str] = []
            merged_occurrences = 0
            moved_open_todos = 0
            for source_id in source_ids:
                source = sources[source_id]
                connection.execute(
                    "UPDATE issue_evidence SET issue_id = ? WHERE issue_id = ?",
                    (target_id, source_id),
                )
                todo_cursor = connection.execute(
                    """
                    UPDATE issue_todos
                    SET issue_id = ?, updated_at = ?
                    WHERE issue_id = ? AND status = 'open'
                    """,
                    (target_id, now, source_id),
                )
                moved_open_todos += max(0, int(todo_cursor.rowcount))
                connection.execute(
                    """
                    UPDATE issues SET status = 'duplicate', duplicate_of = ?,
                        evidence_count = 0, updated_at = ? WHERE id = ?
                    """,
                    (target_id, now, source_id),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO issue_relations(
                        source_issue_id, target_issue_id, relation_type, score, reason, created_at
                    ) VALUES (?, ?, 'duplicate_of', 1, '人工合并', ?)
                    """,
                    (source_id, target_id, now),
                )
                self._audit(connection, source_id, "merge_into", actor, {"target_id": target_id})
                merged.append(source_id)
                try:
                    merged_occurrences += max(1, int(source["occurrence_count"] or 1))
                except (TypeError, ValueError):
                    merged_occurrences += 1
            if preserved_updates:
                assignments = ", ".join(
                    f"{field} = ?" for field in preserved_updates
                )
                connection.execute(
                    f"UPDATE issues SET {assignments} WHERE id = ?",
                    (*preserved_updates.values(), target_id),
                )
            connection.execute(
                """
                UPDATE issues SET
                    evidence_count = (SELECT COUNT(*) FROM issue_evidence WHERE issue_id = ?),
                    occurrence_count = occurrence_count + ?,
                    updated_at = ?, last_seen_at = ?
                WHERE id = ?
                """,
                (target_id, merged_occurrences, now, now, target_id),
            )
            audit_detail: dict[str, Any] = {"source_ids": merged}
            if moved_open_todos:
                audit_detail["moved_open_todos"] = moved_open_todos
            if preserved_fields:
                audit_detail["preserved_fields"] = preserved_fields
            if promoted_fields:
                audit_detail["promoted_fields"] = promoted_fields
            self._audit(connection, target_id, "merge_sources", actor, audit_detail)
        return self.get_issue(target_id)

    def similar_issues(
        self,
        issue_or_query: dict[str, Any] | str,
        *,
        limit: int = 8,
        include_pending: bool = True,
        exclude_id: str = "",
        group_key: str = "",
    ) -> list[dict[str, Any]]:
        if isinstance(issue_or_query, str):
            probe = {"title": issue_or_query, "summary": issue_or_query, "kind": "question"}
        else:
            probe = issue_or_query
            exclude_id = exclude_id or str(probe.get("id") or "")
            group_key = group_key or str(probe.get("group_key") or "")
        statuses = "('pending','triaged','in_progress','resolved','wont_fix')" if include_pending else "('resolved')"
        where = f"status IN {statuses}"
        params: tuple[Any, ...] = ()
        if group_key:
            where += " AND group_key = ?"
            params = (group_key,)
        with self._lock, self._connection() as connection:
            rows = connection.execute(f"SELECT * FROM issues WHERE {where}", params).fetchall()
        scored: list[dict[str, Any]] = []
        for row in rows:
            item = self._row(row) or {}
            if item.get("id") == exclude_id:
                continue
            score = similarity_score(probe, item)
            if score < 0.08:
                continue
            scored.append({**item, "similarity": round(score, 4)})
        scored.sort(key=lambda item: (item["similarity"], item.get("updated_at", "")), reverse=True)
        return scored[: max(1, min(int(limit), 30))]

    def save_similarity_links(self, issue_id: str, candidates: list[dict[str, Any]]) -> None:
        now = utc_now()
        with self._lock, self._connection() as connection:
            for candidate in candidates:
                target_id = str(candidate.get("id") or "")
                if not target_id or target_id == issue_id:
                    continue
                connection.execute(
                    """
                    INSERT INTO issue_relations(
                        source_issue_id, target_issue_id, relation_type, score, reason, created_at
                    ) VALUES (?, ?, 'similar', ?, ?, ?)
                    ON CONFLICT(source_issue_id, target_issue_id, relation_type)
                    DO UPDATE SET score = excluded.score, reason = excluded.reason, created_at = excluded.created_at
                    """,
                    (
                        issue_id,
                        target_id,
                        float(candidate.get("similarity") or 0),
                        str(candidate.get("reason") or "文本与结构相似")[:500],
                        now,
                    ),
                )

    def record_answer(
        self,
        *,
        group_key: str,
        user_id: str,
        user_name: str,
        query: str,
        issue_ids: list[str],
        answer: str,
        confidence: float,
        sent: bool,
        reason: str = "",
    ) -> int:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO answer_audits(
                    group_key, user_id, user_name, query, issue_ids,
                    answer, confidence, sent, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_key,
                    user_id,
                    user_name,
                    query[:4000],
                    json.dumps(issue_ids, ensure_ascii=False),
                    answer[:8000],
                    max(0.0, min(float(confidence), 1.0)),
                    int(sent),
                    reason[:1000],
                    utc_now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_answers(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM answer_audits ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        result = []
        for row in rows:
            item = self._row(row) or {}
            item["issue_ids"] = _json_load(item.get("issue_ids"), [])
            result.append(item)
        return result

    def overview(self, group_key: str = "") -> dict[str, Any]:
        issue_where = "1 = 1"
        issue_params: tuple[Any, ...] = ()
        message_where = "1 = 1"
        message_params: tuple[Any, ...] = ()
        answer_where = "1 = 1"
        answer_params: tuple[Any, ...] = ()
        todo_where = "1 = 1"
        todo_params: tuple[Any, ...] = ()
        if group_key:
            issue_where += " AND group_key = ?"
            issue_params = (group_key,)
            message_where += " AND group_key = ?"
            message_params = (group_key,)
            answer_where += " AND group_key = ?"
            answer_params = (group_key,)
            todo_where += " AND i.group_key = ?"
            todo_params = (group_key,)
        with self._lock, self._connection() as connection:
            status_rows = connection.execute(
                f"SELECT status, COUNT(*) AS count FROM issues WHERE {issue_where} AND status != 'archived' GROUP BY status",
                issue_params,
            ).fetchall()
            kind_rows = connection.execute(
                f"SELECT kind, COUNT(*) AS count FROM issues WHERE {issue_where} AND status != 'archived' GROUP BY kind",
                issue_params,
            ).fetchall()
            category_rows = connection.execute(
                f"SELECT category, COUNT(*) AS count FROM issues WHERE {issue_where} AND status != 'archived' GROUP BY category",
                issue_params,
            ).fetchall()
            domain_rows = connection.execute(
                f"SELECT issue_domain, COUNT(*) AS count FROM issues WHERE {issue_where} AND status != 'archived' GROUP BY issue_domain",
                issue_params,
            ).fetchall()
            plugin_rows = connection.execute(
                f"SELECT plugin_name, COUNT(*) AS count FROM issues WHERE {issue_where} "
                "AND status != 'archived' AND issue_domain = 'plugin' "
                "AND plugin_name != '' GROUP BY plugin_name",
                issue_params,
            ).fetchall()
            verification_rows = connection.execute(
                f"SELECT verification_status, COUNT(*) AS count FROM issues WHERE {issue_where} AND status != 'archived' GROUP BY verification_status",
                issue_params,
            ).fetchall()
            total = int(connection.execute(
                f"SELECT COUNT(*) AS count FROM issues WHERE {issue_where} AND status != 'archived'",
                issue_params,
            ).fetchone()["count"])
            pending_messages = int(connection.execute(
                f"SELECT COUNT(*) AS count FROM raw_messages WHERE {message_where} AND processed_at IS NULL",
                message_params,
            ).fetchone()["count"])
            if group_key:
                enabled_groups = int(connection.execute(
                    "SELECT COUNT(*) AS count FROM groups WHERE group_key = ? AND enabled = 1",
                    (group_key,),
                ).fetchone()["count"])
            else:
                enabled_groups = int(connection.execute("SELECT COUNT(*) AS count FROM groups WHERE enabled = 1").fetchone()["count"])
            resolved = int(connection.execute(
                f"SELECT COUNT(*) AS count FROM issues WHERE {issue_where} AND status = 'resolved'",
                issue_params,
            ).fetchone()["count"])
            answers = int(connection.execute(
                f"SELECT COUNT(*) AS count FROM answer_audits WHERE {answer_where} AND sent = 1",
                answer_params,
            ).fetchone()["count"])
            pending_reviews = int(connection.execute(
                f"SELECT COUNT(*) AS count FROM issues WHERE {issue_where} "
                "AND reviewed_at IS NULL AND status NOT IN ('archived', 'duplicate')",
                issue_params,
            ).fetchone()["count"])
            published_knowledge = int(connection.execute(
                f"SELECT COUNT(*) AS count FROM issues WHERE {issue_where} "
                "AND review_decision = 'approved' "
                "AND knowledge_published_at IS NOT NULL AND status != 'duplicate'",
                issue_params,
            ).fetchone()["count"])
            open_todos = int(connection.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM issue_todos t
                JOIN issues i ON i.id = t.issue_id
                WHERE {todo_where} AND t.status = 'open'
                """,
                todo_params,
            ).fetchone()["count"])
            recent_rows = connection.execute(
                f"SELECT * FROM issues WHERE {issue_where} AND status != 'archived' ORDER BY updated_at DESC LIMIT 8",
                issue_params,
            ).fetchall()
        return {
            "total": total,
            "open": sum(int(row["count"]) for row in status_rows if row["status"] in {"pending", "triaged", "in_progress"}),
            "resolved": resolved,
            "pending_messages": pending_messages,
            "enabled_groups": enabled_groups,
            "answers_sent": answers,
            "pending_reviews": pending_reviews,
            "open_todos": open_todos,
            "published_knowledge": published_knowledge,
            "by_status": {row["status"]: int(row["count"]) for row in status_rows},
            "by_kind": {row["kind"]: int(row["count"]) for row in kind_rows},
            "by_category": {
                row["category"]: int(row["count"]) for row in category_rows
            },
            "by_issue_domain": {
                row["issue_domain"]: int(row["count"]) for row in domain_rows
            },
            "by_plugin_name": {
                row["plugin_name"]: int(row["count"]) for row in plugin_rows
            },
            "by_verification_status": {
                row["verification_status"]: int(row["count"])
                for row in verification_rows
            },
            "recent": [self._row(row) or {} for row in recent_rows],
        }

    def maintenance(self, raw_message_days: int) -> dict[str, int]:
        removed = 0
        if raw_message_days > 0:
            with self._lock, self._connection() as connection:
                cursor = connection.execute(
                    """
                    DELETE FROM raw_messages
                    WHERE processed_at IS NOT NULL
                      AND julianday(created_at) < julianday('now', ?)
                      AND id NOT IN (
                          SELECT raw_message_id FROM issue_evidence WHERE raw_message_id IS NOT NULL
                      )
                    """,
                    (f"-{int(raw_message_days)} days",),
                )
                removed = cursor.rowcount
                connection.execute("PRAGMA optimize")
        return {"raw_messages_removed": max(0, removed)}

    def export_data(self) -> dict[str, Any]:
        with self._lock, self._connection() as connection:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            groups = [self._row(row) or {} for row in connection.execute("SELECT * FROM groups ORDER BY group_key")]
            issues = [self._row(row) or {} for row in connection.execute("SELECT * FROM issues ORDER BY created_at")]
            evidence = [self._row(row) or {} for row in connection.execute("SELECT * FROM issue_evidence ORDER BY id")]
            relations = [self._row(row) or {} for row in connection.execute("SELECT * FROM issue_relations ORDER BY id")]
            todos = [
                self._row(row) or {}
                for row in connection.execute("SELECT * FROM issue_todos ORDER BY id")
            ]
            answer_rows = connection.execute(
                "SELECT * FROM answer_audits ORDER BY id DESC"
            ).fetchall()
            answers = []
            for row in answer_rows:
                item = self._row(row) or {}
                item["issue_ids"] = _json_load(item.get("issue_ids"), [])
                answers.append(item)
        return {
            "format": "astrbot-bug-companion-export",
            "version": 5,
            "exported_at": utc_now(),
            "groups": groups,
            "issues": issues,
            "evidence": evidence,
            "relations": relations,
            "todos": todos,
            "answers": answers,
        }

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        issue_id: str,
        action: str,
        actor: str,
        detail: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO audit_log(issue_id, action, actor, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                issue_id,
                action[:80],
                actor[:100],
                json.dumps(detail, ensure_ascii=False, default=str),
                utc_now(),
            ),
        )
