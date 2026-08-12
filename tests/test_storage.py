# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot_plugin_bug_companion.storage import IssueStore, issue_change_error


def test_repository_cursor_persists_notification_baseline(tmp_path: Path) -> None:
    repository = "https://github.com/menglimi/astrbot_plugin_bug_companion"
    store = IssueStore(tmp_path)

    assert store.get_repository_cursor(repository) is None
    saved = store.set_repository_cursor(
        repository,
        latest_commit="0123456789abcdef",
        latest_release="v0.3.4",
        initialized=True,
    )

    assert saved is not None
    assert saved["repository"] == repository
    assert saved["latest_commit"] == "0123456789abcdef"
    assert saved["latest_release"] == "v0.3.4"
    assert saved["initialized"] is True
    assert saved["initialized_at"]
    assert saved["updated_at"]

    reopened = IssueStore(tmp_path).get_repository_cursor(repository)
    assert reopened == saved


def test_repository_cursor_partial_updates_do_not_clobber_other_stream(
    tmp_path: Path,
) -> None:
    repository = "https://github.com/example/companion"
    store = IssueStore(tmp_path)
    baseline = store.set_repository_cursor(
        repository,
        latest_commit="commit-1",
        latest_release="release-1",
        initialized=True,
    )

    commit_update = store.set_repository_cursor(
        repository,
        latest_commit="commit-2",
    )
    assert baseline is not None and commit_update is not None
    assert commit_update["latest_commit"] == "commit-2"
    assert commit_update["latest_release"] == "release-1"
    assert commit_update["initialized"] is True
    assert commit_update["initialized_at"] == baseline["initialized_at"]

    release_update = store.set_repository_cursor(
        repository,
        latest_release="release-2",
    )
    assert release_update is not None
    assert release_update["latest_commit"] == "commit-2"
    assert release_update["latest_release"] == "release-2"

    cleared_commit = store.set_repository_cursor(repository, latest_commit="")
    assert cleared_commit is not None
    assert cleared_commit["latest_commit"] == ""
    assert cleared_commit["latest_release"] == "release-2"
    assert store.set_repository_cursor("   ", initialized=True) is None


def test_repository_cursor_can_be_created_before_baseline_is_initialized(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    pending = store.set_repository_cursor("https://github.com/example/pending")

    assert pending is not None
    assert pending["initialized"] is False
    assert pending["initialized_at"] is None

    initialized = store.set_repository_cursor(
        pending["repository"],
        latest_commit="first-seen",
        initialized=True,
    )
    assert initialized is not None
    assert initialized["initialized"] is True
    assert initialized["initialized_at"]
    assert [item["repository"] for item in store.list_repository_cursors()] == [
        pending["repository"]
    ]


def test_page_text_limits_are_explicit_without_rejecting_model_normalization() -> None:
    changes = {"solution": "解" * 6001}

    assert issue_change_error(changes) == ""
    assert issue_change_error(changes, enforce_limits=True) == (
        "解决方案不能超过 6000 个字符"
    )


def test_bulk_update_ids_reports_only_successful_items(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    first = store.create_issue({"title": "第一个问题"}, actor="page")
    second = store.create_issue({"title": "第二个问题"}, actor="page")

    updated_ids = store.bulk_update_ids(
        [first["id"], "BUG-999999", second["id"]],
        {"status": "resolved"},
        actor="page",
    )

    assert updated_ids == [first["id"], second["id"]]
    assert store.get_issue(first["id"])["status"] == "resolved"
    assert store.get_issue(second["id"])["status"] == "resolved"


def test_issue_lifecycle_and_similarity(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    store.record_group(
        group_key="onebot:123",
        platform_id="onebot",
        group_id="123",
        umo="onebot:GroupMessage:123",
        enabled=True,
    )
    raw_id = store.add_message(
        {
            "group_key": "onebot:123",
            "platform_id": "onebot",
            "group_id": "123",
            "umo": "onebot:GroupMessage:123",
            "message_id": "m-1",
            "sender_id": "u-1",
            "sender_name": "测试用户",
            "content": "开启 TTS 后，长回复没有分段。",
        }
    )
    assert store.group_keys_for_messages([raw_id]) == ["onebot:123"]
    issue = store.create_issue(
        {
            "title": "TTS 开启后长回复没有分段",
            "kind": "bug",
            "status": "resolved",
            "group_key": "onebot:123",
            "solution": "先完成语音转换，再按音频边界分段。",
            "tags": ["tts", "分段"],
        },
        evidence=[{"id": raw_id, "message_id": "m-1", "sender_name": "测试用户", "content": "开启 TTS 后，长回复没有分段。"}],
    )
    detail = store.get_issue(issue["id"])
    assert detail is not None
    assert detail["evidence_count"] == 1
    assert detail["tags"] == ["tts", "分段"]
    assert detail["resolved_at"]
    assert store.similar_issues("语音开启后分段失效", include_pending=False)

    updated = store.update_issue(issue["id"], {"status": "in_progress", "cause": "分段发生在 TTS 前"})
    assert updated and updated["status"] == "in_progress"
    assert store.list_issues(status="in_progress")["total"] == 1

    duplicate = store.create_issue({"title": "语音分段没有生效", "group_key": "onebot:123"})
    merged = store.merge_issues([duplicate["id"]], issue["id"])
    assert merged and merged["occurrence_count"] == 2
    assert store.get_issue(duplicate["id"])["evidence_count"] == 0


def test_group_override_survives_configured_rediscovery(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    store.record_group(
        group_key="onebot:123",
        platform_id="onebot",
        group_id="123",
        umo="onebot:GroupMessage:123",
        enabled=True,
    )
    assert store.is_group_enabled("onebot:123") is True

    assert store.set_group_enabled("onebot:123", False) is True
    assert store.get_group_override("onebot:123") is False
    store.record_group(
        group_key="onebot:123",
        platform_id="onebot",
        group_id="123",
        umo="onebot:GroupMessage:123",
        enabled=True,
    )
    assert store.is_group_enabled("onebot:123") is False

    assert store.set_group_enabled("onebot:123", True) is True
    assert store.get_group_override("onebot:123") is True


def test_similarity_can_be_scoped_to_one_group(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    for group_id in ("123", "456"):
        store.record_group(
            group_key=f"onebot:{group_id}",
            platform_id="onebot",
            group_id=group_id,
            umo=f"onebot:GroupMessage:{group_id}",
            enabled=True,
        )
    local = store.create_issue(
        {
            "title": "TTS 开启后分段失效",
            "summary": "长回复无法分段",
            "status": "resolved",
            "solution": "更新分段链路",
            "group_key": "onebot:123",
        }
    )
    remote = store.create_issue(
        {
            "title": "TTS 开启后分段失效",
            "summary": "另一个内部群的处理记录",
            "status": "resolved",
            "solution": "内部方案",
            "group_key": "onebot:456",
        }
    )

    matches = store.similar_issues(
        "TTS 分段失效",
        include_pending=False,
        group_key="onebot:123",
    )
    assert [item["id"] for item in matches] == [local["id"]]
    assert remote["id"] not in {item["id"] for item in matches}

    overview = store.overview("onebot:123")
    assert overview["total"] == 1
    assert overview["resolved"] == 1
    assert overview["recent"][0]["id"] == local["id"]


def test_manual_issue_without_group_uses_sentinel(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    issue = store.create_issue({"title": "手动记录", "summary": "来自拓展页"})
    assert issue["id"].startswith("BUG-")
    assert store.get_issue(issue["id"])["group_key"] == ""


def test_issue_metadata_round_trip_filters_bulk_overview_and_export(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    issue = store.create_issue(
        {
            "title": "SnowLuma 在线状态兼容问题",
            "kind": "bug",
            "category": "compatibility",
            "issue_domain": "plugin",
            "status": "triaged",
            "verification_status": "confirmed",
            "introduced_version": "5.10.4",
            "fixed_version": "5.10.5",
            "applicable_scope": "NapCat Desktop + SnowLuma 4.18",
            "last_confirmed_at": "2026-07-24T09:37:53+00:00",
            "solution": "避免重复调用在线状态接口。",
            "tags": ["兼容", "SnowLuma"],
        }
    )

    assert issue["category"] == "compatibility"
    assert issue["issue_domain"] == "plugin"
    assert issue["verification_status"] == "confirmed"
    assert issue["introduced_version"] == "5.10.4"
    assert issue["fixed_version"] == "5.10.5"
    assert issue["applicable_scope"] == "NapCat Desktop + SnowLuma 4.18"
    assert store.list_issues(category="compatibility")["total"] == 1
    assert store.list_issues(issue_domain="plugin")["total"] == 1
    assert store.list_issues(issue_domain="model")["total"] == 0
    assert store.list_issues(verification_status="confirmed")["total"] == 1
    assert store.list_issues(query="5.10.5")["items"][0]["id"] == issue["id"]

    updated = store.update_issue(
        issue["id"],
        {
            "verification_status": "verified",
            "category": "fixed_bug",
            "issue_domain": "astrbot",
        },
    )
    assert updated and updated["verification_status"] == "verified"
    assert updated["issue_domain"] == "astrbot"
    assert updated["last_confirmed_at"]
    assert store.bulk_update_ids(
        [issue["id"]], {"issue_domain": "model"}
    ) == [issue["id"]]
    assert store.overview()["by_category"]["fixed_bug"] == 1
    assert store.overview()["by_issue_domain"] == {"model": 1}
    assert store.overview()["by_verification_status"]["verified"] == 1
    exported = store.export_data()
    assert exported["version"] == 5
    assert exported["issues"][0]["category"] == "fixed_bug"
    assert exported["issues"][0]["issue_domain"] == "model"


def test_issue_domain_values_are_whitelisted(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    issue = store.create_issue(
        {"title": "归属值不能由外部任意扩展", "issue_domain": "PLUGIN; DROP TABLE"}
    )

    assert issue["issue_domain"] == "other"
    assert issue_change_error({"issue_domain": "provider"}) == (
        "字段 issue_domain 的值无效"
    )
    assert store.update_issue(issue["id"], {"issue_domain": "provider"}) is None
    assert store.get_issue(issue["id"])["issue_domain"] == "other"


def test_plugin_name_round_trip_exact_filter_bulk_overview_and_export(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    first = store.create_issue(
        {
            "title": "陪伴插件的问题",
            "issue_domain": "plugin",
            "plugin_name": " astrbot_plugin_private_companion ",
        }
    )
    second = store.create_issue(
        {
            "title": "另一个插件的问题",
            "issue_domain": "plugin",
            "plugin_name": "astrbot_plugin_other",
        }
    )
    non_plugin = store.create_issue(
        {
            "title": "AstrBot 核心问题",
            "issue_domain": "astrbot",
            "plugin_name": "astrbot_plugin_private_companion",
        }
    )

    assert first["plugin_name"] == "astrbot_plugin_private_companion"
    assert non_plugin["plugin_name"] == ""
    assert store.list_issues(
        plugin_name=" astrbot_plugin_private_companion "
    )["items"][0]["id"] == first["id"]
    assert store.list_issues(plugin_name="astrbot_plugin")["total"] == 0

    updated_ids = store.bulk_update_ids(
        [first["id"], non_plugin["id"]],
        {"plugin_name": " astrbot_plugin_bug_companion "},
    )
    assert updated_ids == [first["id"]]
    assert store.get_issue(first["id"])["plugin_name"] == (
        "astrbot_plugin_bug_companion"
    )
    assert store.overview()["by_plugin_name"] == {
        "astrbot_plugin_bug_companion": 1,
        "astrbot_plugin_other": 1,
    }
    exported = store.export_data()
    assert exported["version"] == 5
    exported_by_id = {item["id"]: item for item in exported["issues"]}
    assert exported_by_id[first["id"]]["plugin_name"] == (
        "astrbot_plugin_bug_companion"
    )

    changed_domain = store.update_issue(
        second["id"], {"issue_domain": "model"}
    )
    assert changed_domain and changed_domain["plugin_name"] == ""


def test_v5_migration_adds_blank_plugin_name_without_rewriting_v4_issue(
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
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                severity TEXT NOT NULL DEFAULT 'medium',
                group_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            INSERT INTO issues(
                id, kind, category, issue_domain, title, status, severity,
                group_key, created_at, updated_at, last_seen_at
            ) VALUES (
                'BUG-V4-001', 'bug', 'known_bug', 'plugin',
                '旧插件问题保持不变', 'triaged', 'high', '',
                '2026-07-01T00:00:00+00:00',
                '2026-07-02T00:00:00+00:00',
                '2026-07-03T00:00:00+00:00'
            );
            PRAGMA user_version = 4;
            """
        )

    IssueStore(tmp_path)

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT title, category, issue_domain, plugin_name FROM issues "
            "WHERE id = 'BUG-V4-001'"
        ).fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert row == ("旧插件问题保持不变", "known_bug", "plugin", "")
    assert version == 9


def test_legacy_issue_schema_migration_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "bug_companion.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE issues (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL DEFAULT 'bug',
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                severity TEXT NOT NULL DEFAULT 'medium',
                group_key TEXT NOT NULL DEFAULT '',
                solution TEXT NOT NULL DEFAULT '',
                workaround TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            INSERT INTO issues(
                id, kind, title, status, severity, group_key,
                solution, workaround, created_at, updated_at, last_seen_at
            ) VALUES (
                'BUG-OLD-001', 'question', '中文旧库常见问题', 'resolved',
                'medium', '', '使用完整 UMO', '',
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );
            """
        )

    first = IssueStore(tmp_path)
    with sqlite3.connect(first.db_path) as connection:
        row = connection.execute(
            "SELECT title, category, verification_status, issue_domain FROM issues WHERE id = 'BUG-OLD-001'"
        ).fetchone()
        assert row == ("中文旧库常见问题", "faq", "unverified", "other")
        connection.execute(
            "UPDATE issues SET category = 'compatibility', issue_domain = 'astrbot' WHERE id = 'BUG-OLD-001'"
        )
        connection.commit()

    IssueStore(tmp_path)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT title, category, issue_domain FROM issues WHERE id = 'BUG-OLD-001'"
        ).fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert row == ("中文旧库常见问题", "compatibility", "astrbot")
    assert version == 9


def test_v4_and_v5_migrations_add_metadata_without_rewriting_existing_issue(
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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );
            INSERT INTO issues(
                id, kind, category, title, status, severity, group_key,
                created_at, updated_at, last_seen_at
            ) VALUES (
                'BUG-V3-001', 'bug', 'known_bug', '原始标题保持不变',
                'triaged', 'high', '',
                '2026-07-01T00:00:00+00:00',
                '2026-07-02T00:00:00+00:00',
                '2026-07-03T00:00:00+00:00'
            );
            PRAGMA user_version = 3;
            """
        )

    IssueStore(tmp_path)

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT title, category, status, severity, issue_domain, plugin_name FROM issues "
            "WHERE id = 'BUG-V3-001'"
        ).fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert row == (
        "原始标题保持不变",
        "known_bug",
        "triaged",
        "high",
        "other",
        "",
    )
    assert version == 9


def test_legacy_pending_qq_history_requires_fresh_processing_approval(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "bug_companion.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE raw_messages (
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
                created_at TEXT NOT NULL,
                processed_at TEXT,
                process_error TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO raw_messages(
                group_key, content, source, import_run_id, created_at
            ) VALUES (
                'onebot:123', '旧版本暂存的 QQ 历史', 'qq_history',
                'qqh-old', '2026-07-20T08:00:00+00:00'
            );
            """
        )

    store = IssueStore(tmp_path)

    assert store.pending_messages("onebot:123", 10) == []
    staged = store.list_pending_messages(10)
    assert len(staged) == 1
    assert staged[0]["processing_approved"] == 0


def test_v3_migration_preserves_legacy_disabled_group_override(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    store.record_group(
        group_key="onebot:disabled",
        platform_id="onebot",
        group_id="disabled",
        umo="onebot:GroupMessage:disabled",
        enabled=False,
    )
    store.record_group(
        group_key="onebot:enabled",
        platform_id="onebot",
        group_id="enabled",
        umo="onebot:GroupMessage:enabled",
        enabled=True,
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DELETE FROM group_overrides")
        connection.execute("PRAGMA user_version = 2")
        connection.commit()

    migrated = IssueStore(tmp_path)

    assert migrated.get_group_override("onebot:disabled") is False
    assert migrated.get_group_override("onebot:enabled") is None
    with sqlite3.connect(migrated.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9

    migrated.record_group(
        group_key="onebot:disabled",
        platform_id="onebot",
        group_id="disabled",
        umo="onebot:GroupMessage:disabled",
        enabled=True,
    )
    assert migrated.is_group_enabled("onebot:disabled") is False


def test_staged_history_enters_model_queue_only_after_explicit_approval(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    store.record_group(
        group_key="onebot:123",
        platform_id="onebot",
        group_id="123",
        umo="onebot:GroupMessage:123",
        enabled=True,
    )
    raw_id = store.add_message(
        {
            "group_key": "onebot:123",
            "platform_id": "onebot",
            "group_id": "123",
            "umo": "onebot:GroupMessage:123",
            "message_id": "staged-history",
            "content": "只暂存、不自动交给模型的历史记录",
            "source": "qq_history",
            "processing_approved": False,
        }
    )

    assert store.pending_messages("onebot:123", 10) == []
    assert store.approve_messages([raw_id]) == 1
    assert [item["id"] for item in store.pending_messages("onebot:123", 10)] == [
        raw_id
    ]

    store.reset_messages([raw_id])
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE raw_messages SET processing_approved = 0 WHERE id = ?",
            (raw_id,),
        )
        connection.commit()
    duplicate_id = store.add_message(
        {
            "group_key": "onebot:123",
            "message_id": "staged-history",
            "content": "重复导入时沿用原记录",
            "source": "qq_history",
            "processing_approved": True,
        }
    )

    assert duplicate_id == raw_id
    assert [item["id"] for item in store.pending_messages("onebot:123", 10)] == [
        raw_id
    ]


def test_prepare_pending_messages_does_not_reopen_stale_processed_ids(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    store.record_group(
        group_key="onebot:123",
        platform_id="onebot",
        group_id="123",
        umo="onebot:GroupMessage:123",
        enabled=True,
    )
    raw_id = store.add_message(
        {
            "group_key": "onebot:123",
            "message_id": "already-processed",
            "content": "已经完成整理的旧页面记录",
            "source": "qq_history",
            "processing_approved": False,
        }
    )
    store.mark_messages_processed([raw_id])

    count, group_keys, matches = store.prepare_pending_messages([raw_id])

    assert (count, group_keys, matches) == (0, [], True)
    assert store.list_pending_messages(10) == []
    with sqlite3.connect(store.db_path) as connection:
        processed_at = connection.execute(
            "SELECT processed_at FROM raw_messages WHERE id = ?", (raw_id,)
        ).fetchone()[0]
    assert processed_at is not None


def test_cross_group_merge_is_rejected_without_moving_evidence(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    for group_id in ("123", "456"):
        store.record_group(
            group_key=f"onebot:{group_id}",
            platform_id="onebot",
            group_id=group_id,
            umo=f"onebot:GroupMessage:{group_id}",
            enabled=True,
        )
    raw_id = store.add_message(
        {
            "group_key": "onebot:456",
            "platform_id": "onebot",
            "group_id": "456",
            "umo": "onebot:GroupMessage:456",
            "message_id": "private-evidence",
            "content": "仅属于 456 群的证据",
        }
    )
    target = store.create_issue(
        {"title": "123 群目标问题", "group_key": "onebot:123"}
    )
    source = store.create_issue(
        {"title": "456 群来源问题", "group_key": "onebot:456"},
        evidence=[
            {
                "id": raw_id,
                "message_id": "private-evidence",
                "content": "仅属于 456 群的证据",
            }
        ],
    )

    merged = store.merge_issues([source["id"]], target["id"])

    assert merged is None
    unchanged_target = store.get_issue(target["id"])
    assert unchanged_target is not None
    assert unchanged_target["occurrence_count"] == 1
    assert unchanged_target["evidence"] == []
    unchanged_source = store.get_issue(source["id"])
    assert unchanged_source is not None
    assert unchanged_source["status"] == "pending"
    assert unchanged_source["duplicate_of"] == ""
    assert unchanged_source["evidence_count"] == 1


def test_invalid_status_filter_does_not_include_archived_issues(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    active = store.create_issue({"title": "仍在处理"})
    archived = store.create_issue({"title": "已经归档", "status": "archived"})

    result = store.list_issues(status="not-a-valid-status")
    returned_ids = {item["id"] for item in result["items"]}

    assert active["id"] in returned_ids
    assert archived["id"] not in returned_ids
    assert result["total"] == 1


def test_issue_page_is_clamped_after_result_count_shrinks(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    issues = [store.create_issue({"title": f"分页问题 {index}"}) for index in range(3)]

    second_page = store.list_issues(page=2, page_size=2)
    assert second_page["page"] == 2
    assert second_page["pages"] == 2
    assert len(second_page["items"]) == 1

    store.archive_issue(issues[0]["id"])
    store.archive_issue(issues[1]["id"])
    clamped = store.list_issues(page=2, page_size=2)

    assert clamped["page"] == 1
    assert clamped["pages"] == 1
    assert clamped["total"] == 1
    assert [item["id"] for item in clamped["items"]] == [issues[2]["id"]]


def test_issue_id_sequence_handles_four_digits_and_ignores_external_id(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    first = store.create_issue({"title": "编号占位一"})
    second = store.create_issue({"title": "编号占位二"})
    prefix = datetime.now().strftime("BUG-%Y%m%d")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE issues SET id = ? WHERE id = ?",
            (f"{prefix}-999", first["id"]),
        )
        connection.execute(
            "UPDATE issues SET id = ? WHERE id = ?",
            (f"{prefix}-1000", second["id"]),
        )
        connection.commit()

    generated = store.create_issue({"title": "四位编号后的新问题"})
    injected = store.create_issue(
        {"id": f"{prefix}-zzz", "title": "外部畸形编号不能污染序列"}
    )

    assert generated["id"] == f"{prefix}-1001"
    assert injected["id"] == f"{prefix}-1002"


def test_nested_merge_preserves_all_occurrences(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    target = store.create_issue({"title": "最终目标"})
    middle = store.create_issue({"title": "中间问题"})
    leaf = store.create_issue({"title": "叶子问题"})

    middle = store.merge_issues([leaf["id"]], middle["id"])
    assert middle is not None
    assert middle["occurrence_count"] == 2

    target = store.merge_issues([middle["id"]], target["id"])
    assert target is not None
    assert target["occurrence_count"] == 3


def test_bulk_update_with_no_valid_changes_reports_zero(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    issue = store.create_issue({"title": "不应被伪更新"})
    before = store.get_issue(issue["id"])

    updated = store.bulk_update(
        [issue["id"]],
        {"category": "invalid-category", "unknown_field": "ignored"},
    )

    after = store.get_issue(issue["id"])
    assert updated == 0
    assert before is not None and after is not None
    assert after["updated_at"] == before["updated_at"]
    assert len(after["audit"]) == len(before["audit"])


def test_confirmed_issue_creation_sets_confirmation_time(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)

    issue = store.create_issue(
        {"title": "人工确认的问题", "verification_status": "confirmed"}
    )

    assert issue["last_confirmed_at"]
    assert datetime.fromisoformat(issue["last_confirmed_at"]).tzinfo is not None


def test_resolved_time_survives_normal_edit_and_archive(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    issue = store.create_issue({"title": "已解决问题", "status": "resolved"})
    original = "2020-01-02T03:04:05+00:00"
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE issues SET resolved_at = ? WHERE id = ?", (original, issue["id"])
        )
        connection.commit()

    edited = store.update_issue(
        issue["id"], {"title": "只修改标题", "status": "resolved"}
    )
    assert edited is not None and edited["resolved_at"] == original

    archived = store.update_issue(issue["id"], {"status": "archived"})
    assert archived is not None and archived["resolved_at"] == original

    reopened = store.update_issue(issue["id"], {"status": "pending"})
    assert reopened is not None and reopened["resolved_at"] is None


def test_confirmed_update_treats_empty_confirmation_time_as_missing(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    issue = store.create_issue({"title": "等待确认"})

    confirmed = store.update_issue(
        issue["id"],
        {"verification_status": "confirmed", "last_confirmed_at": ""},
    )

    assert confirmed is not None
    assert confirmed["last_confirmed_at"]
    assert datetime.fromisoformat(confirmed["last_confirmed_at"]).tzinfo is not None


def test_duplicate_issue_cannot_be_merged_again_or_used_as_target(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    first = store.create_issue({"title": "第一条"})
    second = store.create_issue({"title": "第二条"})
    third = store.create_issue({"title": "第三条"})

    merged = store.merge_issues([first["id"]], second["id"])
    assert merged is not None and merged["occurrence_count"] == 2
    assert store.merge_issues([first["id"]], third["id"]) is None
    assert store.merge_issues([second["id"]], first["id"]) is None

    first_after = store.get_issue(first["id"])
    second_after = store.get_issue(second["id"])
    third_after = store.get_issue(third["id"])
    assert first_after is not None and first_after["duplicate_of"] == second["id"]
    assert second_after is not None and second_after["occurrence_count"] == 2
    assert third_after is not None and third_after["occurrence_count"] == 1


def test_bulk_update_rejects_invalid_fields_without_partial_mutation(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    issue = store.create_issue({"title": "保留标题", "tags": ["原标签"]})

    assert store.bulk_update([issue["id"]], {"title": "", "tags": "错误格式"}) == 0

    unchanged = store.get_issue(issue["id"])
    assert unchanged is not None
    assert unchanged["title"] == "保留标题"
    assert unchanged["tags"] == ["原标签"]


def test_export_includes_more_than_five_hundred_answer_audits(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    for index in range(501):
        store.record_answer(
            group_key="",
            user_id="u",
            user_name="用户",
            query=f"问题 {index}",
            issue_ids=[],
            answer="回答",
            confidence=0.8,
            sent=False,
        )

    assert len(store.list_answers(limit=5000)) == 500
    assert len(store.export_data()["answers"]) == 501


def test_overview_status_counts_use_non_archived_scope(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    store.create_issue({"title": "处理中"})
    store.create_issue({"title": "已归档", "status": "archived"})

    overview = store.overview()

    assert overview["total"] == 1
    assert overview["by_status"] == {"pending": 1}


def test_maintenance_compares_iso_timestamps_as_datetimes(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    raw_id = store.add_message({"group_key": "", "content": "可清理的旧消息"})
    store.mark_messages_processed([raw_id])
    expired = (datetime.now(timezone.utc) - timedelta(days=1, seconds=5)).isoformat(
        timespec="seconds"
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE raw_messages SET created_at = ? WHERE id = ?", (expired, raw_id)
        )
        connection.commit()

    assert store.maintenance(1)["raw_messages_removed"] == 1


def test_duplicate_lifecycle_survives_ordinary_and_bulk_updates(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    source = store.create_issue({"title": "重复来源"})
    target = store.create_issue({"title": "保留目标"})

    assert store.merge_issues([source["id"]], target["id"]) is not None
    assert store.update_issue(source["id"], {"status": "pending"}) is None
    assert store.bulk_update([source["id"]], {"status": "resolved"}) == 0

    edited = store.update_issue(source["id"], {"summary": "补充重复问题说明"})
    assert edited is not None
    assert edited["summary"] == "补充重复问题说明"
    assert edited["status"] == "duplicate"
    assert edited["duplicate_of"] == target["id"]


def test_merge_rejects_archived_target_without_mutation(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    source = store.create_issue({"title": "仍在处理的来源"})
    target = store.create_issue({"title": "已经归档的目标", "status": "archived"})
    source_before = store.get_issue(source["id"])
    target_before = store.get_issue(target["id"])

    assert store.merge_issues([source["id"]], target["id"]) is None

    source_after = store.get_issue(source["id"])
    target_after = store.get_issue(target["id"])
    assert source_before is not None and source_after is not None
    assert target_before is not None and target_after is not None
    assert source_after["status"] == source_before["status"] == "pending"
    assert source_after["duplicate_of"] == ""
    assert source_after["occurrence_count"] == source_before["occurrence_count"]
    assert target_after["status"] == target_before["status"] == "archived"
    assert target_after["occurrence_count"] == target_before["occurrence_count"]
    assert len(source_after["audit"]) == len(source_before["audit"])
    assert len(target_after["audit"]) == len(target_before["audit"])


def test_merge_preserves_missing_source_knowledge_with_provenance(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    target = store.create_issue(
        {
            "title": "主问题",
            "solution": "保留已有解决方案",
            "tags": ["语音"],
            "platforms": ["OneBot"],
            "versions": ["5.10.3"],
        }
    )
    first = store.create_issue(
        {
            "title": "第一条重复问题",
            "solution": "不能覆盖目标方案",
            "workaround": "临时关闭增强功能",
            "tags": ["语音", "TTS"],
            "platforms": ["NapCat"],
            "versions": ["5.10.4"],
        }
    )
    second = store.create_issue(
        {
            "title": "第二条重复问题",
            "cause": "分段顺序不正确",
            "workaround": "不能覆盖第一条来源",
            "tags": ["分段"],
            "versions": ["5.10.5"],
        }
    )

    merged = store.merge_issues([first["id"], second["id"]], target["id"])

    assert merged is not None
    assert merged["solution"] == "保留已有解决方案"
    assert merged["workaround"] == "临时关闭增强功能"
    assert merged["cause"] == "分段顺序不正确"
    assert merged["tags"] == ["语音", "TTS", "分段"]
    assert merged["platforms"] == ["OneBot", "NapCat"]
    assert merged["versions"] == ["5.10.3", "5.10.4", "5.10.5"]

    merge_audit = next(
        item for item in merged["audit"] if item["action"] == "merge_sources"
    )
    preserved = merge_audit["detail"]["preserved_fields"]
    assert preserved["workaround"] == [first["id"]]
    assert preserved["cause"] == [second["id"]]
    assert preserved["tags"] == [first["id"], second["id"]]
    assert preserved["platforms"] == [first["id"]]
    assert preserved["versions"] == [first["id"], second["id"]]
    assert "solution" not in preserved


def test_export_uses_one_sqlite_snapshot(tmp_path: Path, monkeypatch) -> None:
    exporting_store = IssueStore(tmp_path)
    writing_store = IssueStore(tmp_path)
    original = exporting_store.create_issue({"title": "导出开始前的问题"})
    original_row = exporting_store._row
    injected_issue_id = ""

    def inject_concurrent_write(row):
        nonlocal injected_issue_id
        item = original_row(row)
        if item and item.get("id") == original["id"] and not injected_issue_id:
            created = writing_store.create_issue(
                {"title": "导出期间新增的问题"},
                evidence=[{"message_id": "concurrent", "content": "并发新增证据"}],
            )
            injected_issue_id = created["id"]
        return item

    monkeypatch.setattr(exporting_store, "_row", inject_concurrent_write)

    exported = exporting_store.export_data()

    assert injected_issue_id
    assert writing_store.get_issue(injected_issue_id) is not None
    assert {item["id"] for item in exported["issues"]} == {original["id"]}
    assert all(
        item["issue_id"] != injected_issue_id for item in exported["evidence"]
    )


def test_merge_promotes_one_reusable_source_without_mixing_knowledge(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    target = store.create_issue({"title": "语音增强后分段失效"})
    unverified = store.create_issue(
        {
            "title": "未经确认的相似记录",
            "category": "fixed_bug",
            "status": "resolved",
            "verification_status": "confirmed",
            "solution": "未经验证的处理方式",
        }
    )
    reusable = store.create_issue(
        {
            "title": "已经验证的修复记录",
            "category": "fixed_bug",
            "status": "resolved",
            "verification_status": "verified",
            "solution": "升级到包含修复的版本",
            "fixed_version": "5.10.5",
            "tags": ["TTS", "分段"],
        }
    )
    resolved_at = "2026-07-20T08:00:00+00:00"
    confirmed_at = "2026-07-21T09:30:00+00:00"
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE issues SET resolved_at = ?, last_confirmed_at = ? WHERE id = ?",
            (resolved_at, confirmed_at, reusable["id"]),
        )
        connection.commit()

    merged = store.merge_issues(
        [unverified["id"], reusable["id"]], target["id"]
    )

    assert merged is not None
    assert merged["category"] == "fixed_bug"
    assert merged["status"] == "resolved"
    assert merged["verification_status"] == "verified"
    assert merged["solution"] == "升级到包含修复的版本"
    assert merged["fixed_version"] == "5.10.5"
    assert merged["resolved_at"] == resolved_at
    assert merged["last_confirmed_at"] == confirmed_at
    assert store.get_issue(reusable["id"])["status"] == "duplicate"
    assert [
        item["id"]
        for item in store.similar_issues(
            "语音增强后分段失效", include_pending=False
        )
    ] == [target["id"]]

    merge_audit = next(
        item for item in merged["audit"] if item["action"] == "merge_sources"
    )
    promoted = merge_audit["detail"]["promoted_fields"]
    assert promoted["category"] == [reusable["id"]]
    assert promoted["status"] == [reusable["id"]]
    assert promoted["verification_status"] == [reusable["id"]]
    assert promoted["resolved_at"] == [reusable["id"]]
    assert promoted["last_confirmed_at"] == [reusable["id"]]
    assert merge_audit["detail"]["preserved_fields"]["solution"] == [
        reusable["id"]
    ]


def test_merge_promotes_unknown_domain_from_one_reusable_source(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    target = store.create_issue(
        {"title": "归属尚不明确的重复问题", "issue_domain": "other"}
    )
    source = store.create_issue(
        {
            "title": "已经验证的插件修复记录",
            "issue_domain": "plugin",
            "category": "fixed_bug",
            "status": "resolved",
            "verification_status": "verified",
            "solution": "升级插件到修复版本",
        }
    )

    merged = store.merge_issues([source["id"]], target["id"])

    assert merged is not None
    assert merged["issue_domain"] == "plugin"
    assert merged["solution"] == "升级插件到修复版本"
    audit = next(item for item in merged["audit"] if item["action"] == "merge_sources")
    assert audit["detail"]["promoted_fields"]["issue_domain"] == [source["id"]]


def test_merge_promotes_unknown_plugin_name_from_one_trusted_source(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    target = store.create_issue(
        {
            "title": "具体插件尚未明确",
            "issue_domain": "plugin",
        }
    )
    source = store.create_issue(
        {
            "title": "已验证的具体插件修复",
            "issue_domain": "plugin",
            "plugin_name": "astrbot_plugin_private_companion",
            "category": "fixed_bug",
            "status": "resolved",
            "verification_status": "verified",
            "solution": "升级陪伴插件",
        }
    )

    merged = store.merge_issues([source["id"]], target["id"])

    assert merged is not None
    assert merged["plugin_name"] == "astrbot_plugin_private_companion"
    assert merged["solution"] == "升级陪伴插件"
    audit = next(item for item in merged["audit"] if item["action"] == "merge_sources")
    assert audit["detail"]["promoted_fields"]["plugin_name"] == [source["id"]]


def test_merge_rejects_trusted_knowledge_from_another_concrete_plugin(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    target = store.create_issue(
        {
            "title": "陪伴插件待确认问题",
            "issue_domain": "plugin",
            "plugin_name": "astrbot_plugin_private_companion",
        }
    )
    source = store.create_issue(
        {
            "title": "其他插件已验证问题",
            "issue_domain": "plugin",
            "plugin_name": "astrbot_plugin_other",
            "category": "fixed_bug",
            "status": "resolved",
            "verification_status": "verified",
            "solution": "升级其他插件",
        }
    )

    assert store.merge_issues([source["id"]], target["id"]) is None
    assert store.get_issue(target["id"])["plugin_name"] == (
        "astrbot_plugin_private_companion"
    )
    assert store.get_issue(target["id"])["status"] == "pending"
    assert store.get_issue(source["id"])["status"] == "resolved"


def test_merge_rejects_cross_domain_reusable_promotion_before_mutation(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    target = store.create_issue(
        {"title": "明确属于插件的待确认问题", "issue_domain": "plugin"}
    )
    source = store.create_issue(
        {
            "title": "AstrBot 核心的已验证问题",
            "issue_domain": "astrbot",
            "category": "fixed_bug",
            "status": "resolved",
            "verification_status": "verified",
            "solution": "升级 AstrBot 核心",
        }
    )

    assert store.merge_issues([source["id"]], target["id"]) is None
    assert store.get_issue(target["id"])["status"] == "pending"
    assert store.get_issue(target["id"])["issue_domain"] == "plugin"
    assert store.get_issue(source["id"])["status"] == "resolved"
    assert store.get_issue(source["id"])["duplicate_of"] == ""


def test_merge_preserves_verified_target_across_unverified_domain_conflict(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    target = store.create_issue(
        {
            "title": "插件侧已验证记录",
            "issue_domain": "plugin",
            "category": "fixed_bug",
            "status": "resolved",
            "verification_status": "verified",
            "workaround": "暂时关闭插件功能",
        }
    )
    source = store.create_issue(
        {
            "title": "归到模型侧的未经验证反馈",
            "issue_domain": "model",
            "solution": "更换模型即可永久解决",
            "cause": "模型行为变化",
        }
    )

    merged = store.merge_issues([source["id"]], target["id"])

    assert merged is not None
    assert merged["issue_domain"] == "plugin"
    assert merged["workaround"] == "暂时关闭插件功能"
    assert merged["solution"] == ""
    assert merged["cause"] == ""
    assert store.get_issue(source["id"])["status"] == "duplicate"


def test_merge_does_not_fill_verified_target_guidance_from_unverified_source(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    target = store.create_issue(
        {
            "title": "已经验证但只记录临时方案的目标",
            "category": "fixed_bug",
            "status": "resolved",
            "verification_status": "verified",
            "workaround": "暂时关闭有问题的功能",
        }
    )
    unverified = store.create_issue(
        {
            "title": "未经验证的重复反馈",
            "solution": "未经验证的永久修复方案",
            "cause": "未经验证的原因",
            "fixed_version": "9.9.9",
        }
    )

    merged = store.merge_issues([unverified["id"]], target["id"])

    assert merged is not None
    assert merged["workaround"] == "暂时关闭有问题的功能"
    assert merged["solution"] == ""
    assert merged["cause"] == ""
    assert merged["fixed_version"] == ""
    assert store.get_issue(unverified["id"])["status"] == "duplicate"
    merge_audit = next(
        item for item in merged["audit"] if item["action"] == "merge_sources"
    )
    assert not any(
        field in merge_audit["detail"].get("preserved_fields", {})
        for field in ("solution", "cause", "fixed_version")
    )


def test_merge_promotes_faq_kind_with_the_same_witness(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    target = store.create_issue({"title": "如何配置完整会话标识"})
    source = store.create_issue(
        {
            "title": "完整会话标识配置方法",
            "kind": "question",
            "category": "faq",
            "status": "resolved",
            "verification_status": "confirmed",
            "solution": "填写平台、消息类型和会话 ID。",
        }
    )

    merged = store.merge_issues([source["id"]], target["id"])

    assert merged is not None
    assert merged["kind"] == "question"
    assert merged["category"] == "faq"
    assert merged["status"] == "resolved"
    assert merged["verification_status"] == "confirmed"
    merge_audit = next(
        item for item in merged["audit"] if item["action"] == "merge_sources"
    )
    assert merge_audit["detail"]["promoted_fields"]["kind"] == [source["id"]]


def test_merge_rejects_conflicting_reusable_categories_before_mutation(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    target = store.create_issue({"title": "分类仍待确认的目标"})
    fixed = store.create_issue(
        {
            "title": "已修复问题",
            "category": "fixed_bug",
            "status": "resolved",
            "verification_status": "verified",
            "solution": "升级插件",
        }
    )
    faq = store.create_issue(
        {
            "title": "稳定配置答案",
            "kind": "question",
            "category": "faq",
            "status": "resolved",
            "verification_status": "verified",
            "solution": "修改配置项",
        }
    )

    assert store.merge_issues([fixed["id"], faq["id"]], target["id"]) is None

    assert store.get_issue(target["id"])["status"] == "pending"
    assert store.get_issue(fixed["id"])["status"] == "resolved"
    assert store.get_issue(faq["id"])["status"] == "resolved"


def test_merge_rejects_conflicting_guidance_before_mutation(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    target = store.create_issue(
        {"title": "已有待确认方案", "solution": "继续使用旧配置"}
    )
    source = store.create_issue(
        {
            "title": "已经验证的修复",
            "category": "fixed_bug",
            "status": "resolved",
            "verification_status": "verified",
            "solution": "迁移到新配置",
        }
    )

    assert store.merge_issues([source["id"]], target["id"]) is None

    target_after = store.get_issue(target["id"])
    source_after = store.get_issue(source["id"])
    assert target_after is not None and target_after["solution"] == "继续使用旧配置"
    assert target_after["status"] == "pending"
    assert source_after is not None and source_after["status"] == "resolved"
    assert source_after["duplicate_of"] == ""


def test_model_candidate_requires_review_before_knowledge_publication(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    issue = store.create_issue(
        {
            "title": "待用户批阅的票拟",
            "category": "known_bug",
            "status": "triaged",
            "verification_status": "confirmed",
            "workaround": "暂时关闭自动同步",
            "pre_review": {
                "recommendation": "approve",
                "reason": "已有两条复现证据",
                "missing_information": ["完整日志"],
                "suggested_todos": ["补充回归测试"],
            },
        },
        requires_review=True,
    )

    assert issue["reviewed_at"] is None
    assert issue["knowledge_published_at"] is None
    assert issue["pre_review_json"]["recommendation"] == "approve"
    assert store.list_review_queue()["items"][0]["id"] == issue["id"]
    assert store.list_issues(review_decision="pending")["total"] == 1

    result = store.review_decide(
        issue["id"],
        "approve",
        "准予入典",
        expected_updated_at=issue["updated_at"],
    )

    assert result["ok"] is True
    reviewed = result["issue"]
    assert reviewed["review_decision"] == "approved"
    assert reviewed["review_note"] == "准予入典"
    assert reviewed["reviewed_at"]
    assert reviewed["knowledge_published_at"]
    assert store.list_review_queue()["total"] == 0
    assert store.list_issues(review_decision="published")["total"] == 1
    assert any(item["action"] == "review_decide" for item in reviewed["audit"])


def test_review_todo_is_atomic_idempotent_and_completable(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    issue = store.create_issue(
        {"title": "需要交办的兼容问题"},
        requires_review=True,
    )
    todo = {
        "content": "在 NapCat 环境补充一次回归验证",
        "due_at": "2026-08-05T12:00:00+00:00",
    }

    first = store.review_decide(
        issue["id"],
        "todo",
        "交开发组核验",
        todo,
        issue["updated_at"],
    )
    repeated = store.review_decide(
        issue["id"],
        "todo",
        "交开发组核验",
        todo,
        issue["updated_at"],
    )

    assert first["ok"] is True and first["idempotent"] is False
    assert repeated["ok"] is True and repeated["idempotent"] is True
    assert first["todo"]["id"] == repeated["todo"]["id"]
    assert first["issue"]["knowledge_published_at"] is None
    assert store.list_issues(review_decision="todo")["total"] == 1
    open_todos = store.list_todos(status="open", issue_id=issue["id"])
    assert len(open_todos) == 1

    completed = store.update_todo(
        open_todos[0]["id"], {"status": "completed"}
    )

    assert completed is not None and completed["status"] == "completed"
    assert completed["completed_at"]
    assert completed["review_reopened"] is True
    reopened = store.get_issue(issue["id"])
    assert reopened is not None
    assert reopened["reviewed_at"] is None
    assert reopened["review_decision"] == ""
    assert store.list_review_queue()["total"] == 1
    assert store.overview()["open_todos"] == 0


def test_review_todo_after_completion_creates_a_new_open_item(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    issue = store.create_issue(
        {"title": "需要再次验证的兼容问题"},
        requires_review=True,
    )
    todo = {"content": "重新验证相同场景", "due_at": ""}
    first = store.review_decide(
        issue["id"],
        "todo",
        todo=todo,
        expected_updated_at=issue["updated_at"],
    )
    completed = store.update_todo(first["todo"]["id"], {"status": "completed"})
    reopened = store.get_issue(issue["id"])

    assert completed is not None and completed["review_reopened"] is True
    assert reopened is not None and reopened["reviewed_at"] is None

    second = store.review_decide(
        issue["id"],
        "todo",
        todo=todo,
        expected_updated_at=reopened["updated_at"],
    )

    assert second["ok"] is True
    assert second["todo"]["id"] != first["todo"]["id"]
    assert second["todo"]["status"] == "open"
    open_todos = store.list_todos(status="open", issue_id=issue["id"])
    assert [item["id"] for item in open_todos] == [second["todo"]["id"]]
    assert store.list_review_queue()["total"] == 0


def test_create_issue_publishes_only_an_approved_decision(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    payload = {
        "title": "已有完整解决办法",
        "category": "fixed_bug",
        "status": "resolved",
        "verification_status": "verified",
        "solution": "升级后重启插件",
        "knowledge_published_at": "2026-08-01T00:00:00+00:00",
    }

    unpublished = [
        store.create_issue({**payload, "review_decision": decision})
        for decision in ("todo", "deferred", "rejected")
    ]
    approved = store.create_issue({**payload, "review_decision": "approved"})

    assert all(issue["knowledge_published_at"] is None for issue in unpublished)
    assert approved["knowledge_published_at"] == payload["knowledge_published_at"]
    published = store.list_issues(review_decision="published")
    assert [issue["id"] for issue in published["items"]] == [approved["id"]]
    assert store.overview()["published_knowledge"] == 1


def test_assigned_issue_reopens_only_after_its_last_todo_is_completed(
    tmp_path: Path,
) -> None:
    store = IssueStore(tmp_path)
    issue = store.create_issue(
        {"title": "需要分两步处理的问题"},
        requires_review=True,
    )
    decision = store.review_decide(
        issue["id"],
        "todo",
        todo={"content": "先复现问题"},
        expected_updated_at=issue["updated_at"],
    )
    second = store.create_todo(issue["id"], "再验证解决办法")
    assert second is not None

    first_done = store.update_todo(
        int(decision["todo"]["id"]),
        {"status": "completed"},
    )
    still_assigned = store.get_issue(issue["id"])
    assert first_done is not None and first_done["review_reopened"] is False
    assert still_assigned is not None
    assert still_assigned["review_decision"] == "todo"
    assert store.list_review_queue()["total"] == 0

    second_done = store.update_todo(int(second["id"]), {"status": "completed"})
    reopened = store.get_issue(issue["id"])
    assert second_done is not None and second_done["review_reopened"] is True
    assert reopened is not None
    assert reopened["reviewed_at"] is None
    assert reopened["review_decision"] == ""
    assert store.list_review_queue()["total"] == 1


def test_review_rejects_stale_decision_without_partial_todo(tmp_path: Path) -> None:
    store = IssueStore(tmp_path)
    issue = store.create_issue({"title": "并发批阅"}, requires_review=True)

    result = store.review_decide(
        issue["id"],
        "todo",
        "",
        {"content": "不应写入", "due_at": ""},
        "stale-revision",
    )

    assert result["ok"] is False and result["error"] == "conflict"
    assert store.list_todos(issue_id=issue["id"]) == []
    assert store.get_issue(issue["id"])["reviewed_at"] is None
