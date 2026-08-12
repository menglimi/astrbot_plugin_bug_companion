# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrbot_plugin_bug_companion.qq_history import (
    QQGroupHistoryImporter,
    QQHistoryImportError,
    message_content,
)
from astrbot_plugin_bug_companion.storage import IssueStore


def _raw_message(
    message_id: str,
    timestamp: int,
    text: str,
    *,
    seq: int | None = None,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "message_seq": seq,
        "time": timestamp,
        "sender": {"user_id": "10001", "nickname": "群友", "card": "测试者"},
        "message": [{"type": "text", "data": {"text": text}}],
        "raw_message": text,
    }


class FakeBot:
    def __init__(
        self,
        *,
        app_name: str,
        pages: dict[str, list[dict[str, Any]]],
        version_error: bool = False,
        reject_napcat_once: bool = False,
    ) -> None:
        self.app_name = app_name
        self.pages = pages
        self.version_error = version_error
        self.reject_napcat_once = reject_napcat_once
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_action(self, action: str, **params: Any) -> dict[str, Any]:
        self.calls.append((action, params))
        if action == "get_version_info":
            if self.version_error:
                raise RuntimeError("unsupported action")
            return {"app_name": self.app_name}
        if self.reject_napcat_once and "reverseOrder" in params:
            self.reject_napcat_once = False
            raise RuntimeError("reverseOrder is unsupported")
        anchor = str(params.get("message_id") or params.get("message_seq") or "first")
        return {"messages": self.pages.get(anchor, [])}


class FakeStore:
    def __init__(self, existing: set[str] | None = None) -> None:
        self.existing = set(existing or set())
        self.groups: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []

    def existing_message_ids(self, group_key: str, message_ids: list[str]) -> list[str]:
        del group_key
        return [item for item in message_ids if item in self.existing]

    def record_group(self, **payload: Any) -> None:
        self.groups.append(payload)

    def add_message(self, payload: dict[str, Any]) -> int:
        if payload["message_id"] not in self.existing:
            self.messages.append(payload)
            self.existing.add(payload["message_id"])
        return len(self.messages)


class FakeEvent:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.unified_msg_origin = "aiocqhttp:GroupMessage:1097283005"

    def get_group_id(self) -> str:
        return "1097283005"

    def get_platform_id(self) -> str:
        return "aiocqhttp"


def test_napcat_history_paginates_deduplicates_and_sorts(tmp_path: Path) -> None:
    bot = FakeBot(
        app_name="NapCat",
        pages={
            "first": [
                _raw_message("m5", 500, "第五条", seq=500),
                _raw_message("m4", 400, "第四条", seq=400),
                _raw_message("m3", 300, "第三条", seq=300),
            ],
            "300": [
                _raw_message("m3", 300, "第三条", seq=300),
                _raw_message("m2", 200, "第二条", seq=200),
                _raw_message("m1", 100, "第一条", seq=100),
            ],
        },
    )
    importer = QQGroupHistoryImporter(FakeStore(), tmp_path)

    result = asyncio.run(
        importer.fetch_history(
            bot,
            group_id="1097283005",
            since_timestamp=150,
            until_timestamp=600,
            max_count=20,
        )
    )

    assert [item["message_id"] for item in result.messages] == ["m2", "m3", "m4", "m5"]
    assert result.pages == 2
    assert result.adapter_mode == "napcat"
    history_calls = [params for action, params in bot.calls if action == "get_group_msg_history"]
    assert history_calls[0]["reverseOrder"] is True
    assert history_calls[1]["message_seq"] == 300


def test_snowluma_uses_message_id_without_reverse_order(tmp_path: Path) -> None:
    bot = FakeBot(
        app_name="SnowLuma",
        pages={
            "first": [_raw_message("m3", 300, "三"), _raw_message("m2", 200, "二")],
            "m2": [_raw_message("m2", 200, "二"), _raw_message("m1", 100, "一")],
            "m1": [],
        },
    )
    importer = QQGroupHistoryImporter(FakeStore(), tmp_path)

    result = asyncio.run(
        importer.fetch_history(
            bot,
            group_id="1097283005",
            since_timestamp=50,
            until_timestamp=400,
            max_count=20,
        )
    )

    assert [item["message_id"] for item in result.messages] == ["m1", "m2", "m3"]
    calls = [params for action, params in bot.calls if action == "get_group_msg_history"]
    assert calls[0] == {"group_id": 1097283005, "count": 20}
    assert calls[1]["message_id"] == "m2"
    assert all("reverseOrder" not in params and "message_seq" not in params for params in calls)


def test_history_continues_past_pages_newer_than_requested_snapshot(tmp_path: Path) -> None:
    bot = FakeBot(
        app_name="NapCat",
        pages={
            "first": [
                _raw_message("future-2", 800, "快照之后", seq=800),
                _raw_message("future-1", 700, "快照之后", seq=700),
            ],
            "700": [
                _raw_message("future-1", 700, "快照之后", seq=700),
                _raw_message("wanted", 590, "快照范围内", seq=590),
            ],
            "590": [],
        },
    )
    importer = QQGroupHistoryImporter(FakeStore(), tmp_path)

    result = asyncio.run(
        importer.fetch_history(
            bot,
            group_id="1097283005",
            since_timestamp=500,
            until_timestamp=600,
            max_count=20,
        )
    )

    assert [item["message_id"] for item in result.messages] == ["wanted"]
    assert result.pages == 3


def test_unknown_adapter_retries_first_page_once_as_snowluma(tmp_path: Path) -> None:
    bot = FakeBot(
        app_name="",
        pages={"first": [_raw_message("m1", 50, "成功读取")]},
        version_error=True,
        reject_napcat_once=True,
    )
    importer = QQGroupHistoryImporter(FakeStore(), tmp_path)

    result = asyncio.run(
        importer.fetch_history(
            bot,
            group_id="1097283005",
            since_timestamp=50,
            until_timestamp=150,
            max_count=10,
        )
    )

    assert result.adapter_mode == "snowluma"
    calls = [params for action, params in bot.calls if action == "get_group_msg_history"]
    assert len(calls) == 2
    assert "reverseOrder" in calls[0]
    assert "reverseOrder" not in calls[1]


def test_failed_onebot_response_is_not_treated_as_empty_history(tmp_path: Path) -> None:
    class FailedResponseBot:
        async def call_action(self, action: str, **params: Any) -> dict[str, Any]:
            del params
            if action == "get_version_info":
                return {"app_name": "NapCat"}
            return {
                "status": "failed",
                "retcode": 1404,
                "message": "unsupported action",
                "data": None,
            }

    importer = QQGroupHistoryImporter(FakeStore(), tmp_path)

    with pytest.raises(QQHistoryImportError, match="读取 QQ 群历史失败"):
        asyncio.run(
            importer.fetch_history(
                FailedResponseBot(),
                group_id="1097283005",
                since_timestamp=50,
                until_timestamp=400,
                max_count=20,
            )
        )


def test_import_stages_utf8_and_marks_database_source(tmp_path: Path) -> None:
    bot = FakeBot(
        app_name="SnowLuma",
        pages={
            "first": [
                {
                    **_raw_message("m2", 199_999, "中文报错：无法切换模型"),
                    "message": [
                        {"type": "text", "data": {"text": "中文报错：无法切换模型"}},
                        {"type": "image", "data": {"file": "截图.png"}},
                    ],
                },
                _raw_message("m1", 199_998, "旧记录"),
            ],
            "m1": [],
        },
    )
    store = FakeStore(existing={"m1"})
    importer = QQGroupHistoryImporter(store, tmp_path)

    result = asyncio.run(
        importer.import_from_event(
            FakeEvent(bot),
            days=1,
            max_count=20,
            until_timestamp=200_000,
        )
    )

    assert result.fetched == 2
    assert result.stored == 1
    assert result.duplicates == 1
    assert result.source == "qq_history"
    assert len(store.messages) == 1
    assert store.messages[0]["source"] == "qq_history"
    assert store.messages[0]["import_run_id"] == result.run_id
    assert store.messages[0]["processing_approved"] is False
    assert store.messages[0]["content"] == "中文报错：无法切换模型[图片:截图.png]"
    archive = Path(result.archive_path)
    staged = [json.loads(line) for line in archive.read_text(encoding="utf-8").splitlines()]
    assert [item["message_id"] for item in staged] == ["m2"]
    assert staged[0]["source"] == "qq_history"
    assert "raw_sender" not in staged[0]
    assert "raw_message" not in staged[0]
    assert "segments" not in staged[0]
    manifest = json.loads(archive.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["flow"].startswith("staged_then_pending")
    assert manifest["stored"] == 1


def test_repeated_import_does_not_archive_duplicate_chat_content(tmp_path: Path) -> None:
    bot = FakeBot(
        app_name="SnowLuma",
        pages={
            "first": [
                _raw_message("m2", 199_999, "第二条历史记录"),
                _raw_message("m1", 199_998, "第一条历史记录"),
            ],
            "m1": [],
        },
    )
    store = FakeStore()
    importer = QQGroupHistoryImporter(store, tmp_path)

    first = asyncio.run(
        importer.import_from_event(
            FakeEvent(bot), days=1, max_count=20, until_timestamp=200_000
        )
    )
    second = asyncio.run(
        importer.import_from_event(
            FakeEvent(bot), days=1, max_count=20, until_timestamp=200_000
        )
    )

    assert first.stored == 2
    assert first.duplicates == 0
    assert second.stored == 0
    assert second.duplicates == 2
    first_archive = Path(first.archive_path).read_text(encoding="utf-8").splitlines()
    second_archive = Path(second.archive_path).read_text(encoding="utf-8").splitlines()
    assert len(first_archive) == 2
    assert second_archive == []
    assert len(store.messages) == 2


def test_prune_archives_removes_only_complete_expired_batches(tmp_path: Path) -> None:
    importer = QQGroupHistoryImporter(FakeStore(), tmp_path)
    old_files = [
        importer.archive_dir / "qqh-old.jsonl",
        importer.archive_dir / "qqh-old.json",
    ]
    new_files = [
        importer.archive_dir / "qqh-new.jsonl",
        importer.archive_dir / "qqh-new.json",
    ]
    unrelated = importer.archive_dir / "notes.json"
    for path in [*old_files, *new_files, unrelated]:
        path.write_text("保留或清理", encoding="utf-8")
    expired = time.time() - (3 * 86400)
    for path in old_files:
        os.utime(path, (expired, expired))

    result = importer.prune_archives(1)

    assert result["qq_history_archives_removed"] == 1
    assert result["qq_history_archive_files_removed"] == 2
    assert result["qq_history_archive_bytes_removed"] > 0
    assert all(not path.exists() for path in old_files)
    assert all(path.exists() for path in new_files)
    assert unrelated.exists()


def test_import_rolls_back_database_batch_when_one_insert_fails(tmp_path: Path) -> None:
    bot = FakeBot(
        app_name="SnowLuma",
        pages={
            "first": [
                _raw_message("m2", 199_999, "第二条"),
                _raw_message("m1", 199_998, "第一条"),
            ],
            "m1": [],
        },
    )
    store = IssueStore(tmp_path / "store")
    original_add = store.add_message
    calls = 0

    def failing_add(payload: dict[str, Any]) -> int:
        nonlocal calls
        calls += 1
        inserted = original_add(payload)
        if calls == 2:
            raise RuntimeError("simulated insert failure")
        return inserted

    store.add_message = failing_add  # type: ignore[method-assign]
    importer = QQGroupHistoryImporter(store, tmp_path / "imports")

    with pytest.raises(RuntimeError, match="simulated insert failure"):
        asyncio.run(
            importer.import_from_event(
                FakeEvent(bot),
                days=1,
                max_count=20,
                until_timestamp=200_000,
            )
        )

    assert store.list_pending_messages(20) == []
    assert store.list_groups() == []


def test_message_content_preserves_logs_and_marks_non_text_segments() -> None:
    content = message_content(
        {
            "message": [
                {"type": "text", "data": {"text": "Error: failed\n  at main.py:10\n"}},
                {"type": "at", "data": {"qq": "123"}},
                {"type": "file", "data": {"name": "日志.txt"}},
            ]
        }
    )
    assert content == "Error: failed\n at main.py:10\n[@123][文件:日志.txt]"


def test_non_onebot_event_is_rejected_without_reading_files(tmp_path: Path) -> None:
    importer = QQGroupHistoryImporter(FakeStore(), tmp_path)
    event = SimpleNamespace(
        bot=SimpleNamespace(),
        unified_msg_origin="telegram:GroupMessage:123",
        get_group_id=lambda: "123",
        get_platform_id=lambda: "telegram",
    )

    with pytest.raises(QQHistoryImportError, match="OneBot"):
        asyncio.run(importer.import_from_event(event, until_timestamp=200_000))
