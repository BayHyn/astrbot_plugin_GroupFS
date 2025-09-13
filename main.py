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
        logger.info(f"生效群组: {self.group_whitelist if self.group_whitelist else '所有群'}")
        logger.info(f"管理员: {self.admin_users}")

    @filter.command("df")
    async def on_delete_file_command(self, event: AstrMessageEvent, filename: str | None = None):
        """
        处理 /df <文件名> 指令
        参数 'filename' 由 astrbot 框架从指令后自动提取并注入。
        """
        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())

        logger.info(f"[{group_id}] 用户 {user_id} 触发指令 /df, 框架解析参数为: '{filename}'")

        # 1. 检查插件是否在此群启用
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

        # 2. 检查权限
        if user_id not in self.admin_users:
            logger.warning(f"[{group_id}] 无权限用户 {user_id} 尝试执行删除操作。")
            await event.send("⚠️ 您没有执行此操作的权限。")
            return

        # 3. 检查指令参数 (由框架注入)
        if not filename:
            logger.warning(f"[{group_id}] 指令 /df 未提供文件名参数。")
            await event.send("❓ 请提供要删除的文件名。用法: /df <文件名>")
            return
        
        # 4. 创建异步任务执行删除流程，避免阻塞
        asyncio.create_task(self._handle_delete_flow(event, filename))
        
        # --- ここが修正点です ---
        # 5. 停止事件继续传播，防止被其他插件（如AI Agent）处理
        event.stop_event()

    async def _handle_delete_flow(self, event: AstrMessageEvent, filename: str):
        """完整的删除流程：查找文件 -> 执行删除"""
        group_id = int(event.get_group_id())
        logger.info(f"[{group_id}] 开始处理删除流程，目标文件: '{filename}'")
        
        await event.send(f"正在查找文件「{filename}」，请稍候...")

        # 5. 查找文件以获取 file_id
        file_info = await self._find_file_by_name(event, filename)

        if not file_info:
            logger.warning(f"[{group_id}] 在群文件中未能找到目标文件: '{filename}'")
            await event.send(f"❌ 未在群文件中找到名为「{filename}」的文件。")
            return

        file_id = file_info.get("file_id")
        if not file_id:
            logger.error(f"[{group_id}] 严重错误：找到文件 '{filename}' 但其信息中缺少 file_id。文件信息: {file_info}")
            await event.send(f"❌ 找到文件「{filename}」，但无法获取其ID，删除失败。")
            return
            
        logger.info(f"[{group_id}] 成功找到文件 '{filename}', File ID: {file_id}。准备执行删除...")

        # 6. 调用 API 删除文件
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot
            
            delete_result = await client.api.call_action(
                'delete_group_file', 
                group_id=group_id, 
                file_id=file_id
            )
            logger.info(f"[{group_id}] 'delete_group_file' API 响应: {delete_result}")

            if delete_result and delete_result.get('retcode') == 0:
                logger.info(f"[{group_id}] 成功删除文件: '{filename}'")
                await event.send(f"✅ 文件「{filename}」已成功删除。")
            else:
                error_msg = delete_result.get('wording', 'API未返回成功状态')
                logger.error(f"[{group_id}] API调用删除文件 '{filename}' 失败: {error_msg}")
                await event.send(f"❌ 删除文件「{filename}」失败: {error_msg}")

        except Exception as e:
            logger.error(f"[{group_id}] 在调用删除API时发生异常，文件: '{filename}', 错误: {e}", exc_info=True)
            await event.send(f"❌ 删除文件时发生内部错误，请检查后台日志。")

    async def _find_file_by_name(self, event: AstrMessageEvent, filename: str) -> Optional[Dict]:
        """在群文件的根目录和所有一级子目录中查找文件。"""
        group_id = int(event.get_group_id())
        logger.info(f"[{group_id}] 开始在服务器上遍历查找文件 '{filename}'...")
        try:
            assert isinstance(event, AiocqhttpMessageEvent)
            client = event.bot

            # 查找根目录
            logger.debug(f"[{group_id}] 正在扫描根目录...")
            root_files_result = await client.api.call_action('get_group_root_files', group_id=group_id)
            if root_files_result and root_files_result.get('files'):
                for file_info in root_files_result['files']:
                    if file_info.get('file_name') == filename:
                        logger.info(f"[{group_id}] 在根目录找到文件 '{filename}'。")
                        return file_info

            # 查找一级子目录
            if root_files_result and root_files_result.get('folders'):
                logger.debug(f"[{group_id}] 正在扫描 {len(root_files_result['folders'])} 个一级子目录...")
                for folder in root_files_result['folders']:
                    folder_id = folder.get('folder_id')
                    folder_name = folder.get('folder_name')
                    if not folder_id: continue
                    
                    logger.debug(f"[{group_id}] 正在扫描文件夹 '{folder_name}' (ID: {folder_id})...")
                    sub_files_result = await client.api.call_action(
                        'get_group_files_by_folder', 
                        group_id=group_id, 
                        folder_id=folder_id
                    )
                    if sub_files_result and sub_files_result.get('files'):
                        for file_info in sub_files_result['files']:
                            if file_info.get('file_name') == filename:
                                logger.info(f"[{group_id}] 在文件夹 '{folder_name}' 中找到文件 '{filename}'。")
                                return file_info
            
            logger.warning(f"[{group_id}] 扫描完所有位置，未找到文件 '{filename}'。")
            return None
        except Exception as e:
            logger.error(f"[{group_id}] 查找文件 '{filename}' 时发生API异常: {e}", exc_info=True)
            return None

    async def terminate(self):
        logger.info("插件 [群文件系统GroupFS] 已卸载。")
