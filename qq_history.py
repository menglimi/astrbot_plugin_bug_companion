# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


QQ_HISTORY_SOURCE = "qq_history"
_SPACE_RE = re.compile(r"[ \t\f\v]+")


class QQHistoryImportError(RuntimeError):
    """Raised when the active QQ adapter cannot provide group history."""


@dataclass(slots=True)
class HistoryFetchResult:
    messages: list[dict[str, Any]]
    pages: int
    adapter_mode: str
    since_timestamp: int
    until_timestamp: int
    truncated: bool


@dataclass(slots=True)
class HistoryImportResult:
    run_id: str
    group_key: str
    group_id: str
    source: str
    fetched: int
    stored: int
    duplicates: int
    pages: int
    adapter_mode: str
    since_timestamp: int
    until_timestamp: int
    truncated: bool
    archive_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value: Any, limit: int = 6000) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_SPACE_RE.sub(" ", line).rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    return text[:limit]


def _sender(raw: dict[str, Any]) -> tuple[str, str]:
    sender = raw.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    sender_id = str(sender.get("user_id") or raw.get("user_id") or "").strip()[:120]
    sender_name = str(
        sender.get("card")
        or sender.get("nickname")
        or raw.get("sender_name")
        or sender_id
    ).strip()[:120]
    return sender_id, sender_name


def _segment_text(segment: Any) -> str:
    if isinstance(segment, str):
        return segment
    if not isinstance(segment, dict):
        return ""
    segment_type = str(segment.get("type") or "").strip().lower()
    data = segment.get("data")
    data = data if isinstance(data, dict) else {}
    if segment_type in {"text", "plain", "markdown"}:
        return str(data.get("text") or data.get("content") or "")
    if segment_type in {"at", "mention"}:
        target = str(data.get("qq") or data.get("user_id") or data.get("name") or "").strip()
        return f"[@{target}]" if target else "[@成员]"
    if segment_type == "reply":
        reply_id = str(data.get("id") or data.get("message_id") or "").strip()
        return f"[回复:{reply_id}]" if reply_id else "[回复消息]"
    if segment_type == "image":
        name = str(data.get("summary") or data.get("file") or data.get("name") or "").strip()
        return f"[图片:{name}]" if name else "[图片]"
    if segment_type in {"record", "voice"}:
        return "[语音]"
    if segment_type == "video":
        return "[视频]"
    if segment_type == "file":
        name = str(data.get("name") or data.get("file") or "").strip()
        return f"[文件:{name}]" if name else "[文件]"
    if segment_type == "face":
        face_id = str(data.get("id") or "").strip()
        return f"[表情:{face_id}]" if face_id else "[表情]"
    if segment_type in {"json", "xml"}:
        return f"[{segment_type.upper()}消息]"
    if segment_type in {"forward", "node"}:
        return "[合并转发]"
    return f"[{segment_type}]" if segment_type else ""


def message_content(raw: dict[str, Any]) -> str:
    chain = raw.get("message")
    if isinstance(chain, list):
        content = "".join(_segment_text(segment) for segment in chain)
    elif isinstance(chain, str):
        content = chain
    else:
        content = ""
    if not content:
        content = str(raw.get("raw_message") or "")
    return _clean_text(content)


def _message_key(raw: dict[str, Any], group_id: str) -> str:
    for key in ("message_id", "message_seq", "real_id", "seq"):
        value = raw.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()[:200]
    sender_id, _ = _sender(raw)
    fingerprint = json.dumps(
        {
            "group_id": group_id,
            "time": _safe_int(raw.get("time")),
            "sender_id": sender_id,
            "message": raw.get("message") or raw.get("raw_message") or "",
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return f"synthetic-{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()[:32]}"


def _oldest_message(messages: list[dict[str, Any]]) -> dict[str, Any]:
    timed = [item for item in messages if _safe_int(item.get("time")) > 0]
    if timed:
        return min(timed, key=lambda item: _safe_int(item.get("time")))
    return messages[0]


def _history_messages(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    status = str(result.get("status") or "").strip().lower()
    retcode = _safe_int(result.get("retcode"), 0)
    if status in {"failed", "error"} or retcode != 0:
        # Adapter error text can contain request details or credentials. Keep the
        # stable numeric code for diagnosis without echoing opaque backend text.
        raise QQHistoryImportError(f"QQ 协议端拒绝读取群历史（retcode={retcode}）")
    payload = result.get("data") if isinstance(result.get("data"), dict) else result
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        return []
    return [item for item in messages if isinstance(item, dict)]


class QQGroupHistoryImporter:
    """Fetch real OneBot group history and stage it before model processing."""

    def __init__(self, store: Any, data_dir: Path, logger: Any | None = None) -> None:
        self.store = store
        self.data_dir = Path(data_dir)
        self.archive_dir = self.data_dir / "qq_history_imports"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger
        self._adapter_modes: dict[int, str] = {}

    @staticmethod
    def _action_caller(bot: Any) -> Callable[..., Awaitable[Any]]:
        caller = getattr(bot, "call_action", None)
        if callable(caller):
            return caller
        api = getattr(bot, "api", None)
        caller = getattr(api, "call_action", None)
        if callable(caller):
            return caller
        raise QQHistoryImportError("当前消息平台不支持 OneBot 群历史接口。")

    async def _detect_adapter_mode(self, bot: Any) -> str:
        cache_key = id(bot)
        if cache_key in self._adapter_modes:
            return self._adapter_modes[cache_key]
        caller = self._action_caller(bot)
        mode = "napcat"
        try:
            result = await caller("get_version_info")
            payload = result.get("data") if isinstance(result, dict) and isinstance(result.get("data"), dict) else result
            app_name = str(payload.get("app_name") or "") if isinstance(payload, dict) else ""
            if app_name.strip().lower() == "snowluma":
                mode = "snowluma"
        except Exception:
            # Version probing is optional. A single history-call fallback below
            # still handles adapters that reject get_version_info.
            pass
        self._adapter_modes[cache_key] = mode
        return mode

    async def _fetch_page(
        self,
        caller: Callable[..., Awaitable[Any]],
        *,
        group_id: str,
        count: int,
        anchor: Any,
        adapter_mode: str,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "group_id": int(group_id) if group_id.isdigit() else group_id,
            "count": count,
        }
        if adapter_mode == "snowluma":
            if anchor not in (None, ""):
                params["message_id"] = anchor
        else:
            params["reverseOrder"] = True
            if anchor not in (None, ""):
                params["message_seq"] = anchor
        return _history_messages(await caller("get_group_msg_history", **params))

    async def fetch_history(
        self,
        bot: Any,
        *,
        group_id: str,
        since_timestamp: int,
        max_count: int,
        until_timestamp: int | None = None,
    ) -> HistoryFetchResult:
        if max_count <= 0:
            raise ValueError("max_count must be positive")
        until_timestamp = int(until_timestamp or time.time())
        since_timestamp = max(0, int(since_timestamp))
        caller = self._action_caller(bot)
        adapter_mode = await self._detect_adapter_mode(bot)
        page_size = 100
        max_pages = max(3, math.ceil(max_count / page_size) + 20)
        seen_ids: set[str] = set()
        collected: list[dict[str, Any]] = []
        anchor: Any = None
        pages = 0
        reached_time_boundary = False

        while len(collected) < max_count and pages < max_pages:
            remaining = max_count - len(collected)
            fetch_count = min(
                page_size,
                remaining + (1 if anchor not in (None, "") else 0),
            )
            try:
                page = await self._fetch_page(
                    caller,
                    group_id=group_id,
                    count=fetch_count,
                    anchor=anchor,
                    adapter_mode=adapter_mode,
                )
            except Exception as first_error:
                if pages or adapter_mode == "snowluma":
                    raise QQHistoryImportError(
                        "读取 QQ 群历史失败，请检查当前 QQ 协议端是否支持 get_group_msg_history。"
                    ) from first_error
                # Some SnowLuma builds do not identify themselves through
                # get_version_info. Retry the first page once with its parameter set.
                adapter_mode = "snowluma"
                self._adapter_modes[id(bot)] = adapter_mode
                try:
                    page = await self._fetch_page(
                        caller,
                        group_id=group_id,
                        count=fetch_count,
                        anchor=None,
                        adapter_mode=adapter_mode,
                    )
                except Exception as second_error:
                    raise QQHistoryImportError(
                        "读取 QQ 群历史失败，请检查当前 QQ 协议端是否支持 get_group_msg_history。"
                    ) from second_error

            pages += 1
            if not page:
                break

            oldest = _oldest_message(page)
            oldest_time = _safe_int(oldest.get("time"))
            added_this_page = 0
            for raw in page:
                timestamp = _safe_int(raw.get("time"))
                if timestamp and not (since_timestamp <= timestamp <= until_timestamp):
                    continue
                message_id = _message_key(raw, group_id)
                if message_id in seen_ids:
                    continue
                seen_ids.add(message_id)
                collected.append(raw)
                added_this_page += 1
                if len(collected) >= max_count:
                    break

            if oldest_time and oldest_time <= since_timestamp:
                reached_time_boundary = True
                break

            new_anchor: Any
            if adapter_mode == "snowluma":
                new_anchor = oldest.get("message_id")
            else:
                new_anchor = (
                    oldest.get("message_seq")
                    or oldest.get("real_id")
                    or oldest.get("seq")
                    or oldest.get("message_id")
                )
            if new_anchor in (None, "") or str(new_anchor) == str(anchor):
                break
            if added_this_page == 0 and not (
                oldest_time and oldest_time > until_timestamp
            ):
                break
            anchor = new_anchor
            if len(collected) < max_count:
                await asyncio.sleep(0.05)

        collected.sort(key=lambda item: (_safe_int(item.get("time")), _message_key(item, group_id)))
        return HistoryFetchResult(
            messages=collected,
            pages=pages,
            adapter_mode=adapter_mode,
            since_timestamp=since_timestamp,
            until_timestamp=until_timestamp,
            truncated=len(collected) >= max_count and not reached_time_boundary,
        )

    @staticmethod
    def _normalize_message(
        raw: dict[str, Any],
        *,
        group_key: str,
        group_id: str,
        platform_id: str,
        umo: str,
        run_id: str,
        approve_processing: bool,
    ) -> dict[str, Any]:
        timestamp = _safe_int(raw.get("time"))
        created_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds") if timestamp else datetime.now(timezone.utc).isoformat(timespec="seconds")
        sender_id, sender_name = _sender(raw)
        return {
            "group_key": group_key,
            "platform_id": platform_id,
            "group_id": group_id,
            "umo": umo,
            "message_id": _message_key(raw, group_id),
            "sender_id": sender_id,
            "sender_name": sender_name,
            "content": message_content(raw),
            "created_at": created_at,
            "source": QQ_HISTORY_SOURCE,
            "import_run_id": run_id,
            "processing_approved": bool(approve_processing),
        }

    def _existing_message_ids(self, group_key: str, message_ids: list[str]) -> set[str]:
        getter = getattr(self.store, "existing_message_ids", None)
        if callable(getter):
            return {str(item) for item in getter(group_key, message_ids)}
        db_path = getattr(self.store, "db_path", None)
        if not db_path or not message_ids:
            return set()
        existing: set[str] = set()
        try:
            with sqlite3.connect(Path(db_path), timeout=15) as connection:
                for offset in range(0, len(message_ids), 500):
                    chunk = message_ids[offset : offset + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    rows = connection.execute(
                        f"SELECT message_id FROM raw_messages "
                        f"WHERE group_key = ? AND message_id IN ({placeholders})",
                        (group_key, *chunk),
                    ).fetchall()
                    existing.update(str(row[0]) for row in rows)
        except sqlite3.Error:
            return set()
        return existing

    def _write_archive(
        self,
        *,
        run_id: str,
        records: list[dict[str, Any]],
        manifest: dict[str, Any],
    ) -> Path:
        jsonl_path = self.archive_dir / f"{run_id}.jsonl"
        manifest_path = self.archive_dir / f"{run_id}.json"
        temp_jsonl = jsonl_path.with_suffix(".jsonl.tmp")
        temp_manifest = manifest_path.with_suffix(".json.tmp")
        with temp_jsonl.open("w", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        with temp_manifest.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
        temp_jsonl.replace(jsonl_path)
        temp_manifest.replace(manifest_path)
        return jsonl_path

    def prune_archives(self, raw_message_days: int) -> dict[str, int]:
        """Remove complete import batches older than the configured retention."""
        days = max(0, int(raw_message_days))
        result = {
            "qq_history_archives_removed": 0,
            "qq_history_archive_files_removed": 0,
            "qq_history_archive_bytes_removed": 0,
        }
        if days <= 0 or not self.archive_dir.is_dir():
            return result
        cutoff = time.time() - (days * 86400)
        batches: dict[str, list[Path]] = {}
        for path in self.archive_dir.iterdir():
            if (
                path.is_file()
                and path.name.startswith("qqh-")
                and path.suffix in {".json", ".jsonl"}
            ):
                batches.setdefault(path.stem, []).append(path)
        for paths in batches.values():
            try:
                latest_mtime = max(path.stat().st_mtime for path in paths)
            except OSError:
                continue
            if latest_mtime >= cutoff:
                continue
            removed_files = 0
            removed_bytes = 0
            for path in paths:
                try:
                    size = path.stat().st_size
                    path.unlink()
                except OSError:
                    continue
                removed_files += 1
                removed_bytes += max(0, int(size))
            if removed_files:
                result["qq_history_archives_removed"] += 1
                result["qq_history_archive_files_removed"] += removed_files
                result["qq_history_archive_bytes_removed"] += removed_bytes
        return result

    async def import_from_event(
        self,
        event: Any,
        *,
        days: int = 30,
        max_count: int = 5000,
        until_timestamp: int | None = None,
        approve_processing: bool = False,
    ) -> HistoryImportResult:
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception as exc:
            raise QQHistoryImportError("该命令只能在 QQ 群聊中使用。") from exc
        if not group_id:
            raise QQHistoryImportError("该命令只能在 QQ 群聊中使用。")
        bot = getattr(event, "bot", None)
        if bot is None:
            raise QQHistoryImportError("当前群聊事件没有可用的 QQ Bot 连接。")
        try:
            platform_id = str(event.get_platform_id() or "").strip()[:120]
        except Exception:
            platform_id = ""
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()[:300]
        group_key = f"{platform_id}:{group_id}" if platform_id else (umo or group_id)
        if not umo:
            umo = f"{platform_id}:GroupMessage:{group_id}" if platform_id else group_id

        days = max(1, min(int(days), 3650))
        max_count = max(1, min(int(max_count), 100000))
        until_timestamp = int(until_timestamp or time.time())
        since_timestamp = until_timestamp - (days * 86400)
        run_id = f"qqh-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        fetched = await self.fetch_history(
            bot,
            group_id=group_id,
            since_timestamp=since_timestamp,
            max_count=max_count,
            until_timestamp=until_timestamp,
        )
        normalized = [
            self._normalize_message(
                raw,
                group_key=group_key,
                group_id=group_id,
                platform_id=platform_id,
                umo=umo,
                run_id=run_id,
                approve_processing=approve_processing,
            )
            for raw in fetched.messages
        ]
        existing = self._existing_message_ids(
            group_key,
            [str(item.get("message_id") or "") for item in normalized],
        )
        new_records = [
            record
            for record in normalized
            if str(record.get("message_id") or "") not in existing
        ]
        manifest = {
            "version": 1,
            "run_id": run_id,
            "source": QQ_HISTORY_SOURCE,
            "group_key": group_key,
            "group_id": group_id,
            "platform_id": platform_id,
            "umo": umo,
            "requested_days": days,
            "requested_max_count": max_count,
            "adapter_mode": fetched.adapter_mode,
            "pages": fetched.pages,
            "since_timestamp": fetched.since_timestamp,
            "until_timestamp": fetched.until_timestamp,
            "fetched": len(normalized),
            "truncated": fetched.truncated,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "flow": (
                "approved_for_processing"
                if approve_processing
                else "staged_then_pending; model_processing_requires_explicit_request"
            ),
        }
        archive_path = self._write_archive(
            run_id=run_id,
            records=new_records,
            manifest=manifest,
        )

        atomic = getattr(self.store, "atomic", None)
        transaction = atomic() if callable(atomic) else nullcontext()
        with transaction:
            self.store.record_group(
                group_key=group_key,
                platform_id=platform_id,
                group_id=group_id,
                umo=umo,
                enabled=True,
            )
            for record in normalized:
                self.store.add_message(record)
        stored = len(new_records)
        result = HistoryImportResult(
            run_id=run_id,
            group_key=group_key,
            group_id=group_id,
            source=QQ_HISTORY_SOURCE,
            fetched=len(normalized),
            stored=max(0, stored),
            duplicates=len(existing),
            pages=fetched.pages,
            adapter_mode=fetched.adapter_mode,
            since_timestamp=fetched.since_timestamp,
            until_timestamp=fetched.until_timestamp,
            truncated=fetched.truncated,
            archive_path=str(archive_path),
        )
        manifest.update({"stored": result.stored, "duplicates": result.duplicates})
        manifest_path = self.archive_dir / f"{run_id}.json"
        with manifest_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
        return result
