# TGFS Manager DDL Downloader

import os
import time
import aria2p
import aiohttp
import logging
import asyncio
from pathlib import Path
from typing import Optional
from pyrogram.types import CallbackQuery
from pyrogram.errors import MessageNotModified

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

aria2 = aria2p.API(
    aria2p.Client(
        host="http://localhost",
        port=6800,
        secret='Kv4crmwrfbfVGxrsHqKk9APBjWRv101MSZjgKcyBqCRUkNfrpRn8d1ZSFaEFV15B' # Do not change this!
    )
)
logger.info(f"Aria2 started successfully. Version: {aria2.client.get_version}")

class AsyncFileDownloader:
    def __init__(self, update_interval: int = 2):  # 2GB default
        """
        Initialize the AsyncFileDownloader
        """
        self.progress_update_interval = update_interval
        self.download_gid: Optional[str] = None
        self.start_time = time.time()

    @staticmethod
    def format_size(size_bytes: int) -> str:
        """Format file size in human readable format"""
        if size_bytes == 0:
            return "0B"

        size_names = ["B", "KB", "MB", "GB"]
        size_index = 0
        size = float(size_bytes)

        while size >= 1024 and size_index < len(size_names) - 1:
            size /= 1024
            size_index += 1

        return f"{size:.2f}{size_names[size_index]}"

    @staticmethod
    def has_files(folder_path):
        folder = Path(folder_path)

        # Check if folder exists first
        if not folder.exists():
            return False, False, False, None

        # Get all files in the folder
        files = [item for item in folder.iterdir() if item.is_file()]

        # 1st: Is there only 1 file?
        has_only_one_file = len(files) == 1

        # 2nd: If 1 file, is it > 0 bytes?
        file_has_content = False
        if has_only_one_file:
            file_has_content = files[0].stat().st_size > 0

        # 3rd: If 1 file, get full absolute path
        file_abs_path = None
        if has_only_one_file:
            file_abs_path = files[0].resolve()

        return has_only_one_file, file_has_content, file_abs_path

    def format_speed(self, bytes_per_sec: float) -> str:
        """Format download speed in human readable format"""
        return f"{self.format_size(int(bytes_per_sec))}/s"

    @staticmethod
    def format_time(seconds: int) -> str:
        """Format time in human readable format"""
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            return f"{seconds // 60}m {seconds % 60}s"
        else:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return f"{hours}h {minutes}m"

    @staticmethod
    async def get_file_info(url: str) -> int:
        try:
            options = {
                "pause": "true",
                "file-allocation": "none",
                "max-connection-per-server": "1",
            }

            download = await asyncio.to_thread(
                aria2.add_uris, [url], options=options
            )

            await asyncio.sleep(1)

            download_info = await asyncio.to_thread(aria2.get_download, download.gid)

            if download_info.total_length == 0:
                await asyncio.to_thread(aria2.resume, [download_info])
                await asyncio.sleep(2)
                download_info = await asyncio.to_thread(aria2.get_download, download.gid)
                await asyncio.to_thread(aria2.pause, [download_info])

            file_size = download_info.total_length

            await asyncio.to_thread(aria2.remove, [download_info], force=True, files=True, clean=True)

            if file_size == 0:
                async with aiohttp.ClientSession() as session:
                    async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                        size = resp.headers.get("Content-Length")
                        if size is not None:
                            file_size = int(size)
                        else:
                            raise Exception("Could not determine file size - server may not provide content-length header")

            return file_size

        except Exception as e:
            raise Exception(f"Failed to get file info: {str(e)}")

    async def monitor_download_progress(self, download_gid: str, file_size: int,
                                        progress_msg, file_name: str):
        """
        Monitor download progress and update message using aria2p
        """
        last_update = 0
        start_time = self.start_time

        while True:
            try:
                # Get download status
                download = await asyncio.to_thread(aria2.get_download, download_gid)

                if download.status == "complete":
                    break

                if download.status == "error" or download.status == "removed":
                    raise Exception(f"Download failed: {download.error_message}")

                current_time = time.time()

                if int(current_time - last_update) >= self.progress_update_interval:
                    downloaded = download.completed_length
                    progress = (downloaded / file_size * 100) if file_size > 0 else 0

                    # Calculate speed and ETA
                    elapsed = current_time - start_time
                    speed = download.download_speed

                    if speed > 0 and file_size > 0:
                        remaining_bytes = file_size - downloaded
                        eta_seconds = int(remaining_bytes / speed)
                        eta_text = self.format_time(eta_seconds)
                    else:
                        eta_text = "Unknown"

                    # Create progress bar
                    bar_length = 20
                    filled_length = int(bar_length * progress / 100)
                    bar = '█' * filled_length + '░' * (bar_length - filled_length)

                    progress_text = (
                        f"📥 **Downloading to server...**\n\n"
                        f"**File:** `{file_name}`\n"
                        f"**Progress:** `{progress:.1f}%`\n"
                        f"`{bar}` \n\n"
                        f"**Downloaded:** `{self.format_size(downloaded)}` / `{self.format_size(file_size)}`\n"
                        f"**Speed:** `{self.format_speed(speed)}`\n"
                        f"**ETA:** `{eta_text}` | **Elapsed:** `{self.format_time(int(elapsed))}`\n"
                        f"**Connections:** `{download.connections}`"
                    )

                    try:
                        await progress_msg.edit_text(progress_text)
                    except MessageNotModified:
                        pass

                    last_update = current_time

                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Progress monitoring error: {e}")
                await asyncio.sleep(1)

    async def download_file(self, url: str, save_path: str, message: CallbackQuery, file_name: str) -> bool:
        """
        Download file with aria2p and progress updates

        Args:
            url: Download URL
            save_path: Path to save the file
            message: Pyrogram message object to reply to
            file_name: File name
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Get file info
            try:
                file_size = await self.get_file_info(url)
            except Exception as e:
                await message.edit_message_text(f"❌ **Error getting file info:**\n`{str(e)}`")
                return False

            # Start download
            progress_msg = await message.edit_message_text(
                f"📥 **Starting download to server...**\n\n"
                f"**File:** `{file_name}`\n"
                f"**Size:** `{self.format_size(file_size)}`\n"
                f"**Status:** `Connecting...`"
            )

            # Configure aria2 options
            options = {
                "dir": os.path.dirname(save_path),
                "max-connection-per-server": "16",
                "split": "16",
                "min-split-size": "1M",
                "file-allocation": "falloc",
                "max-overall-download-limit": "0",
                "max-tries": "5",
                "retry-wait": "2",
                "connect-timeout": "30",
                "allow-overwrite": "true",
                "auto-file-renaming": "true",
                "continue": "true"
            }

            # Start download using aria2p
            download = await asyncio.to_thread(
                aria2.add_uris, [url], options=options
            )
            self.download_gid = download.gid

            # Start progress monitoring
            monitor_task = asyncio.create_task(
                self.monitor_download_progress(self.download_gid, file_size, progress_msg, file_name)
            )

            # Wait for download to complete
            try:
                while True:
                    download_status = await asyncio.to_thread(aria2.get_download, self.download_gid)

                    if download_status.status == "complete":
                        break
                    elif download_status.status in ["error", "removed"]:
                        raise Exception(f"Download failed: {download_status.error_message}")

                    await asyncio.sleep(1)
            finally:
                # Cancel monitoring task
                monitor_task.cancel()
                try:
                    await monitor_task
                except asyncio.CancelledError:
                    pass

            # Download completed
            total_time = time.time() - self.start_time
            downloaded_size = os.path.getsize(save_path)
            avg_speed = downloaded_size / total_time if total_time > 0 else 0

            await progress_msg.edit_text(
                f"✅ **Download completed!**\n\n"
                f"**File:** `{file_name}`\n"
                f"**Size:** `{self.format_size(downloaded_size)}`\n"
                f"**Time:** `{self.format_time(int(total_time))}`\n"
                f"**Avg Speed:** `{self.format_speed(avg_speed)}`\n\n"
                "**Awaiting for** `rclone transfer`"
            )
            logger.info("Download completed")

            return True

        except asyncio.CancelledError:
            logger.error("Download cancelled")
            await message.edit_message_text("❌ **Download cancelled!**")
            return False

        except Exception as e:
            logger.error(f"Download failed: {e}")
            await message.edit_message_text(f"❌ **Download failed:**\n`{str(e)}`")
            return False

    async def download(self, url: str, save_path: str, message: CallbackQuery, file_name: str) -> bool:
        """
        Public method to download file

        Args:
            url: Download URL
            save_path: Path to save the file
            message: Pyrogram message object
            file_name: File name
        Returns:
            bool: True if successful, False otherwise
        """
        return await self.download_file(url, save_path, message, file_name)