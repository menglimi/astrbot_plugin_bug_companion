# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from typing import Any

from astrbot.api import logger
from astrbot.api.web import request

from .service import _normalize_repository, _repository_target
from .storage import issue_change_error, normalize_plugin_name


PAGE_API_PREFIX = "/astrbot_plugin_bug_companion/page"
ISSUE_EDITABLE_FIELDS = {
    "title",
    "summary",
    "symptoms",
    "reproduction",
    "expected",
    "actual",
    "environment",
    "impact",
    "cause",
    "solution",
    "workaround",
    "reporter_name",
    "introduced_version",
    "fixed_version",
    "applicable_scope",
    "kind",
    "category",
    "issue_domain",
    "plugin_name",
    "severity",
    "status",
    "verification_status",
    "last_confirmed_at",
    "tags",
    "platforms",
    "versions",
}
ISSUE_CREATE_FIELDS = ISSUE_EDITABLE_FIELDS | {"group_key"}


class BugCompanionPageApi:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.service = plugin.service
        self._registered_routes: list[tuple[str, Any]] = []
        self._settings_lock = asyncio.Lock()

    def register_routes(self) -> None:
        register = getattr(self.plugin.context, "register_web_api", None)
        if not callable(register):
            logger.warning("[BugCompanion] 当前 AstrBot 不支持插件拓展页 API")
            return
        routes = [
            ("/overview", self.overview, ["GET"], "Bug Companion overview"),
            ("/issues", self.issues, ["GET"], "Bug Companion issues"),
            ("/issues/detail", self.issue_detail, ["GET"], "Bug Companion issue detail"),
            ("/issues/create", self.issue_create, ["POST"], "Bug Companion create issue"),
            ("/issues/update", self.issue_update, ["POST"], "Bug Companion update issue"),
            ("/issues/bulk", self.issue_bulk, ["POST"], "Bug Companion bulk update"),
            ("/issues/merge", self.issue_merge, ["POST"], "Bug Companion merge issues"),
            ("/issues/similar", self.issue_similar, ["GET"], "Bug Companion similar issues"),
            ("/review/queue", self.review_queue, ["GET"], "Bug Companion review queue"),
            ("/review/decide", self.review_decide, ["POST"], "Bug Companion review decision"),
            ("/review/reopen", self.review_reopen, ["POST"], "Bug Companion reopen review"),
            ("/todos", self.todos, ["GET"], "Bug Companion todos"),
            ("/todos/create", self.todo_create, ["POST"], "Bug Companion create todo"),
            ("/todos/update", self.todo_update, ["POST"], "Bug Companion update todo"),
            ("/inbox", self.inbox, ["GET"], "Bug Companion inbox"),
            ("/inbox/reprocess", self.inbox_reprocess, ["POST"], "Bug Companion reprocess inbox"),
            ("/groups", self.groups, ["GET"], "Bug Companion groups"),
            ("/groups/update", self.group_update, ["POST"], "Bug Companion update group"),
            ("/answers", self.answers, ["GET"], "Bug Companion answer audits"),
            ("/settings", self.settings, ["GET"], "Bug Companion settings"),
            ("/settings/save", self.settings_save, ["POST"], "Bug Companion save settings"),
            ("/maintenance", self.maintenance, ["POST"], "Bug Companion maintenance"),
            ("/export", self.export_data, ["GET"], "Bug Companion export"),
        ]
        registered = getattr(self.plugin.context, "registered_web_apis", None)
        route_snapshot = list(registered) if isinstance(registered, list) else None
        try:
            for path, handler, methods, description in routes:
                route = f"{PAGE_API_PREFIX}{path}"
                register(route, handler, methods, description)
                self._registered_routes.append((route, handler))
        except Exception:
            if route_snapshot is not None:
                registered[:] = route_snapshot
                self._registered_routes.clear()
            else:
                self.unregister_routes()
            raise

    def unregister_routes(self) -> None:
        """Detach only this instance's handlers when AstrBot disables the plugin."""
        registered = getattr(self.plugin.context, "registered_web_apis", None)
        if not isinstance(registered, list) or not self._registered_routes:
            return
        owned = {(route, id(handler)) for route, handler in self._registered_routes}
        registered[:] = [
            item
            for item in registered
            if not (
                isinstance(item, tuple)
                and len(item) >= 2
                and (item[0], id(item[1])) in owned
            )
        ]
        self._registered_routes.clear()

    @staticmethod
    async def _payload() -> dict[str, Any]:
        value = await request.json(default={})
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(int(value), maximum))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _text(value: Any, limit: int = 4000) -> str:
        return str(value or "").strip()[:limit]

    @staticmethod
    def _bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        return default

    @classmethod
    def _text_list(cls, value: Any, *, item_limit: int, count_limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            text = cls._text(item, item_limit)
            if text and text not in result:
                result.append(text)
        return result[:count_limit]

    @staticmethod
    def _plugin_names(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            name = normalize_plugin_name(item)
            if name and name not in result:
                result.append(name)
            if len(result) >= 50:
                break
        return result

    @staticmethod
    def _plugin_names_for_save(value: Any) -> tuple[list[str], str]:
        if not isinstance(value, list):
            return [], "管理的插件名称必须是列表"
        result: list[str] = []
        for item in value:
            name = normalize_plugin_name(item, truncate=False)
            if not name or name in result:
                continue
            if len(name) > 120:
                return [], "每个插件名称不能超过 120 个字符"
            result.append(name)
            if len(result) > 50:
                return [], "管理的插件名称最多填写 50 个"
        return result, ""

    def _managed_plugins(self) -> list[str]:
        return self._plugin_names(
            self.service.setting("models", "managed_plugins", [])
        )

    def _plugin_change_error(
        self,
        changes: dict[str, Any],
        existing: dict[str, Any] | None = None,
    ) -> str:
        if "plugin_name" not in changes:
            return ""
        name = normalize_plugin_name(changes.get("plugin_name"))
        changes["plugin_name"] = name
        issue_domain = str(
            changes.get("issue_domain")
            or (existing or {}).get("issue_domain")
            or "other"
        )
        if name and issue_domain != "plugin":
            return "只有插件问题可以选择具体插件"
        if not name:
            return ""
        if existing and name == normalize_plugin_name(existing.get("plugin_name")):
            return ""
        if name not in self._managed_plugins():
            return "具体插件不在管理名单中，请先到模型设置中添加"
        return ""

    @staticmethod
    def _processing_failure_message(result: dict[str, Any]) -> str:
        error = str(result.get("error") or "")
        return {
            "closed": "插件正在停止或重载，本次整理未执行",
            "scope_disabled": "该群已停用或问题收集已关闭，本次整理未执行",
            "processing": "该群正在整理中，请稍后刷新查看结果",
            "cooldown": "整理模型刚刚失败，已保留证据并等待自动重试",
        }.get(error, "整理模型本次未完成，原始证据仍在待整理队列中")

    async def overview(self) -> dict[str, Any]:
        data = self.service.store.overview()
        data["enabled_groups"] = sum(
            1 for item in self.service.store.list_groups() if self._group_is_effectively_enabled(item)
        )
        return {"status": "ok", "data": data}

    def _group_is_effectively_enabled(self, item: dict[str, Any]) -> bool:
        return self.service._configured_target(
            group_key=self._text(item.get("group_key"), 300),
            group_id=self._text(item.get("group_id"), 200),
            platform_id=self._text(item.get("platform_id"), 120),
            umo=self._text(item.get("umo"), 300),
        )

    async def issues(self) -> dict[str, Any]:
        args = request.query
        result = self.service.store.list_issues(
            query=self._text(args.get("q"), 300),
            status=self._text(args.get("status"), 200),
            kind=self._text(args.get("kind"), 40),
            category=self._text(args.get("category"), 40),
            issue_domain=self._text(args.get("issue_domain"), 40),
            plugin_name=self._text(args.get("plugin_name"), 120),
            review_decision=self._text(args.get("review_decision"), 40),
            verification_status=self._text(args.get("verification_status"), 40),
            severity=self._text(args.get("severity"), 40),
            group_key=self._text(args.get("group"), 300),
            sort=self._text(args.get("sort"), 40) or "updated_desc",
            page=self._int(args.get("page"), 1, 1, 100000),
            page_size=self._int(args.get("page_size"), 30, 1, 100),
        )
        return {"status": "ok", "data": result}

    async def issue_detail(self) -> dict[str, Any]:
        issue_id = self._text(request.query.get("id"), 80)
        issue = self.service.store.get_issue(issue_id) if issue_id else None
        if not issue:
            return {"status": "error", "message": "问题不存在", "data": {}}
        return {"status": "ok", "data": issue}

    async def issue_create(self) -> dict[str, Any]:
        payload = await self._payload()
        changes = {field: payload[field] for field in ISSUE_CREATE_FIELDS if field in payload}
        group_key = self._text(changes.get("group_key"), 300)
        if group_key:
            known_groups = {
                self._text(item.get("group_key"), 300)
                for item in self.service.store.list_groups()
            }
            if group_key not in known_groups:
                return {
                    "status": "error",
                    "message": "归属群聊不存在，请刷新群配置后重试",
                    "data": {},
                }
        changes["group_key"] = group_key
        if "plugin_name" in changes:
            changes["plugin_name"] = normalize_plugin_name(
                changes.get("plugin_name"), truncate=False
            )
        validation_error = issue_change_error(
            changes,
            creating=True,
            enforce_limits=True,
        )
        if validation_error:
            return {"status": "error", "message": validation_error, "data": {}}
        plugin_error = self._plugin_change_error(changes)
        if plugin_error:
            return {"status": "error", "message": plugin_error, "data": {}}
        try:
            issue = self.service.store.create_issue(changes, actor="page")
            return {"status": "ok", "data": issue}
        except Exception as exc:
            logger.warning("[BugCompanion] 页面创建问题失败: %s", exc, exc_info=True)
            return {"status": "error", "message": "创建失败，请检查字段后重试", "data": {}}

    async def issue_update(self) -> dict[str, Any]:
        payload = await self._payload()
        issue_id = self._text(payload.get("id"), 80)
        if not issue_id:
            return {"status": "error", "message": "缺少问题 ID", "data": {}}
        changes = {field: payload[field] for field in ISSUE_EDITABLE_FIELDS if field in payload}
        if not changes:
            return {"status": "error", "message": "没有可保存的修改", "data": {}}
        existing = self.service.store.get_issue(issue_id)
        if not existing:
            return {"status": "error", "message": "问题不存在", "data": {}}
        if "plugin_name" in changes:
            changes["plugin_name"] = normalize_plugin_name(
                changes.get("plugin_name"), truncate=False
            )
        validation_error = issue_change_error(changes, enforce_limits=True)
        if validation_error:
            return {"status": "error", "message": validation_error, "data": {}}
        plugin_error = self._plugin_change_error(changes, existing)
        if plugin_error:
            return {"status": "error", "message": plugin_error, "data": {}}
        issue = self.service.store.update_issue(issue_id, changes, actor="page")
        if not issue:
            if existing and (
                existing.get("status") == "duplicate"
                or existing.get("duplicate_of")
            ):
                return {
                    "status": "error",
                    "message": "重复问题的归属关系不能在编辑器中修改，请使用合并目标操作",
                    "data": {},
                }
            return {"status": "error", "message": "问题不存在", "data": {}}
        return {"status": "ok", "data": issue}

    async def issue_bulk(self) -> dict[str, Any]:
        payload = await self._payload()
        ids = self._text_list(payload.get("ids"), item_limit=80, count_limit=500)
        raw_changes = payload.get("changes") if isinstance(payload.get("changes"), dict) else {}
        changes = {
            field: raw_changes[field]
            for field in ISSUE_EDITABLE_FIELDS
            if field in raw_changes
        }
        if not ids:
            return {"status": "error", "message": "请选择至少一条问题", "data": {}}
        if not changes:
            return {"status": "error", "message": "没有可保存的修改", "data": {}}
        if "plugin_name" in changes:
            changes["plugin_name"] = normalize_plugin_name(
                changes.get("plugin_name"), truncate=False
            )
        validation_error = issue_change_error(changes, enforce_limits=True)
        if validation_error:
            return {"status": "error", "message": validation_error, "data": {}}
        for issue_id in ids:
            existing = self.service.store.get_issue(issue_id)
            if not existing:
                continue
            plugin_error = self._plugin_change_error(changes, existing)
            if plugin_error:
                return {"status": "error", "message": plugin_error, "data": {}}
        updated_ids = self.service.store.bulk_update_ids(ids, changes, actor="page")
        return {
            "status": "ok",
            "data": {"updated": len(updated_ids), "updated_ids": updated_ids},
        }

    async def issue_merge(self) -> dict[str, Any]:
        payload = await self._payload()
        source_ids = payload.get("source_ids") if isinstance(payload.get("source_ids"), list) else []
        target_id = self._text(payload.get("target_id"), 80)
        if not target_id or not source_ids:
            return {"status": "error", "message": "请选择目标问题和至少一个来源问题", "data": {}}
        issue = self.service.store.merge_issues(source_ids, target_id, actor="page")
        if not issue:
            return {"status": "error", "message": "合并失败，问题可能已被删除", "data": {}}
        return {"status": "ok", "data": issue}

    async def issue_similar(self) -> dict[str, Any]:
        issue_id = self._text(request.query.get("id"), 80)
        query = self._text(request.query.get("q"), 300)
        issue = self.service.store.get_issue(issue_id) if issue_id else query
        if not issue:
            return {"status": "error", "message": "缺少查询内容", "data": []}
        return {"status": "ok", "data": self.service.store.similar_issues(issue, limit=self._int(request.query.get("limit"), 8, 1, 30), exclude_id=issue_id)}

    async def review_queue(self) -> dict[str, Any]:
        data = self.service.store.list_review_queue(
            self._int(request.query.get("limit"), 100, 1, 500)
        )
        return {"status": "ok", "data": data}

    async def review_decide(self) -> dict[str, Any]:
        payload = await self._payload()
        issue_id = self._text(payload.get("issue_id") or payload.get("id"), 80)
        action = self._text(payload.get("action"), 20).lower()
        if not issue_id:
            return {"status": "error", "message": "缺少问题 ID", "data": {}}
        if not action:
            return {"status": "error", "message": "请选择批阅动作", "data": {}}
        raw_todo = payload.get("todo")
        if isinstance(raw_todo, dict):
            todo: Any = {
                "content": self._text(raw_todo.get("content"), 4000),
                "due_at": self._text(raw_todo.get("due_at"), 80),
            }
        else:
            todo = self._text(raw_todo, 4000)
        result = self.service.store.review_decide(
            issue_id,
            action,
            self._text(payload.get("note"), 4000),
            todo,
            self._text(payload.get("expected_updated_at"), 80),
            actor="page",
        )
        if result.get("ok"):
            return {"status": "ok", "data": result}
        error = str(result.get("error") or "")
        message = {
            "invalid_action": "批阅动作无效",
            "todo_required": "交办时必须填写待办内容",
            "not_found": "问题不存在",
            "conflict": "问题已在其他页面更新，请确认最新内容后重试",
            "missing_issue_id": "缺少问题 ID",
        }.get(error, "批阅失败，请刷新后重试")
        return {
            "status": "error",
            "message": message,
            "data": {"error": error, "issue": result.get("issue")},
        }

    async def review_reopen(self) -> dict[str, Any]:
        payload = await self._payload()
        issue_id = self._text(payload.get("issue_id") or payload.get("id"), 80)
        if not issue_id:
            return {"status": "error", "message": "缺少问题 ID", "data": {}}
        result = self.service.store.review_reopen(
            issue_id,
            self._text(payload.get("expected_updated_at"), 80),
            actor="page",
        )
        if result.get("ok"):
            return {"status": "ok", "data": result}
        error = str(result.get("error") or "")
        message = {
            "not_found": "问题不存在",
            "conflict": "问题已在其他页面更新，请确认最新内容后重试",
            "missing_issue_id": "缺少问题 ID",
        }.get(error, "重呈失败，请刷新后重试")
        return {
            "status": "error",
            "message": message,
            "data": {"error": error, "issue": result.get("issue")},
        }

    async def todos(self) -> dict[str, Any]:
        status = self._text(request.query.get("status"), 20).lower()
        if status and status not in {"open", "completed"}:
            return {"status": "error", "message": "待办状态无效", "data": {}}
        data = self.service.store.list_todos_page(
            status=status,
            issue_id=self._text(
                request.query.get("issue_id") or request.query.get("issue"),
                80,
            ),
            page=self._int(request.query.get("page"), 1, 1, 100000),
            limit=self._int(request.query.get("limit"), 100, 1, 500),
        )
        return {"status": "ok", "data": data}

    async def todo_create(self) -> dict[str, Any]:
        payload = await self._payload()
        issue_id = self._text(payload.get("issue_id") or payload.get("id"), 80)
        content = self._text(payload.get("content"), 4000)
        if not issue_id or not content:
            return {
                "status": "error",
                "message": "请选择问题并填写待办内容",
                "data": {},
            }
        todo = self.service.store.create_todo(
            issue_id,
            content,
            due_at=self._text(payload.get("due_at"), 80),
            created_by="page",
        )
        if not todo:
            return {"status": "error", "message": "问题不存在或待办内容无效", "data": {}}
        return {"status": "ok", "data": todo}

    async def todo_update(self) -> dict[str, Any]:
        payload = await self._payload()
        try:
            todo_id = int(payload.get("id"))
        except (TypeError, ValueError):
            return {"status": "error", "message": "待办 ID 无效", "data": {}}
        changes = {
            field: payload[field]
            for field in ("content", "status", "due_at")
            if field in payload
        }
        if not changes:
            return {"status": "error", "message": "没有可保存的修改", "data": {}}
        if "content" in changes:
            changes["content"] = self._text(changes["content"], 4000)
        if "due_at" in changes:
            changes["due_at"] = self._text(changes["due_at"], 80)
        if "status" in changes:
            changes["status"] = self._text(changes["status"], 20).lower()
            if changes["status"] not in {"open", "completed"}:
                return {"status": "error", "message": "待办状态无效", "data": {}}
        todo = self.service.store.update_todo(todo_id, changes, actor="page")
        if not todo:
            return {"status": "error", "message": "待办不存在或内容无效", "data": {}}
        return {"status": "ok", "data": todo}

    async def inbox(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "data": {
                "items": self.service.store.list_pending_messages(
                    self._int(request.query.get("limit"), 100, 1, 500)
                ),
                "total": self.service.store.pending_message_count(),
            },
        }

    async def inbox_reprocess(self) -> dict[str, Any]:
        payload = await self._payload()
        group_key = self._text(payload.get("group_key"), 300)
        raw_ids = payload.get("message_ids")
        all_pending = self._bool(payload.get("all_pending"), False)
        if not isinstance(raw_ids, list):
            return {
                "status": "error",
                "message": "请明确选择待整理消息，或确认整理当前全部队列",
                "data": {},
            }
        try:
            ids = list(dict.fromkeys(int(item) for item in raw_ids if int(item) > 0))
        except (TypeError, ValueError):
            return {"status": "error", "message": "待整理消息 ID 格式无效", "data": {}}
        if not ids and not all_pending:
            return {
                "status": "error",
                "message": "未选择待整理消息；整理全部队列需要显式确认",
                "data": {},
            }
        if ids:
            count, group_keys, group_matches = self.service.store.prepare_pending_messages(
                ids,
                expected_group_key=group_key,
            )
            if not group_matches:
                return {
                    "status": "error",
                    "message": "所选消息与指定群聊不一致，请刷新后重试",
                    "data": {},
                }
        else:
            group_keys = self.service.store.pending_message_group_keys()
            count = 0
        if group_key:
            if not ids:
                self.service.store.approve_pending_group(group_key)
            result = await self.service.process_group(group_key, force=True)
            ok = bool(result.get("ok"))
            return {
                "status": "ok",
                "data": {
                    "ok": ok,
                    "message": "" if ok else self._processing_failure_message(result),
                    "reset": count,
                    "result": result,
                },
            }
        results = []
        for key in group_keys:
            if not ids:
                self.service.store.approve_pending_group(key)
            results.append(await self.service.process_group(key, force=True))
        failures = [result for result in results if not result.get("ok")]
        return {
            "status": "ok",
            "data": {
                "ok": not failures,
                "message": (
                    self._processing_failure_message(failures[0]) if failures else ""
                ),
                "reset": count,
                "results": results,
            },
        }

    async def groups(self) -> dict[str, Any]:
        items = self.service.store.list_groups()
        for item in items:
            item["enabled"] = self._group_is_effectively_enabled(item)
        return {"status": "ok", "data": {"items": items}}

    async def group_update(self) -> dict[str, Any]:
        payload = await self._payload()
        group_key = self._text(payload.get("group_key"), 300)
        if not group_key:
            return {"status": "error", "message": "缺少群组标识", "data": {}}
        enabled = self._bool(payload.get("enabled"), False)
        updated = self.service.store.set_group_enabled(group_key, enabled, self._text(payload.get("display_name"), 120))
        if not updated:
            # 允许页面先创建一个只知道 key 的群组，再等待真实消息补全平台信息。
            self.service.store.record_group(
                group_key=group_key,
                platform_id=group_key.split(":", 1)[0] if ":" in group_key else "",
                group_id=group_key.rsplit(":", 1)[-1],
                umo="",
                display_name=self._text(payload.get("display_name"), 120),
                enabled=enabled,
            )
        await self.service.refresh_scheduling()
        return {"status": "ok", "data": {"group_key": group_key, "enabled": enabled}}

    async def answers(self) -> dict[str, Any]:
        return {"status": "ok", "data": {"items": self.service.store.list_answers(self._int(request.query.get("limit"), 100, 1, 500))}}

    def _settings_view(self) -> dict[str, Any]:
        data = {
            "collection": {
                "enabled": bool(self.service.setting("collection", "enabled", True)),
                "target_groups": self.service.setting("collection", "target_groups", []),
                "quiet_seconds": self.service.setting("collection", "quiet_seconds", 12),
                "batch_max_messages": self.service.setting("collection", "batch_max_messages", 40),
                "batch_max_chars": self.service.setting("collection", "batch_max_chars", 10000),
                "ignored_sender_ids": self.service.setting("collection", "ignored_sender_ids", []),
            },
            "models": {
                "managed_plugins": self._managed_plugins(),
                "organizer_provider_id": self.service.setting("models", "organizer_provider_id", ""),
                "fallback_provider_id": self.service.setting("models", "fallback_provider_id", ""),
                "timeout_seconds": self.service.setting("models", "timeout_seconds", 60),
                "max_candidates_per_batch": self.service.setting("models", "max_candidates_per_batch", 6),
                "custom_organizer_guidance": self.service.setting("models", "custom_organizer_guidance", ""),
            },
            "answers": {
                "auto_answer": bool(self.service.setting("answers", "auto_answer", True)),
                "answer_min_confidence": self.service.setting("answers", "answer_min_confidence", 72),
                "group_cooldown_seconds": self.service.setting("answers", "group_cooldown_seconds", 120),
                "max_reference_issues": self.service.setting("answers", "max_reference_issues", 4),
            },
            "repository_updates": {
                "enabled": bool(
                    self.service.setting("repository_updates", "enabled", False)
                ),
                "repositories": self._text_list(
                    self.service.setting("repository_updates", "repositories", []),
                    item_limit=500,
                    count_limit=50,
                ),
                "target_groups": self._text_list(
                    self.service.setting("repository_updates", "target_groups", []),
                    item_limit=300,
                    count_limit=50,
                ),
                "interval_minutes": self.service.setting(
                    "repository_updates", "interval_minutes", 60
                ),
                "notify_commits": bool(
                    self.service.setting(
                        "repository_updates", "notify_commits", True
                    )
                ),
                "notify_releases": bool(
                    self.service.setting(
                        "repository_updates", "notify_releases", True
                    )
                ),
                "ai_summary_enabled": bool(
                    self.service.setting(
                        "repository_updates",
                        "ai_summary_enabled",
                        self.service.setting("repository_updates", "ai_summary", False),
                    )
                ),
                "ai_summary_provider_id": self.service.setting(
                    "repository_updates", "ai_summary_provider_id", ""
                ),
                "ai_summary_max_chars": self.service.setting(
                    "repository_updates", "ai_summary_max_chars", 700
                ),
                "max_items_per_check": self.service.setting(
                    "repository_updates", "max_items_per_check", 5
                ),
            },
            "appearance": {
                "theme": self.service.setting("appearance", "theme", "system"),
            },
            "retention": {
                "raw_message_days": self.service.setting("retention", "raw_message_days", 90),
                "store_non_candidate_context": bool(
                    self.service.setting("retention", "store_non_candidate_context", True)
                ),
            },
        }
        serialized = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        data["revision"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:20]
        return data

    async def settings(self) -> dict[str, Any]:
        return {"status": "ok", "data": self._settings_view()}

    async def settings_save(self) -> dict[str, Any]:
        payload = await self._payload()
        async with self._settings_lock:
            return await self._settings_save_payload(payload)

    async def _settings_save_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        base_revision = self._text(payload.get("revision"), 64)
        current_view = self._settings_view()
        if base_revision and base_revision != current_view["revision"]:
            return {
                "status": "error",
                "message": "设置已在其他页面更新，请刷新确认后再保存",
                "data": current_view,
            }
        config_snapshot = copy.deepcopy(dict(self.service.config))
        collection = payload.get("collection") if isinstance(payload.get("collection"), dict) else {}
        models = payload.get("models") if isinstance(payload.get("models"), dict) else {}
        answers = payload.get("answers") if isinstance(payload.get("answers"), dict) else {}
        repository_updates = (
            payload.get("repository_updates")
            if isinstance(payload.get("repository_updates"), dict)
            else {}
        )
        appearance = payload.get("appearance") if isinstance(payload.get("appearance"), dict) else {}
        retention = payload.get("retention") if isinstance(payload.get("retention"), dict) else {}
        managed_plugins: list[str] | None = None
        if "managed_plugins" in models:
            managed_plugins, plugin_error = self._plugin_names_for_save(
                models["managed_plugins"]
            )
            if plugin_error:
                return {
                    "status": "error",
                    "message": plugin_error,
                    "data": current_view,
                }

        current_collection = self.service.config.setdefault("collection", {})
        if not isinstance(current_collection, dict):
            current_collection = {}
            self.service.config["collection"] = current_collection
        if "enabled" in collection:
            current_collection["enabled"] = self._bool(collection["enabled"], True)
        if "target_groups" in collection:
            current_collection["target_groups"] = self._text_list(collection["target_groups"], item_limit=300, count_limit=100)
        if "quiet_seconds" in collection:
            current_collection["quiet_seconds"] = self._int(collection["quiet_seconds"], 12, 2, 180)
        if "batch_max_messages" in collection:
            current_collection["batch_max_messages"] = self._int(collection["batch_max_messages"], 40, 1, 100)
        if "batch_max_chars" in collection:
            current_collection["batch_max_chars"] = self._int(collection["batch_max_chars"], 10000, 1000, 50000)
        if "ignored_sender_ids" in collection:
            current_collection["ignored_sender_ids"] = self._text_list(collection["ignored_sender_ids"], item_limit=120, count_limit=100)

        current_models = self.service.config.setdefault("models", {})
        if not isinstance(current_models, dict):
            current_models = {}
            self.service.config["models"] = current_models
        if "organizer_provider_id" in models:
            current_models["organizer_provider_id"] = self._text(models["organizer_provider_id"], 160)
        if managed_plugins is not None:
            current_models["managed_plugins"] = managed_plugins
        if "fallback_provider_id" in models:
            current_models["fallback_provider_id"] = self._text(models["fallback_provider_id"], 160)
        if "timeout_seconds" in models:
            current_models["timeout_seconds"] = self._int(models["timeout_seconds"], 60, 5, 300)
        if "max_candidates_per_batch" in models:
            current_models["max_candidates_per_batch"] = self._int(models["max_candidates_per_batch"], 6, 1, 12)
        if "custom_organizer_guidance" in models:
            current_models["custom_organizer_guidance"] = self._text(models["custom_organizer_guidance"], 2000)

        current_answers = self.service.config.setdefault("answers", {})
        if not isinstance(current_answers, dict):
            current_answers = {}
            self.service.config["answers"] = current_answers
        if "auto_answer" in answers:
            current_answers["auto_answer"] = self._bool(answers["auto_answer"], True)
        if "answer_min_confidence" in answers:
            current_answers["answer_min_confidence"] = self._int(answers["answer_min_confidence"], 72, 0, 100)
        if "group_cooldown_seconds" in answers:
            current_answers["group_cooldown_seconds"] = self._int(answers["group_cooldown_seconds"], 120, 0, 3600)
        if "max_reference_issues" in answers:
            current_answers["max_reference_issues"] = self._int(answers["max_reference_issues"], 4, 1, 12)

        current_repository_updates = self.service.config.setdefault(
            "repository_updates", {}
        )
        if not isinstance(current_repository_updates, dict):
            current_repository_updates = {}
            self.service.config["repository_updates"] = current_repository_updates
        if "enabled" in repository_updates:
            current_repository_updates["enabled"] = self._bool(
                repository_updates["enabled"], False
            )
        if "repositories" in repository_updates:
            repository_values = self._text_list(
                repository_updates["repositories"], item_limit=500, count_limit=50
            )
            normalized_repositories: list[str] = []
            invalid_repositories: list[str] = []
            for value in repository_values:
                normalized = _normalize_repository(value)
                if not normalized:
                    invalid_repositories.append(value)
                    continue
                if normalized not in normalized_repositories:
                    normalized_repositories.append(normalized)
            if invalid_repositories:
                self.service.config.clear()
                self.service.config.update(config_snapshot)
                return {
                    "status": "error",
                    "message": (
                        "仓库地址必须是 GitHub 仓库 URL 或 owner/repo 格式："
                        f"{invalid_repositories[0]}"
                    ),
                    "data": current_view,
                }
            current_repository_updates["repositories"] = normalized_repositories
        if "target_groups" in repository_updates:
            target_values = self._text_list(
                repository_updates["target_groups"], item_limit=300, count_limit=50
            )
            invalid_targets = [value for value in target_values if not _repository_target(value)]
            if invalid_targets:
                self.service.config.clear()
                self.service.config.update(config_snapshot)
                return {
                    "status": "error",
                    "message": (
                        "固定推送群必须填写完整 UMO，例如 aiocqhttp:GroupMessage:123456："
                        f"{invalid_targets[0]}"
                    ),
                    "data": current_view,
                }
            current_repository_updates["target_groups"] = target_values
        if "interval_minutes" in repository_updates:
            current_repository_updates["interval_minutes"] = self._int(
                repository_updates["interval_minutes"], 60, 10, 1440
            )
        if "notify_commits" in repository_updates:
            current_repository_updates["notify_commits"] = self._bool(
                repository_updates["notify_commits"], True
            )
        if "notify_releases" in repository_updates:
            current_repository_updates["notify_releases"] = self._bool(
                repository_updates["notify_releases"], True
            )
        if "ai_summary_enabled" in repository_updates:
            current_repository_updates["ai_summary_enabled"] = self._bool(
                repository_updates["ai_summary_enabled"], False
            )
        elif "ai_summary" in repository_updates:
            current_repository_updates["ai_summary_enabled"] = self._bool(
                repository_updates["ai_summary"], False
            )
        current_repository_updates.pop("ai_summary", None)
        if "ai_summary_provider_id" in repository_updates:
            current_repository_updates["ai_summary_provider_id"] = self._text(
                repository_updates["ai_summary_provider_id"], 160
            )
        if "ai_summary_max_chars" in repository_updates:
            current_repository_updates["ai_summary_max_chars"] = self._int(
                repository_updates["ai_summary_max_chars"], 700, 120, 1500
            )
        if "max_items_per_check" in repository_updates:
            current_repository_updates["max_items_per_check"] = self._int(
                repository_updates["max_items_per_check"], 5, 1, 20
            )

        current_appearance = self.service.config.setdefault("appearance", {})
        if not isinstance(current_appearance, dict):
            current_appearance = {}
            self.service.config["appearance"] = current_appearance
        if "theme" in appearance:
            theme = self._text(appearance["theme"], 20)
            current_appearance["theme"] = theme if theme in {"system", "light", "dark"} else "system"

        current_retention = self.service.config.setdefault("retention", {})
        if not isinstance(current_retention, dict):
            current_retention = {}
            self.service.config["retention"] = current_retention
        if "raw_message_days" in retention:
            current_retention["raw_message_days"] = self._int(retention["raw_message_days"], 90, 0, 3650)
        if "store_non_candidate_context" in retention:
            current_retention["store_non_candidate_context"] = self._bool(
                retention["store_non_candidate_context"], True
            )
        saver = getattr(self.plugin.config, "save_config", None)
        if callable(saver):
            try:
                result = saver()
                if hasattr(result, "__await__"):
                    await result
            except Exception as exc:
                self.service.config.clear()
                self.service.config.update(config_snapshot)
                logger.warning("[BugCompanion] 保存配置失败: %s", exc)
                return {"status": "error", "message": "AstrBot 配置保存失败，本次修改未生效", "data": self._settings_view()}
        await self.service.refresh_scheduling()
        return {"status": "ok", "data": self._settings_view()}

    async def maintenance(self) -> dict[str, Any]:
        retention = self.service.section("retention")
        raw_message_days = self._int(
            retention.get("raw_message_days"), 90, 0, 3650
        )
        result = self.service.store.maintenance(raw_message_days)
        importer = getattr(self.plugin, "qq_history_importer", None)
        prune_archives = getattr(importer, "prune_archives", None)
        if callable(prune_archives):
            result.update(prune_archives(raw_message_days))
        return {"status": "ok", "data": result}

    async def export_data(self) -> dict[str, Any]:
        return {"status": "ok", "data": self.service.store.export_data()}
