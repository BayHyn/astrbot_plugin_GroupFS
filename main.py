# astrbot_plugin_GroupFS/main.py

# 请确保已安装依赖: pip install croniter aiohttp chardet
import asyncio
import os
import datetime
from typing import List, Dict, Optional

import aiohttp
import chardet
import croniter

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from aiocqhttp.exceptions import ActionFailed

# 从 utils.py 导入辅助函数和常量
from . import utils

@register(
    "astrbot_plugin_GroupFS",
    "Foolllll",
    "管理QQ群文件",
    "0.6",
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
        self.last_cron_check_time: Dict[int, datetime.datetime] = {}
        
        # 新增一个变量来“记住”bot实例
        self.bot = None

        # 解析容量监控配置
        limit_configs = self.config.get("storage_limits", [])
        for item in limit_configs:
            try:
                group_id_str, count_limit_str, space_limit_str = item.split(':')
                group_id = int(group_id_str)
                self.storage_limits[group_id] = {
                    "count_limit": int(count_limit_str),
                    "space_limit_gb": float(space_limit_str)
                }
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
                self.cron_tasks.append((group_id, cron_str))
            except ValueError as e:
                logger.error(f"解析 scheduled_check_tasks 配置 '{item}' 时出错: {e}，已跳过。")
        
        logger.info("插件 [群文件系统GroupFS] 已加载。")
        logger.info(f"定时任务配置: {self.cron_tasks}")

    async def initialize(self):
        if self.cron_tasks:
            logger.info("[定时任务] 启动失效文件检查循环...")
            asyncio.create_task(self.scheduled_check_loop())

    async def scheduled_check_loop(self):
        await asyncio.sleep(10)
        while True:
            now = datetime.datetime.now()
            await asyncio.sleep(60 - now.second)
            now = datetime.datetime.now()
            
            for group_id, cron_str in self.cron_tasks:
                if croniter.croniter.match(cron_str, now):
                    last_check = self.last_cron_check_time.get(group_id)
                    if last_check and last_check.minute == now.minute and last_check.hour == now.hour:
                        continue
                    logger.info(f"[{group_id}] [定时任务] Cron 表达式 '{cron_str}' 已触发，开始执行。")
                    self.last_cron_check_time[group_id] = now
                    asyncio.create_task(self._perform_batch_check_for_cron(group_id))

    async def _perform_batch_check_for_cron(self, group_id: int):
        try:
            if not self.bot:
                logger.warning(f"[{group_id}] [定时任务] 无法执行，因为尚未捕获到 bot 实例。请先触发任意一次指令。")
                return
            bot = self.bot

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
                        if e.retcode == 1200 or '(-134)' in str(e.wording):
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
        if not self.bot: self.bot = event.bot
        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())
        logger.info(f"[{group_id}] 用户 {user_id} 触发 /cdf 失效文件清理指令。")
        if user_id not in self.admin_users:
            await event.send(MessageChain([Comp.Plain("⚠️ 您没有执行此操作的权限。")]))
            return
        await event.send(MessageChain([Comp.Plain("⚠️ **警告**：即将开始扫描并自动删除所有失效文件！\n此过程可能需要几分钟，请耐心等待，完成后将发送报告。")]))
        asyncio.create_task(self._perform_batch_check_and_delete(event))
        event.stop_event()

    async def _perform_batch_check_and_delete(self, event: AstrMessageEvent):
        group_id = int(event.get_group_id())
        try:
            logger.info(f"[{group_id}] [批量清理] 开始获取全量文件列表...")
            all_files = await self._get_all_files_recursive_core(group_id, event.bot)
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
                        await event.bot.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
                    except ActionFailed as e:
                        if e.retcode == 1200 or '(-134)' in str(e.wording):
                            is_invalid = True
                    if is_invalid:
                        logger.warning(f"[{group_id}] [批量清理] 发现失效文件 '{file_name}'，尝试删除...")
                        try:
                            delete_result = await event.bot.api.call_action('delete_group_file', group_id=group_id, file_id=file_id)
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
                logger.info(f"[{group_id}] [批量清理] 批次处理完毕，已检查 {checked_count}/{total_count} 个文件。延时1秒...")
                await asyncio.sleep(1)
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
            await event.send(MessageChain([Comp.Plain(report_message)]))
        except Exception as e:
            logger.error(f"[{group_id}] [批量清理] 执行过程中发生未知异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain("❌ 在执行批量清理时发生内部错误，请检查后台日志。")]))

    @filter.command("cf")
    async def on_check_files_command(self, event: AstrMessageEvent):
        if not self.bot: self.bot = event.bot
        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())
        logger.info(f"[{group_id}] 用户 {user_id} 触发 /cf 失效文件检查指令。")
        if user_id not in self.admin_users:
            await event.send(MessageChain([Comp.Plain("⚠️ 您没有执行此操作的权限。")]))
            return
        await event.send(MessageChain([Comp.Plain("✅ 已开始扫描群内所有文件，查找失效文件...\n这可能需要几分钟，请耐心等待。")]))
        asyncio.create_task(self._perform_batch_check(event))
        event.stop_event()

    async def _perform_batch_check(self, event: AstrMessageEvent):
        group_id = int(event.get_group_id())
        try:
            logger.info(f"[{group_id}] [批量检查] 开始获取全量文件列表...")
            all_files = await self._get_all_files_recursive_core(group_id, event.bot)
            total_count = len(all_files)
            logger.info(f"[{group_id}] [批量检查] 获取到 {total_count} 个文件，准备分批检查。")
            invalid_files_info = []
            checked_count = 0
            batch_size = 50
            for i in range(0, total_count, batch_size):
                batch = all_files[i:i + batch_size]
                logger.info(f"[{group_id}] [批量检查] 正在处理批次 {i//batch_size + 1}/{ -(-total_count // batch_size)}...")
                for file_info in batch:
                    file_id = file_info.get("file_id")
                    if not file_id: continue
                    try:
                        await event.bot.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
                    except ActionFailed as e:
                        if e.retcode == 1200 or '(-134)' in str(e.wording):
                            logger.warning(f"[{group_id}] [批量检查] 发现失效文件: '{file_info.get('file_name')}'")
                            invalid_files_info.append(file_info)
                    checked_count += 1
                logger.info(f"[{group_id}] [批量检查] 批次处理完毕，已检查 {checked_count}/{total_count} 个文件。延时1秒...")
                await asyncio.sleep(1)
            if not invalid_files_info:
                report_message = f"🎉 检查完成！\n在 {total_count} 个群文件中，未发现任何失效文件。"
            else:
                report_message = f"🚨 检查完成！\n在 {total_count} 个群文件中，共发现 {len(invalid_files_info)} 个失效文件：\n"
                report_message += "-" * 20
                for info in invalid_files_info:
                    folder_name = info.get('parent_folder_name', '未知')
                    modify_time = utils.format_timestamp(info.get('modify_time'))
                    report_message += f"\n- {info.get('file_name')}"
                    report_message += f"\n  (文件夹: {folder_name} | 时间: {modify_time})"
                report_message += "\n" + "-" * 20
                report_message += "\n建议使用 /df 指令进行清理。"
            logger.info(f"[{group_id}] [批量检查] 检查全部完成，准备发送报告。")
            await event.send(MessageChain([Comp.Plain(report_message)]))
        except Exception as e:
            logger.error(f"[{group_id}] [批量检查] 执行过程中发生未知异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain("❌ 在执行批量检查时发生内部错误，请检查后台日志。")]))

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_file_upload(self, event: AstrMessageEvent):
        if not self.bot: self.bot = event.bot
        has_file = any(isinstance(seg, Comp.File) for seg in event.get_messages())
        if has_file:
            group_id = int(event.get_group_id())
            logger.info(f"[{group_id}] 检测到文件上传事件，将在5秒后触发容量检查。")
            await asyncio.sleep(5) 
            await self._check_storage_and_notify(event)

    async def _check_storage_and_notify(self, event: AstrMessageEvent):
        group_id = int(event.get_group_id())
        if group_id not in self.storage_limits:
            return
        try:
            client = event.bot
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
                full_notification = "⚠️ **群文件容量警告** ⚠️\n" + "\n".join(notifications) + "\n请及时清理文件！"
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
        if not self.bot: self.bot = event.bot
        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())
        command_parts = event.message_str.split(maxsplit=2)
        if len(command_parts) < 2 or not command_parts[1]:
            await event.send(MessageChain([Comp.Plain("❓ 请提供要搜索的文件名。用法: /sf <文件名> [序号]")]))
            return
        filename_to_find = command_parts[1]
        index_str = command_parts[2] if len(command_parts) > 2 else None
        logger.info(f"[{group_id}] 用户 {user_id} 触发 /sf, 目标: '{filename_to_find}', 序号: {index_str}")
        
        all_files = await self._get_all_files_recursive_core(group_id, event.bot)
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
            await event.send(MessageChain([Comp.Plain(reply_text)]))
            return
        try:
            index = int(index_str)
            if not (1 <= index <= len(found_files)):
                await event.send(MessageChain([Comp.Plain(f"❌ 序号错误！找到了 {len(found_files)} 个文件，请输入 1 到 {len(found_files)} 之间的数字。")]))
                return
            file_to_preview = found_files[index - 1]
            preview_text, error_msg = await self._get_file_preview(event, file_to_preview)
            if error_msg:
                await event.send(MessageChain([Comp.Plain(error_msg)]))
                return
            reply_text = (
                f"📄 文件「{file_to_preview.get('file_name')}」内容预览：\n"
                + "-" * 20 + "\n"
                + preview_text
            )
            await event.send(MessageChain([Comp.Plain(reply_text)]))
        except ValueError:
            await event.send(MessageChain([Comp.Plain("❌ 序号必须是一个数字。")]))
        except Exception as e:
            logger.error(f"[{group_id}] 处理预览时发生未知异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain("❌ 预览文件时发生内部错误，请检查后台日志。")]))
            
    @filter.command("df")
    async def on_delete_file_command(self, event: AstrMessageEvent):
        if not self.bot: self.bot = event.bot
        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())
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

        all_files = await self._get_all_files_recursive_core(group_id, event.bot)
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
            await event.send(MessageChain([Comp.Plain(reply_text)]))
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
            client = event.bot
            delete_result = await client.api.call_action('delete_group_file', group_id=group_id, file_id=file_id_to_delete)
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
                delete_result = await event.bot.api.call_action('delete_group_file', group_id=group_id, file_id=file_id)
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
        await event.send(MessageChain([Comp.Plain(report_message)]))

    async def _get_file_preview(self, event: AstrMessageEvent, file_info: dict) -> tuple[str, str | None]:
        group_id = int(event.get_group_id())
        file_id = file_info.get("file_id")
        file_name = file_info.get("file_name", "")
        _, file_extension = os.path.splitext(file_name)
        if file_extension.lower() not in utils.SUPPORTED_PREVIEW_EXTENSIONS:
            return "", f"❌ 文件「{file_name}」不是支持的文本格式，无法预览。"
        logger.info(f"[{group_id}] 正在为文件 '{file_name}' (ID: {file_id}) 获取预览...")
        try:
            client = event.bot
            url_result = await client.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
        except ActionFailed as e:
            logger.warning(f"[{group_id}] 获取文件 '{file_name}' 下载链接时API调用失败: {e}")
            if e.retcode == 1200 or '(-134)' in str(e.wording):
                error_message = (
                    f"❌ 预览文件「{file_name}」失败：\n"
                    f"该文件可能已失效或被服务器清理。\n"
                    f"建议使用 /df {os.path.splitext(file_name)[0]} 将其删除。"
                )
                return "", error_message
            else:
                return "", f"❌ 预览失败，API返回错误：{e.wording}"
        try:
            if not (url_result and url_result.get('url')):
                return "", f"❌ 无法获取文件「{file_name}」的下载链接。"
            url = url_result['url']
            async with aiohttp.ClientSession() as session:
                headers = {'Range': 'bytes=0-4095'} 
                async with session.get(url, headers=headers, timeout=20) as resp:
                    if resp.status != 200 and resp.status != 206:
                        return "", f"❌ 下载文件「{file_name}」失败 (HTTP: {resp.status})。"
                    content_bytes = await resp.read()
            if not content_bytes:
                return "（文件为空）", None
            detection = chardet.detect(content_bytes)
            encoding = detection.get('encoding', 'utf-8') or 'utf-8'
            decoded_text = content_bytes.decode(encoding, errors='ignore').strip()
            if len(decoded_text) > self.preview_length:
                return decoded_text[:self.preview_length] + "...", None
            return decoded_text, None
        except asyncio.TimeoutError:
            return "", f"❌ 预览文件「{file_name}」超时。"
        except Exception as e:
            logger.error(f"[{group_id}] 获取文件 '{file_name}' 预览时发生未知异常: {e}", exc_info=True)
            return "", f"❌ 预览文件「{file_name}」时发生内部错误。"

    async def terminate(self):
        logger.info("插件 [群文件系统GroupFS] 已卸载。")