#  Copyright (c) 2026, RTE (https://www.rte-france.com)
#  See AUTHORS.txt
#  SPDX-License-Identifier: MPL-2.0
#  This file is part of pypowsybl-mcp.

import mimetypes
import os
import secrets
import tempfile
import threading
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Global registry for temporary download links
download_links: dict[str, dict[str, Any]] = {}
download_links_lock = threading.Lock()


def cleanup_expired_links():
    """Remove expired download links from registry and delete temporary files."""
    with download_links_lock:
        now = datetime.now()
        expired = [
            token for token, info in download_links.items() if info["expires_at"] < now
        ]
        for token in expired:
            link_info = download_links[token]
            # Delete the temporary file and directory if they exist
            if "temp_path" in link_info:
                temp_path = link_info["temp_path"]
                try:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
                        logger.debug(f"Deleted temporary file: {temp_path}")

                        # Try to delete the directory if it's empty
                        temp_dir = os.path.dirname(temp_path)
                        if os.path.isdir(temp_dir) and not os.listdir(temp_dir):
                            os.rmdir(temp_dir)
                            logger.debug(f"Deleted temporary directory: {temp_dir}")
                except Exception as e:
                    logger.warning(
                        f"Failed to delete temporary file/directory {temp_path}: {e}"
                    )

            del download_links[token]
            logger.debug(f"Cleaned up expired download link: {token}")


def generate_download_link(
    filename: str, file_data: bytes, download_base_url: str, expiry_seconds: int = 3600
) -> dict[str, Any]:
    """Generate a temporary download link for a file.

    Creates a local temporary copy of the file and
    returns a link pointing to this temporary file.
    """
    cleanup_expired_links()

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now() + timedelta(seconds=expiry_seconds)

    # Create a temporary directory to store the file with its original name
    temp_dir = tempfile.mkdtemp()
    temp_path = os.path.join(temp_dir, filename)

    try:
        # Write to temporary file with original filename
        with open(temp_path, "wb") as temp_file:
            temp_file.write(file_data)

        logger.info(f"Created temporary copy of {filename} at {temp_path}")

        with download_links_lock:
            download_links[token] = {
                "filename": filename,
                "temp_path": temp_path,
                "expires_at": expires_at,
                "created_at": datetime.now(),
            }

        download_url = f"{download_base_url}/{token}/{filename}"
        logger.info(f"Generated download link for {filename}: {token}")

        return {
            "token": token,
            "download_url": download_url,
            "expires_at": expires_at.isoformat(),
            "expires_in_seconds": expiry_seconds,
        }
    except Exception as e:
        # Clean up temp file and directory if there was an error
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            if os.path.exists(temp_dir):
                os.rmdir(temp_dir)
        except Exception:
            pass
        logger.error(f"Failed to create temporary copy for {filename}: {e}")
        raise


async def download_file_endpoint(request: Request) -> Response:
    """HTTP endpoint to download files using temporary tokens."""
    token = request.path_params.get("token")
    if not token:
        return JSONResponse({"error": "Token not provided"}, status_code=400)

    cleanup_expired_links()

    with download_links_lock:
        link_info = download_links.get(token)

    if not link_info:
        return JSONResponse(
            {"error": "Invalid or expired download link"}, status_code=404
        )

    # Check expiration
    if link_info["expires_at"] < datetime.now():
        # Clean up temp file and registry entry
        temp_path = link_info.get("temp_path")
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
                logger.debug(f"Deleted expired temporary file: {temp_path}")
            except Exception as e:
                logger.warning(f"Failed to delete expired temp file {temp_path}: {e}")

        with download_links_lock:
            del download_links[token]
        return JSONResponse({"error": "Download link has expired"}, status_code=410)

    filename = link_info["filename"]
    temp_path = link_info.get("temp_path")

    try:
        if temp_path and os.path.exists(temp_path):
            with open(temp_path, "rb") as f:
                data = f.read()
            logger.info(
                f"Serving download from temp file: {filename} ({len(data)} bytes)"
            )

            # Guess content type based on filename
            content_type, _ = mimetypes.guess_type(filename)
            if not content_type:
                content_type = "application/octet-stream"

            # Use inline disposition for images, attachment for others
            disposition_type = "attachment"
            if content_type.startswith("image/"):
                disposition_type = "inline"

            return Response(
                content=data,
                status_code=200,
                headers={
                    "Content-Type": content_type,
                    "Content-Disposition": f'{disposition_type}; filename="{filename}"',
                    "Content-Length": str(len(data)),
                },
            )
        else:
            return JSONResponse({"error": "File not found"}, status_code=404)
    except Exception as e:
        logger.error(f"Error serving download for token {token}: {e}")
        return JSONResponse(
            {"error": f"Failed to read file: {str(e)}"}, status_code=500
        )
