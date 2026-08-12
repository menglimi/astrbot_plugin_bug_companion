# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from astrbot_plugin_bug_companion.storage import IssueStore


def _candidate(
    store: IssueStore,
    title: str = "待批阅的问题",
    *,
    pre_review: dict[str, object] | None = None,
) -> dict[str, object]:
    return store.create_issue(
        {
            "title": title,
            "category": "fixed_bug",
            "status": "resolved",
            "verification_status": "verified",
            "solution": "升级插件后重启。",
            "pre_review": pre_review or {},
        },
        actor="model",
        requires_review=True,
    )


def _load_service_class():
    try:
        from astrbot_plugin_bug_companion.service import BugCompanionService

        return BugCompanionService
    except ModuleNotFoundError as exc:
        if not str(exc.name or "").startswith("astrbot"):
            raise

    logger = types.SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
    )
    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logger
    astrbot_module.api = api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    try:
        module = importlib.import_module("astrbot_plugin_bug_companion.service")
        return module.BugCompanionService
    finally:
        sys.modules.pop("astrbot_plugin_bug_companion.service", None)
        sys.modules.pop("astrbot_plugin_bug_companion.organizer", None)
        sys.modules.pop("astrbot.api", None)
        sys.modules.pop("astrbot", None)


def test_legacy_review_migrations_reopen_model_issue_but_preserve_archived(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "bug_companion.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE issues (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'bug',
                category TEXT NOT NULL DEFAULT 'pending_feedback',
                issue_domain TEXT NOT NULL DEFAULT 'other',
                plugin_name TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                severity TEXT NOT NULL DEFAULT 'medium',
                group_key TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'model',
                verification_status TEXT NOT NULL DEFAULT 'unverified',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            INSERT INTO issues(
                id, title, status, created_at, updated_at, last_seen_at
            ) VALUES
                (
                    'BUG-V5-OPEN', '旧库中的有效记录', 'resolved',
                    '2026-07-01T00:00:00+00:00',
                    '2026-07-02T00:00:00+00:00',
                    '2026-07-03T00:00:00+00:00'
                ),
                (
                    'BUG-V5-ARCHIVED', '旧库中的封存记录', 'archived',
                    '2026-07-04T00:00:00+00:00',
                    '2026-07-05T00:00:00+00:00',
                    '2026-07-06T00:00:00+00:00'
                );
            PRAGMA user_version = 5;
            """
        )

    store = IssueStore(tmp_path)

    with sqlite3.connect(store.db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = {
            row["id"]: dict(row)
            for row in connection.execute(
                "SELECT id, reviewed_at, review_decision, "
                "knowledge_published_at, pre_review_json FROM issues"
            )
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == 9
    assert rows["BUG-V5-OPEN"]["reviewed_at"] is None
    assert rows["BUG-V5-OPEN"]["review_decision"] == ""
    assert rows["BUG-V5-OPEN"]["knowledge_published_at"] is None
    assert rows["BUG-V5-ARCHIVED"]["reviewed_at"]
    assert rows["BUG-V5-ARCHIVED"]["review_decision"] == "approved"
    assert rows["BUG-V5-ARCHIVED"]["knowledge_published_at"] is None
    assert rows["BUG-V5-OPEN"]["pre_review_json"] == "{}"


def test_v7_reopens_model_records_and_v8_unpublishes_assigned_work(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "bug_companion.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE issues (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'bug',
                category TEXT NOT NULL DEFAULT 'pending_feedback',
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                severity TEXT NOT NULL DEFAULT 'medium',
                group_key TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'model',
                reviewed_at TEXT,
                review_decision TEXT NOT NULL DEFAULT '',
                review_note TEXT NOT NULL DEFAULT '',
                knowledge_published_at TEXT,
                pre_review_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_id TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                actor TEXT NOT NULL DEFAULT 'system',
                detail TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            INSERT INTO issues(
                id, title, status, source, reviewed_at, review_decision,
                review_note, knowledge_published_at,
                created_at, updated_at, last_seen_at
            ) VALUES
                ('MODEL-AUTO', '模型迁移记录', 'pending', 'model',
                 '2026-07-01T00:00:00+00:00', 'approved', '旧自动批准',
                 '2026-07-01T00:00:00+00:00',
                 '2026-07-01T00:00:00+00:00',
                 '2026-07-01T00:00:00+00:00',
                 '2026-07-01T00:00:00+00:00'),
                ('MODEL-REVIEWED', '人工批阅模型记录', 'resolved', 'model',
                 '2026-07-02T00:00:00+00:00', 'approved', '人工准奏',
                 '2026-07-02T00:00:00+00:00',
                 '2026-07-02T00:00:00+00:00',
                 '2026-07-02T00:00:00+00:00',
                 '2026-07-02T00:00:00+00:00'),
                ('PAGE-APPROVED', '页面直接创建', 'pending', 'page',
                 '2026-07-03T00:00:00+00:00', 'approved', '页面记录',
                 '2026-07-03T00:00:00+00:00',
                 '2026-07-03T00:00:00+00:00',
                 '2026-07-03T00:00:00+00:00',
                 '2026-07-03T00:00:00+00:00'),
                ('MODEL-ARCHIVED', '封存模型记录', 'archived', 'model',
                 '2026-07-04T00:00:00+00:00', 'approved', '保持封存',
                 NULL,
                 '2026-07-04T00:00:00+00:00',
                 '2026-07-04T00:00:00+00:00',
                 '2026-07-04T00:00:00+00:00'),
                ('MODEL-DUPLICATE', '重复模型记录', 'duplicate', 'model',
                 '2026-07-05T00:00:00+00:00', 'approved', '保持重复',
                 '2026-07-05T00:00:00+00:00',
                 '2026-07-05T00:00:00+00:00',
                 '2026-07-05T00:00:00+00:00',
                 '2026-07-05T00:00:00+00:00'),
                ('MODEL-TODO', '已交办模型记录', 'in_progress', 'model',
                 '2026-07-06T00:00:00+00:00', 'todo', '已经交办',
                 '2026-07-06T00:00:00+00:00',
                 '2026-07-06T00:00:00+00:00',
                 '2026-07-06T00:00:00+00:00',
                 '2026-07-06T00:00:00+00:00'),
                ('MODEL-OTHER-AUDIT', '只有普通审计', 'triaged', 'model',
                 '2026-07-07T00:00:00+00:00', 'approved', '仍应重呈',
                 '2026-07-07T00:00:00+00:00',
                 '2026-07-07T00:00:00+00:00',
                 '2026-07-07T00:00:00+00:00',
                 '2026-07-07T00:00:00+00:00');
            INSERT INTO audit_log(issue_id, action, actor, detail, created_at)
            VALUES
                ('MODEL-REVIEWED', 'review_decide', 'page', '{}',
                 '2026-07-08T00:00:00+00:00'),
                ('MODEL-OTHER-AUDIT', 'update', 'page', '{}',
                 '2026-07-08T00:00:00+00:00');
            PRAGMA user_version = 6;
            """
        )

    IssueStore(tmp_path)
    with sqlite3.connect(db_path) as connection:
        rows = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT id, reviewed_at, review_decision, review_note, "
                "knowledge_published_at FROM issues ORDER BY id"
            )
        }
        audits_before = connection.execute(
            "SELECT issue_id, action FROM audit_log ORDER BY id"
        ).fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert version == 9
    assert rows["MODEL-AUTO"] == (None, "", "", None)
    assert rows["MODEL-OTHER-AUDIT"] == (None, "", "", None)
    assert rows["MODEL-REVIEWED"] == (
        "2026-07-02T00:00:00+00:00",
        "approved",
        "人工准奏",
        "2026-07-02T00:00:00+00:00",
    )
    assert rows["PAGE-APPROVED"][1:] == (
        "approved",
        "页面记录",
        "2026-07-03T00:00:00+00:00",
    )
    assert rows["MODEL-ARCHIVED"][1:] == ("approved", "保持封存", None)
    assert rows["MODEL-DUPLICATE"][1:] == (
        "approved",
        "保持重复",
        "2026-07-05T00:00:00+00:00",
    )
    assert rows["MODEL-TODO"][1:] == (
        "todo",
        "已经交办",
        None,
    )
    assert audits_before == [
        ("MODEL-REVIEWED", "review_decide"),
        ("MODEL-OTHER-AUDIT", "update"),
    ]

    IssueStore(tmp_path)
    with sqlite3.connect(db_path) as connection:
        rows_after = {
            row[0]: row[1:]
            for row in connection.execute(
                "SELECT id, reviewed_at, review_decision, review_note, "
                "knowledge_published_at FROM issues ORDER BY id"
            )
        }
        audits_after = connection.execute(
            "SELECT issue_id, action FROM audit_log ORDER BY id"
        ).fetchall()
    assert rows_after == rows
    assert audits_after == audits_before


def test_requires_review_candidate_stays_unreviewed_and_unpublished(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    pre_review = {
        "recommendation": "approve",
        "reason": "已有修复方案",
        "missing_information": [],
        "suggested_todos": ["补充回归测试"],
    }

    issue = _candidate(store, pre_review=pre_review)
    queue = store.list_review_queue()

    assert issue["reviewed_at"] is None
    assert issue["review_decision"] == ""
    assert issue["knowledge_published_at"] is None
    assert issue["pre_review_json"] == pre_review
    assert queue["total"] == 1
    assert [item["id"] for item in queue["items"]] == [issue["id"]]


def test_v9_clears_publication_from_every_nonapproved_decision(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    approved = store.create_issue({"title": "人工认可知识"})
    stale = [store.create_issue({"title": f"错误发布 {index}"}) for index in range(4)]
    decisions = ["", "todo", "deferred", "rejected"]
    with sqlite3.connect(store.db_path) as connection:
        for issue, decision in zip(stale, decisions, strict=True):
            connection.execute(
                "UPDATE issues SET review_decision = ?, knowledge_published_at = ? "
                "WHERE id = ?",
                (decision, "2026-08-01T00:00:00+00:00", issue["id"]),
            )
        connection.execute("PRAGMA user_version = 8")

    IssueStore(tmp_path)

    with sqlite3.connect(store.db_path) as connection:
        rows = dict(
            connection.execute(
                "SELECT id, knowledge_published_at FROM issues"
            ).fetchall()
        )
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == 9
    assert rows[approved["id"]]
    assert all(rows[issue["id"]] is None for issue in stale)


def test_review_decide_supports_approve_todo_defer_and_reject(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    approved = _candidate(store, "准奏记录")
    todo_issue = _candidate(store, "列入待办记录")
    deferred = _candidate(store, "留中记录")
    rejected = _candidate(store, "驳回记录")

    approved_result = store.review_decide(
        str(approved["id"]),
        "approve",
        note="准奏归档",
        expected_updated_at=str(approved["updated_at"]),
    )
    todo_result = store.review_decide(
        str(todo_issue["id"]),
        "todo",
        note="先补齐回归测试",
        todo={"content": "补充回归测试", "due_at": "2026-08-15"},
        expected_updated_at=str(todo_issue["updated_at"]),
    )
    deferred_result = store.review_decide(
        str(deferred["id"]),
        "defer",
        note="等待更多复现信息",
        expected_updated_at=str(deferred["updated_at"]),
    )
    rejected_result = store.review_decide(
        str(rejected["id"]),
        "reject",
        note="不属于插件问题",
        expected_updated_at=str(rejected["updated_at"]),
    )

    assert approved_result["ok"] is True
    assert approved_result["issue"]["review_decision"] == "approved"
    assert approved_result["issue"]["reviewed_at"]
    assert approved_result["issue"]["knowledge_published_at"]

    assert todo_result["ok"] is True
    assert todo_result["issue"]["review_decision"] == "todo"
    assert todo_result["issue"]["reviewed_at"]
    assert todo_result["issue"]["knowledge_published_at"] is None
    assert todo_result["todo"]["content"] == "补充回归测试"
    assert todo_result["todo"]["status"] == "open"

    assert deferred_result["ok"] is True
    assert deferred_result["issue"]["review_decision"] == "deferred"
    assert deferred_result["issue"]["reviewed_at"]
    assert deferred_result["issue"]["knowledge_published_at"] is None

    assert rejected_result["ok"] is True
    assert rejected_result["issue"]["review_decision"] == "rejected"
    assert rejected_result["issue"]["reviewed_at"]
    assert rejected_result["issue"]["knowledge_published_at"] is None

    assert store.list_review_queue() == {"items": [], "total": 0}


def test_review_decide_detects_conflicts_and_retries_idempotently(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    issue = _candidate(store)

    conflict = store.review_decide(
        str(issue["id"]),
        "approve",
        expected_updated_at="2000-01-01T00:00:00+00:00",
    )
    assert conflict["ok"] is False
    assert conflict["error"] == "conflict"
    assert store.get_issue(str(issue["id"]))["reviewed_at"] is None

    first = store.review_decide(
        str(issue["id"]),
        "todo",
        note="安排验证",
        todo={"content": "验证修复方案"},
        expected_updated_at=str(issue["updated_at"]),
    )
    retry = store.review_decide(
        str(issue["id"]),
        "todo",
        note="安排验证",
        todo={"content": "验证修复方案"},
        expected_updated_at=str(issue["updated_at"]),
    )

    assert first["ok"] is True
    assert not first.get("idempotent", False)
    assert retry["ok"] is True
    assert retry["idempotent"] is True
    assert retry["todo"]["id"] == first["todo"]["id"]
    assert len(store.list_todos(issue_id=str(issue["id"]))) == 1


def test_review_decide_rolls_back_issue_and_todo_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = IssueStore(tmp_path)
    issue = _candidate(store)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(store, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        store.review_decide(
            str(issue["id"]),
            "todo",
            todo={"content": "不能留下半条待办"},
        )

    current = store.get_issue(str(issue["id"]))
    assert current is not None
    assert current["reviewed_at"] is None
    assert current["knowledge_published_at"] is None
    assert current["todos"] == []


@pytest.mark.parametrize("action", ["defer", "reject"])
def test_review_reopen_is_conflict_safe_audited_and_idempotent(
    tmp_path: Path,
    action: str,
) -> None:
    store = IssueStore(tmp_path)
    issue = _candidate(store, f"等待{action}后重呈")
    decided = store.review_decide(
        str(issue["id"]),
        action,
        note="暂不入典",
        expected_updated_at=str(issue["updated_at"]),
    )
    assert decided["ok"] is True
    decided_issue = decided["issue"]

    conflict = store.review_reopen(
        str(issue["id"]),
        expected_updated_at="2000-01-01T00:00:00+00:00",
    )
    assert conflict["ok"] is False
    assert conflict["error"] == "conflict"
    assert conflict["issue"]["review_decision"] in {"deferred", "rejected"}

    reopened = store.review_reopen(
        str(issue["id"]),
        expected_updated_at=str(decided_issue["updated_at"]),
    )
    retry = store.review_reopen(
        str(issue["id"]),
        expected_updated_at=str(decided_issue["updated_at"]),
    )

    assert reopened["ok"] is True
    assert reopened["idempotent"] is False
    assert reopened["issue"]["reviewed_at"] is None
    assert reopened["issue"]["review_decision"] == ""
    assert reopened["issue"]["review_note"] == ""
    assert reopened["issue"]["knowledge_published_at"] is None
    assert retry["ok"] is True
    assert retry["idempotent"] is True
    assert retry["issue"]["updated_at"] == reopened["issue"]["updated_at"]
    assert [item["id"] for item in store.list_review_queue()["items"]] == [
        issue["id"]
    ]
    actions = [item["action"] for item in retry["issue"]["audit"]]
    assert actions.count("review_reopen") == 1
    assert "review_decide" in actions


def test_review_reopen_rolls_back_when_audit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = IssueStore(tmp_path)
    issue = _candidate(store)
    decided = store.review_decide(str(issue["id"]), "reject", note="保留原决定")
    assert decided["ok"] is True

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("simulated reopen audit failure")

    monkeypatch.setattr(store, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="simulated reopen audit failure"):
        store.review_reopen(str(issue["id"]))

    current = store.get_issue(str(issue["id"]))
    assert current is not None
    assert current["reviewed_at"]
    assert current["review_decision"] == "rejected"
    assert current["review_note"] == "保留原决定"


def test_todo_create_update_and_filtered_list(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    first_issue = _candidate(store, "第一个问题")
    second_issue = _candidate(store, "第二个问题")
    first = store.create_todo(
        str(first_issue["id"]),
        "收集日志",
        due_at="2026-08-10",
        created_by="reviewer-1",
    )
    second = store.create_todo(str(second_issue["id"]), "验证修复")

    assert first is not None
    assert second is not None
    assert [item["id"] for item in store.list_todos(status="open")] == [
        second["id"],
        first["id"],
    ]
    assert store.list_todos(issue_id=str(first_issue["id"])) == [first]

    completed = store.update_todo(
        int(first["id"]),
        {
            "content": "收集完整日志",
            "status": "completed",
            "due_at": "2026-08-12",
        },
        actor="reviewer-1",
    )

    assert completed is not None
    assert completed["content"] == "收集完整日志"
    assert completed["status"] == "completed"
    assert completed["due_at"] == "2026-08-12"
    assert completed["completed_at"]
    assert [item["id"] for item in store.list_todos(status="completed")] == [
        first["id"]
    ]
    assert store.update_todo(999999, {"status": "completed"}) is None


def test_todo_pagination_counts_full_filtered_result(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    first_issue = _candidate(store, "分页问题一")
    second_issue = _candidate(store, "分页问题二")
    todos = [
        store.create_todo(str(first_issue["id"]), f"第一组待办 {index}")
        for index in range(4)
    ]
    last = store.create_todo(str(second_issue["id"]), "第二组待办")
    assert all(todo is not None for todo in todos)
    assert last is not None
    store.update_todo(int(todos[0]["id"]), {"status": "completed"})
    store.update_todo(int(last["id"]), {"status": "completed"})

    first_page = store.list_todos(page=1, limit=2)
    second_page = store.list_todos(page=2, limit=2)
    completed = store.list_todos(page=1, limit=1, status="completed")
    first_issue_only = store.list_todos(
        page=1,
        limit=2,
        issue_id=str(first_issue["id"]),
    )

    assert isinstance(first_page, dict)
    assert first_page["total"] == 5
    assert first_page["open"] == 3
    assert first_page["page"] == 1
    assert first_page["pages"] == 3
    assert len(first_page["items"]) == 2
    assert second_page["total"] == 5
    assert second_page["page"] == 2
    assert len(second_page["items"]) == 2
    assert completed["total"] == 2
    assert completed["open"] == 3
    assert completed["pages"] == 2
    assert len(completed["items"]) == 1
    assert first_issue_only["total"] == 4
    assert first_issue_only["open"] == 3
    assert first_issue_only["pages"] == 2


def test_review_fields_and_todos_reach_detail_overview_and_export(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    pending = _candidate(store, "仍待批阅")
    approved = _candidate(store, "已准奏归档")
    decision = store.review_decide(str(approved["id"]), "approve")
    todo = store.create_todo(str(approved["id"]), "发布后回访")
    assert decision["ok"] is True
    assert todo is not None

    detail = store.get_issue(str(approved["id"]))
    overview = store.overview()
    exported = store.export_data()

    assert detail is not None
    assert detail["review_decision"] == "approved"
    assert detail["knowledge_published_at"]
    assert [item["id"] for item in detail["todos"]] == [todo["id"]]
    assert overview["pending_reviews"] == 1
    assert overview["open_todos"] == 1
    assert overview["published_knowledge"] >= 1
    assert exported["version"] == 5
    assert exported["todos"][0]["issue_id"] == approved["id"]
    exported_by_id = {item["id"]: item for item in exported["issues"]}
    assert exported_by_id[pending["id"]]["reviewed_at"] is None
    assert exported_by_id[approved["id"]]["review_decision"] == "approved"


def test_merge_moves_open_todos_but_keeps_completed_history(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    target = store.create_issue({"title": "合并目标"})
    source = store.create_issue({"title": "待合并来源"})
    open_todo = store.create_todo(str(source["id"]), "仍需处理")
    completed_todo = store.create_todo(str(source["id"]), "已经完成")
    assert open_todo is not None
    assert completed_todo is not None
    store.update_todo(int(completed_todo["id"]), {"status": "completed"})

    merged = store.merge_issues([str(source["id"])], str(target["id"]))

    assert merged is not None
    assert [item["id"] for item in store.list_todos(issue_id=str(target["id"]))] == [
        open_todo["id"]
    ]
    assert [item["id"] for item in store.list_todos(issue_id=str(source["id"]))] == [
        completed_todo["id"]
    ]


def test_answer_reference_requires_human_publication() -> None:
    BugCompanionService = _load_service_class()

    reusable = {
        "category": "fixed_bug",
        "status": "resolved",
        "verification_status": "verified",
        "solution": "升级插件后重启。",
        "workaround": "",
        "review_decision": "approved",
    }

    assert BugCompanionService._answer_reference_allowed(
        {**reusable, "knowledge_published_at": "2026-07-31T00:00:00+00:00"}
    )
    assert not BugCompanionService._answer_reference_allowed(
        {**reusable, "knowledge_published_at": None}
    )
    assert not BugCompanionService._answer_reference_allowed(
        {
            **reusable,
            "review_decision": "todo",
            "knowledge_published_at": "2026-07-31T00:00:00+00:00",
        }
    )
