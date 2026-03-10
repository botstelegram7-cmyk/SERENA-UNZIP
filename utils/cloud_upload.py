# utils/cloud_upload.py  ─ GoFile (account token) + Catbox fallback
import os
import aiohttp
from typing import Optional

from config import Config


async def upload_to_gofile(file_path: str) -> str:
    """
    Upload file to GoFile using account token.
    Uses Config.GOFILE_ACCOUNT_TOKEN for authenticated upload.
    Falls back to anonymous upload if no token set.
    Returns the download page URL.
    """
    token    = Config.GOFILE_ACCOUNT_TOKEN.strip() or None
    acct_id  = Config.GOFILE_ACCOUNT_ID.strip()    or None

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with aiohttp.ClientSession() as session:
        # Step 1: Get best available server
        async with session.get(
            "https://api.gofile.io/servers",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            r.raise_for_status()
            data = await r.json()
            servers = data.get("data", {}).get("servers", [])
            if not servers:
                raise RuntimeError("GoFile: no servers available")
            server_name = servers[0]["name"]

        # Step 2: Upload
        upload_url = f"https://{server_name}.gofile.io/contents/uploadfile"
        form = aiohttp.FormData()
        with open(file_path, "rb") as f:
            form.add_field(
                "file", f,
                filename=os.path.basename(file_path),
                content_type="application/octet-stream",
            )
            # If we have a token, attach it (keeps the file in account)
            if token:
                form.add_field("token", token)
            if acct_id:
                form.add_field("folderId", acct_id)

            async with session.post(
                upload_url,
                data=form,
                headers=headers if token else {},
                timeout=aiohttp.ClientTimeout(total=600),
            ) as r:
                r.raise_for_status()
                result = await r.json()

    if result.get("status") != "ok":
        raise RuntimeError(f"GoFile upload failed: {result.get('message', result)}")

    file_data = result.get("data", {})
    # Return direct download page
    return (
        file_data.get("downloadPage")
        or f"https://gofile.io/d/{file_data.get('parentFolder', '')}"
    )


async def upload_to_catbox(file_path: str) -> str:
    """
    Upload to catbox.moe (anonymous, max 200 MB).
    Returns direct download URL.
    """
    async with aiohttp.ClientSession() as session:
        form = aiohttp.FormData()
        form.add_field("reqtype", "fileupload")
        form.add_field("userhash", "")
        with open(file_path, "rb") as f:
            form.add_field(
                "fileToUpload", f,
                filename=os.path.basename(file_path),
                content_type="application/octet-stream",
            )
            async with session.post(
                "https://catbox.moe/user/api.php",
                data=form,
                timeout=aiohttp.ClientTimeout(total=300),
            ) as r:
                r.raise_for_status()
                url = await r.text()

    url = url.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"Catbox upload failed: {url}")
    return url


async def smart_upload(file_path: str) -> str:
    """
    Try GoFile first; fallback to Catbox on failure.
    Returns URL string.
    """
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)

    # Catbox max is 200 MB — use GoFile for large files
    if file_size_mb > 190:
        try:
            return await upload_to_gofile(file_path)
        except Exception as e:
            raise RuntimeError(f"GoFile failed: {e}")
    else:
        # Try GoFile first, then Catbox
        try:
            return await upload_to_gofile(file_path)
        except Exception:
            try:
                return await upload_to_catbox(file_path)
            except Exception as e2:
                raise RuntimeError(f"Both GoFile and Catbox failed. Last error: {e2}")
