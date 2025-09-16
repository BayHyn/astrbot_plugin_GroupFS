import asyncio
import os
import datetime
from typing import List, Dict, Optional
import zipfile
import chardet

import aiohttp
import croniter

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.api.message_components import Node
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from aiocqhttp.exceptions import ActionFailed

# 从 utils.py 导入辅助函数和常量
from . import utils

@register(
    "astrbot_plugin_GroupFS",
    "Foolllll",
    "管理QQ群文件",
    "0.8",
    "https://github.com/Foolllll-J/astrbot_plugin_GroupFS"
)
class GroupFSPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict] = None):
        super().__init__(context)
        self.config = config if config else {}
        self.group_whitelist: List[int] = [int(g) for g in self.config.get("group_whitelist", [])]
        self.admin_users: List[int] = [int(u) for u in self.config.get("admin_users", [])]
        self.preview_length: int = self.config.get("preview_length", 300)
        self.storage_limits: Dict[int, Dict] = {}
        self.cron_tasks = []
        self.last_cron_check_time: Dict[str, datetime.datetime] = {}
        self.bot = None
        self.forward_threshold: int = self.config.get("forward_threshold", 0)
        self.running_tasks = set()
        self.scheduler_lock = asyncio.Lock()
        
        # === 新增 Bot QQ号配置项 ===
        self.bot_qq_id = self.config.get("bot_qq_id")

        # === 新增 ZIP 预览相关配置项 ===
        self.enable_zip_preview: bool = self.config.get("enable_zip_preview", False)
        self.default_zip_password: str = self.config.get("default_zip_password", "")
        self.download_semaphore = asyncio.Semaphore(5)

        # 解析容量监控配置
        limit_configs = self.config.get("storage_limits", [])
        for item in limit_configs:
            try:
                group_id_str, count_limit_str, space_limit_str = item.split(':')
                group_id = int(group_id_str)
                self.storage_limits[group_id] = { "count_limit": int(count_limit_str), "space_limit_gb": float(space_limit_str) }
            except ValueError as e:
                logger.error(f"解析 storage_limits 配置 '{item}' 时出错: {e}，已跳过。")
        
        # 解析定时任务配置
        cron_configs = self.config.get("scheduled_check_tasks", [])
        for item in cron_configs:
            try:
                group_id_str, cron_str = item.split(':', 1)
                group_id = int(group_id_str)
                if not croniter.croniter.is_valid(cron_str):
                    raise ValueError(f"无效的 cron 表达式: {cron_str}")
                task_key = f"{group_id}:{cron_str}"
                self.cron_tasks.append((task_key, group_id, cron_str))
            except ValueError as e:
                logger.error(f"解析 scheduled_check_tasks 配置 '{item}' 时出错: {e}，已跳过。")
        
        logger.info("插件 [群文件系统GroupFS] 已加载。")

    async def initialize(self):
        # 延迟初始化，等待bot连接成功
        asyncio.create_task(self._delayed_start_scheduler())

    async def _delayed_start_scheduler(self):
        """延迟启动调度器，给系统时间初始化"""
        try:
            # 等待10秒让系统完全初始化
            await asyncio.sleep(10)
            if self.cron_tasks:
                logger.info("[定时任务] 启动失效文件检查循环...")
                asyncio.create_task(self.scheduled_check_loop())
        except Exception as e:
            logger.error(f"延迟启动调度器失败: {e}", exc_info=True)

    def _get_bot(self) -> Optional[object]:
        """
        获取并更新bot实例。
        优先从 self.context 获取，如果配置了 bot_qq_id，则通过它匹配。
        """
        if self.bot is None:
            if self.context and hasattr(self.context, "bot") and self.context.bot:
                # 检查bot_qq_id是否匹配
                if self.bot_qq_id and str(self.context.bot.self_id) != str(self.bot_qq_id):
                    logger.warning(f"配置的 bot_qq_id ({self.bot_qq_id}) 与 AstrBot 上下文中的bot不匹配 ({self.context.bot.self_id})，请检查配置。")
                    return None
                
                self.bot = self.context.bot
                logger.info(f"[Bot实例] 成功从 context 中获取bot实例 ({self.bot.self_id})。")
            else:
                logger.warning("[Bot实例] 无法从 context 获取bot实例，可能尚未连接。")
        return self.bot
    
    async def _send_or_forward(self, event: AstrMessageEvent, text: str, name: str = "GroupFS"):
        if self.forward_threshold > 0 and len(text) > self.forward_threshold:
            group_id = int(event.get_group_id())
            logger.info(f"[{group_id}] 检测到长消息 (长度: {len(text)} > {self.forward_threshold})，将自动合并转发。")
            try:
                forward_node = Node(uin=event.get_self_id(), name=name, content=[Comp.Plain(text)])
                await event.send(MessageChain([forward_node]))
            except Exception as e:
                logger.error(f"[{group_id}] 合并转发长消息时出错: {e}", exc_info=True)
                await event.send(MessageChain([Comp.Plain(text[:self.forward_threshold] + "... (消息过长且合并转发失败)")]))
        else:
            await event.send(MessageChain([Comp.Plain(text)]))

    async def scheduled_check_loop(self):
        await asyncio.sleep(10)
        while True:
            now = datetime.datetime.now()
            await asyncio.sleep(60 - now.second)
            
            async with self.scheduler_lock:
                now_aligned = datetime.datetime.now().replace(second=0, microsecond=0)
                for task_key, group_id, cron_str in self.cron_tasks:
                    if croniter.croniter.match(cron_str, now_aligned):
                        if task_key in self.running_tasks:
                            logger.warning(f"[{group_id}] [定时任务] 检测到上一个任务 '{task_key}' 仍在运行，本次触发已跳过。")
                            continue
                        logger.info(f"[{group_id}] [定时任务] Cron 表达式 '{cron_str}' 已触发，开始执行。")
                        self.running_tasks.add(task_key)
                        task = asyncio.ensure_future(self._perform_batch_check_for_cron(group_id))
                        task.add_done_callback(lambda t, key=task_key: self.running_tasks.remove(key))

    async def _perform_batch_check_for_cron(self, group_id: int):
        bot = self._get_bot()
        if not bot:
            logger.warning(f"[{group_id}] [定时任务] 无法执行，因为尚未获取到 bot 实例。")
            return
        
        try:
            logger.info(f"[{group_id}] [定时任务] 开始获取全量文件列表...")
            all_files = await self._get_all_files_recursive_core(group_id, bot)
            total_count = len(all_files)
            logger.info(f"[{group_id}] [定时任务] 获取到 {total_count} 个文件，准备分批检查。")
            invalid_files_info = []
            batch_size = 50
            for i in range(0, total_count, batch_size):
                batch = all_files[i:i + batch_size]
                for file_info in batch:
                    file_id = file_info.get("file_id")
                    if not file_id: continue
                    try:
                        await bot.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
                    except ActionFailed as e:
                        if e.result.get('retcode') == 1200:
                            invalid_files_info.append(file_info)
                    await asyncio.sleep(0.2)
            if not invalid_files_info:
                logger.info(f"[{group_id}] [定时任务] 检查完成，未发现失效文件。")
                return 
            report_message = f"🚨 定时检查报告\n在 {total_count} 个群文件中，共发现 {len(invalid_files_info)} 个失效文件：\n"
            report_message += "-" * 20
            for info in invalid_files_info:
                folder_name = info.get('parent_folder_name', '未知')
                modify_time = utils.format_timestamp(info.get('modify_time'))
                report_message += f"\n- {info.get('file_name')}"
                report_message += f"\n  (文件夹: {folder_name} | 时间: {modify_time})"
            report_message += "\n" + "-" * 20
            report_message += "\n建议管理员使用 /cdf 指令进行一键清理。"
            logger.info(f"[{group_id}] [定时任务] 检查全部完成，准备发送报告。")
            await bot.api.call_action('send_group_msg', group_id=group_id, message=report_message)
        except Exception as e:
            logger.error(f"[{group_id}] [定时任务] 执行过程中发生未知异常: {e}", exc_info=True)

    async def _get_all_files_recursive_core(self, group_id: int, bot) -> List[Dict]:
        all_files = []
        folders_to_scan = [(None, "根目录")]
        while folders_to_scan:
            current_folder_id, current_folder_name = folders_to_scan.pop(0)
            try:
                if current_folder_id is None:
                    result = await bot.api.call_action('get_group_root_files', group_id=group_id, file_count=2000)
                else:
                    result = await bot.api.call_action('get_group_files_by_folder', group_id=group_id, folder_id=current_folder_id, file_count=2000)
                if not result: continue
                if result.get('files'):
                    for file_info in result['files']:
                        file_info['parent_folder_name'] = current_folder_name
                        all_files.append(file_info)
                if result.get('folders'):
                    for folder in result['folders']:
                        if folder_id := folder.get('folder_id'):
                            folders_to_scan.append((folder_id, folder.get('folder_name')))
            except Exception as e:
                logger.error(f"[{group_id}] 递归获取文件夹 '{current_folder_name}' 内容时出错: {e}")
                continue
        return all_files
    
    @filter.command("cdf")
    async def on_check_and_delete_command(self, event: AstrMessageEvent):
        # 优先从事件中获取bot实例，并更新本地缓存
        self.bot = event.bot 
        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())
        logger.info(f"[{group_id}] 用户 {user_id} 触发 /cdf 失效文件清理指令。")
        if user_id not in self.admin_users:
            await event.send(MessageChain([Comp.Plain("⚠️ 您没有执行此操作的权限。")]))
            return
        await event.send(MessageChain([Comp.Plain("⚠️ 警告：即将开始扫描并自动删除所有失效文件！\n此过程可能需要几分钟，请耐心等待，完成后将发送报告。")]))
        asyncio.create_task(self._perform_batch_check_and_delete(event))
        event.stop_event()

    async def _perform_batch_check_and_delete(self, event: AstrMessageEvent):
        group_id = int(event.get_group_id())
        bot = self._get_bot()
        if not bot:
            await event.send(MessageChain([Comp.Plain("❌ 无法获取机器人实例，请稍后再试或联系管理员。")]))
            return
        
        try:
            logger.info(f"[{group_id}] [批量清理] 开始获取全量文件列表...")
            all_files = await self._get_all_files_recursive_core(group_id, bot)
            total_count = len(all_files)
            logger.info(f"[{group_id}] [批量清理] 获取到 {total_count} 个文件，准备分批处理。")
            deleted_files = []
            failed_deletions = []
            checked_count = 0
            batch_size = 50
            for i in range(0, total_count, batch_size):
                batch = all_files[i:i + batch_size]
                logger.info(f"[{group_id}] [批量清理] 正在处理批次 {i//batch_size + 1}/{ -(-total_count // batch_size)}...")
                for file_info in batch:
                    file_id = file_info.get("file_id")
                    file_name = file_info.get("file_name", "未知文件名")
                    if not file_id: continue
                    is_invalid = False
                    try:
                        await bot.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
                    except ActionFailed as e:
                        if e.result.get('retcode') == 1200:
                            is_invalid = True
                    if is_invalid:
                        logger.warning(f"[{group_id}] [批量清理] 发现失效文件 '{file_name}'，尝试删除...")
                        try:
                            delete_result = await bot.api.call_action('delete_group_file', group_id=group_id, file_id=file_id)
                            is_success = False
                            if delete_result:
                                trans_result = delete_result.get('transGroupFileResult', {})
                                result_obj = trans_result.get('result', {})
                                if result_obj.get('retCode') == 0:
                                    is_success = True
                            if is_success:
                                logger.info(f"[{group_id}] [批量清理] 成功删除失效文件: '{file_name}'")
                                deleted_files.append(file_name)
                            else:
                                logger.error(f"[{group_id}] [批量清理] 删除失效文件 '{file_name}' 失败，API未返回成功。")
                                failed_deletions.append(file_name)
                        except Exception as del_e:
                            logger.error(f"[{group_id}] [批量清理] 删除失效文件 '{file_name}' 时发生异常: {del_e}")
                            failed_deletions.append(file_name)
                    checked_count += 1
                    await asyncio.sleep(0.2)
                logger.info(f"[{group_id}] [批量清理] 批次处理完毕，已检查 {checked_count}/{total_count} 个文件。")
            report_message = f"✅ 清理完成！\n共扫描了 {total_count} 个文件。\n\n"
            if deleted_files:
                report_message += f"成功删除了 {len(deleted_files)} 个失效文件：\n"
                report_message += "\n".join(f"- {name}" for name in deleted_files)
            else:
                report_message += "未发现或未成功删除任何失效文件。"
            if failed_deletions:
                report_message += f"\n\n🚨 有 {len(failed_deletions)} 个失效文件删除失败，可能需要手动处理：\n"
                report_message += "\n".join(f"- {name}" for name in failed_deletions)
            logger.info(f"[{group_id}] [批量清理] 检查全部完成，准备发送报告。")
            await self._send_or_forward(event, report_message, name="失效文件清理报告")
        except Exception as e:
            logger.error(f"[{group_id}] [批量清理] 执行过程中发生未知异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain("❌ 在执行批量清理时发生内部错误，请检查后台日志。")]))

    @filter.command("cf")
    async def on_check_files_command(self, event: AstrMessageEvent):
        # 优先从事件中获取bot实例，并更新本地缓存
        self.bot = event.bot
        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())
        if user_id not in self.admin_users:
            await event.send(MessageChain([Comp.Plain("⚠️ 您没有执行此操作的权限。")]))
            return
        logger.info(f"[{group_id}] 用户 {user_id} 触发 /cf 失效文件检查指令。")
        await event.send(MessageChain([Comp.Plain("✅ 已开始扫描群内所有文件，查找失效文件...\n这可能需要几分钟，请耐心等待。")]))
        asyncio.create_task(self._perform_batch_check(event))
        event.stop_event()

    async def _perform_batch_check(self, event: AstrMessageEvent, is_daily_check: bool = False):
        group_id = int(event.get_group_id())
        bot = self._get_bot()
        if not bot:
            await event.send(MessageChain([Comp.Plain("❌ 无法获取机器人实例，请稍后再试或联系管理员。")]))
            return
        
        try:
            log_prefix = "[每日自动检查]" if is_daily_check else "[批量检查]"
            logger.info(f"[{group_id}] {log_prefix} 开始获取全量文件列表...")
            all_files = await self._get_all_files_recursive_core(group_id, bot)
            total_count = len(all_files)
            logger.info(f"[{group_id}] {log_prefix} 获取到 {total_count} 个文件，准备分批检查。")
            invalid_files_info = []
            checked_count = 0
            batch_size = 50
            for i in range(0, total_count, batch_size):
                batch = all_files[i:i + batch_size]
                logger.info(f"[{group_id}] {log_prefix} 正在处理批次 {i//batch_size + 1}/{ -(-total_count // batch_size)}...")
                for file_info in batch:
                    file_id = file_info.get("file_id")
                    if not file_id: continue
                    try:
                        await bot.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
                    except ActionFailed as e:
                        if e.result.get('retcode') == 1200:
                            logger.warning(f"[{group_id}] {log_prefix} 判定失效文件: '{file_info.get('file_name')}'，错误: {e.result.get('wording')}")
                            invalid_files_info.append(file_info)
                    checked_count += 1
                    await asyncio.sleep(0.2)
                logger.info(f"[{group_id}] {log_prefix} 批次处理完毕，已检查 {checked_count}/{total_count} 个文件。")
            report_title = "每日检查报告" if is_daily_check else "检查完成！"
            if not invalid_files_info:
                report_message = f"🎉 {report_title}\n在 {total_count} 个群文件中，未发现任何失效文件。"
            else:
                report_message = f"🚨 {report_title}\n在 {total_count} 个群文件中，共发现 {len(invalid_files_info)} 个失效文件：\n"
                report_message += "-" * 20
                for info in invalid_files_info:
                    folder_name = info.get('parent_folder_name', '未知')
                    modify_time = utils.format_timestamp(info.get('modify_time'))
                    report_message += f"\n- {info.get('file_name')}"
                    report_message += f"\n  (文件夹: {folder_name} | 时间: {modify_time})"
                report_message += "\n" + "-" * 20
                report_message += "\n建议使用 /cdf 指令进行一键清理。"
            logger.info(f"[{group_id}] {log_prefix} 检查全部完成，准备发送报告。")
            await self._send_or_forward(event, report_message, name="失效文件检查报告")
        except Exception as e:
            logger.error(f"[{group_id}] {log_prefix} 执行过程中发生未知异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain("❌ 在执行批量检查时发生内部错误，请检查后台日志。")]))
    
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=10)
    async def on_group_file_upload(self, event: AstrMessageEvent):
        # 优先从事件中获取bot实例，并更新本地缓存
        self.bot = event.bot
        has_file = any(isinstance(seg, Comp.File) for seg in event.get_messages())
        if has_file:
            group_id = int(event.get_group_id())
            logger.info(f"[{group_id}] 检测到文件上传事件，将在5秒后触发容量检查。")
            await asyncio.sleep(5) 
            await self._check_storage_and_notify(event)

    async def _check_storage_and_notify(self, event: AstrMessageEvent):
        group_id = int(event.get_group_id())
        bot = self._get_bot()
        if not bot:
            logger.warning(f"[{group_id}] 无法执行容量检查，Bot实例不可用。")
            return
            
        if group_id not in self.storage_limits:
            return
        try:
            client = bot
            system_info = await client.api.call_action('get_group_file_system_info', group_id=group_id)
            if not system_info: return
            file_count = system_info.get('file_count', 0)
            used_space_bytes = system_info.get('used_space', 0)
            used_space_gb = float(utils.format_bytes(used_space_bytes, 'GB'))
            limits = self.storage_limits[group_id]
            count_limit = limits['count_limit']
            space_limit = limits['space_limit_gb']
            notifications = []
            if file_count >= count_limit:
                notifications.append(f"文件数量已达 {file_count}，接近或超过设定的 {count_limit} 上限！")
            if used_space_gb >= space_limit:
                notifications.append(f"已用空间已达 {used_space_gb:.2f}GB，接近或超过设定的 {space_limit:.2f}GB 上限！")
            if notifications:
                full_notification = "⚠️ 群文件容量警告 ⚠️\n" + "\n".join(notifications) + "\n请及时清理文件！"
                logger.warning(f"[{group_id}] 发送容量超限警告: {full_notification}")
                await event.send(MessageChain([Comp.Plain(full_notification)]))
        except ActionFailed as e:
            logger.error(f"[{group_id}] 调用 get_group_file_system_info 失败: {e}")
        except Exception as e:
            logger.error(f"[{group_id}] 处理容量检查时发生未知异常: {e}", exc_info=True)
    
    def _format_search_results(self, files: List[Dict], search_term: str, for_delete: bool = False) -> str:
        reply_text = f"🔍 找到了 {len(files)} 个与「{search_term}」相关的结果：\n"
        reply_text += "-" * 20
        for i, file_info in enumerate(files, 1):
            reply_text += (
                f"\n[{i}] {file_info.get('file_name')}"
                f"\n  上传者: {file_info.get('uploader_name', '未知')}"
                f"\n  大小: {utils.format_bytes(file_info.get('size'))}"
                f"\n  修改时间: {utils.format_timestamp(file_info.get('modify_time'))}"
            )
        reply_text += "\n" + "-" * 20
        if for_delete:
            reply_text += f"\n请使用 /df {search_term} [序号] 来删除指定文件。"
        else:
            reply_text += f"\n如需删除，请使用 /df {search_term} [序号]"
        return reply_text
    
    @filter.command("sf")
    async def on_search_file_command(self, event: AstrMessageEvent):
        # 优先从事件中获取bot实例，并更新本地缓存
        self.bot = event.bot
        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())
        bot = self._get_bot()
        if not bot:
            await event.send(MessageChain([Comp.Plain("❌ 无法获取机器人实例，请稍后再试或联系管理员。")]))
            return
            
        command_parts = event.message_str.split(maxsplit=2)
        if len(command_parts) < 2 or not command_parts[1]:
            await event.send(MessageChain([Comp.Plain("❓ 请提供要搜索的文件名。用法: /sf <文件名> [序号]")]))
            return
        filename_to_find = command_parts[1]
        index_str = command_parts[2] if len(command_parts) > 2 else None
        logger.info(f"[{group_id}] 用户 {user_id} 触发 /sf, 目标: '{filename_to_find}', 序号: {index_str}")
        
        all_files = await self._get_all_files_recursive_core(group_id, bot)
        found_files = []
        for file_info in all_files:
            current_filename = file_info.get('file_name', '')
            base_name, _ = os.path.splitext(current_filename)
            if filename_to_find in base_name or filename_to_find in current_filename:
                found_files.append(file_info)
        
        logger.info(f"[{group_id}] 在 {len(all_files)} 个文件中，找到 {len(found_files)} 个匹配项。")

        if not found_files:
            await event.send(MessageChain([Comp.Plain(f"❌ 未在群文件中找到与「{filename_to_find}」相关的任何文件。")]))
            return
        if not index_str:
            reply_text = self._format_search_results(found_files, filename_to_find)
            await self._send_or_forward(event, reply_text, name="文件搜索结果")
            return
        try:
            index = int(index_str)
            if not (1 <= index <= len(found_files)):
                await event.send(MessageChain([Comp.Plain(f"❌ 序号错误！找到了 {len(found_files)} 个文件，请输入 1 到 {len(found_files)} 之间的数字。")]))
                return
            file_to_preview = found_files[index - 1]
            preview_text, error_msg = await self._get_file_preview(event, file_to_preview)
            if error_msg:
                # 预览失败，直接发送错误信息
                await event.send(MessageChain([Comp.Plain(error_msg)]))
                return
            
            # 预览成功，构建回复消息
            reply_text = (
                f"📄 文件「{file_to_preview.get('file_name')}」内容预览：\n"
                + "-" * 20 + "\n"
                + preview_text
            )
            await self._send_or_forward(event, reply_text, name=f"文件预览：{file_to_preview.get('file_name')}")
        except ValueError:
            await event.send(MessageChain([Comp.Plain("❌ 序号必须是一个数字。")]))
        except Exception as e:
            logger.error(f"[{group_id}] 处理预览时发生未知异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain("❌ 预览文件时发生内部错误，请检查后台日志。")]))
            
    @filter.command("df")
    async def on_delete_file_command(self, event: AstrMessageEvent):
        # 优先从事件中获取bot实例，并更新本地缓存
        self.bot = event.bot
        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())
        bot = self._get_bot()
        if not bot:
            await event.send(MessageChain([Comp.Plain("❌ 无法获取机器人实例，请稍后再试或联系管理员。")]))
            return

        command_parts = event.message_str.split(maxsplit=2)
        if len(command_parts) < 2 or not command_parts[1]:
            await event.send(MessageChain([Comp.Plain("❓ 请提供要删除的文件名。用法: /df <文件名> [序号]")]))
            return
        filename_to_find = command_parts[1]
        index_str = command_parts[2] if len(command_parts) > 2 else None
        logger.info(f"[{group_id}] 用户 {user_id} 触发删除指令 /df, 目标: '{filename_to_find}', 序号: {index_str}")
        if user_id not in self.admin_users:
            await event.send(MessageChain([Comp.Plain("⚠️ 您没有执行此操作的权限。")]))
            return

        all_files = await self._get_all_files_recursive_core(group_id, bot)
        found_files = []
        for file_info in all_files:
            current_filename = file_info.get('file_name', '')
            base_name, _ = os.path.splitext(current_filename)
            if filename_to_find in base_name or filename_to_find in current_filename:
                found_files.append(file_info)

        logger.info(f"[{group_id}] 在 {len(all_files)} 个文件中，找到 {len(found_files)} 个匹配项用于删除。")
            
        if not found_files:
            await event.send(MessageChain([Comp.Plain(f"❌ 未找到与「{filename_to_find}」相关的任何文件。")]))
            return
            
        if index_str == '0':
            asyncio.create_task(self._perform_batch_delete(event, found_files))
            event.stop_event()
            return

        file_to_delete = None
        if len(found_files) == 1 and not index_str:
            file_to_delete = found_files[0]
        elif index_str:
            try:
                index = int(index_str)
                if 1 <= index <= len(found_files):
                    file_to_delete = found_files[index - 1]
                else:
                    await event.send(MessageChain([Comp.Plain(f"❌ 序号错误！找到了 {len(found_files)} 个文件，请输入 1 到 {len(found_files)} 之间的数字。")]))
                    return
            except ValueError:
                await event.send(MessageChain([Comp.Plain("❌ 序号必须是一个数字。")]))
                return
        else:
            reply_text = self._format_search_results(found_files, filename_to_find, for_delete=True)
            await self._send_or_forward(event, reply_text, name="文件搜索结果")
            return

        if not file_to_delete:
            await event.send(MessageChain([Comp.Plain("❌ 内部错误，未能确定要删除的文件。")]))
            return
        try:
            file_id_to_delete = file_to_delete.get("file_id")
            found_filename = file_to_delete.get("file_name")
            if not file_id_to_delete:
                await event.send(MessageChain([Comp.Plain(f"❌ 找到文件「{found_filename}」，但无法获取其ID，删除失败。")]))
                return
            logger.info(f"[{group_id}] 确认删除文件 '{found_filename}', File ID: {file_id_to_delete}...")
            delete_result = await bot.api.call_action('delete_group_file', group_id=group_id, file_id=file_id_to_delete)
            is_success = False
            if delete_result:
                trans_result = delete_result.get('transGroupFileResult', {})
                result_obj = trans_result.get('result', {})
                if result_obj.get('retCode') == 0:
                    is_success = True
            if is_success:
                await event.send(MessageChain([Comp.Plain(f"✅ 文件「{found_filename}」已成功删除。")]))
                logger.info(f"[{group_id}] 文件 '{found_filename}' 已成功删除。")
            else:
                error_msg = delete_result.get('wording', 'API未返回成功状态')
                await event.send(MessageChain([Comp.Plain(f"❌ 删除文件「{found_filename}」失败: {error_msg}")]))
        except Exception as e:
            logger.error(f"[{group_id}] 处理删除流程时发生未知异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain(f"❌ 处理删除时发生内部错误，请检查后台日志。")]))

    async def _perform_batch_delete(self, event: AstrMessageEvent, files_to_delete: List[Dict]):
        group_id = int(event.get_group_id())
        bot = self._get_bot()
        if not bot:
            await event.send(MessageChain([Comp.Plain("❌ 无法获取机器人实例，请稍后再试或联系管理员。")]))
            return

        deleted_files = []
        failed_deletions = []
        total_count = len(files_to_delete)
        logger.info(f"[{group_id}] [批量删除] 开始处理 {total_count} 个文件的删除任务。")
        for i, file_info in enumerate(files_to_delete):
            file_id = file_info.get("file_id")
            file_name = file_info.get("file_name", "未知文件名")
            if not file_id:
                failed_deletions.append(f"{file_name} (缺少File ID)")
                continue
            try:
                logger.info(f"[{group_id}] [批量删除] ({i+1}/{total_count}) 正在删除 '{file_name}'...")
                delete_result = await bot.api.call_action('delete_group_file', group_id=group_id, file_id=file_id)
                is_success = False
                if delete_result:
                    trans_result = delete_result.get('transGroupFileResult', {})
                    result_obj = trans_result.get('result', {})
                    if result_obj.get('retCode') == 0:
                        is_success = True
                if is_success:
                    deleted_files.append(file_name)
                else:
                    failed_deletions.append(file_name)
            except Exception as e:
                logger.error(f"[{group_id}] [批量删除] 删除 '{file_name}' 时发生异常: {e}")
                failed_deletions.append(file_name)
            await asyncio.sleep(0.5)
        report_message = f"✅ 批量删除完成！\n共处理了 {total_count} 个文件。\n\n"
        if deleted_files:
            report_message += f"成功删除了 {len(deleted_files)} 个文件：\n"
            report_message += "\n".join(f"- {name}" for name in deleted_files)
        else:
            report_message += "未能成功删除任何文件。"
        if failed_deletions:
            report_message += f"\n\n🚨 有 {len(failed_deletions)} 个文件删除失败：\n"
            report_message += "\n".join(f"- {name}" for name in failed_deletions)
        logger.info(f"[{group_id}] [批量删除] 任务完成，准备发送报告。")
        await self._send_or_forward(event, report_message, name="批量删除报告")
    
    def _get_preview_from_bytes(self, content_bytes: bytes) -> tuple[str, str]:
        """从字节内容中尝试获取文本预览和编码。"""
        try:
            detection = chardet.detect(content_bytes)
            encoding = detection.get('encoding', 'utf-8') or 'utf-8'
            if encoding and detection['confidence'] > 0.7:
                decoded_text = content_bytes.decode(encoding, errors='ignore').strip()
                return decoded_text, encoding
            return "", "未知"
        except Exception:
            return "", "未知"

    def _fix_zip_filename(self, filename: str) -> str:
        """修复ZIP文件中的乱码文件名。"""
        try:
            return filename.encode('cp437').decode('gbk')
        except (UnicodeEncodeError, UnicodeDecodeError):
            return filename
    
    async def _get_preview_from_zip(self, file_path: str) -> tuple[str, str]:
        """从本地ZIP文件中解压并预览第一个TXT文件。返回 (预览内容, 错误信息)。"""
        def _try_unzip(pwd: Optional[str] = None) -> Optional[tuple[bytes, str]]:
            with zipfile.ZipFile(file_path, 'r') as zf:
                if pwd:
                    zf.setpassword(pwd.encode('utf-8'))
                txt_files_garbled = sorted([f for f in zf.namelist() if f.lower().endswith('.txt')])
                if not txt_files_garbled:
                    return None
                first_txt_garbled = txt_files_garbled[0]
                first_txt_fixed = self._fix_zip_filename(first_txt_garbled)
                content_bytes = zf.read(first_txt_garbled)
                return content_bytes, first_txt_fixed

        content_bytes, inner_filename = None, None
        try:
            result = await asyncio.to_thread(_try_unzip)
            if result:
                content_bytes, inner_filename = result
        except RuntimeError:
            logger.info(f"无密码解压 '{os.path.basename(file_path)}' 失败，尝试使用默认密码...")
            try:
                if self.default_zip_password:
                    result = await asyncio.to_thread(_try_unzip, self.default_zip_password)
                    if result:
                        content_bytes, inner_filename = result
                    else:
                        return "", "压缩包中没有可预览的文本文件"
                else:
                    return "", "文件已加密，未提供解压密码"
            except Exception as e:
                logger.error(f"使用默认密码解压失败: {e}")
                return "", "解压失败"
        except Exception as e:
            logger.error(f"处理ZIP文件时发生未知错误: {e}")
            return "", "处理ZIP文件时发生未知错误"

        if not content_bytes:
            return "", "压缩包中没有可预览的文本文件"

        preview_text, encoding = self._get_preview_from_bytes(content_bytes)
        extra_info = f"ZIP内文件: {inner_filename} (格式 {encoding})"
        return f"{extra_info}\n{preview_text}", ""
    
    async def _get_file_preview(self, event: AstrMessageEvent, file_info: dict) -> tuple[str, str | None]:
        group_id = int(event.get_group_id())
        file_id = file_info.get("file_id")
        file_name = file_info.get("file_name", "")
        _, file_extension = os.path.splitext(file_name)
        
        is_txt = file_extension.lower() == '.txt'
        is_zip = self.enable_zip_preview and file_extension.lower() == '.zip'
        
        if not (is_txt or is_zip):
            return "", f"❌ 文件「{file_name}」不是支持的文本或ZIP格式，无法预览。"
            
        logger.info(f"[{group_id}] 正在为文件 '{file_name}' (ID: {file_id}) 获取预览...")
        
        local_file_path = None
        
        try:
            bot = self._get_bot()
            if not bot:
                return "", "❌ 无法获取机器人实例，请稍后再试或联系管理员。"
            client = bot
            url_result = await client.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
            if not (url_result and url_result.get('url')):
                return "", f"❌ 无法获取文件「{file_name}」的下载链接。"
            url = url_result['url']
        except ActionFailed as e:
            if e.result.get('retcode') == 1200:
                error_message = (
                    f"❌ 预览文件「{file_name}」失败：\n"
                    f"该文件可能已失效或被服务器清理。\n"
                    f"建议使用 /df {os.path.splitext(file_name)[0]} 将其删除。"
                )
                return "", error_message
            else:
                return "", f"❌ 预览失败，API返回错误：{e.result.get('wording', '未知错误')}"
        
        try:
            async with aiohttp.ClientSession() as session:
                async with self.download_semaphore:
                    # 对于ZIP文件，需要下载完整文件，因为可能需要密码解压
                    range_header = None
                    if is_txt:
                        range_header = {'Range': 'bytes=0-4095'}
                    async with session.get(url, headers=range_header, timeout=30) as resp:
                        if resp.status != 200 and resp.status != 206:
                            return "", f"❌ 下载文件「{file_name}」失败 (HTTP: {resp.status})。"
                        
                        # 创建临时文件
                        temp_dir = os.path.join(os.getcwd(), 'temp_file_previews')
                        os.makedirs(temp_dir, exist_ok=True)
                        local_file_path = os.path.join(temp_dir, f"{file_id}_{file_name}")
                        
                        content_bytes = await resp.read()
                        with open(local_file_path, 'wb') as f:
                            f.write(content_bytes)
            
            preview_content = ""
            if is_txt:
                decoded_text, _ = self._get_preview_from_bytes(content_bytes)
                preview_content = decoded_text
            elif is_zip:
                preview_text, error_msg = await self._get_preview_from_zip(local_file_path)
                if error_msg:
                    return "", error_msg
                preview_content = preview_text
            
            if len(preview_content) > self.preview_length:
                preview_content = preview_content[:self.preview_length] + "..."
            
            return preview_content, None
                
        except asyncio.TimeoutError:
            return "", f"❌ 预览文件「{file_name}」超时。"
        except Exception as e:
            logger.error(f"[{group_id}] 获取文件 '{file_name}' 预览时发生未知异常: {e}", exc_info=True)
            return "", f"❌ 预览文件「{file_name}」时发生内部错误。"
        finally:
            if local_file_path and os.path.exists(local_file_path):
                try:
                    os.remove(local_file_path)
                    logger.info(f"已清理临时文件: {local_file_path}")
                except OSError as e:
                    logger.warning(f"删除临时文件 {local_file_path} 失败: {e}")

    async def terminate(self):
        logger.info("插件 [群文件系统GroupFS] 已卸载。")