# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


BugCompanionPageApi = None
BugCompanionService = None
safe_error = None


def _load_runtime() -> None:
    global BugCompanionPageApi, BugCompanionService, safe_error
    if BugCompanionService is not None:
        return
    try:
        from astrbot_plugin_bug_companion.page_api import (
            BugCompanionPageApi as PageApi,
        )
        from astrbot_plugin_bug_companion.service import (
            BugCompanionService as Service,
            _safe_error,
        )
    except Exception as exc:
        raise unittest.SkipTest(f"AstrBot 运行时不可用: {exc}") from exc
    BugCompanionPageApi = PageApi
    BugCompanionService = Service
    safe_error = _safe_error


class FakeEvent:
    def __init__(
        self,
        *,
        group_id: str = "123",
        platform_id: str = "onebot",
        umo: str = "onebot:GroupMessage:123",
        content: str = "TTS 开启后长回复分段失效",
        message_id: str = "message-1",
    ) -> None:
        self._group_id = group_id
        self._platform_id = platform_id
        self.unified_msg_origin = umo
        self.message_str = content
        self.message_obj = SimpleNamespace(message_id=message_id)
        self.is_at_or_wake_command = False
        self.sent_results: list[str] = []

    def get_group_id(self) -> str:
        return self._group_id

    def get_platform_id(self) -> str:
        return self._platform_id

    def get_sender_id(self) -> str:
        return "user-1"

    def get_sender_name(self) -> str:
        return "测试用户"

    def get_self_id(self) -> str:
        return "bot-1"

    def plain_result(self, text: str) -> str:
        return text

    async def send(self, result: str) -> None:
        self.sent_results.append(result)


class FakeWebContext:
    def __init__(self) -> None:
        self.registered_web_apis = [
            ("/unrelated", self.unrelated, ["GET"], "Unrelated API")
        ]

    async def unrelated(self) -> dict[str, str]:
        return {"status": "ok"}

    def register_web_api(self, route, handler, methods, description) -> None:
        for index, item in enumerate(self.registered_web_apis):
            if item[0] == route and item[2] == methods:
                self.registered_web_apis[index] = (route, handler, methods, description)
                return
        self.registered_web_apis.append((route, handler, methods, description))


class RuntimeScopeTestBase:
    def setUp(self) -> None:
        _load_runtime()
        self._temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._temp_dir.name)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def make_service(self, target_groups: list[str]):
        config = {
            "collection": {
                "enabled": True,
                "target_groups": target_groups,
                "quiet_seconds": 2,
            }
        }
        return BugCompanionService(SimpleNamespace(), config, self.data_dir)


class RuntimeScopeTests(RuntimeScopeTestBase, unittest.TestCase):
    def test_answer_reference_requires_reusable_verified_knowledge(self) -> None:
        allowed = BugCompanionService._answer_reference_allowed
        base = {
            "status": "resolved",
            "solution": "更新后重启插件",
            "workaround": "",
            "review_decision": "approved",
            "knowledge_published_at": "2026-07-31T00:00:00+00:00",
        }
        self.assertTrue(
            allowed(
                {
                    **base,
                    "category": "fixed_bug",
                    "verification_status": "verified",
                }
            )
        )
        self.assertFalse(
            allowed(
                {
                    **base,
                    "review_decision": "todo",
                    "category": "fixed_bug",
                    "verification_status": "verified",
                }
            )
        )
        self.assertFalse(
            allowed(
                {
                    **base,
                    "category": "fixed_bug",
                    "verification_status": "fix_pending",
                }
            )
        )
        self.assertFalse(
            allowed(
                {
                    **base,
                    "category": "pending_feedback",
                    "verification_status": "confirmed",
                }
            )
        )
        self.assertTrue(
            allowed(
                {
                    **base,
                    "status": "in_progress",
                    "category": "known_bug",
                    "verification_status": "confirmed",
                    "solution": "",
                    "workaround": "暂时关闭同步状态",
                }
            )
        )

    def test_complete_umo_is_exact_but_bare_group_id_is_cross_platform(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        self.assertTrue(
            service._configured_target(
                group_key="onebot:123",
                group_id="123",
                platform_id="onebot",
                umo="onebot:GroupMessage:123",
            )
        )
        self.assertFalse(
            service._configured_target(
                group_key="qq_official:123",
                group_id="123",
                platform_id="qq_official",
                umo="qq_official:GroupMessage:123",
            )
        )

        service.config["collection"]["target_groups"] = ["123"]
        self.assertTrue(
            service._configured_target(
                group_key="qq_official:123",
                group_id="123",
                platform_id="qq_official",
                umo="qq_official:GroupMessage:123",
            )
        )

    def test_removed_config_target_does_not_inherit_discovery_state(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        service.store.record_group(
            group_key="onebot:123",
            platform_id="onebot",
            group_id="123",
            umo="onebot:GroupMessage:123",
            enabled=True,
        )
        service.config["collection"]["target_groups"] = []
        self.assertFalse(
            service._configured_target(
                group_key="onebot:123",
                group_id="123",
                platform_id="onebot",
                umo="onebot:GroupMessage:123",
            )
        )

        service.store.set_group_enabled("onebot:123", True)
        self.assertTrue(
            service._configured_target(
                group_key="onebot:123",
                group_id="123",
                platform_id="onebot",
                umo="onebot:GroupMessage:123",
            )
        )

    def test_answer_event_binding_requires_unique_match_or_explicit_evidence(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        first = FakeEvent(content="这个报错怎么处理", message_id="first")
        second = FakeEvent(content="这个报错怎么处理", message_id="second")
        service._group_events["onebot:123"] = {1: first, 2: second}
        selected = [
            {"id": 1, "content": "这个报错怎么处理"},
            {"id": 2, "content": "这个报错怎么处理"},
        ]

        ambiguous = service._answer_event_for_batch(
            "onebot:123",
            {"candidates": []},
            selected,
            "这个报错怎么处理",
        )
        explicit = service._answer_event_for_batch(
            "onebot:123",
            {"candidates": [], "answer_evidence_ids": [1]},
            selected,
            "这个报错怎么处理",
        )

        self.assertIsNone(ambiguous)
        self.assertIs(explicit, first)

    def test_private_manual_submission_is_rejected(self) -> None:
        service = self.make_service(["private:FriendMessage:user-1"])
        event = FakeEvent(
            group_id="",
            platform_id="onebot",
            umo="onebot:FriendMessage:user-1",
        )
        reply = asyncio.run(service.manual_add(event, "这里不应写入群问题库"))
        self.assertEqual(reply, "问题提交仅能在已启用的开发者交流群中使用。")
        self.assertEqual(service.store.list_pending_messages(10), [])

    def test_page_api_unregister_removes_only_owned_handlers(self) -> None:
        context = FakeWebContext()
        plugin = SimpleNamespace(context=context, service=self.make_service([]))
        page_api = BugCompanionPageApi(plugin)
        page_api.register_routes()
        self.assertEqual(len(context.registered_web_apis), 24)

        page_api.unregister_routes()
        self.assertEqual(len(context.registered_web_apis), 1)
        self.assertEqual(context.registered_web_apis[0][0], "/unrelated")

    def test_page_api_registration_failure_restores_previous_routes(self) -> None:
        context = FakeWebContext()
        previous_routes = list(context.registered_web_apis)
        calls = 0
        original_register = context.register_web_api

        def fail_second_route(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated route registration failure")
            return original_register(*args, **kwargs)

        context.register_web_api = fail_second_route
        plugin = SimpleNamespace(context=context, service=self.make_service([]))
        page_api = BugCompanionPageApi(plugin)

        with self.assertRaisesRegex(RuntimeError, "route registration failure"):
            page_api.register_routes()

        self.assertEqual(context.registered_web_apis, previous_routes)
        self.assertEqual(page_api._registered_routes, [])


class RuntimeAsyncScopeTests(RuntimeScopeTestBase, unittest.IsolatedAsyncioTestCase):
    async def test_repository_updates_baseline_then_notify_only_new_items(self) -> None:
        service = self.make_service([])
        service.config["repository_updates"] = {
            "enabled": True,
            "repositories": ["https://github.com/Example/Companion.git"],
            "target_groups": ["aiocqhttp:GroupMessage:123"],
            "interval_minutes": 10,
            "max_items_per_check": 5,
        }
        sent: list[tuple[str, str]] = []

        async def send_message(target, chain):
            sent.append((target, getattr(chain.chain[0], "text", "")))
            return True

        service.context.send_message = send_message
        calls = 0

        async def fake_fetch(repository, max_items):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "commit_ok": True,
                    "release_ok": True,
                    "errors": [],
                    "commits": [{"sha": "c1", "commit": {"message": "初始提交"}}],
                    "releases": [{"id": 1, "tag_name": "v1"}],
                }
            if calls == 2:
                return {
                    "commit_ok": True,
                    "release_ok": True,
                    "errors": [],
                    "commits": [
                        {"sha": "c2", "commit": {"message": "新增功能"}},
                        {"sha": "c1", "commit": {"message": "初始提交"}},
                    ],
                    "releases": [
                        {"id": 2, "tag_name": "v2"},
                        {"id": 1, "tag_name": "v1"},
                    ],
                }
            return {
                "commit_ok": True,
                "release_ok": True,
                "errors": [],
                "commits": [{"sha": "c2", "commit": {"message": "新增功能"}}],
                "releases": [{"id": 2, "tag_name": "v2"}],
            }

        service._fetch_repository_updates = fake_fetch
        first = await service.check_repository_updates()
        second = await service.check_repository_updates()
        third = await service.check_repository_updates()

        self.assertEqual(first["baselined"], 1)
        self.assertEqual(first["sent"], 0)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(second["sent"], 1)
        self.assertEqual(third["updated"], 0)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "aiocqhttp:GroupMessage:123")
        self.assertIn("c2", sent[0][1])
        self.assertIn("v2", sent[0][1])
        self.assertTrue(
            service.store.get_repository_cursor(
                "https://github.com/example/companion"
            )["initialized"]
        )
        await service.close()

    async def test_repository_schedule_starts_only_with_fixed_group_and_can_close(self) -> None:
        service = self.make_service([])
        service.config["repository_updates"] = {
            "enabled": True,
            "repositories": ["https://github.com/example/companion"],
            "target_groups": [],
        }
        await service.refresh_repository_schedule()
        self.assertIsNone(service._repository_task)
        service.config["repository_updates"]["target_groups"] = [
            "aiocqhttp:GroupMessage:123"
        ]
        service._fetch_repository_updates = AsyncMock(
            return_value={
                "commit_ok": False,
                "release_ok": False,
                "errors": ["test"],
                "commits": [],
                "releases": [],
            }
        )
        await service.refresh_repository_schedule()
        self.assertIsNotNone(service._repository_task)
        await service.close()
        self.assertIsNone(service._repository_task)

    async def test_repository_updates_can_add_ai_summary_and_fall_back_cleanly(self) -> None:
        service = self.make_service([])
        service.config["repository_updates"] = {
            "enabled": True,
            "repositories": ["https://github.com/example/companion"],
            "target_groups": ["aiocqhttp:GroupMessage:123"],
            "ai_summary_enabled": True,
            "ai_summary_provider_id": "summary-provider",
            "ai_summary_max_chars": 240,
        }
        sent: list[str] = []

        async def send_message(target, chain):
            sent.append(getattr(chain.chain[0], "text", ""))
            return True

        service.context.send_message = send_message
        service._fetch_repository_updates = AsyncMock(
            side_effect=[
                {
                    "commit_ok": True,
                    "release_ok": True,
                    "errors": [],
                    "commits": [{"sha": "c1", "commit": {"message": "基线"}}],
                    "releases": [],
                },
                {
                    "commit_ok": True,
                    "release_ok": True,
                    "errors": [],
                    "commits": [
                        {"sha": "c2", "commit": {"message": "新增仪表盘"}},
                        {"sha": "c1", "commit": {"message": "基线"}},
                    ],
                    "releases": [],
                },
            ]
        )
        service.organizer.summarize_repository_updates = AsyncMock(
            return_value="新增了可视化仪表盘，便于查看运行状态。"
        )

        await service.check_repository_updates()
        result = await service.check_repository_updates()

        self.assertEqual(result["sent"], 1)
        service.organizer.summarize_repository_updates.assert_awaited_once_with(
            "https://github.com/example/companion",
            [
                {"sha": "c2", "commit": {"message": "新增仪表盘"}},
            ],
            [],
            provider_id="summary-provider",
            max_chars=240,
        )
        self.assertIn("AI 摘要", sent[0])
        self.assertIn("可视化仪表盘", sent[0])
        self.assertIn("新增仪表盘", sent[0])
        await service.close()

    async def test_page_review_and_todo_write_endpoints(self) -> None:
        service = self.make_service([])
        issue = service.store.create_issue(
            {"title": "等待页面批阅"},
            actor="model",
            requires_review=True,
        )
        context = FakeWebContext()
        page_api = BugCompanionPageApi(
            SimpleNamespace(context=context, service=service, config=service.config)
        )
        page_api.register_routes()
        registered = {item[0] for item in context.registered_web_apis}
        self.assertTrue(
            {
                "/astrbot_plugin_bug_companion/page/review/queue",
                "/astrbot_plugin_bug_companion/page/review/decide",
                "/astrbot_plugin_bug_companion/page/review/reopen",
                "/astrbot_plugin_bug_companion/page/todos",
                "/astrbot_plugin_bug_companion/page/todos/create",
                "/astrbot_plugin_bug_companion/page/todos/update",
            }.issubset(registered)
        )
        page_api._payload = AsyncMock(
            side_effect=[
                {
                    "issue_id": issue["id"],
                    "action": "todo",
                    "note": "交办验证",
                    "todo": {"content": "完成回归测试", "due_at": "2026-08-15"},
                    "expected_updated_at": issue["updated_at"],
                },
                {"id": 1, "status": "completed"},
                {
                    "issue_id": issue["id"],
                    "action": "reject",
                    "expected_updated_at": issue["updated_at"],
                },
            ]
        )

        reviewed = await page_api.review_decide()
        completed = await page_api.todo_update()
        conflict = await page_api.review_decide()

        self.assertEqual(reviewed["status"], "ok")
        self.assertEqual(reviewed["data"]["issue"]["review_decision"], "todo")
        self.assertIsNone(reviewed["data"]["issue"]["knowledge_published_at"])
        self.assertEqual(completed["status"], "ok")
        self.assertEqual(completed["data"]["status"], "completed")
        self.assertTrue(completed["data"]["review_reopened"])
        self.assertEqual(conflict["status"], "error")
        self.assertEqual(conflict["data"]["error"], "conflict")
        await service.close()

    async def test_page_review_reopen_and_todo_pagination(self) -> None:
        service = self.make_service([])
        issue = service.store.create_issue(
            {"title": "等待重呈"},
            actor="model",
            requires_review=True,
        )
        decided = service.store.review_decide(
            str(issue["id"]),
            "reject",
            note="稍后重呈",
        )
        for index in range(3):
            service.store.create_todo(str(issue["id"]), f"分页待办 {index}")
        latest_updated_at = service.store.get_issue(str(issue["id"]))["updated_at"]
        page_api = BugCompanionPageApi(
            SimpleNamespace(
                context=FakeWebContext(),
                service=service,
                config=service.config,
            )
        )
        page_api._payload = AsyncMock(
            side_effect=[
                {
                    "issue_id": issue["id"],
                    "expected_updated_at": "2000-01-01T00:00:00+00:00",
                },
                {
                    "issue_id": issue["id"],
                    "expected_updated_at": latest_updated_at,
                },
                {
                    "issue_id": issue["id"],
                    "expected_updated_at": latest_updated_at,
                },
            ]
        )

        conflict = await page_api.review_reopen()
        reopened = await page_api.review_reopen()
        retry = await page_api.review_reopen()
        page_api_module = sys.modules[BugCompanionPageApi.__module__]
        original_request = page_api_module.request
        page_api_module.request = SimpleNamespace(query={"page": "2", "limit": "2"})
        try:
            todo_page = await page_api.todos()
        finally:
            page_api_module.request = original_request

        self.assertEqual(conflict["status"], "error")
        self.assertEqual(conflict["data"]["error"], "conflict")
        self.assertEqual(
            conflict["data"]["issue"]["review_decision"],
            "rejected",
        )
        self.assertEqual(reopened["status"], "ok")
        self.assertIsNone(reopened["data"]["issue"]["reviewed_at"])
        self.assertEqual(retry["status"], "ok")
        self.assertTrue(retry["data"]["idempotent"])
        self.assertEqual(todo_page["status"], "ok")
        self.assertEqual(todo_page["data"]["total"], 3)
        self.assertEqual(todo_page["data"]["open"], 3)
        self.assertEqual(todo_page["data"]["page"], 2)
        self.assertEqual(todo_page["data"]["pages"], 2)
        self.assertEqual(len(todo_page["data"]["items"]), 1)
        await service.close()

    async def test_page_issue_create_accepts_only_a_known_group(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        service.store.record_group(
            group_key="onebot:123",
            platform_id="onebot",
            group_id="123",
            umo="onebot:GroupMessage:123",
            display_name="开发群",
            enabled=True,
        )
        page_api = BugCompanionPageApi(
            SimpleNamespace(context=FakeWebContext(), service=service, config=service.config)
        )
        page_api._payload = AsyncMock(
            return_value={
                "title": "页面提交的问题",
                "group_key": "onebot:123",
                "issue_domain": "plugin",
            }
        )

        created = await page_api.issue_create()

        self.assertEqual(created["status"], "ok")
        self.assertEqual(created["data"]["group_key"], "onebot:123")
        self.assertEqual(created["data"]["issue_domain"], "plugin")

        page_api._payload = AsyncMock(
            return_value={"title": "不应创建", "group_key": "onebot:999"}
        )
        rejected = await page_api.issue_create()

        self.assertEqual(rejected["status"], "error")
        self.assertEqual(service.store.list_issues(page_size=100)["total"], 1)

        page_api._payload = AsyncMock(
            return_value={
                "title": "归属枚举无效",
                "group_key": "onebot:123",
                "issue_domain": "provider",
            }
        )
        invalid_domain = await page_api.issue_create()
        self.assertEqual(invalid_domain["status"], "error")
        self.assertIn("issue_domain", invalid_domain["message"])
        self.assertEqual(service.store.list_issues(page_size=100)["total"], 1)
        await service.close()

    async def test_page_plugin_name_requires_managed_plugin_and_plugin_domain(
        self,
    ) -> None:
        service = self.make_service([])
        service.config["models"] = {
            "managed_plugins": ["astrbot_plugin_private_companion"]
        }
        page_api = BugCompanionPageApi(
            SimpleNamespace(
                context=FakeWebContext(),
                service=service,
                config=service.config,
            )
        )
        page_api._payload = AsyncMock(
            side_effect=[
                {
                    "title": "名单内插件问题",
                    "issue_domain": "plugin",
                    "plugin_name": " astrbot_plugin_private_companion ",
                },
                {
                    "title": "名单外插件问题",
                    "issue_domain": "plugin",
                    "plugin_name": "astrbot_plugin_unknown",
                },
                {
                    "title": "核心问题不能带插件名",
                    "issue_domain": "astrbot",
                    "plugin_name": "astrbot_plugin_private_companion",
                },
                {
                    "title": "插件名称过长",
                    "issue_domain": "plugin",
                    "plugin_name": "x" * 121,
                },
            ]
        )

        created = await page_api.issue_create()
        unmanaged = await page_api.issue_create()
        wrong_domain = await page_api.issue_create()
        too_long = await page_api.issue_create()

        self.assertEqual(created["status"], "ok")
        self.assertEqual(
            created["data"]["plugin_name"],
            "astrbot_plugin_private_companion",
        )
        self.assertEqual(unmanaged["status"], "error")
        self.assertIn("管理名单", unmanaged["message"])
        self.assertEqual(wrong_domain["status"], "error")
        self.assertIn("只有插件问题", wrong_domain["message"])
        self.assertEqual(too_long["status"], "error")
        self.assertIn("120", too_long["message"])
        self.assertEqual(service.store.list_issues(page_size=100)["total"], 1)
        await service.close()

    async def test_managed_plugins_are_normalized_without_rewriting_history(
        self,
    ) -> None:
        service = self.make_service([])
        historical = service.store.create_issue(
            {
                "title": "历史插件问题",
                "issue_domain": "plugin",
                "plugin_name": "astrbot_plugin_private_companion",
            }
        )
        page_api = BugCompanionPageApi(
            SimpleNamespace(
                context=FakeWebContext(),
                service=service,
                config=service.config,
            )
        )
        many = [f" plugin_{index} " for index in range(48)]
        page_api._payload = AsyncMock(
            side_effect=[
                {
                    "models": {
                        "managed_plugins": [
                            " astrbot_plugin_private_companion ",
                            "astrbot_plugin_private_companion",
                            "plugin with spaces",
                            *many,
                        ]
                    }
                },
                {
                    "models": {
                        "managed_plugins": [f"overflow_{index}" for index in range(51)]
                    }
                },
                {"models": {"managed_plugins": []}},
            ]
        )

        saved = await page_api.settings_save()
        rejected = await page_api.settings_save()
        removed = await page_api.settings_save()

        self.assertEqual(saved["status"], "ok")
        self.assertEqual(len(saved["data"]["models"]["managed_plugins"]), 50)
        self.assertEqual(
            saved["data"]["models"]["managed_plugins"][:2],
            ["astrbot_plugin_private_companion", "plugin with spaces"],
        )
        self.assertEqual(rejected["status"], "error")
        self.assertIn("50", rejected["message"])
        self.assertEqual(len(rejected["data"]["models"]["managed_plugins"]), 50)
        self.assertEqual(removed["status"], "ok")
        self.assertEqual(removed["data"]["models"]["managed_plugins"], [])
        self.assertEqual(
            service.store.get_issue(historical["id"])["plugin_name"],
            "astrbot_plugin_private_companion",
        )
        await service.close()

    async def test_settings_save_failure_restores_runtime_configuration(self) -> None:
        class FailingConfig(dict):
            def save_config(self):
                raise RuntimeError("simulated save failure")

        config = FailingConfig(
            {
                "collection": {
                    "enabled": True,
                    "target_groups": ["onebot:GroupMessage:123"],
                    "quiet_seconds": 12,
                }
            }
        )
        service = BugCompanionService(SimpleNamespace(), config, self.data_dir)
        page_api = BugCompanionPageApi(
            SimpleNamespace(context=FakeWebContext(), service=service, config=config)
        )
        page_api._payload = AsyncMock(
            return_value={
                "collection": {
                    "target_groups": ["onebot:GroupMessage:999"],
                    "quiet_seconds": 30,
                }
            }
        )

        result = await page_api.settings_save()

        self.assertEqual(result["status"], "error")
        self.assertEqual(
            config["collection"]["target_groups"],
            ["onebot:GroupMessage:123"],
        )
        self.assertEqual(config["collection"]["quiet_seconds"], 12)
        self.assertIs(service.organizer.config, config)
        await service.close()

    async def test_settings_save_keeps_repository_ai_summary_preferences(self) -> None:
        service = self.make_service([])
        service.refresh_scheduling = AsyncMock()
        page_api = BugCompanionPageApi(
            SimpleNamespace(
                context=FakeWebContext(),
                service=service,
                config=service.config,
            )
        )
        page_api._payload = AsyncMock(
            return_value={
                "repository_updates": {
                    "ai_summary_enabled": True,
                    "ai_summary_provider_id": "summary-provider",
                    "ai_summary_max_chars": 320,
                }
            }
        )

        result = await page_api.settings_save()

        self.assertEqual(result["status"], "ok")
        repository_updates = service.config["repository_updates"]
        self.assertTrue(repository_updates["ai_summary_enabled"])
        self.assertEqual(repository_updates["ai_summary_provider_id"], "summary-provider")
        self.assertEqual(repository_updates["ai_summary_max_chars"], 320)
        self.assertNotIn("ai_summary", repository_updates)
        self.assertTrue(result["data"]["repository_updates"]["ai_summary_enabled"])
        await service.close()

    async def test_settings_save_rejects_stale_revision_without_mutation(self) -> None:
        class TrackingConfig(dict):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.save_calls = 0

            def save_config(self):
                self.save_calls += 1

        config = TrackingConfig(
            {
                "collection": {
                    "enabled": True,
                    "target_groups": ["onebot:GroupMessage:123"],
                    "quiet_seconds": 12,
                }
            }
        )
        service = BugCompanionService(SimpleNamespace(), config, self.data_dir)
        service.refresh_scheduling = AsyncMock()
        page_api = BugCompanionPageApi(
            SimpleNamespace(context=FakeWebContext(), service=service, config=config)
        )
        original_revision = page_api._settings_view()["revision"]
        page_api._payload = AsyncMock(
            side_effect=[
                {
                    "revision": original_revision,
                    "collection": {"quiet_seconds": 20},
                },
                {
                    "revision": original_revision,
                    "collection": {"quiet_seconds": 45},
                },
            ]
        )

        saved = await page_api.settings_save()
        stale = await page_api.settings_save()

        self.assertEqual(saved["status"], "ok")
        self.assertNotEqual(saved["data"]["revision"], original_revision)
        self.assertEqual(stale["status"], "error")
        self.assertIn("其他页面更新", stale["message"])
        self.assertEqual(config["collection"]["quiet_seconds"], 20)
        self.assertEqual(stale["data"]["collection"]["quiet_seconds"], 20)
        self.assertEqual(config.save_calls, 1)
        service.refresh_scheduling.assert_awaited_once()
        await service.close()

    async def test_inbox_reprocess_requires_explicit_all_pending_confirmation(
        self,
    ) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        service.store.record_group(
            group_key="onebot:123",
            platform_id="onebot",
            group_id="123",
            umo="onebot:GroupMessage:123",
            enabled=True,
        )
        raw_id = service.store.add_message(
            {
                "group_key": "onebot:123",
                "message_id": "pending-history",
                "content": "等待人工确认整理的历史记录",
                "source": "qq_history",
                "processing_approved": False,
            }
        )
        service.process_group = AsyncMock(return_value={"ok": True})
        page_api = BugCompanionPageApi(
            SimpleNamespace(context=FakeWebContext(), service=service, config=service.config)
        )
        page_api._payload = AsyncMock(
            return_value={"group_key": "onebot:123", "message_ids": []}
        )

        result = await page_api.inbox_reprocess()

        self.assertEqual(result["status"], "error")
        self.assertIn("显式确认", result["message"])
        service.process_group.assert_not_awaited()
        pending = service.store.list_pending_messages(10)
        self.assertEqual([item["id"] for item in pending], [raw_id])
        self.assertEqual(pending[0]["processing_approved"], 0)
        await service.close()

    async def test_inbox_reprocess_reports_model_failure_as_failure(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        service.store.record_group(
            group_key="onebot:123",
            platform_id="onebot",
            group_id="123",
            umo="onebot:GroupMessage:123",
            enabled=True,
        )
        raw_id = service.store.add_message(
            {
                "group_key": "onebot:123",
                "message_id": "retry-failure",
                "content": "模型本轮整理失败",
                "source": "qq_history",
                "processing_approved": False,
            }
        )
        service.process_group = AsyncMock(
            return_value={"ok": False, "error": "cooldown", "messages": 1}
        )
        page_api = BugCompanionPageApi(
            SimpleNamespace(context=FakeWebContext(), service=service, config=service.config)
        )
        page_api._payload = AsyncMock(
            return_value={
                "group_key": "onebot:123",
                "message_ids": [raw_id],
            }
        )

        result = await page_api.inbox_reprocess()

        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["data"]["ok"])
        self.assertEqual(result["data"]["reset"], 1)
        self.assertIn("等待自动重试", result["data"]["message"])
        service.process_group.assert_awaited_once_with("onebot:123", force=True)
        await service.close()

    async def test_at_or_wake_message_does_not_trigger_duplicate_auto_answer(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        event = FakeEvent(content="@机器人 这个报错以前有人遇到过吗")
        event.is_at_or_wake_command = True

        await service.handle_group_message(event)
        flush_task = service._flush_tasks.pop("onebot:123")
        flush_task.cancel()
        await asyncio.gather(flush_task, return_exceptions=True)

        async def needs_answer(*_args, **_kwargs):
            return {
                "candidates": [],
                "needs_answer": True,
                "answer_query": "这个报错以前有人遇到过吗",
            }

        service.organizer.analyze_batch = needs_answer
        service.answer_query = AsyncMock(return_value={"sent": True})

        result = await service.process_group("onebot:123")

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["answer"],
            {"sent": False, "reason": "handled_by_default_llm"},
        )
        service.answer_query.assert_not_awaited()
        await service.close()

    async def test_inflight_answer_stays_bound_to_selected_message_event(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        query_event = FakeEvent(
            content="这个报错以前有人遇到过吗",
            message_id="question-message",
        )
        await service.handle_group_message(query_event)
        flush_task = service._flush_tasks.pop("onebot:123")
        flush_task.cancel()
        await asyncio.gather(flush_task, return_exceptions=True)

        started = asyncio.Event()
        release = asyncio.Event()

        async def needs_answer(*_args, **_kwargs):
            started.set()
            await release.wait()
            return {
                "candidates": [],
                "needs_answer": True,
                "answer_query": "这个报错以前有人遇到过吗",
            }

        service.organizer.analyze_batch = needs_answer
        service.answer_query = AsyncMock(return_value={"sent": True})
        worker = asyncio.create_task(service.process_group("onebot:123"))
        service._flush_tasks["onebot:123"] = worker
        await asyncio.wait_for(started.wait(), timeout=2)

        later_event = FakeEvent(
            content="整理模型运行期间出现的无关新消息",
            message_id="later-message",
        )
        later_event.is_at_or_wake_command = True
        await service.handle_group_message(later_event)
        release.set()
        result = await asyncio.wait_for(worker, timeout=2)

        self.assertTrue(result["ok"])
        service.answer_query.assert_awaited_once()
        self.assertIs(service.answer_query.await_args.args[0], query_event)
        await service.close()

    async def test_automatic_answer_rechecks_delivery_authorization_after_model_call(self) -> None:
        references = [
            {
                "id": "BUG-1",
                "title": "问题一",
                "category": "faq",
                "status": "resolved",
                "verification_status": "verified",
                "solution": "方案一",
                "review_decision": "approved",
                "knowledge_published_at": "2026-07-31T00:00:00+00:00",
            }
        ]
        cases = {
            "group_disabled": "群聊已停用",
            "collection_disabled": "问题收集已关闭",
            "auto_answer_disabled": "自动答疑已关闭",
            "service_closed": "插件已停止",
        }
        for mode, expected_reason in cases.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                service = BugCompanionService(
                    SimpleNamespace(),
                    {
                        "collection": {
                            "enabled": True,
                            "target_groups": ["onebot:GroupMessage:123"],
                        },
                        "answers": {
                            "auto_answer": True,
                            "answer_min_confidence": 72,
                            "group_cooldown_seconds": 0,
                        },
                    },
                    Path(temp_dir),
                )
                service.store.similar_issues = lambda *_args, **_kwargs: references
                started = asyncio.Event()
                release = asyncio.Event()

                async def draft_answer(*_args, **_kwargs):
                    started.set()
                    await release.wait()
                    return {
                        "answer": "使用方案一。",
                        "confidence": 0.95,
                        "issue_ids": ["BUG-1"],
                    }

                service.organizer.draft_answer = draft_answer
                event = FakeEvent()
                task = asyncio.create_task(
                    service.answer_query(event, "如何处理", automatic=True)
                )
                await asyncio.wait_for(started.wait(), timeout=2)
                if mode == "group_disabled":
                    service.store.set_group_enabled("onebot:123", False)
                elif mode == "collection_disabled":
                    service.config["collection"]["enabled"] = False
                elif mode == "auto_answer_disabled":
                    service.config["answers"]["auto_answer"] = False
                else:
                    await service.close()
                release.set()
                result = await asyncio.wait_for(task, timeout=2)

                self.assertFalse(result["sent"])
                self.assertIn(expected_reason, result["reason"])
                self.assertEqual(event.sent_results, [])
                if not service._closed:
                    await service.close()

    async def test_start_resumes_persisted_pending_queue(self) -> None:
        config = {
            "collection": {
                "enabled": True,
                "target_groups": ["onebot:GroupMessage:123"],
            }
        }
        first = BugCompanionService(SimpleNamespace(), config, self.data_dir)
        first.store.record_group(
            group_key="onebot:123",
            platform_id="onebot",
            group_id="123",
            umo="onebot:GroupMessage:123",
            enabled=True,
        )
        first.store.add_message(
            {
                "group_key": "onebot:123",
                "platform_id": "onebot",
                "group_id": "123",
                "umo": "onebot:GroupMessage:123",
                "message_id": "restart-pending",
                "sender_id": "user-1",
                "sender_name": "测试用户",
                "content": "重载后仍需整理的问题",
            }
        )
        await first.close()

        recovered = BugCompanionService(SimpleNamespace(), config, self.data_dir)
        recovered.organizer.analyze_batch = AsyncMock(
            return_value={"candidates": [], "needs_answer": False, "answer_query": ""}
        )
        await recovered.start()
        recovery_task = recovered._flush_tasks.get("onebot:123")

        self.assertIsInstance(recovery_task, asyncio.Task)
        await asyncio.wait_for(recovery_task, timeout=3)
        self.assertEqual(recovered.store.pending_messages("onebot:123", 10), [])
        await recovered.close()

    async def test_start_keeps_unapproved_history_local(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        service.store.record_group(
            group_key="onebot:123",
            platform_id="onebot",
            group_id="123",
            umo="onebot:GroupMessage:123",
            enabled=True,
        )
        service.store.add_message(
            {
                "group_key": "onebot:123",
                "platform_id": "onebot",
                "group_id": "123",
                "umo": "onebot:GroupMessage:123",
                "message_id": "staged-only",
                "sender_id": "user-1",
                "sender_name": "测试用户",
                "content": "默认暂存的 QQ 历史",
                "source": "qq_history",
                "processing_approved": False,
            }
        )
        service.organizer.analyze_batch = AsyncMock()

        await service.start()

        self.assertNotIn("onebot:123", service._flush_tasks)
        service.organizer.analyze_batch.assert_not_awaited()
        self.assertEqual(service.store.pending_messages("onebot:123", 10), [])
        self.assertEqual(len(service.store.list_pending_messages(10)), 1)
        await service.close()

    async def test_scheduling_stops_when_collection_is_disabled_and_resumes_later(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        service.store.record_group(
            group_key="onebot:123",
            platform_id="onebot",
            group_id="123",
            umo="onebot:GroupMessage:123",
            enabled=True,
        )
        service.store.add_message(
            {
                "group_key": "onebot:123",
                "platform_id": "onebot",
                "group_id": "123",
                "umo": "onebot:GroupMessage:123",
                "message_id": "toggle-pending",
                "sender_id": "user-1",
                "sender_name": "测试用户",
                "content": "切换开关期间保留的待整理问题",
            }
        )
        service.organizer.analyze_batch = AsyncMock(
            return_value={"candidates": [], "needs_answer": False, "answer_query": ""}
        )

        await service.start()
        first_task = service._flush_tasks.get("onebot:123")
        self.assertIsInstance(first_task, asyncio.Task)

        service.config["collection"]["enabled"] = False
        await service.refresh_scheduling()

        self.assertTrue(first_task.cancelled())
        self.assertNotIn("onebot:123", service._flush_tasks)
        self.assertEqual(len(service.store.pending_messages("onebot:123", 10)), 1)
        service.organizer.analyze_batch.assert_not_awaited()

        blocked = await service.process_group("onebot:123", force=True)
        self.assertEqual(blocked["error"], "scope_disabled")

        service.config["collection"]["enabled"] = True
        await service.refresh_scheduling()
        resumed_task = service._flush_tasks.get("onebot:123")
        self.assertIsInstance(resumed_task, asyncio.Task)
        await asyncio.wait_for(resumed_task, timeout=3)

        service.organizer.analyze_batch.assert_awaited_once()
        self.assertEqual(service.store.pending_messages("onebot:123", 10), [])
        await service.close()

    async def test_answer_cooldown_is_restored_from_persistent_audit(self) -> None:
        config = {
            "collection": {
                "enabled": True,
                "target_groups": ["onebot:GroupMessage:123"],
            },
            "answers": {
                "auto_answer": True,
                "answer_min_confidence": 72,
                "group_cooldown_seconds": 120,
            },
        }
        first = BugCompanionService(SimpleNamespace(), config, self.data_dir)
        first.store.record_answer(
            group_key="onebot:123",
            user_id="user-1",
            user_name="测试用户",
            query="第一次答疑",
            issue_ids=["BUG-1"],
            answer="方案一",
            confidence=0.95,
            sent=True,
        )
        await first.close()

        recovered = BugCompanionService(SimpleNamespace(), config, self.data_dir)
        recovered.store.similar_issues = lambda *_args, **_kwargs: [
            {
                "id": "BUG-1",
                "title": "问题一",
                "category": "faq",
                "status": "resolved",
                "verification_status": "verified",
                "solution": "方案一",
                "review_decision": "approved",
                "knowledge_published_at": "2026-07-31T00:00:00+00:00",
            }
        ]
        recovered.organizer.draft_answer = AsyncMock(
            return_value={
                "answer": "方案一",
                "confidence": 0.95,
                "issue_ids": ["BUG-1"],
            }
        )

        result = await recovered.answer_query(FakeEvent(), "再次询问", automatic=True)

        self.assertFalse(result["sent"])
        self.assertIn("同群答疑冷却中", result["reason"])
        self.assertGreater(recovered._answer_last_sent["onebot:123"], 0)
        await recovered.close()

    async def test_unauthorized_group_event_leaves_no_group_or_message_record(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        await service.handle_group_message(
            FakeEvent(
                group_id="123",
                platform_id="qq_official",
                umo="qq_official:GroupMessage:123",
                content="这个未授权群的内容不应被保存",
            )
        )

        self.assertEqual(service.store.list_groups(), [])
        self.assertEqual(service.store.list_pending_messages(10), [])
        self.assertEqual(service.store.pending_messages("qq_official:123", 10), [])
        self.assertNotIn("qq_official:123", service._group_events)
        self.assertNotIn("qq_official:123", service._flush_tasks)
        await service.close()

    async def test_new_message_does_not_cancel_inflight_organizer(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        service.store.record_group(
            group_key="onebot:123",
            platform_id="onebot",
            group_id="123",
            umo="onebot:GroupMessage:123",
            enabled=True,
        )
        service.store.add_message(
            {
                "group_key": "onebot:123",
                "platform_id": "onebot",
                "group_id": "123",
                "umo": "onebot:GroupMessage:123",
                "message_id": "message-1",
                "sender_id": "user-1",
                "sender_name": "测试用户",
                "content": "第一条问题证据",
            }
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_analysis(*_args, **_kwargs):
            started.set()
            await release.wait()
            return {"candidates": [], "needs_answer": False, "answer_query": ""}

        service.organizer.analyze_batch = slow_analysis
        worker = asyncio.create_task(service.process_group("onebot:123"))
        service._flush_tasks["onebot:123"] = worker
        await asyncio.wait_for(started.wait(), timeout=2)

        await service.handle_group_message(
            FakeEvent(content="模型整理期间追加的第二条证据", message_id="message-2")
        )
        self.assertFalse(worker.cancelled())
        self.assertFalse(worker.done())

        release.set()
        result = await asyncio.wait_for(worker, timeout=2)
        self.assertTrue(result["ok"])
        self.assertEqual(
            len(service.store.pending_messages("onebot:123", limit=10)),
            1,
        )
        await service.close()

    async def test_debounced_worker_retries_when_group_is_already_processing(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        service.store.record_group(
            group_key="onebot:123",
            platform_id="onebot",
            group_id="123",
            umo="onebot:GroupMessage:123",
            enabled=True,
        )
        service.store.add_message(
            {
                "group_key": "onebot:123",
                "platform_id": "onebot",
                "group_id": "123",
                "umo": "onebot:GroupMessage:123",
                "message_id": "message-busy",
                "sender_id": "user-1",
                "sender_name": "测试用户",
                "content": "整理冲突后仍需自动处理的证据",
            }
        )
        service._processing.add("onebot:123")
        first_worker = asyncio.create_task(service._debounced_flush("onebot:123", 0))
        service._flush_tasks["onebot:123"] = first_worker

        await asyncio.wait_for(first_worker, timeout=2)
        retry_worker = service._flush_tasks.get("onebot:123")
        self.assertIsInstance(retry_worker, asyncio.Task)
        self.assertIsNot(retry_worker, first_worker)
        self.assertFalse(retry_worker.done())

        service._processing.discard("onebot:123")
        service.organizer.analyze_batch = AsyncMock(
            return_value={"candidates": [], "needs_answer": False, "answer_query": ""}
        )
        await asyncio.wait_for(retry_worker, timeout=3)
        self.assertEqual(service.store.pending_messages("onebot:123", limit=10), [])
        await service.close()

    async def test_evidence_linked_message_is_acknowledged_when_context_retention_is_off(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        service.config["retention"] = {"store_non_candidate_context": False}
        service.store.record_group(
            group_key="onebot:123",
            platform_id="onebot",
            group_id="123",
            umo="onebot:GroupMessage:123",
            enabled=True,
        )
        raw_id = service.store.add_message(
            {
                "group_key": "onebot:123",
                "platform_id": "onebot",
                "group_id": "123",
                "umo": "onebot:GroupMessage:123",
                "message_id": "evidence-message",
                "sender_id": "user-1",
                "sender_name": "测试用户",
                "content": "已经归档为证据的消息",
            }
        )
        issue = service.store.create_issue(
            {"title": "已有问题", "group_key": "onebot:123"},
            evidence=[
                {
                    "id": raw_id,
                    "message_id": "evidence-message",
                    "sender_id": "user-1",
                    "sender_name": "测试用户",
                    "content": "已经归档为证据的消息",
                }
            ],
        )
        service.store.reset_messages([raw_id])
        service.organizer.analyze_batch = AsyncMock(
            return_value={"candidates": [], "needs_answer": False, "answer_query": ""}
        )

        result = await service.process_group("onebot:123", force=True)

        self.assertTrue(result["ok"])
        self.assertEqual(service.store.pending_messages("onebot:123", limit=10), [])
        self.assertEqual(service.store.get_issue(issue["id"])["evidence"][0]["raw_message_id"], raw_id)
        self.assertNotIn("onebot:123", service._flush_tasks)
        await service.close()

    async def test_answer_audit_uses_model_selected_references_and_command_delivery(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        references = [
            {
                "id": "BUG-1",
                "title": "问题一",
                "category": "faq",
                "status": "resolved",
                "verification_status": "verified",
                "solution": "方案一",
                "review_decision": "approved",
                "knowledge_published_at": "2026-07-31T00:00:00+00:00",
            },
            {
                "id": "BUG-2",
                "title": "问题二",
                "category": "faq",
                "status": "resolved",
                "verification_status": "verified",
                "solution": "方案二",
                "review_decision": "approved",
                "knowledge_published_at": "2026-07-31T00:00:00+00:00",
            },
        ]
        service.store.similar_issues = lambda *_args, **_kwargs: references
        service.organizer.draft_answer = AsyncMock(
            return_value={
                "answer": "使用方案二。",
                "confidence": 0.45,
                "issue_ids": ["BUG-2"],
                "reason": "第二条记录匹配",
            }
        )

        result = await service.answer_query(
            FakeEvent(),
            "如何处理",
            automatic=False,
            command_result=True,
        )

        self.assertTrue(result["sent"])
        self.assertEqual([item["id"] for item in result["references"]], ["BUG-2"])
        audit = service.store.list_answers(1)[0]
        self.assertTrue(audit["sent"])
        self.assertEqual(audit["issue_ids"], ["BUG-2"])
        await service.close()

    async def test_automatic_answer_cooldown_has_an_explicit_audit_reason(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        references = [
            {
                "id": "BUG-1",
                "title": "问题一",
                "category": "faq",
                "status": "resolved",
                "verification_status": "verified",
                "solution": "方案一",
                "review_decision": "approved",
                "knowledge_published_at": "2026-07-31T00:00:00+00:00",
            }
        ]
        service.store.similar_issues = lambda *_args, **_kwargs: references
        service.organizer.draft_answer = AsyncMock(
            return_value={
                "answer": "使用方案一。",
                "confidence": 0.95,
                "issue_ids": ["BUG-1"],
                "reason": "历史记录匹配",
            }
        )
        service._answer_last_sent["onebot:123"] = time.time()

        result = await service.answer_query(FakeEvent(), "如何处理", automatic=True)

        self.assertFalse(result["sent"])
        self.assertIn("同群答疑冷却中", result["reason"])
        self.assertNotIn("低于自动答疑阈值", result["reason"])
        self.assertIn("同群答疑冷却中", service.store.list_answers(1)[0]["reason"])
        await service.close()

    async def test_model_error_reply_is_generic_and_diagnostics_are_redacted(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])

        async def fail_analysis(*_args, **_kwargs):
            raise RuntimeError(
                '401 {"api_key":"top-secret","access_token":"oauth-secret"} '
                "Authorization: Bearer bearer-secret sk-abcdefghijklmnop "
                "https://db-user:db-password@example.invalid/api"
            )

        service.organizer.analyze_batch = fail_analysis
        reply = await service.manual_add(
            FakeEvent(),
            "模型错误时仍要保留这条原始证据",
        )
        self.assertEqual(
            reply,
            "已保存原始问题，但模型整理暂未完成。可稍后在拓展页“待整理”中重试。",
        )
        self.assertNotIn("top-secret", reply)
        self.assertNotIn("oauth-secret", reply)
        self.assertNotIn("bearer-secret", reply)

        diagnostics = service.store.pending_messages("onebot:123", limit=10)[0][
            "process_error"
        ]
        self.assertNotIn("top-secret", diagnostics)
        self.assertNotIn("oauth-secret", diagnostics)
        self.assertNotIn("bearer-secret", diagnostics)
        self.assertNotIn("abcdefghijklmnop", diagnostics)
        self.assertNotIn("db-password", diagnostics)
        self.assertGreaterEqual(diagnostics.count("<redacted>"), 3)
        self.assertNotIn("top-secret", safe_error('{"password": "top-secret"}'))
        await service.close()

    async def test_partial_candidate_failure_rolls_back_the_whole_batch(self) -> None:
        service = self.make_service(["onebot:GroupMessage:123"])
        try:
            service.store.record_group(
                group_key="onebot:123",
                platform_id="onebot",
                group_id="123",
                umo="onebot:GroupMessage:123",
                enabled=True,
            )
            message_ids = []
            for index in (1, 2):
                message_ids.append(
                    service.store.add_message(
                        {
                            "group_key": "onebot:123",
                            "platform_id": "onebot",
                            "group_id": "123",
                            "umo": "onebot:GroupMessage:123",
                            "message_id": f"batch-message-{index}",
                            "sender_id": "user-1",
                            "sender_name": "测试用户",
                            "content": f"第 {index} 个问题候选",
                        }
                    )
                )

            async def two_candidates(*_args, **_kwargs):
                return {
                    "candidates": [
                        {"title": "候选一", "evidence_ids": [message_ids[0]]},
                        {"title": "候选二", "evidence_ids": [message_ids[1]]},
                    ],
                    "needs_answer": False,
                    "answer_query": "",
                }

            service.organizer.analyze_batch = two_candidates
            original_create_issue = service.store.create_issue
            create_calls = 0

            def fail_second_candidate(*args, **kwargs):
                nonlocal create_calls
                create_calls += 1
                if create_calls == 2:
                    raise RuntimeError("simulated second candidate failure")
                return original_create_issue(*args, **kwargs)

            service.store.create_issue = fail_second_candidate
            first = await service.process_group("onebot:123", force=True)
            service.store.create_issue = original_create_issue

            self.assertFalse(first["ok"])
            self.assertEqual(service.store.list_issues(page_size=100)["total"], 0)
            pending = service.store.pending_messages("onebot:123", limit=10)
            self.assertEqual(len(pending), 2)
            self.assertTrue(
                all("simulated second candidate failure" in item["process_error"] for item in pending)
            )

            second = await service.process_group("onebot:123", force=True)
            self.assertTrue(second["ok"])
            self.assertEqual(second["created"], 2)
            self.assertEqual(service.store.list_issues(page_size=100)["total"], 2)
            self.assertEqual(service.store.pending_messages("onebot:123", limit=10), [])
            self.assertEqual(
                {item["title"] for item in service.store.list_issues(page_size=100)["items"]},
                {"候选一", "候选二"},
            )
        finally:
            await service.close()


if __name__ == "__main__":
    unittest.main()
