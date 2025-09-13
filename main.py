# astrbot_plugin_GroupFS/main.py

import asyncio
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
                await event.send(MessageChain([Comp.Plain(f"❌ 未在群文件中找到名为「{filename}」的文件。请检查后台DEBUG日志获取详细查找过程。")]))
                return

            file_id = file_info.get("file_id")
            if not file_id:
                await event.send(MessageChain([Comp.Plain(f"❌ 找到文件「{filename}」，但无法获取其ID，删除失败。")]))
                return
            
            logger.info(f"[{group_id}] 找到文件 '{filename}', File ID: {file_id}。准备删除...")
            
            # 此处省略了删除逻辑，因为主要问题在查找
            # await self._perform_delete(event, file_id, filename) # 假设删除逻辑封装在另一个方法
            await event.send(MessageChain([Comp.Plain(f"调试：已成功找到文件「{filename}」，下一步将执行删除。")]))


        except Exception as e:
            logger.error(f"[{group_id}] 处理删除流程时发生未知异常: {e}", exc_info=True)
            await event.send(MessageChain([Comp.Plain(f"❌ 处理删除时发生内部错误，请检查后台日志。")]))

    async def _find_file_by_name(self, event: AstrMessageEvent, filename: str) -> Optional[Dict]:
        """在群文件的根目录和所有一级子目录中查找文件，并提供详细日志。"""
        group_id = int(event.get_group_id())
        logger.info(f"[{group_id}] 开始执行详细文件查找, 目标: '{filename}'")
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot

            # 1. 查找根目录
            logger.debug(f"[{group_id}] [步骤1] 正在请求群文件根目录列表...")
            root_files_result = await client.api.call_action('get_group_root_files', group_id=group_id)
            logger.debug(f"[{group_id}] [步骤1] API 原始响应: {root_files_result}")

            if root_files_result and root_files_result.get('files'):
                logger.debug(f"[{group_id}] [步骤1] 根目录包含 {len(root_files_result['files'])} 个文件，开始遍历...")
                for file_info in root_files_result['files']:
                    current_filename = file_info.get('file_name')
                    logger.debug(f"[{group_id}] [步骤1] 正在检查根目录文件: '{current_filename}'")
                    if current_filename == filename:
                        logger.info(f"[{group_id}] [成功] 在根目录找到匹配文件: '{filename}'")
                        return file_info
            else:
                logger.debug(f"[{group_id}] [步骤1] 根目录中没有文件。")

            # 2. 查找一级子目录
            if root_files_result and root_files_result.get('folders'):
                folders = root_files_result['folders']
                logger.debug(f"[{group_id}] [步骤2] 根目录包含 {len(folders)} 个文件夹，准备遍历...")
                for folder in folders:
                    folder_id = folder.get('folder_id')
                    folder_name = folder.get('folder_name')
                    if not folder_id:
                        logger.debug(f"[{group_id}] [步骤2] 跳过一个没有ID的文件夹: {folder}")
                        continue
                    
                    logger.debug(f"[{group_id}] [步骤2] 正在进入文件夹 '{folder_name}' (ID: {folder_id}) 请求文件列表...")
                    sub_files_result = await client.api.call_action(
                        'get_group_files_by_folder', group_id=group_id, folder_id=folder_id
                    )
                    logger.debug(f"[{group_id}] [步骤2] 文件夹 '{folder_name}' 的API原始响应: {sub_files_result}")

                    if sub_files_result and sub_files_result.get('files'):
                        logger.debug(f"[{group_id}] [步骤2] 文件夹 '{folder_name}' 包含 {len(sub_files_result['files'])} 个文件，开始遍历...")
                        for file_info in sub_files_result['files']:
                            current_filename = file_info.get('file_name')
                            logger.debug(f"[{group_id}] [步骤2] 正在检查文件: '{current_filename}'")
                            if current_filename == filename:
                                logger.info(f"[{group_id}] [成功] 在文件夹 '{folder_name}' 中找到匹配文件: '{filename}'")
                                return file_info
                    else:
                         logger.debug(f"[{group_id}] [步骤2] 文件夹 '{folder_name}' 中没有文件。")
            else:
                logger.debug(f"[{group_id}] [步骤2] 根目录中没有文件夹。")

            logger.warning(f"[{group_id}] [失败] 遍历完所有位置，未能找到目标文件: '{filename}'")
            return None
        except Exception as e:
            logger.error(f"[{group_id}] 查找文件时发生API异常: {e}", exc_info=True)
            return None

    async def terminate(self):
        logger.info("插件 [群文件系统GroupFS] 已卸载。")
