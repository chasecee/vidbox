"""Dashboard route for LOOP web server."""

import shutil
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter

from ..core.models import DashboardData, DeviceStatus, PlayerStatus, WiFiStatus, UpdateStatus, StorageInfo, APIResponse
from ..core.storage import get_dir_size
from ..core.events import broadcaster
from display.player import DisplayPlayer
from boot.wifi import WiFiManager
from deployment.updater import SystemUpdater
from utils.media_index import media_index
from utils.logger import get_logger

logger = get_logger("web.dashboard")

# Aggressive caching for Pi Zero 2 performance
_dashboard_cache: Optional[DashboardData] = None
_cache_timestamp: float = 0
_cache_ttl: float = 5.0  # 5 second cache TTL

async def collect_device_status(
    display_player: DisplayPlayer = None,
    wifi_manager: WiFiManager = None,
    updater: SystemUpdater = None
) -> DeviceStatus:
    """Shared device status collection logic for dashboard and WebSocket routes."""
    device_status = DeviceStatus()

    if display_player:
        try:
            player_data = display_player.get_status()
            device_status.player = PlayerStatus(**player_data)
        except Exception:
            pass

    if wifi_manager:
        try:
            wifi_data = wifi_manager.get_status()
            device_status.wifi = WiFiStatus(**wifi_data)
        except Exception:
            pass

    if updater:
        try:
            update_data = updater.get_update_status()
            device_status.updates = UpdateStatus(**update_data)
        except Exception:
            pass

    return device_status

def create_dashboard_router(
    display_player: DisplayPlayer = None,
    wifi_manager: WiFiManager = None,
    updater: SystemUpdater = None
) -> APIRouter:
    """Create dashboard router with dependencies."""
    
    router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
    
    def get_storage_info():
        """Get storage information - completely separate from dashboard."""
        total, used, free = shutil.disk_usage("/")

        backend_root = Path(__file__).resolve().parent.parent.parent
        media_path = backend_root / "media"
        media_size = get_dir_size(media_path)
        backend_size = get_dir_size(backend_root)
        app_size = max(0, backend_size - media_size)
        system_size = max(0, used - backend_size)

        return StorageInfo(
            total=total,
            used=used,
            free=free,
            system=system_size,
            app=app_size,
            media=media_size,
        )
    
    @router.get("", response_model=DashboardData)
    async def get_dashboard():
        """Get consolidated dashboard data - no storage included for performance."""
        global _dashboard_cache, _cache_timestamp
        
        current_time = time.time()
        
        # Return cached data if it's still valid
        if _dashboard_cache and (current_time - _cache_timestamp) < _cache_ttl:
            return _dashboard_cache
        
        # Cache miss - rebuild dashboard data (fast, no storage)
        start_time = time.time()
        
        # System status
        device_status = await collect_device_status(display_player, wifi_manager, updater)

        # Media / loop / processing data
        dashboard_data = media_index.get_dashboard_data()

        dashboard_result = DashboardData(
            status=device_status,
            media=dashboard_data["media"],
            active=dashboard_data["active"],
            loop=dashboard_data["loop"],
            last_updated=dashboard_data["last_updated"],
            processing=dashboard_data["processing"],
        )
        
        # Cache the result
        _dashboard_cache = dashboard_result
        _cache_timestamp = current_time
        
        build_time = time.time() - start_time
        logger.debug(f"Dashboard data rebuilt in {build_time*1000:.1f}ms (no storage)")
        
        # Broadcast dashboard update via WebSocket (fire and forget)
        try:
            import asyncio
            asyncio.create_task(broadcaster.dashboard_updated({
                "status": device_status.dict(),
                "media": dashboard_data["media"],
                "active": dashboard_data["active"],
                "loop": dashboard_data["loop"],
                "last_updated": dashboard_data["last_updated"],
                "processing": dashboard_data["processing"]
            }))
        except Exception as e:
            logger.debug(f"WebSocket broadcast failed: {e}")
        
        return dashboard_result
    
    @router.get("/storage", response_model=StorageInfo)
    async def get_storage():
        """Get storage information separately - for settings modal only."""
        return get_storage_info()
    
    @router.get("/memory", response_model=APIResponse)
    async def get_memory_stats():
        """Get memory pool statistics for performance monitoring."""
        try:
            from display.memory_pool import get_memory_stats
            stats = get_memory_stats()
            return APIResponse(
                success=True,
                data=stats
            )
        except Exception as e:
            logger.warning(f"Failed to get memory stats: {e}")
            return APIResponse(
                success=False,
                message="Memory stats unavailable",
                data={}
            )
    
    return router

def invalidate_dashboard_cache():
    """Invalidate the dashboard cache to force refresh on next request."""
    global _dashboard_cache, _cache_timestamp
    _dashboard_cache = None
    _cache_timestamp = 0
    logger.debug("Dashboard cache invalidated") 