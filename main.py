# astrbot_plugin_GroupFS/main.py

import asyncio
import os
import datetime
from typing import List, Dict, Optional

import aiohttp
import chardet

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from aiocqhttp.exceptions import ActionFailed

# --- 輔助函數 ---
def _format_bytes(size: int, target_unit=None) -> str:
    if size is None: return "未知大小"
    power = 1024
    n = 0
    power_labels = {0: 'B', 1: 'KB', 2: 'MB', 3: 'GB', 4: 'TB'}
    if target_unit and target_unit.upper() in power_labels.values():
        target_n = list(power_labels.keys())[list(power_labels.values()).index(target_unit.upper())]
        while n < target_n:
            size /= power
            n += 1
        return f"{size:.2f}"
    while size > power and n < len(power_labels) -1 :
        size /= power
        n += 1
    return f"{size:.2f} {power_labels[n]}"

def _format_timestamp(ts: int) -> str:
    if ts is None or ts == 0: return "未知时间"
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')

SUPPORTED_PREVIEW_EXTENSIONS = (
    '.txt', '.md', '.json', '.xml', '.html', '.css', 
    '.js', '.py', '.java', '.c', '.cpp', '.h', '.hpp', 
    '.go', '.rs', '.rb', '.php', '.log', '.ini', '.yml', '.yaml'
)

@register(
    "astrbot_plugin_GroupFS",
    "Foolllll",
    "管理QQ群文件",
    "0.4",
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
        logger.info("插件 [群文件系统GroupFS] 已加载。")
        logger.info(f"容量监控配置: {self.storage_limits}")

    @filter.command("cf")
    async def on_check_files_command(self, event: AstrMessageEvent):
        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())
        logger.info(f"[{group_id}] 用户 {user_id} 触发 /cf 失效文件检查指令。")
        if user_id not in self.admin_users:
            await event.send(MessageChain([Comp.Plain("⚠️ 您没有执行此操作的权限。")]))
            return
        await event.send(MessageChain([Comp.Plain("✅ 已开始扫描群内所有文件，查找失效文件...\n这可能需要几分钟到数十分钟，请耐心等待，完成后会在此发送报告。")]))
        asyncio.create_task(self._perform_batch_check(event))

    async def _perform_batch_check(self, event: AstrMessageEvent):
        group_id = int(event.get_group_id())
        try:
            logger.info(f"[{group_id}] [批量检查] 开始获取全量文件列表...")
            all_files = await self._get_all_files_recursive(event)
            total_count = len(all_files)
            logger.info(f"[{group_id}] [批量检查] 获取到 {total_count} 个文件，准备分批检查。")
            invalid_files = []
            checked_count = 0
            batch_size = 50
            for i in range(0, total_count, batch_size):
                batch = all_files[i:i + batch_size]
                logger.info(f"[{group_id}] [批量检查] 正在处理批次 {i//batch_size + 1}/{ -(-total_count // batch_size)}...")
                for file_info in batch:
                    file_id = file_info.get("file_id")
                    file_name = file_info.get("file_name", "未知文件名")
                    if not file_id:
                        continue
                    try:
                        await event.bot.api.call_action('get_group_file_url', group_id=group_id, file_id=file_id)
                    except ActionFailed as e:
                        if e.retcode == 1200 or '(-134)' in str(e.wording):
                            logger.warning(f"[{group_id}] [批量检查] 发现失效文件: '{file_name}'")
                            invalid_files.append(file_name)
                    checked_count += 1
                logger.info(f"[{group_id}] [批量检查] 批次处理完毕，已检查 {checked_count}/{total_count} 个文件。延时1秒...")
                await asyncio.sleep(1)
            if not invalid_files:
                report_message = f"🎉 检查完成！\n在 {total_count} 个群文件中，未发现任何失效文件。"
            else:
                report_message = f"🚨 检查完成！\n在 {total_count} 个群文件中，共发现 {len(invalid_files)} 个失效文件：\n"
                report_message += "-" * 20
                for file_name in invalid_files:
                    report_message += f"\n- {file_name}"
                report_message += "\n" + "-" * 20
                report_message += "\n建议使用 /df 指令进行清理。"
            logger.info(f"[{group_id}] [批量检查] 检查全部完成，准备发送报告。")
            await event.send(MessageChain([Comp.Plain(report_message)]))
        except Exception as e:
            logger.error(f"[{group_id}] [批量检查] 执行过程中发生未知异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain("❌ 在执行批量检查时发生内部错误，请检查后台日志。")]))

    async def _get_all_files_recursive(self, event: AstrMessageEvent) -> List[Dict]:
        group_id = int(event.get_group_id())
        all_files = []
        folders_to_scan = [None]
        while folders_to_scan:
            current_folder_id = folders_to_scan.pop(0)
            if current_folder_id is None:
                result = await event.bot.api.call_action('get_group_root_files', group_id=group_id)
            else:
                result = await event.bot.api.call_action('get_group_files_by_folder', group_id=group_id, folder_id=current_folder_id)
            if not result: continue
            if result.get('files'):
                all_files.extend(result['files'])
            if result.get('folders'):
                for folder in result['folders']:
                    if folder_id := folder.get('folder_id'):
                        folders_to_scan.append(folder_id)
        return all_files

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_file_upload(self, event: AstrMessageEvent):
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
            used_space_gb = float(_format_bytes(used_space_bytes, 'GB'))
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
    
    def _format_search_results(self, files: List[Dict], search_term: str) -> str:
        reply_text = f"🔍 找到了 {len(files)} 个与「{search_term}」相关的结果：\n"
        reply_text += "-" * 20
        for i, file_info in enumerate(files, 1):
            reply_text += (
                f"\n[{i}] {file_info.get('file_name')}"
                f"\n  上传者: {file_info.get('uploader_name', '未知')}"
                f"\n  大小: {_format_bytes(file_info.get('size'))}"
                f"\n  修改时间: {_format_timestamp(file_info.get('modify_time'))}"
            )
        reply_text += "\n" + "-" * 20
        reply_text += f"\n如需预览，请使用 /sf {search_term} [序号]"
        return reply_text
    
    @filter.command("sf")
    async def on_search_file_command(self, event: AstrMessageEvent):
        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())
        command_parts = event.message_str.split(maxsplit=2)
        if len(command_parts) < 2 or not command_parts[1]:
            await event.send(MessageChain([Comp.Plain("❓ 请提供要搜索的文件名。用法: /sf <文件名> [序号]")]))
            return
        filename_to_find = command_parts[1]
        index_str = command_parts[2] if len(command_parts) > 2 else None
        logger.info(f"[{group_id}] 用户 {user_id} 触发 /sf, 目标: '{filename_to_find}', 序号: {index_str}")
        
        # --- 关键修改：移除了此处的 "正在处理..." 回复 ---

        found_files = await self._find_all_matching_files(event, filename_to_find)
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
            
            await event.send(MessageChain([Comp.Plain(f"正在获取「{found_files[index-1].get('file_name')}」的预览，请稍候...")]))
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
        found_files = await self._find_all_matching_files(event, filename_to_find)
        if not found_files:
            await event.send(MessageChain([Comp.Plain(f"❌ 未找到要删除的目标文件「{filename_to_find}」。")]))
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
            reply_text = self._format_search_results(found_files, filename_to_find).replace("如需预览", "如需删除")
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

    async def _get_file_preview(self, event: AstrMessageEvent, file_info: dict) -> tuple[str, str | None]:
        group_id = int(event.get_group_id())
        file_id = file_info.get("file_id")
        file_name = file_info.get("file_name", "")
        _, file_extension = os.path.splitext(file_name)
        if file_extension.lower() not in SUPPORTED_PREVIEW_EXTENSIONS:
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

    async def _find_all_matching_files(self, event: AstrMessageEvent, filename_to_find: str) -> List[Dict]:
        group_id = int(event.get_group_id())
        logger.info(f"[{group_id}] 开始遍历所有文件查找, 目标: '{filename_to_find}'")
        matching_files = []
        try:
            client = event.bot
            root_files_result = await client.api.call_action('get_group_root_files', group_id=group_id)
            if root_files_result and root_files_result.get('files'):
                for file_info in root_files_result['files']:
                    current_filename = file_info.get('file_name', '')
                    base_name, _ = os.path.splitext(current_filename)
                    if filename_to_find in base_name or filename_to_find in current_filename:
                        matching_files.append(file_info)
            if root_files_result and root_files_result.get('folders'):
                for folder in root_files_result['folders']:
                    folder_id = folder.get('folder_id')
                    if not folder_id: continue
                    sub_files_result = await client.api.call_action('get_group_files_by_folder', group_id=group_id, folder_id=folder_id)
                    if sub_files_result and sub_files_result.get('files'):
                        for file_info in sub_files_result['files']:
                            current_filename = file_info.get('file_name', '')
                            base_name, _ = os.path.splitext(current_filename)
                            if filename_to_find in base_name or filename_to_find in current_filename:
                                matching_files.append(file_info)
            logger.info(f"[{group_id}] 查找结束，共找到 {len(matching_files)} 个匹配文件。")
            return matching_files
        except Exception as e:
            logger.error(f"[{group_id}] 查找文件时发生API异常: {e}", exc_info=True)
            return []
            
    async def terminate(self):
        logger.info("插件 [群文件系统GroupFS] 已卸载。")
