# TGFS Manager Pyro Bot

import os
import re
import time
import aiohttp
import asyncio
import logging
from datetime import datetime
from dotenv import load_dotenv
import xml.etree.ElementTree as ET
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from typing import Dict, List, Optional
from pyrogram import utils as pyroutils
from urllib.parse import unquote, quote
from pyrogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

ROOT_FOLDER_NAME = os.getenv('TGFS_ROOT_CHANNEL_NAME', '') if os.getenv('TGFS_ROOT_CHANNEL_NAME', '') != '/' else ''

pyroutils.MIN_CHAT_ID = -999999999999
pyroutils.MIN_CHANNEL_ID = -100999999999999

class TGFSBot:
    def __init__(self):
        # Load environment variables
        self.bot_token = os.getenv('BOT_TOKEN')
        self.api_id = int(os.getenv('API_ID', '0'))
        self.api_hash = os.getenv('API_HASH', '')
        self.allowed_user_ids = [
            int(uid.strip()) for uid in os.getenv("ALLOWED_USER_IDS", "").split(",") if uid.strip()
        ]
        self.multi_index_bots = [
            token.strip() for token in os.getenv("INDEX_BOT_TOKENS", "").split(",") if token.strip()
        ]
        self.multi_index_bot_holder: Dict[str, Client] = {}
        self.storage_channel_id = int(os.getenv('STORAGE_CHANNEL_ID', '0'))
        self.enable_upload_records = os.getenv('ENABLE_UPLOAD_RECORDS', 'False').lower() in ['true', '1', 't']
        self.items_per_page = int(os.getenv('ITEMS_PER_PAGE', '10'))
        self.small_flood_wait = int(os.getenv('SMALL_FLOOD_WAIT', '10'))
        self.big_flood_wait = int(os.getenv('BIG_FLOOD_WAIT', '320'))
        self.path_cache: Dict[str, str] = {}
        self.reverse_path_cache: Dict[str, str] = {}
        self.cache_counter = 0
        self._bot_round_robin_index = 0

        # TGFS API settings
        self.tgfs_base_url = os.getenv('TGFS_BASE_URL', 'https://tg-webdav.mlwa.xyz')
        self.tgfs_username = os.getenv('TGFS_USERNAME', 'admin')
        self.tgfs_password = os.getenv('TGFS_PASSWORD', 'password')

        # Global state
        self.auth_token: Optional[str] = None
        self.user_sessions: Dict[int, dict] = {}
        self.file_sessions: Dict[str, dict] = {}
        self.import_sessions: Dict[str, dict] = {}
        self.index_sessions: Dict[str, dict] = {}

        # Initialize Pyrogram client variable
        self.app = None

        # Token Management
        self.token_last_checked = None
        self.token_check_interval = int(os.getenv('TOKEN_CHECK_INTERVAL', '3600'))
        self.token_check_task = None

    async def validate_token(self):
        """Validate the current token and refresh if needed"""
        if not self.auth_token:
            return await self.authenticate()

        try:
            headers = await self.get_auth_headers()
            test_path = quote(f'/webdav/{ROOT_FOLDER_NAME}', safe='')

            async with aiohttp.ClientSession() as session:
                async with session.get(
                        f"{self.tgfs_base_url}/api/tasks?path={test_path}",
                        headers=headers,
                        timeout=10
                ) as response:
                    if response.status == 200:
                        logger.info("Token validation successful")
                        return True
                    elif response.status == 401:
                        logger.warning("Token expired, refreshing...")
                        return await self.authenticate()
                    else:
                        logger.warning(f"Token validation failed with status: {response.status}")
                        return False

        except Exception as e:
            logger.error(f"Token validation error: {e}")
            return False

    async def token_validation_loop(self):
        """Periodic token validation loop"""
        while True:
            try:
                await asyncio.sleep(self.token_check_interval)
                logger.info("Running periodic token validation...")
                await self.validate_token()
                self.token_last_checked = datetime.now()
            except asyncio.CancelledError:
                logger.info("Token validation loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in token validation loop: {e}")
                await asyncio.sleep(300)

    async def start_token_validation(self):
        """Start the token validation background task"""
        if self.token_check_task and not self.token_check_task.done():
            self.token_check_task.cancel()

        self.token_check_task = asyncio.create_task(self.token_validation_loop())
        logger.info("Token validation task started")

    async def stop_token_validation(self):
        """Stop the token validation background task"""
        if self.token_check_task:
            self.token_check_task.cancel()
            try:
                await self.token_check_task
            except asyncio.CancelledError:
                pass
            logger.info("Token validation task stopped")

    def register_handlers(self):
        """Register all message and callback handlers"""

        # Group message handler (only for groups)
        @self.app.on_message(filters.group)
        async def handle_group_message(client, message: Message):
            await message.reply("Please use TGFS Indexer Bot in Private Chat.", reply_to_message_id=message.id)

        # Main message handler (only for private chats)
        @self.app.on_message(filters.private)
        async def handle_private_message(client, message: Message):
            # Check if user is authorized
            if message.from_user.id not in self.allowed_user_ids:
                await message.reply("Unauthorized", reply_to_message_id=message.id)
                return

            if message.forward_from_chat:
                user_id = message.from_user.id
                for index_session_id, index_session in list(self.index_sessions.items()):
                    if (index_session['user_id'] == user_id and
                            index_session.get('step') == 'waiting_forward'):
                        await self.handle_index_forward_message(client, message, index_session_id, index_session)
                        return

            items = list(self.import_sessions.items())
            display_msg = False
            for i, (import_session_id, import_session) in enumerate(items):
                if i == len(items) - 1:
                    display_msg = True
                user_id = message.from_user.id
                if (import_session['user_id'] == user_id and
                        import_session.get('waiting_for_files') and
                        any([message.video, message.document, message.audio, message.voice, message.video_note])):
                    await self.handle_import_session_file(client, message, import_session_id, import_session, display_message=display_msg)
                    return

            if message.photo:
                await message.reply("Please send this image as a file. 📃", reply_to_message_id=message.id)
                return

            if any([message.video, message.document, message.audio, message.voice, message.video_note]):
                await self.handle_media_upload(client, message)
            elif message.text and message.text.startswith('/'):
                await self.handle_command(client, message)
            elif message.text:
                # Handle text messages (for folder creation, channel indexing, etc.)
                await self.handle_text_message(client, message)

        @self.app.on_callback_query()
        async def handle_callback(client, callback_query: CallbackQuery):
            if callback_query.from_user.id not in self.allowed_user_ids:
                await callback_query.answer("Unauthorized", show_alert=True)
                return

            await self.handle_callback_query(client, callback_query)

    def get_path_hash(self, path: str) -> str:
        """Generate a short hash for a path"""
        if path in self.reverse_path_cache:
            return self.reverse_path_cache[path]

        # Use a simple counter-based hash to keep it short
        hash_str = f"p{self.cache_counter}"
        self.cache_counter += 1

        self.path_cache[hash_str] = path
        self.reverse_path_cache[path] = hash_str
        return hash_str

    def get_path_from_hash(self, hash_str: str) -> Optional[str]:
        """Retrieve path from hash"""
        return self.path_cache.get(hash_str)

    async def authenticate(self) -> bool:
        """Authenticate with TGFS API and store token"""
        try:
            async with aiohttp.ClientSession() as session:
                login_data = {
                    "username": self.tgfs_username,
                    "password": self.tgfs_password
                }

                async with session.post(
                        f"{self.tgfs_base_url}/login",
                        json=login_data,
                        headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        self.auth_token = result.get('token')
                        logger.info("Successfully authenticated with TGFS API")
                        return True
                    else:
                        logger.error(f"Authentication failed: {response.status}")
                        return False
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return False

    async def get_auth_headers(self) -> Dict[str, str]:
        """Get authorization headers, authenticate if needed"""
        if not self.auth_token:
            await self.authenticate()

        return {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json"
        }

    async def get_folder_structure(self, path: str = f"/webdav/{ROOT_FOLDER_NAME}") -> List[Dict]:
        """Get folder structure (both folders and files) from TGFS WebDAV"""
        try:
            headers = await self.get_auth_headers()
            headers["Depth"] = "1"

            async with aiohttp.ClientSession() as session:
                async with session.request(
                        "PROPFIND",
                        f"{self.tgfs_base_url}{path}",
                        headers=headers
                ) as response:
                    if response.status == 207:
                        xml_content = await response.text()
                        # Start background task to clean up completed tasks for this path
                        asyncio.create_task(self.cleanup_completed_tasks(path))

                        return self.parse_webdav_response(xml_content, path)
                    else:
                        logger.error(f"Failed to get folder structure: {response.status}")
                        return []
        except Exception as e:
            logger.error(f"Error getting folder structure: {e}")
            return []

    async def get_active_tasks(self, path: str) -> List[Dict]:
        """Get active tasks for a specific path from TGFS API"""
        try:
            headers = await self.get_auth_headers()

            # URL encode the path
            encoded_path = quote(path, safe='')

            async with aiohttp.ClientSession() as session:
                async with session.get(
                        f"{self.tgfs_base_url}/api/tasks?path={encoded_path}",
                        headers=headers
                ) as response:
                    if response.status == 200:
                        tasks = await response.json()
                        return tasks
                    else:
                        logger.error(f"Failed to get active tasks: {response.status}")
                        return []
        except Exception as e:
            logger.error(f"Error getting active tasks: {e}")
            return []

    async def cleanup_completed_tasks(self, path: str):
        """Clean up completed tasks for a specific path in the background"""
        try:
            # Get active tasks for the path
            tasks = await self.get_active_tasks(path)

            if not tasks:
                return

            # Filter completed tasks
            old_tasks = [task for task in tasks if task.get('status') in ['completed', 'failed', 'cancelled']]

            if not old_tasks:
                return

            logger.info(f"Found {len(old_tasks)} completed tasks to clean up for path: {path}")

            # Delete completed tasks
            delete_tasks = []
            for task in old_tasks:
                task_id = task.get('id')
                if task_id:
                    delete_tasks.append(self.delete_task(task_id))

            if delete_tasks:
                results = await asyncio.gather(*delete_tasks, return_exceptions=True)

                # Log results
                success_count = sum(1 for result in results if result is True)
                failure_count = len(results) - success_count

                if success_count > 0:
                    logger.info(f"Successfully deleted {success_count} completed tasks")
                if failure_count > 0:
                    logger.warning(f"Failed to delete {failure_count} tasks")

        except Exception as e:
            logger.error(f"Error in cleanup_completed_tasks: {e}")

    async def delete_task(self, task_id: str) -> bool:
        """Delete a specific task by ID"""
        try:
            headers = await self.get_auth_headers()

            async with aiohttp.ClientSession() as session:
                # First check if the task exists with OPTIONS
                async with session.options(
                        f"{self.tgfs_base_url}/api/tasks/{task_id}",
                        headers=headers
                ) as options_response:
                    if options_response.status != 200:
                        logger.debug(f"Task {task_id} doesn't exist or OPTIONS failed: {options_response.status}")
                        return False

                # Then delete the task
                async with session.delete(
                        f"{self.tgfs_base_url}/api/tasks/{task_id}",
                        headers=headers
                ) as delete_response:
                    if delete_response.status == 200:
                        logger.info(f"Successfully deleted completed task: {task_id}")
                        return True
                    else:
                        logger.error(f"Failed to delete task {task_id}: {delete_response.status}")
                        return False

        except Exception as e:
            logger.error(f"Error deleting task {task_id}: {e}")
            return False

    @staticmethod
    def parse_number_list(text: str) -> List[int]:
        text = text.strip()

        text = re.sub(r'[,\s]+', ' ', text)
        numbers = []

        for part in text.split():
            try:
                num = int(part)
                if num > 0:
                    numbers.append(num)
            except ValueError:
                continue

        return sorted(list(set(numbers)))

    @staticmethod
    def parse_webdav_response(xml_content: str, current_path: str) -> List[Dict]:
        """Parse WebDAV XML response to extract folder and file information"""
        items = []
        try:
            root = ET.fromstring(xml_content)

            # Define namespace
            namespaces = {'D': 'DAV:'}

            for response in root.findall('.//D:response', namespaces):
                href_elem = response.find('D:href', namespaces)
                if href_elem is not None:
                    href = href_elem.text

                    # Normalize paths for comparison
                    normalized_href = href.rstrip('/')
                    normalized_current = current_path.rstrip('/')

                    if normalized_href == normalized_current:
                        continue

                    # Check if it's a directory or file
                    collection_elem = response.find('.//D:collection', namespaces)
                    is_directory = collection_elem is not None

                    # Get display name
                    display_name_elem = response.find('.//D:displayname', namespaces)

                    if display_name_elem is not None and display_name_elem.text:
                        display_name = display_name_elem.text
                        # Check if this is a root-level item with wrong displayname
                        if (display_name == 'root' and
                                href.startswith('/webdav/') and
                                href.count('/') == 3):  # /webdav/folder_name/ pattern
                            # Extract name from href for root-level folders
                            clean_path = href[len('/webdav/'):].rstrip('/')
                            display_name = clean_path if clean_path else 'Root'
                    else:
                        # Fallback: extract from path
                        path_parts = href.rstrip('/').split('/')
                        display_name = path_parts[-1] if path_parts else href

                    # Get content length for files
                    content_length = 0
                    if not is_directory:
                        content_length_elem = response.find('.//D:getcontentlength', namespaces)
                        if content_length_elem is not None:
                            try:
                                content_length = int(content_length_elem.text)
                            except (ValueError, TypeError):
                                content_length = 0

                    items.append({
                        'path': href,
                        'name': display_name,
                        'is_directory': is_directory,
                        'size': content_length
                    })
        except Exception as e:
            logger.error(f"Error parsing WebDAV response: {e}")

        return items

    async def create_folder(self, folder_path: str) -> bool:
        """Create a new folder using WebDAV MKCOL"""
        try:
            headers = await self.get_auth_headers()
            del headers["Content-Type"]  # MKCOL doesn't need Content-Type

            # Ensure path ends with /
            if not folder_path.endswith('/'):
                folder_path += '/'

            async with aiohttp.ClientSession() as session:
                async with session.request(
                        "MKCOL",
                        f"{self.tgfs_base_url}{folder_path}",
                        headers=headers
                ) as response:
                    return response.status in [201, 204]
        except Exception as e:
            logger.error(f"Error creating folder: {e}")
            return False

    async def import_file(self, directory: str, filename: str, channel_id: int, message_id: int) -> bool:
        """Import file using TGFS API"""
        try:
            headers = await self.get_auth_headers()

            # Remove /webdav prefix from directory for import API
            if directory.startswith('/webdav'):
                directory = directory.removeprefix('/webdav')  # Remove '/webdav'

            import_data = {
                "directory": unquote(directory),
                "name": filename,
                "channel_id": int(str(channel_id).removeprefix("-100")),
                "message_id": message_id
            }

            async with aiohttp.ClientSession() as session:
                # Try up to 2 times (initial + 1 retry)
                for attempt in range(2):
                    async with session.post(
                            f"{self.tgfs_base_url}/api/import",
                            json=import_data,
                            headers=headers
                    ) as response:
                        if response.status == 200:
                            return True
                        else:
                            error_text = await response.text()
                            logger.error(
                                f"Import failed: {response.status} - {error_text}. Retrying one more time before giving up this file")
                            # If this is the last attempt, don't wait
                            if attempt < 1:
                                await asyncio.sleep(1)

                return False
        except Exception as e:
            logger.error(f"Error importing file: {e}")
            return False

    async def delete_item(self, item_path: str) -> bool:
        """Delete a file or folder using WebDAV DELETE"""
        try:
            headers = await self.get_auth_headers()
            if "Content-Type" in headers:
                del headers["Content-Type"]

            async with aiohttp.ClientSession() as session:
                async with session.request(
                        "DELETE",
                        f"{self.tgfs_base_url}{item_path}",
                        headers=headers,
                        timeout=300
                ) as response:
                    return response.status in [200, 204]
        except asyncio.TimeoutError:
            logger.error(f"Delete operation timed out for: {item_path}")
            return False
        except Exception as e:
            logger.error(f"Error deleting item: {e}")
            return False

    async def handle_command(self, client, message: Message):
        """Handle bot commands"""
        if message.text == '/start':
            await message.reply(
                "🤖 **TGFS File Manager Bot**\n\n"
                "Send me any file (photo, video, document, audio) and I'll help you organize it in your TGFS WEBDAV server!\n\n"
                "Commands:\n"
                "• Send or forward a file(s) - Start file organization and importing\n"
                "• /browse - Manage files, Upload/Import multiple files, Browse folder structure\n"
                "• /indexchannel - Index/Import files from a channel to TGFS Server"
            )
        elif message.text == '/browse':
            await self.handle_browse_command(client, message)
        elif message.text == '/indexchannel':
            await self.handle_index_channel_command(client, message)

    async def handle_text_message(self, client, message: Message):
        """Handle text messages for folder creation with file session support"""
        user_id = message.from_user.id

        for index_session_id, index_session in list(self.index_sessions.items()):
            if (index_session['user_id'] == user_id and
                    index_session.get('step') == 'waiting_forward' and
                    message.forward_from_chat):
                await self.handle_index_forward_message(client, message, index_session_id, index_session)
                return


        if not message.reply_to_message:
            return

        for index_session_id, index_session in list(self.index_sessions.items()):
            if (index_session['user_id'] == user_id and
                    index_session.get('expecting_reply_to') == message.reply_to_message.id):
                await self.handle_index_session_text(client, message, index_session_id, index_session)
                return

        for import_session_id, import_session in self.import_sessions.items():
            if (import_session['user_id'] == user_id and
                    import_session.get('expecting_reply_to') == message.reply_to_message.id):
                await self.handle_import_session_text(client, message, import_session_id, import_session)
                return

        # Find which file session this reply belongs to
        target_file_session_id = None
        for file_session_id, file_session in self.file_sessions.items():
            if (file_session['user_id'] == user_id and
                    file_session.get('expecting_reply_to') == message.reply_to_message.id):
                target_file_session_id = file_session_id
                break

        if not target_file_session_id:
            return

        file_session = self.file_sessions[target_file_session_id]

        if file_session.get('waiting_for') == 'delete_numbers':
            await self.handle_delete_multiple_input(client, message, target_file_session_id, file_session)
            return

        if file_session.get('waiting_for') == 'folder_name':
            folder_name = message.text.strip()
            target_path = file_session.get('target_path')

            if not folder_name:
                await message.reply("❌ Folder name cannot be empty. Please try again.",
                                    reply_to_message_id=message.reply_to_message.id)
                return

            # Create the new folder
            new_folder_path = target_path.rstrip('/') + f'/{folder_name}/'

            create_folder_message = await message.reply(
                f"🔄 Creating folder `{folder_name}`...",
                reply_to_message_id=message.reply_to_message.id
            )

            if await self.create_folder(new_folder_path):
                # Update file session and show the new folder
                await create_folder_message.delete()
                file_session['current_path'] = new_folder_path
                file_session['waiting_for'] = None
                file_session['target_path'] = None
                file_session['expecting_reply_to'] = None

                keyboard = await self.create_folder_keyboard(new_folder_path, target_file_session_id)

                # Update the original file message with new keyboard
                try:
                    await client.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=file_session.get('reply_message_id'),
                        text=(
                            f"📁 **File received:** `{file_session['file_info']['name']}`\n"
                            f"📏 **Size:** {self.format_file_size(file_session['file_info']['size'])}\n"
                            f"🗂 **Type:** {file_session['file_info']['type']}\n"
                            f"🛣️ **Current Path:** `{unquote(new_folder_path)}`\n\n"
                            f"**New Folder Created ✅**\n\n"
                            "**Please select destination folder:**"
                        ),
                        reply_markup=keyboard
                    )

                except Exception as e:
                    logger.error(f"Failed to update message.: {e}. Retrying with edit_message_reply_markup.")
                    # If that fails, try to update the buttons only
                    try:
                        await client.edit_message_reply_markup(
                            chat_id=message.chat.id,
                            message_id=file_session.get('reply_message_id'),
                            reply_markup=keyboard
                        )
                    except Exception as e2:
                        logger.error(f"Failed to update message: {e2}. Sending new message.")
                        await message.delete()
                        # Fallback: send new message
                        await message.reply(
                            f"✅ Folder `{folder_name}` created successfully!\n\n"
                            f"Current location: `{new_folder_path}`",
                            reply_markup=keyboard,
                            reply_to_message_id=file_session.get('reply_message_id')
                        )
            else:
                await message.reply(
                    "❌ Failed to create folder. Please try again.",
                    reply_to_message_id=message.reply_to_message.id
                )

            await message.delete()
            if 'prompt_message_id' in file_session:
                try:
                    await client.delete_messages(message.chat.id, file_session['prompt_message_id'])
                except Exception as e:
                    logger.error(f"Failed to delete prompt message: {e}")
                    pass

            # Clean up the waiting state
            file_session['waiting_for'] = None
            file_session['target_path'] = None

    async def handle_browse_command(self, client, message: Message):
        """Handle /browse command to browse folders without file upload"""
        user_id = message.from_user.id

        # Generate a unique browse session ID
        import_session_id = f"browse_{user_id}_{message.id}"
        current_path = f'/webdav/{ROOT_FOLDER_NAME}'

        # Store import session
        self.import_sessions[import_session_id] = {
            'user_id': user_id,
            'current_path': current_path,
            'created_time': time.time(),
            'current_page': 1,
            'items_per_page': self.items_per_page,
            'files_to_import': [],  # Store files for batch import
            'waiting_for_files': False,
            'original_message_id': message.id
        }

        # Send browsing message
        reply_msg = await message.reply(
            f"📁 **Browse Mode**\n"
            f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
            "**Select folder to browse or import files:**",
            reply_markup=await self.create_browse_keyboard(current_path, import_session_id),
            reply_to_message_id=message.id
        )

        # Store the reply message ID for later updates
        self.import_sessions[import_session_id]['reply_message_id'] = reply_msg.id

    async def handle_index_channel_command(self, client, message: Message):
        """Handle /indexchannel command"""
        user_id = message.from_user.id

        # Generate a unique index session ID
        index_session_id = f"index_{user_id}_{message.id}"

        # Store index session
        self.index_sessions[index_session_id] = {
            'user_id': user_id,
            'created_time': time.time(),
            'step': 'waiting_channel_id',
            'original_message_id': message.id
        }

        reply_msg = await message.reply(
            "📺 **Channel Indexing**\n\n"
            "Please send the channel ID (e.g., `-100123456789`).\n\n"
            "⚠️ **Important:**\n"
            "• The ID should be from a **CHANNEL** (not a group)\n"
            "• Add this bot as an **admin** to that channel\n"
            "• The bot needs permission to read messages\n\n"
            "• SEND YOUR INDEX CHANNEL ID AS A REPLY TO THIS MESSAGE\n\n"
            "• IF YOU ARE USING MULTI BOT MODE FOR INDEXING, MAKE SURE ALL YOUR BOTS ARE ADMINS IN BOTH TARGET AND SOURCE CHANNELS",
            reply_to_message_id=message.id
        )

        self.index_sessions[index_session_id]['reply_message_id'] = reply_msg.id
        self.index_sessions[index_session_id]['expecting_reply_to'] = reply_msg.id

    async def handle_index_session_text(self, client, message: Message, index_session_id: str, index_session: dict):
        """Handle text input for channel indexing"""
        if index_session.get('step') == 'waiting_channel_id':
            try:
                channel_id = int(message.text.strip())
                if channel_id >= 0:
                    await message.reply("❌ Channel ID must be negative (e.g., -100123456789)",
                                        reply_to_message_id=message.reply_to_message.id)
                    return
            except ValueError:
                await message.reply("❌ Invalid channel ID. Please send a valid number.",
                                    reply_to_message_id=message.reply_to_message.id)
                return

            await client.edit_message_text(
                chat_id=message.chat.id,
                message_id=index_session['reply_message_id'],
                text="🔍 **Testing channel access...**"
            )

            try:
                # Try to send a test message
                await client.send_message(
                    chat_id=channel_id,
                    text="Configure Message From TGFS Manager Bot, You can delete this message"
                )

                # Store channel info
                index_session['channel_id'] = channel_id
                index_session['step'] = 'waiting_forward'

                await client.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=index_session['reply_message_id'],
                    text=(
                        "✅ **Channel Access Confirmed**\n\n"
                        f"📺 **Channel ID:** `{channel_id}`\n\n"
                        "**Now forward a message from this channel to set the ending point for indexing.**\n"
                        "All messages from message ID 1 up to your forwarded message will be indexed.\n\n"
                        "**⚠️ IMPORTANT!\nThis forwarded message must be originally sent by a channel admin. "
                        "Do not forward a file that was already forwarded from another source.\n\n**"
                        "💁‍♂️ Tip: If you cannot find a message that originated from the target channel, simply send a new text message "
                        "to the channel and forward it to this bot."
                    )
                )

            except Exception as e:
                await client.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=index_session['reply_message_id'],
                    text=(
                        "❌ **Cannot access channel**\n\n"
                        "Please ensure:\n"
                        "• The channel ID is correct\n"
                        "• This bot is added as admin to the channel\n"
                        "• The channel is actually a channel (not a group)\n\n"
                        f"Error: `{str(e)}`"
                    )
                )

            await message.delete()

        elif index_session.get('waiting_for') == 'folder_name' and index_session.get('step') == 'select_path':
            # Parse folder name for index session
            folder_name = message.text.strip()
            target_path = index_session.get('target_path')

            if not folder_name:
                await message.reply("Folder name cannot be empty. Please try again.",
                                    reply_to_message_id=message.reply_to_message.id)
                return

            # Create the new folder
            new_folder_path = target_path.rstrip('/') + f'/{folder_name}/'

            create_folder_message = await message.reply(
                f"Creating folder `{folder_name}`...",
                reply_to_message_id=message.reply_to_message.id
            )

            if await self.create_folder(new_folder_path):
                await create_folder_message.delete()
                index_session['current_path'] = new_folder_path
                index_session['waiting_for'] = None
                index_session['target_path'] = None
                index_session['expecting_reply_to'] = None

                keyboard = await self.create_index_path_keyboard(new_folder_path, index_session_id)

                try:
                    await client.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=index_session.get('reply_message_id'),
                        text=(
                            f"**Endpoint Set**\n\n"
                            f"**Channel:** `{index_session['channel_id']}`\n"
                            f"**End Message ID:** `{index_session['end_message_id']}`\n\n"
                            f"**New Folder Created**\n\n"
                            f"**Select target folder for indexing:**"
                        ),
                        reply_markup=keyboard
                    )
                except Exception as e:
                    logger.error(f"Failed to update index message: {e}")

            else:
                await message.reply(
                    "Failed to create folder. Please try again.",
                    reply_to_message_id=message.reply_to_message.id
                )

            await message.delete()
            if 'prompt_message_id' in index_session:
                try:
                    await client.delete_messages(message.chat.id, index_session['prompt_message_id'])
                    del index_session['prompt_message_id']
                except Exception as e:
                    logger.error(f"Failed to delete prompt message: {e}")

        elif index_session.get('step') == 'waiting_skip_number':
            # Parse skip number and check for folder creation flag
            text = message.text.strip()

            # Check if user wants to create folders from filenames
            create_folders_flag = False
            skip_text = text

            if text.lower().startswith('folders ') or ' folders' in text.lower():
                create_folders_flag = True
                # Extract the skip number part
                skip_text = text.replace('folders', '').replace('FOLDERS', '').strip()

            try:
                skip_number = int(skip_text)
                if skip_number < 0:
                    await message.reply("❌ Skip number must be 0 or positive",
                                        reply_to_message_id=message.reply_to_message.id)
                    return
            except ValueError:
                await message.reply("❌ Invalid skip number. Please send a valid number.",
                                    reply_to_message_id=message.reply_to_message.id)
                return

            index_session['skip_number'] = skip_number
            index_session['create_folders'] = create_folders_flag
            index_session['step'] = 'ready_to_index'

            # Calculate actual range
            start_id = skip_number + 1
            end_id = index_session['end_message_id']
            total_messages = end_id - start_id + 1

            if total_messages <= 0:
                await client.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=index_session['reply_message_id'],
                    text="❌ **Invalid range**: Skip number is too high. No messages to index."
                )
                await message.delete()
                return

            folder_info = "✅ **Create folders from filenames**\n" if create_folders_flag else "📁 **Import to parent folder**\n"

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🚀 Start Indexing", callback_data=f"start_indexing:{index_session_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"cancel_indexing:{index_session_id}")
                ]
            ])

            await client.edit_message_text(
                chat_id=message.chat.id,
                message_id=index_session['reply_message_id'],
                text=(
                    f"📋 **Indexing Summary**\n\n"
                    f"📺 **Channel:** `{index_session['channel_id']}`\n"
                    f"📁 **Target Path:** `{unquote(index_session['target_path'])}`\n"
                    f"{folder_info}"
                    f"📊 **Range:** Messages {start_id} to {end_id}\n"
                    f"📈 **Total Messages:** {total_messages}\n"
                    f"⏭️ **Skip First:** {skip_number} messages\n\n"
                    "Ready to start indexing?"
                ),
                reply_markup=keyboard
            )

            await message.delete()
            if 'prompt_msg' in index_session:
                await index_session['prompt_msg'].delete()

    async def handle_index_forward_message(self, client, message: Message, index_session_id: str, index_session: dict):
        """Handle forwarded message for indexing endpoint"""
        if not message.forward_from_chat:
            await message.reply("❌ Please forward a message from the target channel.",
                                reply_to_message_id=message.reply_to_message.id)
            return

        forwarded_channel_id = message.forward_from_chat.id
        expected_channel_id = index_session.get('channel_id')

        if forwarded_channel_id != expected_channel_id:
            await message.reply(
                f"❌ Forwarded message is from wrong channel.\n"
                f"Expected: `{expected_channel_id}`\n"
                f"Got: `{forwarded_channel_id}`",
                reply_to_message_id=message.reply_to_message.id
            )
            return

        end_message_id = message.forward_from_message_id
        index_session['end_message_id'] = end_message_id
        index_session['step'] = 'select_path'

        # Show path selection
        current_path = f'/webdav/{ROOT_FOLDER_NAME}'
        index_session['current_path'] = current_path

        keyboard = await self.create_index_path_keyboard(current_path, index_session_id)

        await client.edit_message_text(
            chat_id=message.chat.id,
            message_id=index_session['reply_message_id'],
            text=(
                f"✅ **Endpoint Set**\n\n"
                f"📺 **Channel:** `{expected_channel_id}`\n"
                f"🔚 **End Message ID:** `{end_message_id}`\n\n"
                f"📁 **Select target folder for indexing:**"
            ),
            reply_markup=keyboard
        )

        await message.delete()

    async def create_index_path_keyboard(self, current_path: str, index_session_id: str) -> InlineKeyboardMarkup:
        """Create keyboard for path selection during indexing setup"""
        items = await self.get_folder_structure(current_path)
        keyboard = []

        # Add folder navigation
        folders = [item for item in items if item['is_directory']]

        for folder in folders:
            path_hash = self.get_path_hash(folder['path'])
            keyboard.append([
                InlineKeyboardButton(f"📁 {folder['name']}", callback_data=f"index_nav:{index_session_id}:{path_hash}")
            ])

        # Action buttons
        action_row_1 = []
        action_row_2 = []

        # Back button
        if current_path != f'/webdav/{ROOT_FOLDER_NAME}':
            parent_path = '/'.join(current_path.rstrip('/').split('/')[:-1])
            if parent_path == '/webdav':
                parent_path = f'/webdav/{ROOT_FOLDER_NAME}'
            parent_hash = self.get_path_hash(parent_path)
            action_row_1.append(
                InlineKeyboardButton("⬅️ Back", callback_data=f"index_nav:{index_session_id}:{parent_hash}")
            )

        # Select current path
        current_hash = self.get_path_hash(current_path)
        action_row_1.append(
            InlineKeyboardButton("✅ Select This Path",
                                 callback_data=f"index_select_path:{index_session_id}:{current_hash}")
        )

        # Create new folder
        action_row_2.append(
            InlineKeyboardButton("➕ New Folder", callback_data=f"index_newfolder:{index_session_id}:{current_hash}")
        )

        if action_row_1:
            keyboard.append(action_row_1)
        if action_row_2:
            keyboard.append(action_row_2)

        return InlineKeyboardMarkup(keyboard)

    async def start_channel_indexing(self, client, index_session_id: str):
        """Start the actual channel indexing process"""
        index_session = self.index_sessions[index_session_id]

        end_message_id = index_session['end_message_id']
        skip_number = index_session['skip_number']

        start_id = skip_number + 1
        total_messages = end_message_id - start_id + 1

        index_session['current_message_id'] = start_id
        index_session['total_messages'] = total_messages
        index_session['processed_count'] = 0
        index_session['success_count'] = 0
        index_session['failed_count'] = 0
        index_session['is_running'] = True
        index_session['start_time'] = time.time()

        # Start the indexing task
        indexing_task = asyncio.create_task(
            self.process_channel_indexing(client, index_session_id)
        )
        index_session['indexing_task'] = indexing_task

        # Start the progress monitoring task
        progress_task = asyncio.create_task(
            self.monitor_indexing_progress(client, index_session_id)
        )
        index_session['progress_task'] = progress_task

    async def get_next_bot_client(self) -> Client:
        """Get the next bot client in round-robin fashion"""
        if not self.multi_index_bot_holder:
            return self.app  # Fall back to main bot if no helpers

        # Get all bot IDs and sort them for consistent ordering
        bot_ids = sorted(self.multi_index_bot_holder.keys())

        # Get the next bot
        bot_id = bot_ids[self._bot_round_robin_index % len(bot_ids)]
        self._bot_round_robin_index += 1

        return self.multi_index_bot_holder[bot_id]

    async def process_channel_indexing(self, client, index_session_id: str):
        """Process channel indexing with multiple messages concurrently using all available bots"""
        index_session = self.index_sessions[index_session_id]

        channel_id = index_session['channel_id']
        end_message_id = index_session['end_message_id']
        target_path = index_session['target_path']
        current_message_id = index_session['current_message_id']

        # Get all available bots (main + helpers)
        all_bots = [self.app] + list(self.multi_index_bot_holder.values())
        batch_size = len(all_bots)

        try:
            while current_message_id <= end_message_id and index_session.get('is_running'):
                try:
                    # Clear any flood wait status
                    index_session['flood_wait_until'] = None
                    index_session['flood_wait_reason'] = None

                    # Calculate batch end ID
                    batch_end_id = min(current_message_id + batch_size - 1, end_message_id)

                    # Create tasks for each message in the batch
                    tasks = []
                    for message_id in range(current_message_id, batch_end_id + 1):
                        # Assign bot in round-robin fashion
                        bot_index = (message_id - current_message_id) % len(all_bots)
                        current_bot = all_bots[bot_index]

                        tasks.append(
                            self._process_single_message(
                                current_bot, channel_id, message_id, target_path, index_session
                            )
                        )

                    # Process batch concurrently
                    batch_results = await asyncio.gather(*tasks, return_exceptions=True)

                    if not index_session.get('is_running'):
                        break

                    # Update counts
                    for result in batch_results:
                        if isinstance(result, Exception):
                            index_session['failed_count'] += 1
                        elif result is True:
                            index_session['success_count'] += 1
                        elif result is False:
                            index_session['failed_count'] += 1
                        # None results are for non-media messages, don't count as failures

                    index_session['processed_count'] += len(tasks)
                    current_message_id = batch_end_id + 1
                    index_session['current_message_id'] = current_message_id

                    # Small delay between batches to avoid rate limits
                    if not index_session.get('flood_wait_until'):
                        await asyncio.sleep(self.small_flood_wait)

                except Exception as e:
                    logger.error(f"Error processing batch starting at {current_message_id}: {e}")
                    index_session['failed_count'] += batch_size
                    current_message_id += batch_size
                    index_session['current_message_id'] = current_message_id

            # Mark as completed
            index_session['is_running'] = False
            index_session['completed'] = True

        except Exception as e:
            logger.error(f"Channel indexing error: {e}")
            index_session['is_running'] = False
            index_session['error'] = str(e)

    async def _process_single_message(self, bot_client: Client, channel_id: int, message_id: int,
                                      target_path: str, index_session: dict) -> Optional[bool]:
        """Process a single message with the specified bot client"""
        try:
            # Get message with flood wait handling
            msg = await self._get_message_with_flood_wait(bot_client, channel_id, message_id, index_session)

            if not index_session.get('is_running'):
                return None

            if msg and not msg.empty:
                # Check if message has media
                file_info = self.extract_file_info(msg)

                if file_info:
                    # Determine final target path based on folder creation setting
                    final_target_path = target_path
                    create_folders = index_session.get('create_folders', False)

                    if create_folders:
                        # Create folder from filename
                        folder_name = os.path.splitext(file_info['name'])[0]
                        folder_path = target_path.rstrip('/') + f'/{folder_name}/'

                        # Create the folder if it doesn't exist
                        if not await self.create_folder(folder_path):
                            logger.error(f"Failed to create folder: {folder_path}")
                        else:
                            final_target_path = folder_path

                    # Send to storage channel with flood wait handling
                    storage_msg = await self._send_to_storage_with_flood_wait(
                        bot_client, msg, file_info, channel_id, index_session
                    )

                    if not index_session.get('is_running'):
                        return None

                    if storage_msg:
                        # Import to TGFS
                        success = await self.import_file(
                            directory=final_target_path,
                            filename=file_info['name'],
                            channel_id=self.storage_channel_id,
                            message_id=storage_msg.id
                        )
                        return success
                    else:
                        return False
            # Return None for non-media messages (don't count as failure)
            return None
        except Exception as e:
            logger.error(f"Error processing message {message_id}: {e}")
            return False

    async def _get_message_with_flood_wait(self, bot_client: Client, channel_id: int, message_id: int,
                                           index_session: dict):
        """Get message with flood wait handling using specified bot client"""
        max_retries = 3
        retry_count = 0

        while retry_count < max_retries:
            try:
                return await bot_client.get_messages(channel_id, message_id)
            except FloodWait as e:
                wait_time = e.value
                cooldown_time = self.big_flood_wait
                total_wait = wait_time + cooldown_time

                logger.info(f"FloodWait on bot {bot_client.name if hasattr(bot_client, 'name') else 'main'}: {wait_time}s + {cooldown_time}s cooldown = {total_wait}s total")

                # Store flood wait info for progress display
                index_session['flood_wait_until'] = time.time() + total_wait
                index_session['flood_wait_reason'] = f"get_messages (message {message_id}) using bot {bot_client.name if hasattr(bot_client, 'name') else 'main'}"

                await asyncio.sleep(total_wait)
                retry_count += 1

            except Exception as e:
                logger.error(f"Error getting message {message_id} with bot {bot_client.name if hasattr(bot_client, 'name') else 'main'}: {e}")
                if retry_count < max_retries - 1:
                    await asyncio.sleep(2)
                    retry_count += 1
                else:
                    raise

        return None

    async def _send_to_storage_with_flood_wait(self, bot_client: Client, msg, file_info: dict, source_channel_id: int,
                                               index_session: dict):
        """Send file to storage channel with flood wait handling using specified bot client"""
        max_retries = 3
        retry_count = 0

        while retry_count < max_retries:
            try:
                # Send based on media type
                if msg.video:
                    return await bot_client.send_video(
                        chat_id=self.storage_channel_id,
                        video=file_info['file_id'],
                        caption=f"📺 Indexed from {source_channel_id} | {file_info['name']}"
                    )
                elif msg.document:
                    return await bot_client.send_document(
                        chat_id=self.storage_channel_id,
                        document=file_info['file_id'],
                        caption=f"📺 Indexed from {source_channel_id} | {file_info['name']}"
                    )
                elif msg.audio:
                    return await bot_client.send_audio(
                        chat_id=self.storage_channel_id,
                        audio=file_info['file_id'],
                        caption=f"📺 Indexed from {source_channel_id} | {file_info['name']}"
                    )
                elif msg.voice:
                    return await bot_client.send_voice(
                        chat_id=self.storage_channel_id,
                        voice=file_info['file_id'],
                        caption=f"📺 Indexed from {source_channel_id} | {file_info['name']}"
                    )
                elif msg.video_note:
                    return await bot_client.send_video_note(
                        chat_id=self.storage_channel_id,
                        video_note=file_info['file_id']
                    )

            except FloodWait as e:
                wait_time = e.value
                cooldown_time = self.big_flood_wait
                total_wait = wait_time + cooldown_time

                logger.info(f"FloodWait on bot {bot_client.name if hasattr(bot_client, 'name') else 'main'} send: {wait_time}s + {cooldown_time}s cooldown = {total_wait}s total")

                # Store flood wait info for progress display
                index_session['flood_wait_until'] = time.time() + total_wait
                index_session['flood_wait_reason'] = f"send_to_storage ({file_info['name']}) using bot {bot_client.name if hasattr(bot_client, 'name') else 'main'}"

                await asyncio.sleep(total_wait)
                retry_count += 1

            except Exception as e:
                logger.error(f"Error sending file {file_info['name']} with bot {bot_client.name if hasattr(bot_client, 'name') else 'main'}: {e}")
                if retry_count < max_retries - 1:
                    await asyncio.sleep(2)
                    retry_count += 1
                else:
                    raise

        return None

    async def monitor_indexing_progress(self, client, index_session_id: str):
        """Monitor and update indexing progress every 5 seconds with flood wait display"""
        index_session = self.index_sessions[index_session_id]

        while index_session.get('is_running'):
            try:
                await asyncio.sleep(5)

                if not index_session.get('is_running'):
                    break

                # Calculate progress
                processed = index_session.get('processed_count', 0)
                total = index_session.get('total_messages', 1)
                success = index_session.get('success_count', 0)
                failed = index_session.get('failed_count', 0)
                current_id = index_session.get('current_message_id', 0)

                folder_mode = "✅ Folders from filenames" if index_session.get('create_folders') else "📁 Parent folder"

                progress_percent = (processed / total) * 100
                elapsed_time = time.time() - index_session.get('start_time', time.time())

                # Check for flood wait status
                flood_wait_until = index_session.get('flood_wait_until')
                flood_wait_reason = index_session.get('flood_wait_reason', '')

                # Add bot usage info
                bot_count = len(self.multi_index_bot_holder) + 1  # +1 for main bot
                bot_info = f"🤖 **Bots Active:** {bot_count} (main + {len(self.multi_index_bot_holder)} helpers)\n"

                status_line = ""
                if flood_wait_until and time.time() < flood_wait_until:
                    remaining_wait = int(flood_wait_until - time.time())
                    status_line = f"⏳ **FLOOD WAIT:** {remaining_wait}s remaining\n📝 **Reason:** {flood_wait_reason}\n\n"
                    eta_text = f"Paused (flood wait)"
                else:
                    # Estimate time remaining
                    if processed > 0:
                        time_per_message = elapsed_time / processed
                        remaining_messages = total - processed
                        eta_seconds = time_per_message * remaining_messages
                        eta_text = f"{int(eta_seconds // 60)}m {int(eta_seconds % 60)}s"
                    else:
                        eta_text = "Calculating..."

                bar_length = 20
                filled_length = int(bar_length * processed // total)
                bar = "█" * filled_length + "░" * (bar_length - filled_length)

                # Stop button
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⏹️ Stop Indexing", callback_data=f"stop_indexing:{index_session_id}")]
                ])

                progress_text = (
                    f"📊 **Channel Indexing Progress**\n\n"
                    f"📺 **Channel:** `{index_session['channel_id']}`\n"
                    f"📁 **Target:** `{unquote(index_session['target_path'])}`\n"
                    f"📂 **Mode:** {folder_mode}\n\n"
                    f"{bot_info}"
                    f"{status_line}"
                    f"🔄 **Progress:** {progress_percent:.1f}%\n"
                    f"`{bar}` {processed}/{total}\n\n"
                    f"📨 **Current Message ID:** {current_id}\n"
                    f"✅ **Success:** {success}\n"
                    f"⚠️ **Unsupported/Text Messages:** {max(0, processed - (success + failed))}\n"
                    f"❌ **Failed:** {failed}\n"
                    f"⏱️ **Elapsed:** {int(elapsed_time // 60)}m {int(elapsed_time % 60)}s\n"
                    f"🕐 **ETA:** {eta_text}"
                )

                await client.edit_message_text(
                    chat_id=index_session['user_id'],
                    message_id=index_session['reply_message_id'],
                    text=progress_text,
                    reply_markup=keyboard
                )

            except Exception as e:
                logger.error(f"Error updating progress: {e}")

        # Final update when completed
        if index_session.get('completed'):
            total_time = time.time() - index_session.get('start_time', time.time())
            folder_mode = "with folders from filenames" if index_session.get('create_folders') else "to parent folder"
            await client.edit_message_text(
                chat_id=index_session['user_id'],
                message_id=index_session['reply_message_id'],
                text=(
                    f"🎉 **Channel Indexing Complete!**\n\n"
                    f"📺 **Channel:** `{index_session['channel_id']}`\n"
                    f"📁 **Target:** `{unquote(index_session['target_path'])}`\n"
                    f"📂 **Mode:** {folder_mode}\n\n"
                    f"🤖 **Bots Used:** {len(self.multi_index_bot_holder) + 1}\n\n"
                    f"📊 **Results:**\n"
                    f"✅ **Success:** {index_session.get('success_count', 0)}\n"
                    f"⚠️ **Unsupported/Text Messages:** {max(0, index_session.get('total_messages', 0) -
                                                             (index_session.get('success_count', 0) + index_session.get('failed_count', 0)))}\n"
                    f"❌ **Failed:** {index_session.get('failed_count', 0)}\n"
                    f"📈 **Total Processed:** {index_session.get('processed_count', 0)}\n"
                    f"⏱️ **Total Time:** {int(total_time // 60)}m {int(total_time % 60)}s"
                )
            )

    async def handle_delete_multiple_input(self, client, message: Message, file_session_id: str, file_session: dict):
        """Handle user input for delete multiple numbers"""
        numbers = self.parse_number_list(message.text)

        if not numbers:
            await message.reply("❌ No valid numbers found. Please send numbers like '1,2,3' or '1 2 3'.",
                                reply_to_message_id=message.reply_to_message.id)
            return

        # Get current folder items
        current_path = file_session['current_path']
        items = await self.get_folder_structure(current_path)

        # Separate folders and files, then combine
        folders = [item for item in items if item['is_directory']]
        files = [item for item in items if not item['is_directory']]
        all_items = folders + files

        # Validate numbers
        valid_numbers = [n for n in numbers if 1 <= n <= len(all_items)]
        invalid_numbers = [n for n in numbers if n not in valid_numbers]

        if invalid_numbers:
            await message.reply(
                f"❌ Invalid numbers: {', '.join(map(str, invalid_numbers))}. "
                f"Valid range: 1-{len(all_items)}",
                reply_to_message_id=message.reply_to_message.id
            )
            return

        # Get items to delete
        items_to_delete = [all_items[n - 1] for n in valid_numbers]

        # Store in session for confirmation
        file_session['delete_multiple_items'] = items_to_delete
        file_session['delete_multiple_numbers'] = valid_numbers
        file_session['waiting_for'] = None
        file_session['expecting_reply_to'] = None

        # Create confirmation message
        items_list = "\n".join([f"{n}. {'📁' if item['is_directory'] else '📄'} {item['name']}"
                                for n, item in zip(valid_numbers, items_to_delete)])

        confirmation_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, Delete Selected",
                                     callback_data=f"delete_multiple_yes:{file_session_id}:{file_session['current_hash']}"),
                InlineKeyboardButton("❌ Cancel",
                                     callback_data=f"delete_multiple_no:{file_session_id}:{file_session['current_hash']}")
            ]
        ])

        # Update the original message
        try:
            await client.edit_message_text(
                chat_id=message.chat.id,
                message_id=file_session.get('reply_message_id'),
                text=(
                    f"🗑️ **Confirm Multiple Deletion**\n\n"
                    f"You selected {len(valid_numbers)} items to delete:\n\n"
                    f"{items_list}\n\n"
                    "Are you sure you want to delete these items?"
                ),
                reply_markup=confirmation_keyboard
            )
        except Exception as e:
            logger.error(f"Failed to update message: {e}")

        # Clean up messages
        await message.delete()
        if 'prompt_message_id' in file_session:
            try:
                await client.delete_messages(message.chat.id, file_session['prompt_message_id'])
                del file_session['prompt_message_id']
            except Exception as e:
                logger.error(f"Failed to delete prompt message: {e}")

    async def handle_media_upload(self, client, message: Message):
        """Handle file uploads from user - supports multiple files"""
        user_id = message.from_user.id

        # Determine file info
        file_info = self.extract_file_info(message)
        if not file_info:
            await message.reply("❌ Unsupported file type", reply_to_message_id=message.id)
            return

        # Generate a unique session ID for this file
        file_session_id = f"{user_id}_{message.id}"
        current_path = f'/webdav/{ROOT_FOLDER_NAME}'

        # Store file session
        self.file_sessions[file_session_id] = {
            'original_message': message,
            'file_info': file_info,
            'current_path': current_path,
            'user_id': user_id,
            'created_time': time.time(),
            'waiting_for': None,
            'target_path': None,
            'expecting_reply_to': None,
            'current_page': 1,
            'items_per_page': self.items_per_page
        }

        # Store in user session for easy access
        if user_id not in self.user_sessions:
            self.user_sessions[user_id] = {'file_sessions': []}
        self.user_sessions[user_id]['file_sessions'].append(file_session_id)

        # Send file received message with reply to the original file
        reply_msg = await message.reply(
            f"📁 **File received:** `{file_info['name']}`\n"
            f"📏 **Size:** {self.format_file_size(file_info['size'])}\n"
            f"🗂 **Type:** {file_info['type']}\n"
            f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
            "**Please select destination folder:**",
            reply_to_message_id=message.id,
            reply_markup=await self.create_folder_keyboard(current_path, file_session_id)
        )

        # Store the reply message ID for later updates
        self.file_sessions[file_session_id]['reply_message_id'] = reply_msg.id

    @staticmethod
    def extract_file_info(message: Message) -> Optional[Dict]:
        """Extract file information from message"""
        if message.video:
            return {
                'name': message.video.file_name or f"video_{message.id}.mp4",
                'size': message.video.file_size,
                'type': 'Video',
                'file_id': message.video.file_id
            }
        elif message.document:
            return {
                'name': message.document.file_name or f"document_{message.id}",
                'size': message.document.file_size,
                'type': 'Document',
                'file_id': message.document.file_id
            }
        elif message.audio:
            return {
                'name': message.audio.file_name or f"audio_{message.id}.mp3",
                'size': message.audio.file_size,
                'type': 'Audio',
                'file_id': message.audio.file_id
            }
        elif message.voice:
            return {
                'name': f"voice_{message.id}.ogg",
                'size': message.voice.file_size,
                'type': 'Voice',
                'file_id': message.voice.file_id
            }
        elif message.video_note:
            return {
                'name': f"video_note_{message.id}.mp4",
                'size': message.video_note.file_size,
                'type': 'Video Note',
                'file_id': message.video_note.file_id
            }
        return None

    @staticmethod
    def format_file_size(size_bytes: int) -> str:
        """Format file size in human readable format"""
        if size_bytes == 0:
            return "0 B"
        size_names = ["B", "KB", "MB", "GB", "TB"]
        import math
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_names[i]}"

    async def create_folder_keyboard(self, current_path: str, file_session_id: str) -> InlineKeyboardMarkup:
        """Create inline keyboard for folder and file navigation with pagination"""
        items = await self.get_folder_structure(current_path)
        keyboard = []

        # Get pagination info from file session
        file_session = self.file_sessions.get(file_session_id, {})
        current_page = file_session.get('current_page', 1)
        items_per_page = file_session.get('items_per_page', 10)

        # Separate folders and files
        folders = [item for item in items if item['is_directory']]
        files = [item for item in items if not item['is_directory']]
        all_items = folders + files

        # Calculate pagination
        total_items = len(all_items)
        total_pages = (total_items + items_per_page - 1) // items_per_page

        # Add pagination numbers at top, if more than 1 page
        if total_pages > 1:
            page_buttons = []

            if total_pages <= 10:
                # Show all page numbers
                for page in range(1, total_pages + 1):
                    button_text = f"• {page} •" if page == current_page else str(page)
                    page_buttons.append(
                        InlineKeyboardButton(button_text, callback_data=f"page:{file_session_id}:{page}")
                    )
            else:
                # Handle more than 10 pages
                if current_page <= 8:
                    # Show pages 1-8, ..., last_page
                    for page in range(1, 9):
                        button_text = f"• {page} •" if page == current_page else str(page)
                        page_buttons.append(
                            InlineKeyboardButton(button_text, callback_data=f"page:{file_session_id}:{page}")
                        )
                    page_buttons.append(
                        InlineKeyboardButton("...", callback_data=f"page:{file_session_id}:{total_pages}")
                    )
                    page_buttons.append(
                        InlineKeyboardButton(str(total_pages), callback_data=f"page:{file_session_id}:{total_pages}")
                    )
                else:
                    # Show 1, ..., current_page-1, current_page, current_page+1, ..., last_page
                    page_buttons.append(
                        InlineKeyboardButton("1", callback_data=f"page:{file_session_id}:1")
                    )
                    page_buttons.append(
                        InlineKeyboardButton("...", callback_data=f"page:{file_session_id}:1")
                    )

                    for page in range(max(2, current_page - 1), min(total_pages, current_page + 2)):
                        button_text = f"• {page} •" if page == current_page else str(page)
                        page_buttons.append(
                            InlineKeyboardButton(button_text, callback_data=f"page:{file_session_id}:{page}")
                        )

                    if current_page < total_pages - 1:
                        page_buttons.append(
                            InlineKeyboardButton("...", callback_data=f"page:{file_session_id}:{total_pages}")
                        )
                    page_buttons.append(
                        InlineKeyboardButton(str(total_pages), callback_data=f"page:{file_session_id}:{total_pages}")
                    )

            # Add page buttons in rows of 5
            for i in range(0, len(page_buttons), 5):
                keyboard.append(page_buttons[i:i + 5])

        # Get items for current page
        start_idx = (current_page - 1) * items_per_page
        end_idx = start_idx + items_per_page
        page_items = all_items[start_idx:end_idx]

        # Add item buttons (1 per row) with numbering
        item_number = 1
        for item in page_items:
            if item['is_directory']:
                path_hash = self.get_path_hash(item['path'])
                keyboard.append([
                    InlineKeyboardButton(f"{item_number}. 📁 {item['name']}", callback_data=f"nav:{file_session_id}:{path_hash}")
                ])
            else:
                file_hash = self.get_path_hash(item['path'])
                keyboard.append([
                    InlineKeyboardButton(
                        f"{item_number}. 📄 {item['name']} ({self.format_file_size(item['size'])})",
                        callback_data=f"file_delete_confirm:{file_session_id}:{file_hash}"
                    )
                ])
            item_number += 1

        # Add action buttons
        action_row_1 = []
        action_row_2 = []
        action_row_3 = []

        # Back button (if not at root)
        if current_path != f'/webdav/{ROOT_FOLDER_NAME}':
            parent_path = '/'.join(current_path.rstrip('/').split('/')[:-1])
            if parent_path == '/webdav':
                parent_path = f'/webdav/{ROOT_FOLDER_NAME}'

            parent_hash = self.get_path_hash(parent_path)
            action_row_1.append(
                InlineKeyboardButton("⬅️ Back", callback_data=f"nav:{file_session_id}:{parent_hash}")
            )

        # Select current location (for file uploads)
        current_hash = self.get_path_hash(current_path)
        action_row_1.append(
            InlineKeyboardButton("✅ Select Here", callback_data=f"select:{file_session_id}:{current_hash}")
        )

        # Create new folder
        action_row_2.append(
            InlineKeyboardButton("➕ New Folder", callback_data=f"newfolder:{file_session_id}:{current_hash}")
        )

        # Create folder from filename
        action_row_2.append(
            InlineKeyboardButton("📂 Folder from File Name",
                                 callback_data=f"folderfromname:{file_session_id}:{current_hash}")
        )

        # Delete current folder button (if not at root)
        if current_path != f'/webdav/{ROOT_FOLDER_NAME}':
            action_row_3.append(
                InlineKeyboardButton("🗑️ Delete This Folder",
                                     callback_data=f"delete_confirm:{file_session_id}:{current_hash}")
            )

        # Delete multiple items button (if there are items to delete)
        if all_items and current_path != f'/webdav/{ROOT_FOLDER_NAME}':
            action_row_3.append(
                InlineKeyboardButton("🗑️ Delete Multiple",
                                     callback_data=f"delete_multiple:{file_session_id}:{current_hash}")
            )

        if action_row_1:
            keyboard.append(action_row_1)
        if action_row_2:
            keyboard.append(action_row_2)
        if action_row_3:
            keyboard.append(action_row_3)

        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    async def create_delete_confirmation_keyboard(file_session_id: str, item_hash: str, is_file: bool = False) -> InlineKeyboardMarkup:
        """Create confirmation keyboard for delete operations"""
        item_type = "file" if is_file else "folder"
        keyboard = [
            [
                InlineKeyboardButton(f"✅ Yes, Delete {item_type}", callback_data=f"delete_yes:{file_session_id}:{item_hash}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"delete_no:{file_session_id}:{item_hash}")
            ]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def create_browse_keyboard(self, current_path: str, import_session_id: str) -> InlineKeyboardMarkup:
        """Create inline keyboard for browsing without file selection"""
        items = await self.get_folder_structure(current_path)
        keyboard = []

        # Get pagination info from import session
        import_session = self.import_sessions.get(import_session_id, {})
        current_page = import_session.get('current_page', 1)
        items_per_page = import_session.get('items_per_page', 10)

        # Separate folders and files
        folders = [item for item in items if item['is_directory']]
        files = [item for item in items if not item['is_directory']]
        all_items = folders + files

        # Calculate pagination
        total_items = len(all_items)
        total_pages = (total_items + items_per_page - 1) // items_per_page

        # Add pagination numbers at top (if more than 1 page)
        if total_pages > 1:
            page_buttons = []
            if total_pages <= 10:
                for page in range(1, total_pages + 1):
                    button_text = f"• {page} •" if page == current_page else str(page)
                    page_buttons.append(
                        InlineKeyboardButton(button_text, callback_data=f"browse_page:{import_session_id}:{page}")
                    )
            else:
                # Handle more than 10 pages (similar logic as before)
                if current_page <= 8:
                    for page in range(1, 9):
                        button_text = f"• {page} •" if page == current_page else str(page)
                        page_buttons.append(
                            InlineKeyboardButton(button_text, callback_data=f"browse_page:{import_session_id}:{page}")
                        )
                    page_buttons.append(
                        InlineKeyboardButton("...", callback_data=f"browse_page:{import_session_id}:{total_pages}")
                    )
                    page_buttons.append(
                        InlineKeyboardButton(str(total_pages),
                                             callback_data=f"browse_page:{import_session_id}:{total_pages}")
                    )
                else:
                    page_buttons.append(
                        InlineKeyboardButton("1", callback_data=f"browse_page:{import_session_id}:1")
                    )
                    page_buttons.append(
                        InlineKeyboardButton("...", callback_data=f"browse_page:{import_session_id}:1")
                    )
                    for page in range(max(2, current_page - 1), min(total_pages, current_page + 2)):
                        button_text = f"• {page} •" if page == current_page else str(page)
                        page_buttons.append(
                            InlineKeyboardButton(button_text, callback_data=f"browse_page:{import_session_id}:{page}")
                        )
                    if current_page < total_pages - 1:
                        page_buttons.append(
                            InlineKeyboardButton("...", callback_data=f"browse_page:{import_session_id}:{total_pages}")
                        )
                    page_buttons.append(
                        InlineKeyboardButton(str(total_pages),
                                             callback_data=f"browse_page:{import_session_id}:{total_pages}")
                    )

            # Add page buttons in rows of 5
            for i in range(0, len(page_buttons), 5):
                keyboard.append(page_buttons[i:i + 5])

        # Get items for current page
        start_idx = (current_page - 1) * items_per_page
        end_idx = start_idx + items_per_page
        page_items = all_items[start_idx:end_idx]

        # Add item buttons (1 per row) with numbering
        item_number = 1
        for item in page_items:
            if item['is_directory']:
                path_hash = self.get_path_hash(item['path'])
                keyboard.append([
                    InlineKeyboardButton(f"{item_number}. 📁 {item['name']}",
                                         callback_data=f"browse_nav:{import_session_id}:{path_hash}")
                ])
            else:
                file_hash = self.get_path_hash(item['path'])
                keyboard.append([
                    InlineKeyboardButton(
                        f"{item_number}. 📄 {item['name']} ({self.format_file_size(item['size'])})",
                        callback_data=f"browse_file_delete:{import_session_id}:{file_hash}"
                    )
                ])
            item_number += 1

        # Add action buttons
        action_row_1 = []
        action_row_2 = []
        action_row_3 = []

        # Back button (if not at root)
        if current_path != f'/webdav/{ROOT_FOLDER_NAME}':
            parent_path = '/'.join(current_path.rstrip('/').split('/')[:-1])
            if parent_path == '/webdav':
                parent_path = f'/webdav/{ROOT_FOLDER_NAME}'
            parent_hash = self.get_path_hash(parent_path)
            action_row_1.append(
                InlineKeyboardButton("⬅️ Back", callback_data=f"browse_nav:{import_session_id}:{parent_hash}")
            )

        # Import Files button
        current_hash = self.get_path_hash(current_path)
        action_row_1.append(
            InlineKeyboardButton("📥 Import Files", callback_data=f"import_files:{import_session_id}:{current_hash}")
        )

        # Create new folder
        action_row_2.append(
            InlineKeyboardButton("➕ New Folder", callback_data=f"browse_newfolder:{import_session_id}:{current_hash}")
        )

        # Delete current folder button (if not at root)
        if current_path != f'/webdav/{ROOT_FOLDER_NAME}':
            action_row_3.append(
                InlineKeyboardButton("🗑️ Delete This Folder",
                                     callback_data=f"browse_delete_confirm:{import_session_id}:{current_hash}")
            )

        # Delete multiple items button (if there are items to delete)
        if all_items and current_path != f'/webdav/{ROOT_FOLDER_NAME}':
            action_row_3.append(
                InlineKeyboardButton("🗑️ Delete Multiple",
                                     callback_data=f"browse_delete_multiple:{import_session_id}:{current_hash}")
            )

        if action_row_1:
            keyboard.append(action_row_1)
        if action_row_2:
            keyboard.append(action_row_2)
        if action_row_3:
            keyboard.append(action_row_3)

        return InlineKeyboardMarkup(keyboard)

    async def handle_import_session_text(self, client, message: Message, import_session_id: str, import_session: dict):
        """Handle text messages for import sessions (browse mode)"""
        if import_session.get('waiting_for') == 'folder_name':
            folder_name = message.text.strip()
            target_path = import_session.get('target_path')

            if not folder_name:
                await message.reply("❌ Folder name cannot be empty. Please try again.",
                                    reply_to_message_id=message.reply_to_message.id)
                return

            # Create the new folder
            new_folder_path = target_path.rstrip('/') + f'/{folder_name}/'

            create_folder_message = await message.reply(
                f"🔄 Creating folder `{folder_name}`...",
                reply_to_message_id=message.reply_to_message.id
            )

            if await self.create_folder(new_folder_path):
                await create_folder_message.delete()
                import_session['current_path'] = new_folder_path
                import_session['waiting_for'] = None
                import_session['target_path'] = None
                import_session['expecting_reply_to'] = None

                keyboard = await self.create_browse_keyboard(new_folder_path, import_session_id)

                try:
                    await client.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=import_session.get('reply_message_id'),
                        text=(
                            f"📁 **Browse Mode**\n"
                            f"🛣️ **Current Path:** `{unquote(new_folder_path)}`\n\n"
                            f"**New Folder Created ✅**\n\n"
                            "**Select folder to browse or import files:**"
                        ),
                        reply_markup=keyboard
                    )
                except Exception as e:
                    logger.error(f"Failed to update browse message: {e}")

            else:
                await message.reply(
                    "❌ Failed to create folder. Please try again.",
                    reply_to_message_id=message.reply_to_message.id
                )

            await message.delete()
            if 'prompt_message_id' in import_session:
                try:
                    await client.delete_messages(message.chat.id, import_session['prompt_message_id'])
                    del import_session['prompt_message_id']
                except Exception as e:
                    logger.error(f"Failed to delete prompt message: {e}")

        elif import_session.get('waiting_for') == 'delete_numbers':
            numbers = self.parse_number_list(message.text)

            if not numbers:
                await message.reply("❌ No valid numbers found. Please send numbers like '1,2,3' or '1 2 3'.",
                                    reply_to_message_id=message.reply_to_message.id)
                return

            # Get current folder items
            current_path = import_session['current_path']
            items = await self.get_folder_structure(current_path)
            folders = [item for item in items if item['is_directory']]
            files = [item for item in items if not item['is_directory']]
            all_items = folders + files

            # Validate numbers
            valid_numbers = [n for n in numbers if 1 <= n <= len(all_items)]
            invalid_numbers = [n for n in numbers if n not in valid_numbers]

            if invalid_numbers:
                await message.reply(
                    f"❌ Invalid numbers: {', '.join(map(str, invalid_numbers))}. "
                    f"Valid range: 1-{len(all_items)}",
                    reply_to_message_id=message.reply_to_message.id
                )
                return

            # Get items to delete
            items_to_delete = [all_items[n - 1] for n in valid_numbers]

            # Store in session for confirmation
            import_session['delete_multiple_items'] = items_to_delete
            import_session['delete_multiple_numbers'] = valid_numbers
            import_session['waiting_for'] = None
            import_session['expecting_reply_to'] = None

            # Create confirmation message
            items_list = "\n".join([f"{n}. {'📁' if item['is_directory'] else '📄'} {item['name']}"
                                    for n, item in zip(valid_numbers, items_to_delete)])

            confirmation_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, Delete Selected",
                                         callback_data=f"browse_delete_multiple_yes:{import_session_id}:{import_session.get('current_hash')}"),
                    InlineKeyboardButton("❌ Cancel",
                                         callback_data=f"browse_delete_multiple_no:{import_session_id}:{import_session.get('current_hash')}")
                ]
            ])

            try:
                await client.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=import_session.get('reply_message_id'),
                    text=(
                        f"🗑️ **Confirm Multiple Deletion**\n\n"
                        f"You selected {len(valid_numbers)} items to delete:\n\n"
                        f"{items_list}\n\n"
                        "Are you sure you want to delete these items?"
                    ),
                    reply_markup=confirmation_keyboard
                )
            except Exception as e:
                logger.error(f"Failed to update message: {e}")

            await message.delete()
            if 'prompt_message_id' in import_session:
                try:
                    await client.delete_messages(message.chat.id, import_session['prompt_message_id'])
                    del import_session['prompt_message_id']
                except Exception as e:
                    logger.error(f"Failed to delete prompt message: {e}")

    async def handle_import_session_file(self, client, message: Message, import_session_id: str, import_session: dict,
                                         display_message=False):
        """Handle file uploads during import session"""
        file_info = self.extract_file_info(message)
        if not file_info:
            await message.reply("❌ Unsupported file type", reply_to_message_id=message.id)
            return

        # Add file to import session
        import_session['files_to_import'].append({
            'message': message,
            'file_info': file_info
        })

        if display_message:
            # Update the import session message to show collected files
            files_list = "\n".join([f"• {file['file_info']['name']} ({self.format_file_size(file['file_info']['size'])})"
                 for file in import_session['files_to_import']])

            try:
                await client.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=import_session.get('reply_message_id'),
                    text=(
                        f"📥 **Import Files Mode**\n"
                        f"🛣️ **Target Path:** `{unquote(import_session['current_path'])}`\n\n"
                        f"**Files Collected ({len(import_session['files_to_import'])}):**\n"
                        f"{files_list}\n\n"
                        "**Send more files or click an option below:**\n\n"
                        "**⚠️ DO NOT DELETE THE FILES YOU SENT OR FORWARDED**"

                    ),
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Done - Import Files",
                                                 callback_data=f"import_done:{import_session_id}"),
                            InlineKeyboardButton("📂 Import with Folders",
                                                 callback_data=f"import_with_folders:{import_session_id}")
                        ],
                        [
                            InlineKeyboardButton("❌ Cancel",
                                                 callback_data=f"import_cancel:{import_session_id}")
                        ]
                    ])
                )
            except Exception as e:
                logger.error(f"Failed to update import message: {e}")

    async def handle_callback_query(self, client, callback_query: CallbackQuery):
        """Handle inline keyboard callbacks with file session support"""
        data = callback_query.data
        user_id = callback_query.from_user.id

        if data.startswith('import_done:') or data.startswith('import_cancel:') or data.startswith('import_with_folders:'):
            await self.handle_import_callback(client, callback_query, data)
            return

        if data.startswith('start_indexing:') or data.startswith('stop_indexing:') or data.startswith(
                'cancel_indexing:'):
            parts = data.split(':', 1)
            if len(parts) != 2:
                await callback_query.answer("Invalid callback data", show_alert=True)
                return

            action, session_id = parts[0], parts[1]

            # Handle index session callbacks
            if session_id not in self.index_sessions:
                await callback_query.answer("Index session expired. Please use /indexchannel again.", show_alert=True)
                return

            session = self.index_sessions[session_id]
            if session['user_id'] != user_id:
                await callback_query.answer("Unauthorized", show_alert=True)
                return

            if action == "start_indexing":
                await self.start_channel_indexing(client, session_id)
                await callback_query.answer("Indexing started!")
            elif action == "stop_indexing":
                session['is_running'] = False
                if 'indexing_task' in session:
                    session['indexing_task'].cancel()
                if 'progress_task' in session:
                    session['progress_task'].cancel()
                folder_mode = "with folders from filenames" if session.get('create_folders') else "to parent folder"

                await callback_query.edit_message_text(
                    f"**Indexing Stopped**\n\n"
                    f"**Progress:** {session.get('processed_count', 0)}/{session.get('total_messages', 0)}\n"
                    f"**Success:** {session.get('success_count', 0)}\n"
                    f"**Unsupported/Text Messages:** {max(0, session.get('processed_count', 0) - (session.get('success_count', 0) + session.get('failed_count', 0)))}\n"
                    f"**Mode:** Imported {folder_mode}\n"
                    f"**Failed:** {session.get('failed_count', 0)}"
                )
                await callback_query.answer("Indexing stopped")
            elif action == "cancel_indexing":
                await callback_query.edit_message_text("**Channel indexing cancelled**")
                await callback_query.answer("Cancelled")

            return

        # Parse the callback data format: action:session_id:path_hash
        parts = data.split(':', 2)
        if len(parts) < 3:
            await callback_query.answer("Invalid callback data", show_alert=True)
            return

        action, session_id, path_hash = parts[0], parts[1], parts[2]

        # Check if this is a browse session first
        if session_id.startswith('browse_'):
            # Handle import session
            if session_id not in self.import_sessions:
                await callback_query.answer("Browse session expired. Please use /browse again.", show_alert=True)
                return

            session = self.import_sessions[session_id]

            # Verify user ownership
            if session['user_id'] != user_id:
                await callback_query.answer("Unauthorized", show_alert=True)
                return

            # Handle browse-specific actions

            elif action == "browse_nav":
                # Navigate in browse mode
                folder_path = self.get_path_from_hash(path_hash)
                if not folder_path:
                    await callback_query.answer("Invalid navigation path", show_alert=True)
                    return

                import_session_id = parts[1]
                if import_session_id not in self.import_sessions:
                    await callback_query.answer("Session expired. Please use /browse again.", show_alert=True)
                    return

                import_session = self.import_sessions[import_session_id]
                import_session['current_path'] = folder_path
                new_keyboard = await self.create_browse_keyboard(folder_path, import_session_id)

                await callback_query.edit_message_text(
                    f"📁 **Browse Mode**\n"
                    f"🛣️ **Current Path:** `{unquote(folder_path)}`\n\n"
                    "**Select folder to browse or import files:**",
                    reply_markup=new_keyboard
                )

                folder_name = unquote(folder_path.rstrip('/').split('/')[-1])
                if not folder_name or folder_path == f'/webdav/{ROOT_FOLDER_NAME}':
                    folder_name = 'Root' if not ROOT_FOLDER_NAME else ROOT_FOLDER_NAME
                await callback_query.answer(f"📁 Browsing: {folder_name}")

            elif action == "browse_page":
                # Handle pagination in browse mode
                try:
                    page_num = int(path_hash)
                    import_session_id = parts[1]
                    if import_session_id not in self.import_sessions:
                        await callback_query.answer("Session expired", show_alert=True)
                        return

                    import_session = self.import_sessions[import_session_id]
                    import_session['current_page'] = page_num

                    current_path = import_session['current_path']
                    new_keyboard = await self.create_browse_keyboard(current_path, import_session_id)

                    await callback_query.edit_message_text(
                        f"📁 **Browse Mode**\n"
                        f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
                        f"📄 **Page {page_num}**\n\n"
                        "**Select folder to browse or import files:**",
                        reply_markup=new_keyboard
                    )
                    await callback_query.answer(f"📄 Page {page_num}")
                except ValueError:
                    await callback_query.answer("Invalid page number", show_alert=True)

            elif action == "import_files":
                # Start import files mode
                import_session_id = parts[1]
                if import_session_id not in self.import_sessions:
                    await callback_query.answer("Session expired", show_alert=True)
                    return

                import_session = self.import_sessions[import_session_id]
                import_session['waiting_for_files'] = True
                import_session['files_to_import'] = []

                await callback_query.edit_message_text(
                    f"📥 **Import Files Mode**\n"
                    f"🛣️ **Target Path:** `{unquote(import_session['current_path'])}`\n\n"
                    "**Send or forward files you want to import to this folder.**\n"
                    "Files will be imported when you click an option below.\n\n"
                    "**Send files now...**",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Done - Import Files",
                                                 callback_data=f"import_done:{import_session_id}"),
                            InlineKeyboardButton("📂 Import with Folders",
                                                 callback_data=f"import_with_folders:{import_session_id}")
                        ],
                        [
                            InlineKeyboardButton("❌ Cancel",
                                                 callback_data=f"import_cancel:{import_session_id}")
                        ]
                    ])
                )
                await callback_query.answer("📥 Import mode activated. Send files now!")

            elif action == "browse_newfolder":
                # Create new folder in browse mode
                current_path = self.get_path_from_hash(path_hash)
                if not current_path:
                    await callback_query.answer("Invalid path", show_alert=True)
                    return

                import_session_id = parts[1]
                import_session = self.import_sessions[import_session_id]
                import_session['waiting_for'] = 'folder_name'
                import_session['target_path'] = current_path
                import_session['expecting_reply_to'] = callback_query.message.id

                prompt_msg = await callback_query.message.reply(
                    "📝 **Create New Folder**\n\n"
                    "Please send the name for the new folder **AS A REPLY TO THE 📁 Browse Mode MESSAGE**:",
                    reply_to_message_id=import_session.get('reply_message_id')
                )

                import_session['prompt_message_id'] = prompt_msg.id
                await callback_query.answer()

            elif action == "browse_delete_confirm":
                # Folder delete confirmation in browse mode
                item_path = self.get_path_from_hash(path_hash)
                if not item_path:
                    await callback_query.answer("Invalid path", show_alert=True)
                    return

                import_session_id = parts[1]
                import_session = self.import_sessions[import_session_id]
                import_session['delete_item'] = item_path
                import_session['delete_hash'] = path_hash
                import_session['is_file_delete'] = False

                confirmation_keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Yes, Delete Folder",
                                             callback_data=f"browse_delete_yes:{import_session_id}:{path_hash}"),
                        InlineKeyboardButton("❌ Cancel",
                                             callback_data=f"browse_delete_no:{import_session_id}:{path_hash}")
                    ]
                ])
                await callback_query.edit_message_reply_markup(reply_markup=confirmation_keyboard)
                await callback_query.answer("Confirm folder deletion")

            elif action == "browse_file_delete":
                # File delete confirmation in browse mode
                file_path = self.get_path_from_hash(path_hash)
                if not file_path:
                    await callback_query.answer("Invalid file path", show_alert=True)
                    return

                import_session_id = parts[1]
                import_session = self.import_sessions[import_session_id]
                import_session['delete_item'] = file_path
                import_session['delete_hash'] = path_hash
                import_session['is_file_delete'] = True

                confirmation_keyboard = InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("✅ Yes, Delete File",
                                             callback_data=f"browse_delete_yes:{import_session_id}:{path_hash}"),
                        InlineKeyboardButton("❌ Cancel",
                                             callback_data=f"browse_delete_no:{import_session_id}:{path_hash}")
                    ]
                ])
                await callback_query.edit_message_reply_markup(reply_markup=confirmation_keyboard)
                await callback_query.answer("Confirm file deletion")

            elif action == "browse_delete_yes":
                # Confirm deletion in browse mode
                item_path = self.get_path_from_hash(path_hash)
                if not item_path:
                    await callback_query.answer("Invalid path", show_alert=True)
                    return

                import_session_id = parts[1]
                import_session = self.import_sessions[import_session_id]
                is_file_delete = import_session.get('is_file_delete', False)

                if is_file_delete:
                    await callback_query.edit_message_text("🗑️ **Deleting file...**")
                else:
                    await callback_query.edit_message_text(
                        "🗑️ **Deleting... This may take a while for large folders.**")

                success = await self.delete_item(item_path)

                if success:
                    # Determine where to navigate after deletion
                    if is_file_delete:
                        current_path = import_session['current_path']
                    else:
                        current_path = '/'.join(item_path.rstrip('/').split('/')[:-1])
                        if current_path == '/webdav':
                            current_path = f'/webdav/{ROOT_FOLDER_NAME}'
                        import_session['current_path'] = current_path

                    new_keyboard = await self.create_browse_keyboard(current_path, import_session_id)

                    await callback_query.edit_message_text(
                        f"📁 **Browse Mode**\n"
                        f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
                        f"✅ **{'File' if is_file_delete else 'Folder'} deleted successfully!**\n\n"
                        "**Select folder to browse or import files:**",
                        reply_markup=new_keyboard
                    )
                else:
                    await callback_query.edit_message_text(
                        f"❌ **Failed to delete {'file' if is_file_delete else 'folder'}.**")

                # Clean up delete session
                for key in ['delete_item', 'delete_hash', 'is_file_delete']:
                    if key in import_session:
                        del import_session[key]

            elif action == "browse_delete_no":
                # Cancel deletion in browse mode
                import_session_id = parts[1]
                import_session = self.import_sessions[import_session_id]
                current_path = import_session['current_path']

                new_keyboard = await self.create_browse_keyboard(current_path, import_session_id)
                await callback_query.edit_message_text(
                    f"📁 **Browse Mode**\n"
                    f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
                    "**Select folder to browse or import files:**",
                    reply_markup=new_keyboard
                )
                await callback_query.answer("Deletion cancelled ✅")

                # Clean up delete session
                for key in ['delete_item', 'delete_hash', 'is_file_delete']:
                    if key in import_session:
                        del import_session[key]

            elif action == "browse_delete_multiple":
                # Start multiple deletion in browse mode
                current_path = self.get_path_from_hash(path_hash)
                if not current_path:
                    await callback_query.answer("Invalid path", show_alert=True)
                    return

                import_session_id = parts[1]
                import_session = self.import_sessions[import_session_id]

                items = await self.get_folder_structure(current_path)
                folders = [item for item in items if item['is_directory']]
                files = [item for item in items if not item['is_directory']]
                all_items = folders + files

                if not all_items:
                    await callback_query.answer("No items to delete in this folder", show_alert=True)
                    return

                import_session['waiting_for'] = 'delete_numbers'
                import_session['expecting_reply_to'] = callback_query.message.id
                import_session['current_hash'] = path_hash

                items_list = "\n".join([f"{i + 1}. {'📁' if item['is_directory'] else '📄'} {item['name']}"
                                        for i, item in enumerate(all_items)])

                prompt_msg = await callback_query.message.reply(
                    f"🗑️ **Delete Multiple Items**\n\n"
                    f"Current folder items:\n{items_list}\n\n"
                    f"Please send the numbers of items you want to delete **AS A REPLY TO THE 📁 Browse Mode MESSAGE**\n\n"
                    f"Format: `1,2,3` or `1 2 3`\n"
                    f"Example: `1,3,5` to delete items 1, 3, and 5",
                    reply_to_message_id=import_session.get('reply_message_id')
                )

                import_session['prompt_message_id'] = prompt_msg.id
                await callback_query.answer()

            elif action == "browse_delete_multiple_yes":
                # Confirm multiple deletion in browse mode
                import_session_id = parts[1]
                import_session = self.import_sessions[import_session_id]
                items_to_delete = import_session.get('delete_multiple_items', [])

                if not items_to_delete:
                    await callback_query.answer("No items to delete", show_alert=True)
                    return

                await callback_query.edit_message_text("🗑️ **Deleting multiple items... This may take a while.**")

                delete_tasks = [self.delete_item(item['path']) for item in items_to_delete]
                results = await asyncio.gather(*delete_tasks, return_exceptions=True)

                success_count = 0
                failed_items = []

                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        failed_items.append(items_to_delete[i]['name'])
                        logger.error(f"Delete failed for {items_to_delete[i]['name']}: {result}")
                    elif result is True:
                        success_count += 1
                    else:
                        failed_items.append(items_to_delete[i]['name'])

                current_path = import_session['current_path']
                new_keyboard = await self.create_browse_keyboard(current_path, import_session_id)

                result_text = f"✅ **Multiple Deletion Complete**\n\n"
                result_text += f"Successfully deleted: {success_count}/{len(items_to_delete)} items\n"

                if failed_items:
                    result_text += f"\n❌ Failed to delete:\n" + "\n".join([f"• {item}" for item in failed_items])

                await callback_query.edit_message_text(
                    f"📁 **Browse Mode**\n"
                    f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
                    f"{result_text}\n\n"
                    "**Select folder to browse or import files:**",
                    reply_markup=new_keyboard
                )

                # Clean up delete session
                for key in ['delete_multiple_items', 'delete_multiple_numbers', 'current_hash']:
                    if key in import_session:
                        del import_session[key]

            elif action == "browse_delete_multiple_no":
                # Cancel multiple deletion in browse mode
                import_session_id = parts[1]
                import_session = self.import_sessions[import_session_id]
                current_path = import_session['current_path']

                new_keyboard = await self.create_browse_keyboard(current_path, import_session_id)
                await callback_query.edit_message_text(
                    f"📁 **Browse Mode**\n"
                    f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
                    "**Select folder to browse or import files:**",
                    reply_markup=new_keyboard
                )
                await callback_query.answer("Multiple deletion cancelled ✅")

                # Clean up delete session
                for key in ['delete_multiple_items', 'delete_multiple_numbers', 'current_hash']:
                    if key in import_session:
                        del import_session[key]
            return

        # Check if this is an index session
        if session_id.startswith('index_'):
            if session_id not in self.index_sessions:
                await callback_query.answer("Index session expired. Please use /indexchannel again.", show_alert=True)
                return

            session = self.index_sessions[session_id]

            if session['user_id'] != user_id:
                await callback_query.answer("Unauthorized", show_alert=True)
                return

            # Handle index-specific actions
            if action == "index_nav":
                # Navigate in index path selection
                folder_path = self.get_path_from_hash(path_hash)
                if not folder_path:
                    await callback_query.answer("Invalid navigation path", show_alert=True)
                    return

                session['current_path'] = folder_path
                new_keyboard = await self.create_index_path_keyboard(folder_path, session_id)

                await callback_query.edit_message_text(
                    f"📁 **Select target folder for indexing:**\n"
                    f"🛣️ **Current Path:** `{unquote(folder_path)}`",
                    reply_markup=new_keyboard
                )

            elif action == "index_select_path":
                # Path selected, ask for skip number
                selected_path = self.get_path_from_hash(path_hash)
                if not selected_path:
                    await callback_query.answer("Invalid path", show_alert=True)
                    return

                session['target_path'] = selected_path
                session['step'] = 'waiting_skip_number'
                session['expecting_reply_to'] = callback_query.message.id

                prompt_msg = await callback_query.message.reply(
                    f"⏭️ **Set Skip Number & Folder Options**\n\n"
                    f"📁 **Target Path:** `{unquote(selected_path)}`\n"
                    f"🔚 **End Message ID:** `{session['end_message_id']}`\n\n"
                    f"**Send the number of messages to skip from the beginning.**\n"
                    f"(Send `0` to start from message ID 1)\n\n"
                    f"📂 **To create folders from filenames, add 'folders' to your message:**\n"
                    f"• `0` - Skip Number Only = Import to parent folder\n"
                    f"• `0 folders` - Skip Number with 'folders' = Create folders from filenames\n"
                    f"• `10 folders` - Skip 10 messages + create folders\n\n"
                    f"**REPLY TO 'Select target' MESSAGE WITH VALID VALUE(S):**",
                    reply_to_message_id=session['reply_message_id']
                )

                session['prompt_message_id'] = prompt_msg.id
                session['prompt_msg'] = prompt_msg
                await callback_query.answer()

            elif action == "index_newfolder":
                # Create new folder in index path selection
                current_path = self.get_path_from_hash(path_hash)
                if not current_path:
                    await callback_query.answer("Invalid path", show_alert=True)
                    return

                session['waiting_for'] = 'folder_name'
                session['target_path'] = current_path
                session['expecting_reply_to'] = callback_query.message.id

                prompt_msg = await callback_query.message.reply(
                    "**Create New Folder**\n\n"
                    "Please send the name for the new folder **AS A REPLY TO THE Select target folder MESSAGE**:",
                    reply_to_message_id=session.get('reply_message_id')
                )

                session['prompt_message_id'] = prompt_msg.id
                await callback_query.answer()

            return

        # Otherwise handle as file session
        if session_id not in self.file_sessions:
            await callback_query.answer("Session expired. Please send the file again.", show_alert=True)
            return

        file_session_id = session_id

        file_session = self.file_sessions[file_session_id]

        # Verify user ownership
        if file_session['user_id'] != user_id:
            await callback_query.answer("Unauthorized", show_alert=True)
            return

        if action == "nav":
            # Navigate to folder
            folder_path = self.get_path_from_hash(path_hash)
            if not folder_path:
                await callback_query.answer("Invalid navigation path", show_alert=True)
                return

            file_session['current_path'] = folder_path
            new_keyboard = await self.create_folder_keyboard(folder_path, file_session_id)

            # Update the message
            file_info = file_session['file_info']
            await callback_query.edit_message_text(
                f"📁 **File received:** `{file_info['name']}`\n"
                f"📏 **Size:** {self.format_file_size(file_info['size'])}\n"
                f"🗂 **Type:** {file_info['type']}\n"
                f"🛣️ **Current Path:** `{unquote(folder_path)}`\n\n"
                "**Please select destination folder:**",
                reply_markup=new_keyboard
            )
            folder_name = unquote(folder_path.rstrip('/').split('/')[-1])
            if not folder_name or folder_path == f'/webdav/{ROOT_FOLDER_NAME}':
                folder_name = 'Root' if not ROOT_FOLDER_NAME else ROOT_FOLDER_NAME
            await callback_query.answer(f"📁 Browsing: {folder_name}")

        elif action == "select":
            selected_path = self.get_path_from_hash(path_hash)

            if not selected_path:
                await callback_query.answer("Invalid selection path", show_alert=True)
                return

            await self.process_file_storage(client, callback_query, selected_path, file_session_id)

        elif action == "newfolder":
            current_path = self.get_path_from_hash(path_hash)
            if not current_path:
                await callback_query.answer("Invalid path", show_alert=True)
                return

            file_session['waiting_for'] = 'folder_name'
            file_session['target_path'] = current_path
            file_session['expecting_reply_to'] = callback_query.message.id

            prompt_msg = await callback_query.message.reply(
                "📝 **Create New Folder**\n\n"
                "Please send the name for the new folder **AS A REPLY TO THE 📁 File received:... MESSAGE**:",
                reply_to_message_id=file_session.get('reply_message_id')
            )

            file_session['prompt_message_id'] = prompt_msg.id

            await callback_query.answer()

        elif action == "folderfromname":
            current_path = self.get_path_from_hash(path_hash)

            if not current_path:
                await callback_query.answer("Invalid path", show_alert=True)
                return
            file_info = file_session['file_info']
            # Extract folder name from filename (remove extension)
            folder_name = os.path.splitext(file_info['name'])[0]
            new_folder_path = current_path.rstrip('/') + f'/{folder_name}/'

            if await self.create_folder(new_folder_path):
                await self.process_file_storage(client, callback_query, new_folder_path, file_session_id)
            else:
                await callback_query.answer("❌ Failed to create folder", show_alert=True)

        elif action == "delete_confirm":
            # Folder delete confirmation
            item_path = self.get_path_from_hash(path_hash)
            if not item_path:
                await callback_query.answer("Invalid path", show_alert=True)
                return

            # Store the item to delete in session
            file_session['delete_item'] = item_path
            file_session['delete_hash'] = path_hash
            file_session['is_file_delete'] = False

            confirmation_keyboard = await self.create_delete_confirmation_keyboard(file_session_id, path_hash, is_file=False)
            await callback_query.edit_message_reply_markup(reply_markup=confirmation_keyboard)
            await callback_query.answer("Confirm folder deletion")

        elif action == "file_delete_confirm":
            # File delete confirmation
            file_path = self.get_path_from_hash(path_hash)
            if not file_path:
                await callback_query.answer("Invalid file path", show_alert=True)
                return

            # Store the file to delete in session
            file_session['delete_item'] = file_path
            file_session['delete_hash'] = path_hash
            file_session['is_file_delete'] = True

            confirmation_keyboard = await self.create_delete_confirmation_keyboard(file_session_id, path_hash, is_file=True)
            await callback_query.edit_message_reply_markup(reply_markup=confirmation_keyboard)
            await callback_query.answer("Confirm file deletion")

        elif action == "delete_yes":
            # Confirm deletion
            item_path = self.get_path_from_hash(path_hash)
            if not item_path:
                await callback_query.answer("Invalid path", show_alert=True)
                return

            is_file_delete = file_session.get('is_file_delete', False)

            if is_file_delete:
                await callback_query.edit_message_text("🗑️ **Deleting file...**")
            else:
                await callback_query.edit_message_text("🗑️ **Deleting... This may take a while for large folders.**")

            success = await self.delete_item(item_path)

            if success:
                # Determine where to navigate after deletion
                if is_file_delete:
                    # For file deletion, stay in the current directory
                    current_path = file_session['current_path']
                else:
                    # For folder deletion, navigate to parent directory
                    current_path = '/'.join(item_path.rstrip('/').split('/')[:-1])
                    if current_path == '/webdav':
                        current_path = f'/webdav/{ROOT_FOLDER_NAME}'
                    # Update session with new current path
                    file_session['current_path'] = current_path

                # Refresh the keyboard
                new_keyboard = await self.create_folder_keyboard(current_path, file_session_id)

                file_info = file_session['file_info']
                await callback_query.edit_message_text(
                    f"📁 **File received:** `{file_info['name']}`\n"
                    f"📏 **Size:** {self.format_file_size(file_info['size'])}\n"
                    f"🗂 **Type:** {file_info['type']}\n"
                    f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
                    f"✅ **{'File' if is_file_delete else 'Folder'} deleted successfully!**\n\n"
                    "**Please select destination folder:**",
                    reply_markup=new_keyboard
                )
            else:
                await callback_query.edit_message_text(f"❌ **Failed to delete {'file' if is_file_delete else 'folder'}.**")

            # Clean up delete session
            for key in ['delete_item', 'delete_hash', 'is_file_delete']:
                if key in file_session:
                    del file_session[key]

        elif action == "delete_no":
            # Cancel deletion and go back to normal view
            current_path = file_session['current_path']

            # Restore normal keyboard
            new_keyboard = await self.create_folder_keyboard(current_path, file_session_id)
            file_info = file_session['file_info']
            await callback_query.edit_message_text(
                f"📁 **File received:** `{file_info['name']}`\n"
                f"📏 **Size:** {self.format_file_size(file_info['size'])}\n"
                f"🗂 **Type:** {file_info['type']}\n"
                f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
                "**Please select destination folder:**",
                reply_markup=new_keyboard
            )
            await callback_query.answer("Deletion cancelled ✅")

            # Clean up delete session
            for key in ['delete_item', 'delete_hash', 'is_file_delete']:
                if key in file_session:
                    del file_session[key]

        elif action == "delete_multiple":
            current_path = self.get_path_from_hash(path_hash)
            if not current_path:
                await callback_query.answer("Invalid path", show_alert=True)
                return

            # Get current folder items for numbering reference
            items = await self.get_folder_structure(current_path)
            folders = [item for item in items if item['is_directory']]
            files = [item for item in items if not item['is_directory']]
            all_items = folders + files

            if not all_items:
                await callback_query.answer("No items to delete in this folder", show_alert=True)
                return

            file_session['waiting_for'] = 'delete_numbers'
            file_session['expecting_reply_to'] = callback_query.message.id
            file_session['current_hash'] = path_hash

            # Create numbered list for user reference
            items_list = "\n".join([f"{i + 1}. {'📁' if item['is_directory'] else '📄'} {item['name']}"
                                    for i, item in enumerate(all_items)])

            prompt_msg = await callback_query.message.reply(
                f"🗑️ **Delete Multiple Items**\n\n"
                f"Current folder items:\n{items_list}\n\n"
                f"Please send the numbers of items you want to delete **AS A REPLY TO THE 📁 File received:... MESSAGE**\n\n"
                f"Format: `1,2,3` or `1 2 3`\n"
                f"Example: `1,3,5` to delete items 1, 3, and 5",
                reply_to_message_id=file_session.get('reply_message_id')
            )

            file_session['prompt_message_id'] = prompt_msg.id
            await callback_query.answer()

        elif action == "delete_multiple_yes":
            # Confirm multiple deletion
            items_to_delete = file_session.get('delete_multiple_items', [])

            if not items_to_delete:
                await callback_query.answer("No items to delete", show_alert=True)
                return

            await callback_query.edit_message_text("🗑️ **Deleting multiple items... This may take a while.**")

            # Delete items concurrently
            delete_tasks = [self.delete_item(item['path']) for item in items_to_delete]
            results = await asyncio.gather(*delete_tasks, return_exceptions=True)

            success_count = 0
            failed_items = []

            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    failed_items.append(items_to_delete[i]['name'])
                    logger.error(f"Delete failed for {items_to_delete[i]['name']}: {result}")
                elif result is True:
                    success_count += 1
                else:
                    failed_items.append(items_to_delete[i]['name'])

            # Navigate back to current folder and refresh
            current_path = file_session['current_path']
            new_keyboard = await self.create_folder_keyboard(current_path, file_session_id)

            # Create result message
            result_text = f"✅ **Multiple Deletion Complete**\n\n"
            result_text += f"Successfully deleted: {success_count}/{len(items_to_delete)} items\n"

            if failed_items:
                result_text += f"\n❌ Failed to delete:\n" + "\n".join([f"• {item}" for item in failed_items])

            file_info = file_session['file_info']
            await callback_query.edit_message_text(
                f"📁 **File received:** `{file_info['name']}`\n"
                f"📏 **Size:** {self.format_file_size(file_info['size'])}\n"
                f"🗂 **Type:** {file_info['type']}\n"
                f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
                f"{result_text}\n\n"
                "**Please select destination folder:**",
                reply_markup=new_keyboard
            )

            # Clean up delete session
            for key in ['delete_multiple_items', 'delete_multiple_numbers', 'current_hash']:
                if key in file_session:
                    del file_session[key]

        elif action == "delete_multiple_no":
            # Cancel multiple deletion and go back to normal view
            current_path = file_session['current_path']

            # Restore normal keyboard
            new_keyboard = await self.create_folder_keyboard(current_path, file_session_id)
            file_info = file_session['file_info']
            await callback_query.edit_message_text(
                f"📁 **File received:** `{file_info['name']}`\n"
                f"📏 **Size:** {self.format_file_size(file_info['size'])}\n"
                f"🗂 **Type:** {file_info['type']}\n"
                f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
                "**Please select destination folder:**",
                reply_markup=new_keyboard
            )
            await callback_query.answer("Multiple deletion cancelled ✅")

            # Clean up delete session
            for key in ['delete_multiple_items', 'delete_multiple_numbers', 'current_hash']:
                if key in file_session:
                    del file_session[key]

        elif action == "page":
            # Handle pagination
            try:
                page_num = int(path_hash)
                file_session['current_page'] = page_num

                current_path = file_session['current_path']
                new_keyboard = await self.create_folder_keyboard(current_path, file_session_id)

                file_info = file_session['file_info']
                await callback_query.edit_message_text(
                    f"📁 **File received:** `{file_info['name']}`\n"
                    f"📏 **Size:** {self.format_file_size(file_info['size'])}\n"
                    f"🗂 **Type:** {file_info['type']}\n"
                    f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
                    f"📄 **Page {page_num}**\n\n"
                    "**Please select destination folder:**",
                    reply_markup=new_keyboard
                )
                await callback_query.answer(f"📄 Page {page_num}")
            except ValueError:
                await callback_query.answer("Invalid page number", show_alert=True)

        else:
            await callback_query.answer("Unknown action", show_alert=True)

    async def handle_import_callback(self, client, callback_query: CallbackQuery, data: str):
        """Handle import-specific callbacks (import_done, import_cancel, import_with_folders)"""
        def truncate_path(path: str, max_length: int = 140) -> str:
            if len(path) <= max_length:
                return path

            display_path = path
            if display_path.startswith('/webdav'):
                display_path = display_path[len('/webdav'):]

            if len(display_path) <= max_length:
                return display_path

            available_length = max_length - 3
            start_chars = int(available_length * 0.6)
            end_chars = available_length - start_chars

            start_part = display_path[:start_chars]
            end_part = display_path[-end_chars:] if end_chars > 0 else ""

            last_sep_in_start = start_part.rfind('/')
            if last_sep_in_start != -1 and last_sep_in_start > (start_chars - 20):
                start_part = start_part[:last_sep_in_start + 1]

            first_sep_in_end = end_part.find('/')
            if first_sep_in_end != -1 and first_sep_in_end < (end_chars - 10):
                end_part = end_part[first_sep_in_end:]

            truncated_path = f"{start_part}...{end_part}"
            return truncated_path

        user_id = callback_query.from_user.id

        if data.startswith('import_done:') or data.startswith('import_with_folders:'):
            # Handle import completion (both regular and with folders)
            import_type = data.split(':', 1)[0]
            import_session_id = data.split(':', 1)[1]

            if import_session_id not in self.import_sessions:
                await callback_query.answer("Import session expired", show_alert=True)
                return

            import_session = self.import_sessions[import_session_id]

            prepared_path = truncate_path(unquote(import_session['current_path']))
            if import_type == 'import_done':
                await callback_query.answer(f"Files will be imported directly to the parent '{prepared_path}' folder", show_alert=True)
            elif import_type == 'import_with_folders':
                await callback_query.answer(f"A new folder will be created for each file inside the parent '{prepared_path}' folder", show_alert=True)

            # Verify user ownership
            if import_session['user_id'] != user_id:
                await callback_query.answer("Unauthorized", show_alert=True)
                return

            files_to_import = import_session.get('files_to_import', [])

            if not files_to_import:
                await callback_query.answer("No files to import", show_alert=True)
                return

            await callback_query.edit_message_text(
                f"📤 **Importing {len(files_to_import)} files{' with folders' if import_type == 'import_with_folders' else ''}...**\n\n"
                "This may take a while. Please wait..."
            )

            success_count = 0
            failed_files = []

            for file_data in files_to_import:
                original_message = file_data['message']
                file_info = file_data['file_info']

                try:
                    # Determine target path based on import type
                    if import_type == 'import_with_folders':
                        # Create folder from filename
                        folder_name = os.path.splitext(file_info['name'])[0]
                        target_folder_path = import_session['current_path'].rstrip('/') + f'/{folder_name}/'

                        # Create the folder
                        if not await self.create_folder(target_folder_path):
                            logger.error(f"Failed to create folder: {target_folder_path}")
                            failed_files.append(file_info['name'])
                            continue

                        target_path = target_folder_path
                    else:
                        # Regular import to current path
                        target_path = import_session['current_path']

                    # Upload to storage channel
                    if self.enable_upload_records:
                        await client.send_message(
                            chat_id=self.storage_channel_id,
                            text=f"File: {file_info['name']}\nFrom: @{callback_query.from_user.username or callback_query.from_user.first_name}\nTarget: {target_path}"
                        )

                    # Send file to storage channel based on type
                    if original_message.video:
                        storage_msg = await client.send_video(
                            chat_id=self.storage_channel_id,
                            video=file_info['file_id'],
                            caption=f"🎥 {file_info['name']}"
                        )
                        await original_message.delete()
                    elif original_message.document:
                        storage_msg = await client.send_document(
                            chat_id=self.storage_channel_id,
                            document=file_info['file_id'],
                            caption=f"📄 {file_info['name']}"
                        )
                        await original_message.delete()
                    elif original_message.audio:
                        storage_msg = await client.send_audio(
                            chat_id=self.storage_channel_id,
                            audio=file_info['file_id'],
                            caption=f"🎵 {file_info['name']}"
                        )
                        await original_message.delete()
                    elif original_message.voice:
                        storage_msg = await client.send_voice(
                            chat_id=self.storage_channel_id,
                            voice=file_info['file_id'],
                            caption=f"🗣 {file_info['name']}"
                        )
                        await original_message.delete()
                    elif original_message.video_note:
                        storage_msg = await client.send_video_note(
                            chat_id=self.storage_channel_id,
                            video_note=file_info['file_id']
                        )
                        await original_message.delete()
                    else:
                        continue

                    # Import file using TGFS API
                    success = await self.import_file(
                        directory=target_path,
                        filename=file_info['name'],
                        channel_id=self.storage_channel_id,
                        message_id=storage_msg.id
                    )

                    if success:
                        success_count += 1
                    else:
                        failed_files.append(file_info['name'])

                except Exception as e:
                    logger.error(f"Error importing file {file_info['name']}: {e}")
                    failed_files.append(file_info['name'])

            # Show results
            import_type_text = "with folders" if import_type == 'import_with_folders' else ""
            result_text = f"📥 **Import Complete {import_type_text}**\n\n"
            result_text += f"✅ Successfully imported: {success_count}/{len(files_to_import)} files\n"

            if failed_files:
                result_text += f"\n❌ Failed to import:\n" + "\n".join([f"• {file}" for file in failed_files])

            # Return to browse mode
            current_path = import_session['current_path']
            new_keyboard = await self.create_browse_keyboard(current_path, import_session_id)

            await callback_query.edit_message_text(
                f"📁 **Browse Mode**\n"
                f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
                f"{result_text}\n\n"
                "**Select folder to browse or import files:**",
                reply_markup=new_keyboard
            )

            # Clean up import session
            import_session['waiting_for_files'] = False
            import_session['files_to_import'] = []

        elif data.startswith('import_cancel:'):
            # Handle import cancellation
            import_session_id = data.split(':', 1)[1]

            if import_session_id not in self.import_sessions:
                await callback_query.answer("Import session expired", show_alert=True)
                return

            import_session = self.import_sessions[import_session_id]

            # Verify user ownership
            if import_session['user_id'] != user_id:
                await callback_query.answer("Unauthorized", show_alert=True)
                return

            # Return to browse mode
            current_path = import_session['current_path']
            new_keyboard = await self.create_browse_keyboard(current_path, import_session_id)

            await callback_query.edit_message_text(
                f"📁 **Browse Mode**\n"
                f"🛣️ **Current Path:** `{unquote(current_path)}`\n\n"
                "**Select folder to browse or import files:**",
                reply_markup=new_keyboard
            )

            # Clean up import session
            import_session['waiting_for_files'] = False
            import_session['files_to_import'] = []
            await callback_query.answer("Import cancelled")

    async def process_file_storage(self, client, callback_query: CallbackQuery, target_path: str, file_session_id: str):
        """Process the file storage after path selection"""
        file_session = self.file_sessions[file_session_id]
        original_message = file_session['original_message']
        file_info = file_session['file_info']
        user_id = file_session['user_id']

        # Send file to storage channel first
        await callback_query.edit_message_text(
            "📤 **Uploading file to storage channel...**"
        )

        try:
            # Forward/send file to storage channel
            if self.enable_upload_records:
                await client.send_message(
                    chat_id=self.storage_channel_id,
                    text=f"File: {file_info['name']}\nFrom: @{callback_query.from_user.username or callback_query.from_user.first_name}\nTarget: {target_path}"
                )

            # Copy the media to storage channel
            if original_message.video:
                storage_msg = await client.send_video(
                    chat_id=self.storage_channel_id,
                    video=file_info['file_id'],
                    caption=f"🎥 {file_info['name']}"
                )
            elif original_message.document:
                storage_msg = await client.send_document(
                    chat_id=self.storage_channel_id,
                    document=file_info['file_id'],
                    caption=f"📄 {file_info['name']}"
                )
            elif original_message.audio:
                storage_msg = await client.send_audio(
                    chat_id=self.storage_channel_id,
                    audio=file_info['file_id'],
                    caption=f"🎵 {file_info['name']}"
                )
            elif original_message.voice:
                storage_msg = await client.send_voice(
                    chat_id=self.storage_channel_id,
                    voice=file_info['file_id'],
                    caption=f"🗣 {file_info['name']}"
                )
            elif original_message.video_note:
                storage_msg = await client.send_video_note(
                    chat_id=self.storage_channel_id,
                    video_note=file_info['file_id']
                )
            else:
                await callback_query.edit_message_text(
                    "w❌ **Invalid, Unsupported or Unknown File Type**"
                )
                return

            # Get the message ID of the sent file
            storage_message_id = storage_msg.id

            # Update status
            await callback_query.edit_message_text(
                "📁 **Creating folder structure and importing file...**"
            )

            # Import file using TGFS API
            success = await self.import_file(
                directory=target_path,
                filename=file_info['name'],
                channel_id=self.storage_channel_id,
                message_id=storage_message_id
            )

            if success:
                await callback_query.edit_message_text(
                    f"✅ **File Successfully Stored!**\n\n"
                    f"📁 **Location:** `{unquote(target_path)}`\n"
                    f"📄 **File:** `{file_info['name']}`\n"
                    f"📏 **Size:** {self.format_file_size(file_info['size'])}\n"
                    f"🕐 **Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
            else:
                await callback_query.edit_message_text(
                    f"⚠️ **File uploaded but import failed**\n\n"
                    f"The file has been uploaded to the storage channel but couldn't be imported to TGFS.\n"
                    f"Message ID: {storage_message_id}\n"
                    f"Please check the TGFS configuration."
                )

            if file_session_id in self.file_sessions:
                del self.file_sessions[file_session_id]

            # Also remove from user session
            if user_id in self.user_sessions and 'file_sessions' in self.user_sessions[user_id]:
                self.user_sessions[user_id]['file_sessions'] = [
                    fsid for fsid in self.user_sessions[user_id]['file_sessions']
                    if fsid != file_session_id
                ]

        except Exception as e:
            logger.error(f"Error processing file storage: {e}")
            await callback_query.edit_message_text(
                f"❌ **Error occurred during file storage:**\n\n"
                f"`{str(e)}`\n\n"
                "Please try again."
            )
        finally:
            # Clean up session
            if user_id in self.user_sessions:
                del self.user_sessions[user_id]

    async def cleanup_old_sessions(self):
        """Clean up old file and import sessions periodically"""
        while True:
            await asyncio.sleep(3600)
            current_time = time.time()
            expired_file_sessions = []
            expired_import_sessions = []
            expired_index_sessions = []

            # Clean up file sessions
            for file_session_id, session in list(self.file_sessions.items()):
                if current_time - session.get('created_time', 0) > 86400:
                    expired_file_sessions.append(file_session_id)

            # Clean up import sessions
            for import_session_id, session in list(self.import_sessions.items()):
                if current_time - session.get('created_time', 0) > 86400:
                    expired_import_sessions.append(import_session_id)

            # Clean up index sessions
            for index_session_id, session in list(self.index_sessions.items()):
                session_age = current_time - session.get('created_time', 0)
                is_running = session.get('is_running', False)

                # Only clean up sessions that are:
                # 1. Very old (24+ hours) AND not currently running
                # 2. OR sessions that failed/completed and are old (6+ hours)
                should_cleanup = False

                if session_age > 86400 and not is_running:
                    should_cleanup = True
                elif session_age > 21600 and (session.get('completed') or session.get('error')):
                    should_cleanup = True

                if should_cleanup:
                    expired_index_sessions.append(index_session_id)

            for session_id in expired_file_sessions:
                del self.file_sessions[session_id]

            for session_id in expired_import_sessions:
                del self.import_sessions[session_id]

            for session_id in expired_index_sessions:
                # Only cancel tasks if the session is truly abandoned
                session = self.index_sessions[session_id]

                # Cancel tasks only if they exist and the session isn't actively running
                if not session.get('is_running'):
                    if 'indexing_task' in session:
                        session['indexing_task'].cancel()
                    if 'progress_task' in session:
                        session['progress_task'].cancel()

                del self.index_sessions[session_id]

            if expired_file_sessions or expired_import_sessions or expired_index_sessions:
                logger.info(
                    f"Cleaned up {len(expired_file_sessions)} expired file sessions, "
                    f"{len(expired_import_sessions)} expired import sessions, and "
                    f"{len(expired_index_sessions)} expired index sessions"
                )

    async def initialize_helper_bots(self):
        """Initialize helper bot clients using bot tokens concurrently"""
        if not self.multi_index_bots:
            logger.info("No helper bots configured")
            return

        async def init_single_bot(i, bot_token):
            try:
                # Extract bot ID from token (the part before the colon)
                bot_id = bot_token.split(':')[0]

                client = Client(
                    f"helper_bot_{i}",
                    api_id=self.api_id,
                    api_hash=self.api_hash,
                    bot_token=bot_token,
                    no_updates=True,
                    in_memory=True
                )

                self.multi_index_bot_holder[bot_id] = client
                logger.info(f"Initialized helper bot client for token index: {i}")
                return True

            except Exception as e:
                logger.error(f"Failed to initialize helper bot {i}: {e}")
                return False

        # Initialize all bots concurrently
        tasks = [
            init_single_bot(i, bot_token)
            for i, bot_token in enumerate(self.multi_index_bots)
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Count successful initializations
        successful = sum(1 for result in results if result is True)
        logger.info(f"Successfully initialized {successful}/{len(self.multi_index_bots)} helper bots")

    async def start_helper_bots(self):
        """Start all helper bot clients concurrently"""
        if not self.multi_index_bot_holder:
            logger.info("No helper bots to start")
            return

        async def start_single_bot(bot_id, client):
            try:
                await client.start()
                me = await client.get_me()
                logger.info(f"Helper bot started: @{me.username} (ID: {me.id})")
                return True
            except Exception as e:
                logger.error(f"Failed to start helper bot {bot_id}: {e}")
                return False

        # Start all bots concurrently
        tasks = [
            start_single_bot(bot_id, client)
            for bot_id, client in self.multi_index_bot_holder.items()
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Count successful starts
        successful = sum(1 for result in results if result is True)
        logger.info(f"Successfully started {successful}/{len(self.multi_index_bot_holder)} helper bots")

    async def stop_helper_bots(self):
        """Stop all helper bot clients concurrently"""
        if not self.multi_index_bot_holder:
            logger.info("No helper bots to stop")
            return

        async def stop_single_bot(bot_id, client):
            try:
                if hasattr(client, 'is_connected') and client.is_connected:
                    await client.stop()
                    logger.info(f"Stopped helper bot: {bot_id}")
                    return True
                return False
            except Exception as e:
                logger.error(f"Error stopping helper bot {bot_id}: {e}")
                return False

        # Stop all bots concurrently
        tasks = [
            stop_single_bot(bot_id, client)
            for bot_id, client in self.multi_index_bot_holder.items()
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Count successful stops
        successful = sum(1 for result in results if result is True)
        logger.info(f"Successfully stopped {successful}/{len(self.multi_index_bot_holder)} helper bots")

    async def run(self):
        """Start the bot"""
        cleanup_task = None
        try:
            # Authenticate with TGFS API
            logger.info("Authenticating with TGFS API...")
            if not await self.authenticate():
                logger.error("Failed to authenticate with TGFS API")
                return
            logger.info("TGFS authentication successful")

            # Initialize main bot client
            self.app = Client(
                "tgfs_bot",
                api_id=self.api_id,
                api_hash=self.api_hash,
                bot_token=self.bot_token,
                in_memory=True
            )

            # Initialize helper bot clients
            logger.info("Initializing helper bot clients...")
            await self.initialize_helper_bots()

            logger.info("Registering handlers...")
            self.register_handlers()

            logger.info("Starting TGFS Bot...")
            await self.app.start()

            # Start all helper bot clients
            logger.info("Starting helper bot clients...")
            await self.start_helper_bots()

            result = await self.app.set_bot_commands([
                BotCommand("start", "Start the bot"),
                BotCommand("browse", "Browse the TGFS Server, Import Multiple Files, Manage Files and Folders"),
                BotCommand("indexchannel", "Index files from a channel")
            ])
            if result:
                logger.info('Bot Commands Set Successfully')

            me = await self.app.get_me()
            logger.info(f"Bot started successfully as @{me.username}")
            logger.info(f"Bot ID: {me.id}")
            logger.info(f"Bot name: {me.first_name}")

            cleanup_task = asyncio.create_task(self.cleanup_old_sessions())

            # Start Token Validation Task
            await self.start_token_validation()

            logger.info("Bot is now running and listening for messages...")
            logger.info("👨‍💻 TGFS Manger => Created by The Seeker")

            # Create a stop event for graceful shutdown
            stop_event = asyncio.Event()

            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                logger.info("Bot received shutdown signal")

        except KeyboardInterrupt:
            logger.info("Bot stopped by user (KeyboardInterrupt)")
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            raise
        finally:
            # Stop all helper bots first
            await self.stop_helper_bots()
            await self.stop_token_validation()

            if cleanup_task:
                cleanup_task.cancel()
            if self.app and hasattr(self.app, 'is_connected') and self.app.is_connected:
                logger.info("Stopping bot...")
                await self.app.stop()
                logger.info("Bot stopped successfully")

if __name__ == "__main__":
    # Load environment variables from .env file
    try:
        load_dotenv('settings.env')
    except ImportError:
        logger.warning("python-dotenv not installed. Make sure environment variables are set.")

    bot = TGFSBot()
    asyncio.run(bot.run())