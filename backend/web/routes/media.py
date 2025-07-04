"""Media management routes for LOOP web server."""

import json
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import List
from datetime import datetime

from fastapi import APIRouter, UploadFile, File, HTTPException, Request

from ..core.models import APIResponse
from ..core.storage import invalidate_storage_cache
from ..core.events import broadcaster
from display.player import DisplayPlayer
from utils.media_index import media_index
from utils.logger import get_logger
from .dashboard import invalidate_dashboard_cache

logger = get_logger("web.media")

def create_media_router(
    display_player: DisplayPlayer = None,
    media_raw_dir: Path = None,
    media_processed_dir: Path = None
) -> APIRouter:
    """Create media management router with dependencies."""
    
    router = APIRouter(prefix="/api/media", tags=["media"])
    
    @router.get("", response_model=APIResponse)
    async def get_media():
        """Get all media items."""
        try:
            media_list = media_index.list_media()
            return APIResponse(
                success=True,
                data={
                    "media": media_list,
                    "active": media_index.get_active(),
                    "last_updated": int(time.time())
                }
            )
        except Exception as e:
            logger.error(f"Failed to get media: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("", response_model=APIResponse)
    async def upload_media(request: Request, files: List[UploadFile] = File(...)):
        """Upload media files with bulletproof transaction-based processing."""
        # Generate unique request ID to track duplicates
        request_id = str(uuid.uuid4())[:8]
        
        # Log with request ID to track duplicates
        logger.info(f"🎬 Upload request [{request_id}]: {len(files)} files")
        
        try:
            # Use new transaction-based coordinator
            from ..core.upload_coordinator import upload_coordinator
            
            upload_result = await upload_coordinator.process_upload(
                files, media_raw_dir, media_processed_dir, display_player
            )
            
            # Invalidate caches
            invalidate_storage_cache()
            invalidate_dashboard_cache()
            
            logger.info(f"✅ Upload complete [{request_id}]: {upload_result['processed']} files processed")
            
            return APIResponse(
                success=upload_result["success"], 
                message=f"Processed {upload_result['processed']} files", 
                data={
                    "slug": upload_result["last_slug"],
                    "job_ids": []  # No jobs in transaction system
                }
            )
            
        except Exception as e:
            logger.error(f"❌ Upload failed [{request_id}]: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/{slug}/activate", response_model=APIResponse)
    async def activate_media(slug: str):
        """Set a media item as active."""
        if not display_player:
            raise HTTPException(status_code=503, detail="Display player not available")
        
        try:
            media_index.add_to_loop(slug)  # Ensure in loop
            media_index.set_active(slug)
            display_player.set_active_media(slug)
            invalidate_dashboard_cache()
            return APIResponse(success=True, message=f"Activated media: {slug}")
        except KeyError:
            raise HTTPException(status_code=404, detail="Media not found")
    
    @router.delete("/{slug}", response_model=APIResponse)
    async def delete_media(slug: str):
        """Delete a media item."""
        try:
            # Handle display player deletion first (before removing files)
            if display_player:
                display_player.handle_media_deletion(slug)

            # Fetch metadata BEFORE removal so we can identify duplicates
            all_media = media_index.get_media_dict()
            deleted_meta = all_media.get(slug, {})

            # Perform primary removal
            media_index.remove_media(slug)

            # Remove from filesystem AFTER stopping playback
            media_dir = media_processed_dir / slug
            raw_files = list(media_raw_dir.glob(f"*{slug}*"))

            if media_dir.exists():
                shutil.rmtree(media_dir)
                logger.info(f"Removed processed directory: {media_dir}")

            for raw_file in raw_files:
                raw_file.unlink()
                logger.info(f"Removed raw file: {raw_file}")

            # ALSO remove any duplicate raw upload that shares the same filename
            if deleted_meta.get("filename"):
                dup_slug = next(
                    (
                        s
                        for s, m in all_media.items()
                        if s != slug
                        and m.get("filename") == deleted_meta["filename"]
                        and m.get("processing_status") == "uploaded"
                    ),
                    None,
                )

                if dup_slug:
                    logger.info(f"Deleting duplicate raw upload: {dup_slug}")
                    media_index.remove_media(dup_slug)

                    dup_dir = media_processed_dir / dup_slug
                    if dup_dir.exists():
                        shutil.rmtree(dup_dir)
                        logger.info(f"Removed processed directory: {dup_dir}")

                    dup_raw_files = list(media_raw_dir.glob(f"*{dup_slug}*"))
                    for rf in dup_raw_files:
                        rf.unlink()
                        logger.info(f"Removed raw file: {rf}")

            invalidate_storage_cache()
            invalidate_dashboard_cache()

            return APIResponse(success=True, message=f"Deleted media: {slug}")

        except Exception as e:
            logger.error(f"Failed to delete media {slug}: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @router.post("/cleanup", response_model=APIResponse)
    async def cleanup_orphaned_media():
        """Clean up orphaned media files."""
        try:
            cleanup_count = media_index.cleanup_orphaned_files(media_raw_dir, media_processed_dir)
            
            if display_player:
                display_player.refresh_media_list()
            
            if cleanup_count:
                invalidate_storage_cache()
                invalidate_dashboard_cache()
            
            return APIResponse(
                success=True,
                message=f"Cleaned up {cleanup_count} orphaned files"
            )
        except Exception as e:
            logger.error(f"Failed to cleanup media: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    # Legacy /media/progress endpoints removed – processing completes synchronously
    
    return router 