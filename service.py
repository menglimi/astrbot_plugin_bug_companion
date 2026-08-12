# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain

from .organizer import BugOrganizer
from .storage import IssueStore


_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"""(?ix)
    (?P<prefix>
        [\"']?
        (?:
            api[_-]?key | authorization | access[_-]?token | refresh[_-]?token |
            id[_-]?token | token | client[_-]?secret | secret | password | cookie
        )
        [\"']?
        \s*[:=]\s*
    )
    (?:
        (?P<quote>[\"'])(?P<quoted>.*?)(?P=quote)
        |
        (?P<bare>[^\s,;}\]&]+)
    )
    """
)
_OPENAI_SECRET_RE = re.compile(r"\b(?:sk|rk|sess)-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_URL_CREDENTIAL_RE = re.compile(r"(?i)(?<=://)[^\s/@:]+:[^\s/@]+@")


def _clean(value: Any, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_content(value: Any, limit: int = 6000) -> str:
    """Keep stack traces and code readable while bounding stored evidence."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{4,}", "\n\n\n", text).strip()
    return text[:limit]


def _safe_error(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    text = _BEARER_TOKEN_RE.sub("Bearer <redacted>", text)
    text = _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}<redacted>",
        text,
    )
    text = _OPENAI_SECRET_RE.sub("secret-<redacted>", text)
    text = _URL_CREDENTIAL_RE.sub("<redacted>@", text)
    return _clean(text, limit)


def _int_value(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _bool_value(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return default


def _float_value(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(float(value), maximum))
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_repository(value: Any) -> str:
    """Normalize a public GitHub repository URL to a stable cursor key."""
    text = str(value or "").strip()
    if not text:
        return ""
    candidate = text if "://" in text else f"https://github.com/{text}"
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() != "github.com":
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        return ""
    owner, name = parts
    if name.lower().endswith(".git"):
        name = name[:-4]
    if not owner or not name or any(char.isspace() for char in owner + name):
        return ""
    if parsed.query or parsed.fragment:
        return ""
    return f"https://github.com/{owner}/{name}".lower()


def _repository_slug(repository: str) -> str:
    parsed = urlparse(repository)
    return "/".join(part for part in parsed.path.split("/") if part)


def _repository_target(value: Any) -> str:
    """Accept only group UMO values for proactive delivery."""
    text = str(value or "").strip()
    parts = text.split(":", 2)
    if len(parts) != 3 or parts[1] != "GroupMessage" or not parts[0] or not parts[2]:
        return ""
    return text


def _repository_settings(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("repository_updates", {})
    section = section if isinstance(section, dict) else {}
    repositories: list[str] = []
    repository_values = section.get("repositories", [])
    if not isinstance(repository_values, list):
        repository_values = []
    for value in repository_values:
        repository = _normalize_repository(value)
        if repository and repository not in repositories:
            repositories.append(repository)
    target_groups: list[str] = []
    target_values = section.get("target_groups", [])
    if not isinstance(target_values, list):
        target_values = []
    for value in target_values:
        target = _repository_target(value)
        if target and target not in target_groups:
            target_groups.append(target)
    return {
        "enabled": _bool_value(section.get("enabled", False), False),
        "repositories": repositories[:50],
        "target_groups": target_groups[:50],
        "interval_minutes": _int_value(section.get("interval_minutes"), 60, 5, 1440),
        "notify_commits": _bool_value(section.get("notify_commits", True), True),
        "notify_releases": _bool_value(section.get("notify_releases", True), True),
        "ai_summary_enabled": _bool_value(
            section.get("ai_summary_enabled", section.get("ai_summary", False)),
            False,
        ),
        "ai_summary_provider_id": _clean(
            section.get("ai_summary_provider_id"), 160
        ),
        "ai_summary_max_chars": _int_value(
            section.get("ai_summary_max_chars"), 700, 120, 1500
        ),
        "max_items_per_check": _int_value(section.get("max_items_per_check"), 5, 1, 20),
    }


class BugCompanionService:
    def __init__(self, context: Any, config: dict[str, Any], data_dir: Path) -> None:
        self.context = context
        self.config = config if config is not None else {}
        self.data_dir = Path(data_dir)
        self.store = IssueStore(self.data_dir)
        self.organizer = BugOrganizer(context, self.config)
        self._flush_tasks: dict[str, asyncio.Task[Any]] = {}
        self._group_events: dict[str, dict[int, Any]] = {}
        self._processing: set[str] = set()
        self._error_cooldown: dict[str, float] = {}
        self._retry_attempts: dict[str, int] = {}
        self._answer_last_sent: dict[str, float] = {}
        self._repository_task: asyncio.Task[Any] | None = None
        self._repository_schedule_key: tuple[Any, ...] | None = None
        self._repository_check_lock = asyncio.Lock()
        self._started = False
        self._closed = False
        self._restore_answer_cooldowns()

    def section(self, name: str) -> dict[str, Any]:
        value = self.config.get(name, {})
        return value if isinstance(value, dict) else {}

    def setting(self, section: str, key: str, default: Any) -> Any:
        value = self.section(section).get(key, default)
        return default if value is None else value

    def repository_settings(self) -> dict[str, Any]:
        """Return normalized repository notification settings for runtime use."""
        return _repository_settings(self.config)

    @staticmethod
    def _repository_schedule_signature(settings: dict[str, Any]) -> tuple[Any, ...]:
        return (
            bool(settings.get("enabled")),
            tuple(settings.get("repositories") or []),
            tuple(settings.get("target_groups") or []),
            int(settings.get("interval_minutes") or 60),
            bool(settings.get("notify_commits")),
            bool(settings.get("notify_releases")),
            bool(settings.get("ai_summary_enabled")),
            str(settings.get("ai_summary_provider_id") or ""),
            int(settings.get("ai_summary_max_chars") or 700),
            int(settings.get("max_items_per_check") or 5),
        )

    @staticmethod
    def _timestamp(value: Any) -> float:
        text = str(value or "").strip()
        if not text:
            return 0.0
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def _restore_answer_cooldowns(self) -> None:
        """Seed in-memory throttles from persistent sent-answer audits."""
        try:
            audits = self.store.list_answers(500)
        except Exception as exc:
            logger.debug("[BugCompanion] 无法恢复答疑冷却: %s", _safe_error(exc))
            return
        for audit in audits:
            if not audit.get("sent"):
                continue
            group_key = _clean(audit.get("group_key"), 300)
            if not group_key or group_key in self._answer_last_sent:
                continue
            sent_at = self._timestamp(audit.get("created_at"))
            if sent_at > 0:
                self._answer_last_sent[group_key] = sent_at

    async def start(self) -> None:
        """Resume persisted live work after AstrBot loads or reloads the plugin."""
        if self._closed:
            return
        self._started = True
        await self.refresh_scheduling()

    def _group_processing_allowed(self, group: dict[str, Any] | None) -> bool:
        if self._closed or not bool(self.setting("collection", "enabled", True)):
            return False
        if not group:
            return False
        return self._configured_target(
            group_key=_clean(group.get("group_key"), 300),
            group_id=_clean(group.get("group_id"), 200),
            platform_id=_clean(group.get("platform_id"), 120),
            umo=_clean(group.get("umo"), 300),
        )

    async def refresh_scheduling(self) -> None:
        """Cancel newly unauthorized work and resume eligible pending queues."""
        if self._closed:
            return
        groups = self.store.list_groups()
        allowed = {
            _clean(group.get("group_key"), 300): group
            for group in groups
            if self._group_processing_allowed(group)
        }
        cancelled: list[asyncio.Task[Any]] = []
        current_task = asyncio.current_task()
        for group_key, task in list(self._flush_tasks.items()):
            if task.done():
                self._flush_tasks.pop(group_key, None)
                continue
            if group_key in allowed:
                continue
            if task is current_task:
                continue
            task.cancel()
            cancelled.append(task)
            self._flush_tasks.pop(group_key, None)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

        scheduled = 0
        for group_key, group in allowed.items():
            if (
                not group_key
                or not self.store.pending_messages(group_key, limit=1)
            ):
                continue
            current = self._flush_tasks.get(group_key)
            if isinstance(current, asyncio.Task) and not current.done():
                continue
            self._flush_tasks[group_key] = asyncio.create_task(
                self._debounced_flush(group_key, 1),
                name=f"bug_companion_resume_{re.sub(r'[^a-zA-Z0-9]+', '_', group_key)[:36]}",
            )
            scheduled += 1
        if scheduled:
            logger.info("[BugCompanion] 已恢复 %s 个群聊的待整理队列", scheduled)
        await self.refresh_repository_schedule()

    async def refresh_repository_schedule(self) -> None:
        """Start, stop, or restart the repository watcher after config changes."""
        if self._closed:
            return
        settings = self.repository_settings()
        signature = self._repository_schedule_signature(settings)
        current = self._repository_task
        if isinstance(current, asyncio.Task) and current.done():
            self._repository_task = None
            current = None
        if current is not None and signature == self._repository_schedule_key:
            return
        if current is not None and current is not asyncio.current_task():
            current.cancel()
            await asyncio.gather(current, return_exceptions=True)
        self._repository_task = None
        self._repository_schedule_key = signature
        if not (
            settings["enabled"]
            and settings["repositories"]
            and settings["target_groups"]
        ):
            return
        self._repository_task = asyncio.create_task(
            self._repository_loop(signature),
            name="bug_companion_repository_updates",
        )
        logger.info(
            "[BugCompanion] 已启用仓库动态播报：%s 个仓库，每 %s 分钟检查",
            len(settings["repositories"]),
            settings["interval_minutes"],
        )

    async def _repository_loop(self, signature: tuple[Any, ...]) -> None:
        """Run an isolated watcher; network errors must not affect chat workers."""
        try:
            while not self._closed:
                if self._repository_schedule_key != signature:
                    return
                try:
                    await self.check_repository_updates()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("[BugCompanion] 仓库动态检查失败: %s", _safe_error(exc))
                settings = self.repository_settings()
                await asyncio.sleep(max(300, int(settings["interval_minutes"]) * 60))
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _fetch_json_sync(url: str) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "astrbot-plugin-bug-companion",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if github_token:
            headers["Authorization"] = f"Bearer {github_token}"
        request = Request(
            url,
            headers=headers,
        )
        try:
            with urlopen(request, timeout=15) as response:
                payload = response.read(2_000_000)
        except HTTPError as exc:
            raise RuntimeError(f"GitHub API HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"网络请求失败: {_safe_error(exc.reason)}") from exc
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("GitHub API 返回了无法解析的数据") from exc

    async def _fetch_repository_updates(
        self,
        repository: str,
        max_items: int,
    ) -> dict[str, Any]:
        slug = _repository_slug(repository)
        owner, name = (slug.split("/", 1) + [""])[:2]
        if not owner or not name:
            return {
                "commits": [],
                "releases": [],
                "commit_ok": False,
                "release_ok": False,
                "errors": ["仓库地址格式无效"],
            }
        per_page = min(100, max(20, max_items * 4))
        encoded_owner = quote(owner, safe="")
        encoded_name = quote(name, safe="")
        base = f"https://api.github.com/repos/{encoded_owner}/{encoded_name}"
        urls = (
            f"{base}/commits?per_page={per_page}",
            f"{base}/releases?per_page={per_page}",
        )
        results = await asyncio.gather(
            *(asyncio.to_thread(self._fetch_json_sync, url) for url in urls),
            return_exceptions=True,
        )
        errors: list[str] = []
        commits: list[dict[str, Any]] = []
        releases: list[dict[str, Any]] = []
        commit_ok = isinstance(results[0], list)
        release_ok = isinstance(results[1], list)
        if commit_ok:
            commits = [item for item in results[0] if isinstance(item, dict)]
        else:
            errors.append(f"提交读取失败: {_safe_error(results[0])}")
        if release_ok:
            releases = [item for item in results[1] if isinstance(item, dict)]
        else:
            errors.append(f"版本读取失败: {_safe_error(results[1])}")
        return {
            "commits": commits,
            "releases": releases,
            "commit_ok": commit_ok,
            "release_ok": release_ok,
            "errors": errors,
        }

    @staticmethod
    def _repository_commit_id(item: dict[str, Any]) -> str:
        return _clean(item.get("sha"), 120)

    @staticmethod
    def _repository_release_id(item: dict[str, Any]) -> str:
        value = item.get("id") or item.get("tag_name") or item.get("name")
        return _clean(value, 160)

    @classmethod
    def _new_repository_items(
        cls,
        items: list[dict[str, Any]],
        cursor: str,
        identity,
    ) -> list[dict[str, Any]]:
        if not items:
            return []
        if not cursor:
            return items
        fresh: list[dict[str, Any]] = []
        for item in items:
            if identity(item) == cursor:
                break
            fresh.append(item)
        return fresh

    @classmethod
    def _format_repository_update(
        cls,
        repository: str,
        commits: list[dict[str, Any]],
        releases: list[dict[str, Any]],
        max_items: int,
        summary: str = "",
    ) -> str:
        slug = _repository_slug(repository)
        lines = [f"仓库更新 · {slug}"]
        clean_summary = _clean_content(summary, 1500)
        if clean_summary:
            lines.extend(["AI 摘要：", clean_summary])
        if releases:
            lines.append("版本发布：")
            for item in releases[:max_items]:
                tag = _clean(item.get("tag_name") or item.get("name") or "未命名版本", 160)
                url = _clean(item.get("html_url"), 500)
                lines.append(f"- {tag}" + (f"  {url}" if url else ""))
            if len(releases) > max_items:
                lines.append(f"（另有 {len(releases) - max_items} 个版本更新未展开）")
        if commits:
            lines.append("最新提交：")
            for item in commits[:max_items]:
                sha = cls._repository_commit_id(item)
                short_sha = sha[:7] if sha else "未知提交"
                commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
                message = _clean(str(commit.get("message") or "").splitlines()[0], 220)
                url = _clean(item.get("html_url"), 500)
                detail = f"{short_sha} {message}".strip()
                lines.append(f"- {detail}" + (f"  {url}" if url else ""))
            if len(commits) > max_items:
                lines.append(f"（另有 {len(commits) - max_items} 条提交未展开）")
        lines.append(f"查看：{repository}")
        return "\n".join(lines)

    async def _send_repository_message(self, target: str, text: str) -> bool:
        try:
            sender = getattr(self.context, "send_message", None)
            if not callable(sender):
                return False
            result = sender(target, MessageChain().message(text).use_markdown(False))
            if hasattr(result, "__await__"):
                result = await result
            return result is not False
        except Exception as exc:
            logger.warning("[BugCompanion] 仓库动态发送失败 target=%s: %s", target, _safe_error(exc))
            return False

    @staticmethod
    def _compose_repository_message(notifications: list[str], limit: int = 12000) -> str:
        header = "仓库动态通知"
        selected: list[str] = []
        current_length = len(header)
        for notification in notifications:
            addition = len(notification) + 2
            if current_length + addition <= limit:
                selected.append(notification)
                current_length += addition
                continue
            if not selected:
                clipped = notification[: max(200, limit - len(header) - 80)]
                selected.append(clipped.rsplit("\n", 1)[0] + "\n（内容较多，已截断，请打开仓库查看）")
            break
        omitted = max(0, len(notifications) - len(selected))
        message = header + "\n\n" + "\n\n".join(selected)
        if omitted:
            message += f"\n\n另有 {omitted} 个仓库包含更新，请打开对应仓库查看。"
        return message

    async def check_repository_updates(self, *, force: bool = False) -> dict[str, Any]:
        """Fetch configured repositories and proactively notify fixed groups."""
        settings = self.repository_settings()
        if self._closed:
            return {"status": "skipped", "reason": "closed", "checked": 0}
        if not force and not settings["enabled"]:
            return {"status": "skipped", "reason": "disabled", "checked": 0}
        if not settings["repositories"] or not settings["target_groups"]:
            return {"status": "skipped", "reason": "missing_config", "checked": 0}
        async with self._repository_check_lock:
            notifications: list[str] = []
            errors: list[str] = []
            checked = 0
            baselined = 0
            updated = 0
            for repository in settings["repositories"]:
                checked += 1
                fetched = await self._fetch_repository_updates(
                    repository,
                    settings["max_items_per_check"],
                )
                errors.extend(f"{repository}: {error}" for error in fetched["errors"])
                cursor = self.store.get_repository_cursor(repository)
                commits = fetched["commits"]
                releases = fetched["releases"]
                commit_id = self._repository_commit_id(commits[0]) if commits else ""
                release_id = self._repository_release_id(releases[0]) if releases else ""
                if cursor is None:
                    self.store.set_repository_cursor(
                        repository,
                        latest_commit=commit_id if fetched["commit_ok"] else None,
                        latest_release=release_id if fetched["release_ok"] else None,
                        initialized=fetched["commit_ok"] and fetched["release_ok"],
                    )
                    if fetched["commit_ok"] and fetched["release_ok"]:
                        baselined += 1
                    continue
                if not cursor.get("initialized"):
                    self.store.set_repository_cursor(
                        repository,
                        latest_commit=commit_id if fetched["commit_ok"] else None,
                        latest_release=release_id if fetched["release_ok"] else None,
                        initialized=fetched["commit_ok"] and fetched["release_ok"],
                    )
                    if fetched["commit_ok"] and fetched["release_ok"]:
                        baselined += 1
                    continue

                new_commits = self._new_repository_items(
                    commits,
                    _clean(cursor.get("latest_commit"), 120),
                    self._repository_commit_id,
                ) if fetched["commit_ok"] else []
                new_releases = self._new_repository_items(
                    releases,
                    _clean(cursor.get("latest_release"), 160),
                    self._repository_release_id,
                ) if fetched["release_ok"] else []
                if fetched["commit_ok"]:
                    self.store.set_repository_cursor(repository, latest_commit=commit_id or None)
                if fetched["release_ok"]:
                    self.store.set_repository_cursor(repository, latest_release=release_id or None)
                visible_commits = new_commits if settings["notify_commits"] else []
                visible_releases = new_releases if settings["notify_releases"] else []
                if visible_commits or visible_releases:
                    summary = ""
                    if settings["ai_summary_enabled"]:
                        try:
                            summary = await self.organizer.summarize_repository_updates(
                                repository,
                                visible_commits,
                                visible_releases,
                                provider_id=settings["ai_summary_provider_id"],
                                max_chars=settings["ai_summary_max_chars"],
                            )
                        except Exception as exc:
                            logger.warning(
                                "[BugCompanion] 仓库动态 AI 摘要失败 repository=%s: %s",
                                repository,
                                _safe_error(exc),
                            )
                    updated += 1
                    notifications.append(
                        self._format_repository_update(
                            repository,
                            visible_commits,
                            visible_releases,
                            settings["max_items_per_check"],
                            summary,
                        )
                    )
            sent = 0
            if notifications:
                text = self._compose_repository_message(notifications)
                for target in settings["target_groups"]:
                    if await self._send_repository_message(target, text):
                        sent += 1
            if errors:
                logger.warning("[BugCompanion] 仓库动态检查有失败：%s", "；".join(errors[:8]))
            return {
                "status": "ok",
                "checked": checked,
                "baselined": baselined,
                "updated": updated,
                "sent": sent,
                "errors": errors[:20],
            }

    def _remember_group_event(self, group_key: str, raw_id: int, event: Any) -> None:
        self._group_events.setdefault(group_key, {})[int(raw_id)] = event

    def _discard_group_events(self, group_key: str, raw_ids: list[int]) -> None:
        events = self._group_events.get(group_key)
        if not events:
            return
        for raw_id in raw_ids:
            events.pop(int(raw_id), None)
        if not events:
            self._group_events.pop(group_key, None)

    @staticmethod
    def _query_matches_content(query: str, content: Any) -> bool:
        query_text = re.sub(r"\s+", "", str(query or "")).lower()
        content_text = re.sub(r"\s+", "", str(content or "")).lower()
        if not query_text or not content_text:
            return False
        if query_text == content_text:
            return True
        return min(len(query_text), len(content_text)) >= 4 and (
            query_text in content_text or content_text in query_text
        )

    def _answer_event_for_batch(
        self,
        group_key: str,
        result: dict[str, Any],
        selected: list[dict[str, Any]],
        query: str,
    ) -> Any | None:
        """Resolve the live event that supplied the answer query in this batch."""
        events = self._group_events.get(group_key) or {}
        if not events:
            return None
        selected_by_id = {int(item["id"]): item for item in selected}
        explicit_ids: list[int] = []
        explicit_values = result.get("answer_evidence_ids")
        if not isinstance(explicit_values, list):
            explicit_values = [result.get("answer_evidence_id")]
        for value in explicit_values:
            try:
                evidence_id = int(value)
            except (TypeError, ValueError):
                continue
            if (
                evidence_id in selected_by_id
                and evidence_id in events
                and evidence_id not in explicit_ids
            ):
                explicit_ids.append(evidence_id)
        if len(explicit_ids) == 1:
            return events[explicit_ids[0]]
        if len(explicit_ids) > 1:
            matching_explicit = [
                evidence_id
                for evidence_id in explicit_ids
                if self._query_matches_content(
                    query,
                    selected_by_id[evidence_id].get("content"),
                )
            ]
            return events[matching_explicit[0]] if len(matching_explicit) == 1 else None

        candidate_ids: list[int] = []
        for candidate in result.get("candidates", []):
            if not isinstance(candidate, dict) or not candidate.get("needs_answer"):
                continue
            candidate_query = _clean(candidate.get("answer_query"), 4000)
            if candidate_query and not (
                self._query_matches_content(query, candidate_query)
                or self._query_matches_content(candidate_query, query)
            ):
                continue
            for value in candidate.get("evidence_ids", []):
                try:
                    evidence_id = int(value)
                except (TypeError, ValueError):
                    continue
                if evidence_id not in candidate_ids:
                    candidate_ids.append(evidence_id)
        matching_candidate_ids = [
            evidence_id
            for evidence_id in candidate_ids
            if evidence_id in selected_by_id
            and evidence_id in events
            and self._query_matches_content(
                query,
                selected_by_id[evidence_id].get("content"),
            )
        ]
        if len(matching_candidate_ids) == 1:
            return events[matching_candidate_ids[0]]
        if len(matching_candidate_ids) > 1:
            return None

        matching_batch_ids = [
            int(message["id"])
            for message in selected
            if int(message["id"]) in events
            and self._query_matches_content(query, message.get("content"))
        ]
        return events[matching_batch_ids[0]] if len(matching_batch_ids) == 1 else None

    def _automatic_delivery_block_reason(self, event: Any, expected_group_key: str) -> str:
        if self._closed:
            return "插件已停止，取消自动答疑"
        if not bool(self.setting("collection", "enabled", True)):
            return "问题收集已关闭，取消自动答疑"
        if not bool(self.setting("answers", "auto_answer", True)):
            return "自动答疑已关闭"
        group_key, group_id, platform_id, umo, _ = self._event_group(event)
        if group_key != expected_group_key:
            return "答疑事件与整理群不一致"
        if not group_id or not self._configured_target(
            group_key=group_key,
            group_id=group_id,
            platform_id=platform_id,
            umo=umo,
        ):
            return "群聊已停用，取消自动答疑"
        return ""

    @staticmethod
    def _answer_reference_allowed(issue: dict[str, Any]) -> bool:
        """Keep only records whose category and verification support reuse."""
        if (
            str(issue.get("review_decision") or "") != "approved"
            or not issue.get("knowledge_published_at")
        ):
            return False
        category = str(issue.get("category") or "pending_feedback")
        verification = str(issue.get("verification_status") or "unverified")
        status = str(issue.get("status") or "pending")
        has_guidance = bool(issue.get("solution") or issue.get("workaround"))
        if not has_guidance:
            return False
        if category == "fixed_bug":
            return status == "resolved" and verification == "verified"
        if category == "faq":
            return status == "resolved" and verification in {"confirmed", "verified"}
        if category in {"known_bug", "compatibility"}:
            return status in {"triaged", "in_progress", "resolved", "wont_fix"} and verification in {
                "confirmed",
                "fix_pending",
                "verified",
            }
        return False

    @staticmethod
    def _event_group(event: Any) -> tuple[str, str, str, str, str]:
        try:
            group_id = _clean(event.get_group_id(), 200)
        except Exception:
            group_id = ""
        try:
            platform_id = _clean(event.get_platform_id(), 120)
        except Exception:
            platform_id = ""
        umo = _clean(getattr(event, "unified_msg_origin", ""), 300)
        group_key = f"{platform_id}:{group_id}" if platform_id and group_id else (umo or group_id)
        try:
            sender_id = _clean(event.get_sender_id(), 120)
        except Exception:
            sender_id = ""
        try:
            sender_name = _clean(event.get_sender_name(), 120) or sender_id
        except Exception:
            sender_name = sender_id
        return group_key, group_id, platform_id, umo, sender_id or sender_name

    @staticmethod
    def _event_message_id(event: Any) -> str:
        message_obj = getattr(event, "message_obj", None)
        return _clean(getattr(message_obj, "message_id", ""), 200)

    @staticmethod
    def _event_content(event: Any) -> str:
        text = _clean_content(getattr(event, "message_str", ""), 6000)
        if text:
            return text
        try:
            messages = event.get_messages()
        except Exception:
            messages = []
        parts: list[str] = []
        for component in messages or []:
            component_type = str(getattr(component, "type", ""))
            if component_type.lower() in {"plain", "text"}:
                parts.append(_clean_content(getattr(component, "text", ""), 2000))
            elif component_type.lower() in {"image", "imageurl"}:
                parts.append("[图片]")
            elif component_type.lower() in {"at", "mention"}:
                parts.append(f"[@{_clean(getattr(component, 'name', ''), 80)}]")
        return _clean_content("\n".join(parts), 6000)

    def _configured_target(self, *, group_key: str, group_id: str, platform_id: str, umo: str) -> bool:
        override = self.store.get_group_override(group_key)
        if override is not None:
            return override
        configured = self.setting("collection", "target_groups", [])
        if not isinstance(configured, list):
            configured = []
        candidates = {item.strip().lower() for item in (group_key, group_id, umo) if item}
        for item in configured:
            text = _clean(item, 300).lower()
            if not text:
                continue
            if text in candidates:
                return True
            # Only a bare numeric/string group ID may match across platforms.
            # A complete UMO or platform-prefixed key is always exact.
            if group_id and ":" not in text and text == group_id.lower():
                return True
        # A persisted group row records discovery and UI state. It is not by itself
        # an authorization grant: otherwise a group removed from target_groups could
        # keep being collected simply because it was seen in an older configuration.
        # Explicit page/command choices are handled above through group_overrides.
        return False

    def _is_self_sender(self, event: Any, sender_id: str) -> bool:
        try:
            self_id = _clean(event.get_self_id(), 120)
        except Exception:
            self_id = ""
        ignored = self.setting("collection", "ignored_sender_ids", [])
        ignored_values = {str(item).strip() for item in ignored} if isinstance(ignored, list) else set()
        return bool(sender_id and (sender_id == self_id or sender_id in ignored_values))

    async def handle_group_message(self, event: Any) -> None:
        if self._closed or not bool(self.setting("collection", "enabled", True)):
            return
        try:
            group_id = _clean(event.get_group_id(), 200)
        except Exception:
            group_id = ""
        if not group_id:
            return
        group_key, group_id, platform_id, umo, sender_hint = self._event_group(event)
        try:
            sender_id = _clean(event.get_sender_id(), 120)
        except Exception:
            sender_id = sender_hint
        try:
            sender_name = _clean(event.get_sender_name(), 120) or sender_id
        except Exception:
            sender_name = sender_id
        configured_target = self._configured_target(
            group_key=group_key,
            group_id=group_id,
            platform_id=platform_id,
            umo=umo,
        )
        if not configured_target:
            return
        # Do not retain identifiers for unrelated groups merely because the
        # platform delivered their events to AstrBot.
        self.store.record_group(
            group_key=group_key,
            platform_id=platform_id,
            group_id=group_id,
            umo=umo,
            display_name="",
            enabled=True,
        )
        if self._is_self_sender(event, sender_id):
            return
        content = self._event_content(event)
        if not content:
            return
        # 管理命令由命令处理器单独处理，避免把命令本身当作问题证据。
        if re.match(r"^\s*/?(?:修bug|修Bug|bug)(?:\s|$)", content):
            return
        raw_id = self.store.add_message(
            {
                "group_key": group_key,
                "platform_id": platform_id,
                "group_id": group_id,
                "umo": umo,
                "message_id": self._event_message_id(event),
                "sender_id": sender_id,
                "sender_name": sender_name,
                "content": content,
                "created_at": _now_iso(),
            }
        )
        self._remember_group_event(group_key, raw_id, event)
        self._retry_attempts.pop(group_key, None)
        task = self._flush_tasks.get(group_key)
        if isinstance(task, asyncio.Task) and not task.done():
            if group_key in self._processing:
                # The active worker sees this newly stored message and schedules a
                # follow-up batch. Cancelling it here would abort an in-flight model call.
                return
            task.cancel()
        quiet = _int_value(self.setting("collection", "quiet_seconds", 12), 12, 2, 180)
        cooldown_until = self._error_cooldown.get(group_key, 0.0)
        if cooldown_until > time.time():
            quiet = max(quiet, int(cooldown_until - time.time()) + 1)
        pending = self.store.pending_messages(group_key, limit=200)
        if len(pending) >= max(5, _int_value(self.setting("collection", "batch_max_messages", 40), 40, 1, 100)):
            quiet = 1
        self._flush_tasks[group_key] = asyncio.create_task(
            self._debounced_flush(group_key, quiet),
            name=f"bug_companion_flush_{re.sub(r'[^a-zA-Z0-9]+', '_', group_key)[:40]}",
        )
        logger.debug("[BugCompanion] 收集群消息 raw_id=%s group=%s", raw_id, group_key)

    async def _debounced_flush(self, group_key: str, quiet_seconds: int) -> None:
        try:
            await asyncio.sleep(quiet_seconds)
            result = await self.process_group(group_key)
            error = result.get("error")
            if error in {"cooldown", "processing"} and not self._closed:
                remaining = 1
                if error == "cooldown":
                    remaining = max(
                        1,
                        int(self._error_cooldown.get(group_key, time.time()) - time.time()) + 1,
                    )
                if self._flush_tasks.get(group_key) is asyncio.current_task():
                    self._flush_tasks[group_key] = asyncio.create_task(
                        self._debounced_flush(group_key, remaining),
                        name=f"bug_companion_retry_wait_{re.sub(r'[^a-zA-Z0-9]+', '_', group_key)[:36]}",
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[BugCompanion] 整理群 %s 失败: %s", group_key, _safe_error(exc))
        finally:
            current = self._flush_tasks.get(group_key)
            if current is asyncio.current_task():
                self._flush_tasks.pop(group_key, None)

    async def process_group(self, group_key: str, *, force: bool = False) -> dict[str, Any]:
        if self._closed:
            return {"ok": False, "error": "closed"}
        if not self._group_processing_allowed(self.store.get_group(group_key)):
            return {"ok": False, "error": "scope_disabled"}
        if group_key in self._processing:
            return {"ok": False, "error": "processing"}
        if not force and time.time() < self._error_cooldown.get(group_key, 0):
            return {"ok": False, "error": "cooldown"}
        self._processing.add(group_key)
        attempted_ids: list[int] = []
        try:
            max_messages = _int_value(self.setting("collection", "batch_max_messages", 40), 40, 1, 100)
            messages = self.store.pending_messages(group_key, limit=max_messages)
            if not messages:
                return {"ok": True, "created": 0, "messages": 0}
            max_chars = _int_value(self.setting("collection", "batch_max_chars", 10000), 10000, 1000, 50000)
            selected: list[dict[str, Any]] = []
            total_chars = 0
            for message in messages:
                content_length = len(str(message.get("content") or ""))
                if selected and total_chars + content_length > max_chars:
                    break
                selected.append(message)
                total_chars += content_length
            attempted_ids = [int(item["id"]) for item in selected]
            result = await self.organizer.analyze_batch(
                selected,
                umo=str(selected[-1].get("umo") or ""),
            )
            if self._closed:
                return {"ok": False, "error": "closed", "messages": len(attempted_ids)}
            by_id = {int(message["id"]): message for message in selected}
            created: list[dict[str, Any]] = []
            # Candidate persistence and raw-message acknowledgement are one
            # transaction. A later candidate failure cannot leave an earlier
            # issue committed while the same evidence remains pending for retry.
            with self.store.atomic():
                for candidate in result.get("candidates", []):
                    evidence = [by_id[item_id] for item_id in candidate.get("evidence_ids", []) if item_id in by_id]
                    if not evidence:
                        continue
                    reporter = evidence[0]
                    payload = {
                        **candidate,
                        "group_key": group_key,
                        "reporter_id": reporter.get("sender_id", ""),
                        "reporter_name": reporter.get("sender_name", ""),
                        "source": "model",
                    }
                    payload.pop("evidence_ids", None)
                    payload.pop("needs_answer", None)
                    payload.pop("answer_query", None)
                    issue = self.store.create_issue(
                        payload,
                        evidence=evidence,
                        actor="model",
                        requires_review=True,
                    )
                    similar = self.store.similar_issues(
                        issue,
                        limit=8,
                        include_pending=True,
                        exclude_id=issue.get("id", ""),
                        group_key=group_key,
                    )
                    self.store.save_similarity_links(issue.get("id", ""), similar[:5])
                    issue["similar"] = similar[:5]
                    created.append(issue)
                selected_ids = attempted_ids
                if not created and not bool(self.setting("retention", "store_non_candidate_context", True)):
                    self.store.delete_messages(selected_ids)
                    # Evidence-linked rows are intentionally retained by delete_messages.
                    # Acknowledge those survivors so they do not re-enter the model
                    # queue forever when a manual reprocess yields no new candidate.
                    self.store.mark_messages_processed(selected_ids)
                else:
                    self.store.mark_messages_processed(selected_ids)
            answer_result = {"sent": False, "reason": ""}
            query = _clean(result.get("answer_query"), 4000)
            if not query:
                for candidate in result.get("candidates", []):
                    if candidate.get("needs_answer") and candidate.get("answer_query"):
                        query = _clean(candidate.get("answer_query"), 4000)
                        break
            if result.get("needs_answer") and query and bool(self.setting("answers", "auto_answer", True)):
                event = self._answer_event_for_batch(group_key, result, selected, query)
                # AstrBot will already run its normal model reply for an @ or
                # wake-prefix message. The evidence worker runs later, so it
                # cannot safely replace that reply and must not add a second one.
                if event is not None and not bool(getattr(event, "is_at_or_wake_command", False)):
                    answer_result = await self.answer_query(event, query, automatic=True)
                elif event is not None:
                    answer_result = {"sent": False, "reason": "handled_by_default_llm"}
                else:
                    answer_result = {"sent": False, "reason": "answer_event_unbound"}
            # A busy group can exceed one model batch while no new message arrives.
            # Continue with the remaining evidence instead of leaving it pending until
            # somebody speaks again.
            if not self._closed and self.store.pending_messages(group_key, limit=1):
                current = self._flush_tasks.get(group_key)
                if current is None or current is asyncio.current_task() or current.done():
                    self._flush_tasks[group_key] = asyncio.create_task(
                        self._debounced_flush(group_key, 1),
                        name=f"bug_companion_follow_up_{re.sub(r'[^a-zA-Z0-9]+', '_', group_key)[:36]}",
                    )
            self._retry_attempts.pop(group_key, None)
            self._error_cooldown.pop(group_key, None)
            self._discard_group_events(group_key, attempted_ids)
            return {"ok": True, "created": len(created), "messages": len(selected), "issues": created, "answer": answer_result}
        except Exception as exc:
            error = _safe_error(exc)
            if attempted_ids:
                self.store.mark_messages_error(attempted_ids, error)
            self._error_cooldown[group_key] = time.time() + 60
            logger.warning("[BugCompanion] 模型整理失败，保留待处理证据 group=%s: %s", group_key, error)
            attempts = self._retry_attempts.get(group_key, 0)
            if not self._closed and attempted_ids and attempts < 1 and self.store.pending_messages(group_key, limit=1):
                self._retry_attempts[group_key] = attempts + 1
                current = self._flush_tasks.get(group_key)
                if current is None or current is asyncio.current_task() or current.done():
                    self._flush_tasks[group_key] = asyncio.create_task(
                        self._debounced_flush(group_key, 61),
                        name=f"bug_companion_retry_{re.sub(r'[^a-zA-Z0-9]+', '_', group_key)[:40]}",
                    )
            else:
                self._group_events.pop(group_key, None)
            return {"ok": False, "error": error, "messages": len(attempted_ids)}
        finally:
            self._processing.discard(group_key)
            if not self.store.pending_messages(group_key, limit=1):
                self._group_events.pop(group_key, None)

    async def answer_query(
        self,
        event: Any,
        query: str,
        *,
        automatic: bool = False,
        command_result: bool = False,
    ) -> dict[str, Any]:
        query = _clean(query, 4000)
        group_key, group_id, platform_id, umo, sender_hint = self._event_group(event)
        if self._closed:
            return {
                "sent": False,
                "answer": "",
                "confidence": 0.0,
                "references": [],
                "reason": "service_closed",
            }
        if automatic and not bool(self.setting("answers", "auto_answer", True)):
            return {
                "sent": False,
                "answer": "",
                "confidence": 0.0,
                "references": [],
                "reason": "auto_answer_disabled",
            }
        try:
            user_id = _clean(event.get_sender_id(), 120) or sender_hint
        except Exception:
            user_id = sender_hint
        try:
            user_name = _clean(event.get_sender_name(), 120) or user_id
        except Exception:
            user_name = user_id
        if not group_id or not self._configured_target(
            group_key=group_key,
            group_id=group_id,
            platform_id=platform_id,
            umo=umo,
        ):
            return {
                "sent": False,
                "answer": "问题答疑仅在已启用的开发者交流群中开放。",
                "confidence": 0.0,
                "references": [],
                "reason": "scope_not_enabled",
            }
        if not query:
            return {"sent": False, "answer": "请描述你遇到的现象、版本和复现方式。", "reason": "empty"}
        reference_limit = _int_value(
            self.setting("answers", "max_reference_issues", 4),
            4,
            1,
            12,
        )
        references = self.store.similar_issues(
            query,
            limit=min(30, reference_limit * 5),
            include_pending=True,
            group_key=group_key,
        )
        references = [
            item for item in references if self._answer_reference_allowed(item)
        ][:reference_limit]
        if not references:
            answer = "我暂时没有找到已确认的相同问题。请补充使用平台、插件版本、复现步骤和相关日志，我先把它记录进问题库。"
            sent = bool(command_result and not automatic)
            if not automatic and not command_result:
                sent = await self._send_event_reply(event, answer)
            self.store.record_answer(
                group_key=group_key,
                user_id=user_id,
                user_name=user_name,
                query=query,
                issue_ids=[],
                answer=answer,
                confidence=0.0,
                sent=sent,
                reason="没有已解决的相似记录",
            )
            return {"sent": sent, "answer": answer, "confidence": 0.0, "references": []}
        try:
            result = await self.organizer.draft_answer(query, references, umo=umo)
        except Exception as exc:
            logger.warning("[BugCompanion] 答疑模型失败: %s", _safe_error(exc))
            result = {"ok": False, "answer": "我找到相似记录，但暂时无法生成可靠答复，请稍后再试。", "confidence": 0.0, "reason": "model_error"}
        threshold = _float_value(self.setting("answers", "answer_min_confidence", 72), 72.0, 0.0, 100.0) / 100
        confidence = _float_value(result.get("confidence", 0), 0.0, 0.0, 1.0)
        answer = _clean(result.get("answer"), 8000) or "我找到相似记录，但还不能确认它们是同一个问题。"
        has_answer = bool(result.get("answer"))
        meets_threshold = has_answer and confidence >= threshold
        allowed = has_answer if not automatic else meets_threshold
        cooldown_blocked = False
        delivery_block_reason = ""
        if automatic:
            cooldown = _int_value(self.setting("answers", "group_cooldown_seconds", 120), 120, 0, 3600)
            last = self._answer_last_sent.get(group_key, 0.0)
            cooldown_blocked = allowed and time.time() - last < cooldown
            allowed = allowed and not cooldown_blocked
            if allowed:
                delivery_block_reason = self._automatic_delivery_block_reason(event, group_key)
                allowed = allowed and not delivery_block_reason
        sent = False
        if allowed:
            sent = bool(command_result and not automatic)
            if not sent:
                sent = await self._send_event_reply(event, answer)
            if sent:
                self._answer_last_sent[group_key] = time.time()
        reference_by_id = {str(item.get("id") or ""): item for item in references}
        selected_ids: list[str] = []
        raw_selected_ids = result.get("issue_ids")
        if isinstance(raw_selected_ids, list):
            for value in raw_selected_ids:
                issue_id = str(value or "").strip()
                if issue_id in reference_by_id and issue_id not in selected_ids:
                    selected_ids.append(issue_id)
        used_references = (
            [item for item in references if str(item.get("id") or "") in selected_ids]
            if selected_ids
            else references
        )
        outcome_reason = ""
        if delivery_block_reason:
            outcome_reason = delivery_block_reason
        elif automatic and cooldown_blocked:
            outcome_reason = "同群答疑冷却中"
        elif automatic and not meets_threshold:
            outcome_reason = "低于自动答疑阈值"
        elif allowed and not sent:
            outcome_reason = "回复发送失败"
        reason_parts = [
            value
            for value in (_clean(result.get("reason"), 1000), outcome_reason)
            if value
        ]
        audit_reason = "；".join(dict.fromkeys(reason_parts))
        self.store.record_answer(
            group_key=group_key,
            user_id=user_id,
            user_name=user_name,
            query=query,
            issue_ids=[str(item.get("id")) for item in used_references],
            answer=answer,
            confidence=confidence,
            sent=sent,
            reason=audit_reason,
        )
        return {
            "sent": sent,
            "answer": answer,
            "confidence": confidence,
            "references": used_references,
            "reason": audit_reason,
        }

    async def _send_event_reply(self, event: Any, text: str) -> bool:
        try:
            result = event.plain_result(text)
            await event.send(result)
            return True
        except Exception as exc:
            logger.debug("[BugCompanion] 通过事件回复失败: %s", _safe_error(exc))
            return False

    async def manual_add(self, event: Any, content: str) -> str:
        group_key, group_id, platform_id, umo, sender_hint = self._event_group(event)
        if not content.strip():
            return "请在命令后补充问题现象，例如：/bug 提交 TTS 开启后分段失效，版本 5.10.4。"
        if not group_id:
            return "问题提交仅能在已启用的开发者交流群中使用。"
        if not self._configured_target(
            group_key=group_key,
            group_id=group_id,
            platform_id=platform_id,
            umo=umo,
        ):
            return "当前群尚未启用问题收集。请先在拓展页启用该群，或由管理员使用“/修bug 启用当前群”。"
        sender_id = _clean(event.get_sender_id(), 120) if hasattr(event, "get_sender_id") else sender_hint
        sender_name = _clean(event.get_sender_name(), 120) if hasattr(event, "get_sender_name") else sender_id
        self.store.record_group(
            group_key=group_key,
            platform_id=platform_id,
            group_id=group_id,
            umo=umo,
            enabled=True,
        )
        self.store.add_message(
            {
                "group_key": group_key,
                "platform_id": platform_id,
                "group_id": group_id,
                "umo": umo,
                "message_id": f"manual-{time.time_ns()}",
                "sender_id": sender_id,
                "sender_name": sender_name or sender_id,
                "content": _clean_content(content, 6000),
                "created_at": _now_iso(),
            }
        )
        result = await self.process_group(group_key, force=True)
        if result.get("ok"):
            return f"已收录并整理 {result.get('created', 0)} 个问题候选，证据仍可在拓展页编辑和确认。"
        return "已保存原始问题，但模型整理暂未完成。可稍后在拓展页“待整理”中重试。"

    async def close(self) -> None:
        self._closed = True
        tasks = list(self._flush_tasks.values())
        if self._repository_task is not None:
            tasks.append(self._repository_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._flush_tasks.clear()
        self._repository_task = None
        self._repository_schedule_key = None
        self._group_events.clear()
        self._retry_attempts.clear()
