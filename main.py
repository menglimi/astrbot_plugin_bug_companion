# -*- coding: utf-8 -*-
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.event.filter import PermissionType, permission_type
from astrbot.api.star import Context, Star, StarTools, register

from .qq_history import QQGroupHistoryImporter, QQHistoryImportError
from .service import BugCompanionService


PLUGIN_NAME = "astrbot_plugin_bug_companion"
PLUGIN_VERSION = "0.3.5"


@register(
    "BugCompanion",
    "menglimi",
    "开发者群问题收集、人工审批、待办追踪、仓库动态播报与已审批知识答疑插件。",
    PLUGIN_VERSION,
    "https://github.com/menglimi/astrbot_plugin_bug_companion",
)
class BugCompanionPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any]):
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        data_dir.mkdir(parents=True, exist_ok=True)
        self.service = BugCompanionService(context, self.config, data_dir)
        self.qq_history_importer = QQGroupHistoryImporter(
            self.service.store,
            data_dir,
            logger=logger,
        )
        self.page_api = None
        self._register_page_api()
        logger.info("[BugCompanion] 我会陪你修 Bug 已启动，数据目录=%s", data_dir)

    async def initialize(self) -> None:
        await self.service.start()

    def _register_page_api(self) -> None:
        if not callable(getattr(self.context, "register_web_api", None)):
            logger.warning("[BugCompanion] 当前 AstrBot 不支持插件拓展页 API")
            return
        try:
            from .page_api import BugCompanionPageApi

            self.page_api = BugCompanionPageApi(self)
            self.page_api.register_routes()
        except Exception as exc:
            self.page_api = None
            logger.warning("[BugCompanion] 拓展页 API 注册失败: %s", exc, exc_info=True)

    def _enabled_group_key(self, event: AstrMessageEvent) -> str:
        group_key, group_id, platform_id, umo, _ = self.service._event_group(event)
        if not group_id:
            return ""
        enabled = self.service._configured_target(
            group_key=group_key,
            group_id=group_id,
            platform_id=platform_id,
            umo=umo,
        )
        return group_key if enabled else ""

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=1000)
    async def collect_group_message(self, event: AstrMessageEvent):
        """Silently collect configured developer-group messages without stopping normal handlers."""
        await self.service.handle_group_message(event)

    @filter.command_group("修bug", alias={"bug", "修Bug"})
    def bug_commands(self):
        """开发者问题收集和答疑。"""
        pass

    @bug_commands.command("提交", alias={"report", "add"}, priority=10)
    async def cmd_bug_submit(
        self,
        event: AstrMessageEvent,
        content: str = "",
    ) -> AsyncGenerator[MessageEventResult, None]:
        yield event.plain_result(await self.service.manual_add(event, content))

    @bug_commands.command("答疑", alias={"ask", "search"}, priority=10)
    async def cmd_bug_answer(
        self,
        event: AstrMessageEvent,
        query: str = "",
    ) -> AsyncGenerator[MessageEventResult, None]:
        result = await self.service.answer_query(
            event,
            query,
            automatic=False,
            command_result=True,
        )
        yield event.plain_result(str(result.get("answer") or "暂时无法生成答复。"))

    @bug_commands.command("状态", alias={"status"}, priority=10)
    async def cmd_bug_status(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        group_key = self._enabled_group_key(event)
        if not group_key:
            yield event.plain_result("问题状态仅在已启用的开发者交流群中可查看。")
            return
        overview = self.service.store.overview(group_key)
        yield event.plain_result(
            "我会陪你修 Bug\n"
            f"当前群问题总数：{overview['total']}\n"
            f"处理中：{overview['open']}\n"
            f"已解决：{overview['resolved']}\n"
            f"待整理消息：{overview['pending_messages']}\n"
            "详细管理请打开 AstrBot 拓展页「修Bug工作台」。"
        )

    @bug_commands.command("列表", alias={"list"}, priority=10)
    async def cmd_bug_list(
        self,
        event: AstrMessageEvent,
        limit: int = 8,
    ) -> AsyncGenerator[MessageEventResult, None]:
        group_key = self._enabled_group_key(event)
        if not group_key:
            yield event.plain_result("问题列表仅在已启用的开发者交流群中可查看。")
            return
        result = self.service.store.list_issues(
            status="pending,triaged,in_progress",
            group_key=group_key,
            page=1,
            page_size=max(1, min(int(limit), 15)),
        )
        if not result["items"]:
            yield event.plain_result("当前没有待处理问题。")
            return
        lines = ["待处理问题："]
        for issue in result["items"]:
            lines.append(f"{issue['id']} · {issue['title']} · {issue['status']}")
        yield event.plain_result("\n".join(lines))

    @permission_type(PermissionType.ADMIN)
    @bug_commands.command("启用当前群", alias={"enable"}, priority=10)
    async def cmd_bug_enable_group(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        group_key, group_id, platform_id, umo, _ = self.service._event_group(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return
        self.service.store.record_group(
            group_key=group_key,
            platform_id=platform_id,
            group_id=group_id,
            umo=umo,
            enabled=True,
        )
        self.service.store.set_group_enabled(group_key, True)
        await self.service.refresh_scheduling()
        yield event.plain_result("已将当前群加入开发者问题收集范围。")

    @permission_type(PermissionType.ADMIN)
    @bug_commands.command("停用当前群", alias={"disable"}, priority=10)
    async def cmd_bug_disable_group(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        group_key, group_id, _, _, _ = self.service._event_group(event)
        if not group_id:
            yield event.plain_result("该命令只能在群聊中使用。")
            return
        self.service.store.set_group_enabled(group_key, False)
        await self.service.refresh_scheduling()
        yield event.plain_result("已停止收集当前群的新问题，历史记录仍会保留。")

    @permission_type(PermissionType.ADMIN)
    @bug_commands.command("导入群历史", alias={"导入历史", "import_history"}, priority=10)
    async def cmd_bug_import_group_history(
        self,
        event: AstrMessageEvent,
        days: int = 30,
        limit: int = 5000,
        action: str = "",
    ) -> AsyncGenerator[MessageEventResult, None]:
        group_key = self._enabled_group_key(event)
        if not group_key:
            yield event.plain_result("群历史导入仅能由管理员在已启用的开发者交流群中执行。")
            return
        normalized_action = str(action or "").strip().lower()
        organize_now = normalized_action in {"整理", "立即整理", "organize", "process"}
        if normalized_action and normalized_action not in {
            "暂存",
            "仅导入",
            "stage",
            "整理",
            "立即整理",
            "organize",
            "process",
        }:
            yield event.plain_result(
                "用法：/修bug 导入群历史 [天数] [上限] [暂存|整理]\n"
                "默认只暂存真实 QQ 历史，不会自动交给整理模型。"
            )
            return
        try:
            result = await self.qq_history_importer.import_from_event(
                event,
                days=days,
                max_count=limit,
                approve_processing=organize_now,
            )
        except (QQHistoryImportError, TypeError, ValueError) as exc:
            logger.warning("[BugCompanion] QQ 群历史导入失败 group=%s: %s", group_key, exc)
            yield event.plain_result(str(exc) or "QQ 群历史导入失败，请检查当前协议端是否支持历史接口。")
            return
        except Exception as exc:
            logger.warning("[BugCompanion] QQ 群历史导入异常 group=%s: %s", group_key, exc, exc_info=True)
            yield event.plain_result("QQ 群历史已停止导入，已成功暂存的内容不会丢失；请查看日志中的一次性错误摘要。")
            return

        lines = [
            f"QQ 群历史导入完成：读取 {result.fetched} 条，新增 {result.stored} 条，跳过重复 {result.duplicates} 条。",
            f"来源：真实 QQ 历史（{result.adapter_mode}，{result.pages} 页）",
            f"导入批次：{result.run_id}",
        ]
        if result.truncated:
            lines.append("已达到本次条数上限；需要更早记录时，可提高上限后再次导入，重复消息会自动跳过。")
        if organize_now and result.fetched:
            process_result = await self.service.process_group(group_key, force=True)
            if process_result.get("ok"):
                lines.append(
                    f"已开始整理：首批处理 {process_result.get('messages', 0)} 条，"
                    f"生成 {process_result.get('created', 0)} 个候选；剩余批次会继续处理。"
                )
            else:
                lines.append("历史已经安全暂存，但本次整理模型未完成；可稍后使用“/修bug 重新整理”。")
        else:
            lines.append("当前仅暂存到“待整理”，没有把聊天记录发送给整理模型。")
        yield event.plain_result("\n".join(lines))

    @permission_type(PermissionType.ADMIN)
    @bug_commands.command("重新整理", alias={"reprocess"}, priority=10)
    async def cmd_bug_reprocess(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        group_key = self._enabled_group_key(event)
        if not group_key:
            yield event.plain_result("重新整理仅能在已启用的开发者交流群中使用。")
            return
        self.service.store.approve_pending_group(group_key)
        result = await self.service.process_group(group_key, force=True)
        if result.get("ok"):
            yield event.plain_result(
                f"整理完成：处理 {result.get('messages', 0)} 条消息，生成 {result.get('created', 0)} 个问题候选。"
            )
        else:
            yield event.plain_result("暂时无法完成整理，原始证据已保留。请稍后在拓展页“待整理”中重试。")

    @bug_commands.command("帮助", alias={"help"}, priority=10)
    async def cmd_bug_help(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[MessageEventResult, None]:
        yield event.plain_result(
            "/修bug 提交 <问题描述>\n"
            "/修bug 答疑 <现象或问题>\n"
            "/修bug 状态\n"
            "/修bug 列表 [数量]\n"
            "管理员：/修bug 启用当前群、停用当前群、重新整理\n"
            "管理员：/修bug 导入群历史 [天数] [上限] [暂存|整理]\n"
            "问题编辑、合并、批量处理、导出和群配置请使用拓展页「修Bug工作台」。"
        )

    async def terminate(self):
        try:
            if self.page_api is not None:
                self.page_api.unregister_routes()
        finally:
            await self.service.close()
        logger.info("[BugCompanion] 我会陪你修 Bug 已停止")
