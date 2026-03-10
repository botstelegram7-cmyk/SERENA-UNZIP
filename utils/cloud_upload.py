# utils/cloud_upload.py
import os
import aiohttp
from typing import Optional


async def upload_to_gofile(file_path: str, api_key: Optional[str] = None) -> str:
    """
    Upload file to gofile.io (free, no registration required).
    Returns the download page URL.
    """
    async with aiohttp.ClientSession() as session:
        # 1. Get best server
        async with session.get("https://api.gofile.io/servers") as r:
            r.raise_for_status()
            data = await r.json()
            servers = data.get("data", {}).get("servers", [])
            if not servers:
                raise RuntimeError("GoFile: no servers available")
            server_name = servers[0]["name"]

        # 2. Upload file
        upload_url = f"https://{server_name}.gofile.io/contents/uploadfile"
        form = aiohttp.FormData()
        with open(file_path, "rb") as f:
            form.add_field(
                "file",
                f,
                filename=os.path.basename(file_path),
                content_type="application/octet-stream",
            )
            if api_key:
                form.add_field("token", api_key)

            async with session.post(upload_url, data=form) as r:
                r.raise_for_status()
                result = await r.json()

        if result.get("status") != "ok":
            raise RuntimeError(f"GoFile upload failed: {result}")

        return result["data"]["downloadPage"]


async def upload_to_catbox(file_path: str) -> str:
    """
    Upload file to catbox.moe (free anonymous upload, max 200 MB).
    Returns direct download URL.
    """
    async with aiohttp.ClientSession() as session:
        form = aiohttp.FormData()
        form.add_field("reqtype", "fileupload")
        form.add_field("userhash", "")
        with open(file_path, "rb") as f:
            form.add_field(
                "fileToUpload",
                f,
                filename=os.path.basename(file_path),
                content_type="application/octet-stream",
            )
            async with session.post("https://catbox.moe/user/api.php", data=form) as r:
                r.raise_for_status()
                url = await r.text()
        if not url.startswith("http"):
            raise RuntimeError(f"Catbox upload failed: {url}")
        return url.strip()
