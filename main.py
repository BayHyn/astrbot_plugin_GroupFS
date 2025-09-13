# astrbot_plugin_GroupFS/main.py

import asyncio
from typing import List, Dict, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
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
        # 从配置中读取白名单群组和有权删除文件的管理员QQ号
        self.group_whitelist: List[int] = [int(g) for g in self.config.get("group_whitelist", [])]
        self.admin_users: List[int] = [int(u) for u in self.config.get("admin_users", [])]
        logger.info("插件 [群文件系统GroupFS] 已加载。")

    @filter.command("df")
    async def on_delete_file_command(self, event: AstrMessageEvent, *args, **kwargs):
        """处理 /df <文件名> 指令"""
        group_id = int(event.get_group_id())
        user_id = int(event.get_user_id())

        # 1. 检查插件是否在此群启用
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        # 2. 检查权限
        if user_id not in self.admin_users:
            logger.warning(f"[{group_id}] 无权限用户 {user_id} 尝试执行删除操作。")
            await event.send("⚠️ 您没有执行此操作的权限。")
            return

        # 3. 检查指令参数
        command_args = event.get_command_args()
        if not command_args:
            await event.send("❓ 请提供要删除的文件名。用法: /df <文件名>")
            return
        
        target_filename = command_args[0]
        
        # 4. 创建异步任务执行删除流程，避免阻塞
        asyncio.create_task(self._handle_delete_flow(event, target_filename))

    async def _handle_delete_flow(self, event: AstrMessageEvent, filename: str):
        """完整的删除流程：查找文件 -> 执行删除"""
        group_id = int(event.get_group_id())
        logger.info(f"[{group_id}] 开始查找文件: '{filename}'")
        
        await event.send(f"正在查找文件「{filename}」，请稍候...")

        # 5. 查找文件以获取 file_id
        file_info = await self._find_file_by_name(event, filename)

        if not file_info:
            logger.warning(f"[{group_id}] 未能找到文件: '{filename}'")
            await event.send(f"❌ 未在群文件中找到名为「{filename}」的文件。")
            return

        file_id = file_info.get("file_id")
        if not file_id:
            logger.error(f"[{group_id}] 找到文件 '{filename}' 但其信息中缺少 file_id。")
            await event.send(f"❌ 找到文件「{filename}」，但无法获取其ID，删除失败。")
            return
            
        logger.info(f"[{group_id}] 找到文件 '{filename}', File ID: {file_id}。准备执行删除...")

        # 6. 调用 API 删除文件
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            
            delete_result = await client.api.call_action(
                'delete_group_file', 
                group_id=group_id, 
                file_id=file_id
            )

            # API调用本身不报错，但业务上可能失败，需要检查响应
            # 注意：根据go-cqhttp的实现，retcode为0通常表示成功
            if delete_result and delete_result.get('retcode') == 0:
                logger.info(f"[{group_id}] 成功删除文件: '{filename}'")
                await event.send(f"✅ 文件「{filename}」已成功删除。")
            else:
                error_msg = delete_result.get('wording', '未知错误')
                logger.error(f"[{group_id}] API调用删除文件 '{filename}' 失败: {error_msg}")
                await event.send(f"❌ 删除文件「{filename}」失败: {error_msg}")

        except Exception as e:
            logger.error(f"[{group_id}] 删除文件 '{filename}' 过程中发生异常: {e}")
            await event.send(f"❌ 删除文件时发生内部错误，请检查后台日志。")


    async def _find_file_by_name(self, event: AstrMessageEvent, filename: str) -> Optional[Dict]:
        """
        在群文件的根目录和所有一级子目录中查找文件。
        参考了您 file_checker 插件的遍历逻辑。
        """
        group_id = int(event.get_group_id())
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot

            # 查找根目录
            root_files_result = await client.api.call_action('get_group_root_files', group_id=group_id)
            if root_files_result and root_files_result.get('files'):
                for file_info in root_files_result['files']:
                    if file_info.get('file_name') == filename:
                        return file_info

            # 查找一级子目录
            if root_files_result and root_files_result.get('folders'):
                for folder in root_files_result['folders']:
                    folder_id = folder.get('folder_id')
                    if not folder_id:
                        continue
                    
                    sub_files_result = await client.api.call_action(
                        'get_group_files_by_folder', 
                        group_id=group_id, 
                        folder_id=folder_id
                    )
                    if sub_files_result and sub_files_result.get('files'):
                        for file_info in sub_files_result['files']:
                            if file_info.get('file_name') == filename:
                                return file_info
            
            return None # 遍历完成，未找到
        except Exception as e:
            logger.error(f"[{group_id}] 查找文件 '{filename}' 时发生异常: {e}")
            return None

    async def terminate(self):
        logger.info("插件 [群文件系统GroupFS] 已卸载。")