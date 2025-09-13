# astrbot_plugin_GroupFS/main.py

import asyncio
import os
from typing import List, Dict, Optional

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

@register(
    "astrbot_plugin_GroupFS",
    "Foolllll",
    "管理QQ群文件",
    "0.1",
    "https://github.com/Foolllll-J/astrbot_plugin_GroupFS"
)
class GroupFSPlugin(Star):
    def __init__(self, context: Context, config: Optional[Dict] = None):
        super().__init__(context)
        self.config = config if config else {}
        self.group_whitelist: List[int] = [int(g) for g in self.config.get("group_whitelist", [])]
        self.admin_users: List[int] = [int(u) for u in self.config.get("admin_users", [])]
        logger.info("插件 [群文件系统GroupFS] 已加载。")

    @filter.command("df")
    async def on_delete_file_command(self, event: AstrMessageEvent):
        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())
        
        command_parts = event.message_str.split(maxsplit=1)
        
        if len(command_parts) < 2 or not command_parts[1]:
            await event.send(MessageChain([Comp.Plain("❓ 请提供要删除的文件名。用法: /df <文件名>")]))
            return
            
        filename = command_parts[1]
        
        logger.info(f"[{group_id}] 用户 {user_id} 触发指令 /df, 参数为: '{filename}'")

        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        if user_id not in self.admin_users:
            await event.send(MessageChain([Comp.Plain("⚠️ 您没有执行此操作的权限。")]))
            return
        
        try:
            await event.send(MessageChain([Comp.Plain(f"正在查找文件「{filename}」，请稍候...")]))

            file_info = await self._find_file_by_name(event, filename)

            if not file_info:
                await event.send(MessageChain([Comp.Plain(f"❌ 未在群文件中找到名为「{filename}」的文件。")]))
                return

            file_id_to_delete = file_info.get("file_id")
            found_filename = file_info.get("file_name")

            if not file_id_to_delete:
                await event.send(MessageChain([Comp.Plain(f"❌ 找到文件「{found_filename}」，但无法获取其ID，删除失败。")]))
                return
            
            logger.info(f"[{group_id}] 找到文件 '{found_filename}', File ID: {file_id_to_delete}。准备执行删除...")

            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            delete_result = await client.api.call_action(
                'delete_group_file', group_id=group_id, file_id=file_id_to_delete
            )
            logger.info(f"[{group_id}] API响应: {delete_result}")

            # --- ここが修正点です ---
            # 修正对API响应的判断逻辑，以正确解析嵌套的JSON
            is_success = False
            if delete_result:
                trans_result = delete_result.get('transGroupFileResult', {})
                result_obj = trans_result.get('result', {})
                if result_obj.get('retCode') == 0:
                    is_success = True

            if is_success:
                await event.send(MessageChain([Comp.Plain(f"✅ 文件「{found_filename}」已成功删除。")]))
            else:
                error_msg = delete_result.get('wording', 'API未返回成功状态或格式未知')
                await event.send(MessageChain([Comp.Plain(f"❌ 删除文件「{found_filename}」失败: {error_msg}")]))

        except Exception as e:
            logger.error(f"[{group_id}] 处理删除流程时发生未知异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain(f"❌ 处理删除时发生内部错误，请检查后台日志。")]))


    async def _find_file_by_name(self, event: AstrMessageEvent, filename_to_find: str) -> Optional[Dict]:
        """在群文件的根目录和所有一级子目录中查找文件，严格按无扩展名匹配。"""
        group_id = int(event.get_group_id())
        logger.info(f"[{group_id}] --->>> 开始严格按无扩展名查找, 目标: '{filename_to_find}'")
        try:
            client = event.bot

            root_files_result = await client.api.call_action('get_group_root_files', group_id=group_id)
            if root_files_result and root_files_result.get('files'):
                for file_info in root_files_result['files']:
                    current_filename = file_info.get('file_name')
                    base_name, _ = os.path.splitext(current_filename)
                    if base_name == filename_to_find:
                        logger.info(f"[{group_id}] [成功] 在根目录找到匹配文件: '{current_filename}'")
                        return file_info

            if root_files_result and root_files_result.get('folders'):
                for folder in root_files_result['folders']:
                    folder_id = folder.get('folder_id')
                    folder_name = folder.get('folder_name')
                    if not folder_id: continue
                    
                    sub_files_result = await client.api.call_action(
                        'get_group_files_by_folder', group_id=group_id, folder_id=folder_id
                    )
                    if sub_files_result and sub_files_result.get('files'):
                        for file_info in sub_files_result['files']:
                            current_filename = file_info.get('file_name')
                            base_name, _ = os.path.splitext(current_filename)
                            if base_name == filename_to_find:
                                logger.info(f"[{group_id}] [成功] 在文件夹 '{folder_name}' 中找到匹配文件: '{current_filename}'")
                                return file_info
            
            logger.warning(f"[{group_id}] [失败] <<<--- 遍历完所有位置，未能找到目标文件: '{filename_to_find}'")
            return None
        except Exception as e:
            logger.error(f"[{group_id}] 查找文件时发生API异常: {e}", exc_info=True)
            return None

    async def terminate(self):
        logger.info("插件 [群文件系统GroupFS] 已卸载。")
