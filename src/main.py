from fastapi import FastAPI, WebSocket, Request, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import os
from dotenv import load_dotenv
import json
import asyncio
from typing import List, Dict, Any, Optional
import base64
import aiohttp
from datetime import datetime, timedelta
import logging
from PIL import Image
import io
import math
import signal
import sys
from .database import Database
from contextlib import asynccontextmanager
import socket

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Enable debug logging temporarily to diagnose issues
logger.setLevel(logging.DEBUG)

load_dotenv()

# Initialize database
db = Database()

# Signal handling for graceful shutdown

# CORS middleware configuration
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Spotify API configuration
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")
SPOTIFY_SCOPE = "user-read-playback-state user-read-currently-playing"

# Spotify auth manager
auth_manager = SpotifyOAuth(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
    redirect_uri=SPOTIFY_REDIRECT_URI,
    scope=SPOTIFY_SCOPE,
    open_browser=False
)

def get_spotify_client():
    # Try to get token from database first
    token_info = db.get_token()
    
    if token_info:
        # Check if token is expired
        now = datetime.now().timestamp()
        is_expired = token_info['expires_at'] - now < 60  # Add 60 second buffer
        
        if is_expired:
            try:
                # Refresh the token
                logger.info("Token expired, refreshing...")
                token_info = auth_manager.refresh_access_token(token_info['refresh_token'])
                db.save_token(token_info)
                logger.info("Token refreshed and saved to database")
            except Exception as e:
                logger.error(f"Error refreshing token: {e}")
                return None
    else:
        # Try to get cached token from Spotipy's cache
        token_info = auth_manager.get_cached_token()
        if token_info:
            # Save to our database
            db.save_token(token_info)
            logger.info("Retrieved cached token and saved to database")
    
    if token_info:
        return spotipy.Spotify(auth=token_info['access_token'])
    return None

@app.get("/login")
async def login():
    """Redirect to Spotify authorization page"""
    auth_url = auth_manager.get_authorize_url()
    return RedirectResponse(url=auth_url)

@app.get("/callback")
async def callback(request: Request):
    """Handle the callback from Spotify"""
    code = request.query_params.get("code")
    if code:
        try:
            token_info = auth_manager.get_access_token(code)
            # Save token to database
            db.save_token(token_info)
            logger.info("New token saved to database")
            return {"status": "Successfully authenticated with Spotify!"}
        except Exception as e:
            logger.error(f"Error getting access token: {e}")
            return {"error": "Failed to get access token"}
    return {"error": "No code provided"}

@app.get("/")
async def root():
    """Check authentication status and redirect if needed"""
    if not get_spotify_client():
        return RedirectResponse(url="/login")
    return {"status": "Authenticated", "message": "Spotify WebSocket server is running"}

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.current_playback: Optional[Dict[str, Any]] = None
        self.last_album_art: Optional[str] = None
        self.last_art_data: Optional[bytes] = None

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"Client connected. Total connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
        logger.info(f"Client disconnected. Remaining connections: {len(self.active_connections)}")

    async def broadcast(self, message: str):
        """Broadcast a message to all connected clients"""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except WebSocketDisconnect:
                disconnected.append(connection)
            except Exception as e:
                logger.error(f"Error broadcasting message: {e}")
                disconnected.append(connection)
        
        # Clean up disconnected clients
        for conn in disconnected:
            if conn in self.active_connections:
                self.disconnect(conn)

    async def optimize_album_art(self, image_data: bytes, target_size: int = 150) -> bytes:
        try:
            # Open the image using PIL
            with Image.open(io.BytesIO(image_data)) as img:
                # Convert to RGB mode first
                if img.mode in ('RGBA', 'LA', 'P'):
                    img = img.convert('RGB')
                
                # Calculate new dimensions while maintaining aspect ratio
                ratio = min(target_size / img.width, target_size / img.height)
                new_size = (int(img.width * ratio), int(img.height * ratio))
                
                # Resize image using high-quality downsampling
                img = img.resize(new_size, Image.Resampling.LANCZOS)
                
                # Convert to JPEG with optimization
                output = io.BytesIO()
                img.save(output, format='JPEG', optimize=True, quality=85)
                return output.getvalue()
        except Exception as e:
            logger.error(f"Error optimizing image: {e}")
            return image_data  # Return original if optimization fails

    async def fetch_album_art(self, url: str) -> Optional[bytes]:
        if not url:
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        image_data = await response.read()
                        # First optimize the image to reduce size
                        optimized = await self.optimize_album_art(image_data, target_size=120)  # Reduced to 120px
                        logger.info(f"Image size reduced from {len(image_data)} to {len(optimized)} bytes")
                        return optimized
                    logger.error(f"Failed to fetch album art. Status: {response.status}")
                    return None
        except Exception as e:
            logger.error(f"Error fetching album art: {e}")
            return None

    async def broadcast_album_art_binary(self, art_data: bytes):
        """Send album art as binary data to all connected clients"""
        if not art_data:
            return
            
        try:
            # Store the art data to avoid re-sending the same image
            self.last_art_data = art_data
            
            disconnected = []
            for connection in self.active_connections:
                try:
                    # Send a JSON message first to inform client that binary data is coming
                    await connection.send_text(json.dumps({
                        "type": "album_art_binary",
                        "size": len(art_data),
                        "format": "jpeg"
                    }))
                    
                    # Then send the actual binary data
                    await connection.send_bytes(art_data)
                    
                except WebSocketDisconnect:
                    disconnected.append(connection)
                except Exception as e:
                    logger.error(f"Error sending binary album art: {e}")
                    disconnected.append(connection)
            
            # Clean up disconnected clients
            for conn in disconnected:
                if conn in self.active_connections:
                    self.disconnect(conn)
                    
        except Exception as e:
            logger.error(f"Error in broadcast_album_art_binary: {e}")

manager = ConnectionManager()

@app.websocket("/ws/spotify")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send current playback data if available when client connects
        if manager.current_playback:
            await websocket.send_text(json.dumps(manager.current_playback))
            
            # Also send the current album art if available
            if manager.last_art_data:
                # Send metadata first
                await websocket.send_text(json.dumps({
                    "type": "album_art_binary",
                    "size": len(manager.last_art_data),
                    "format": "jpeg"
                }))
                # Then send the binary data
                await websocket.send_bytes(manager.last_art_data)
        
        while True:
            data = await websocket.receive_text()  # Keep connection alive
            # Handle any incoming messages if needed
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
                elif msg.get("type") == "request_update":
                    if manager.current_playback:
                        await websocket.send_text(json.dumps(manager.current_playback))
                        # Also resend album art on request
                        if manager.last_art_data:
                            await websocket.send_text(json.dumps({
                                "type": "album_art_binary",
                                "size": len(manager.last_art_data),
                                "format": "jpeg"
                            }))
                            await websocket.send_bytes(manager.last_art_data)
            except json.JSONDecodeError:
                pass  # Ignore non-JSON messages
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        manager.disconnect(websocket)

async def update_playback():
    """Background task to update playback information from Spotify"""
    running = True
    while running:
        try:
            spotify = get_spotify_client()
            logger.debug(f"Acquired Spotify client for playback update {spotify.current_user}")
            if not spotify:
                logger.error("No valid Spotify client available - authentication needed")
                await asyncio.sleep(5)  # Wait longer when auth is needed
                continue

            try:
                logger.debug("Fetching current playback from Spotify API")
                # Add a timeout to prevent hanging
                current_playback = await asyncio.wait_for(
                    asyncio.to_thread(spotify.current_playback), timeout=10
                )
                logger.debug(f"Current playback data: {current_playback}")  # Log the raw playback data for debugging
                if current_playback and current_playback.get("item"):
                    track_name = current_playback["item"]["name"]
                    artist_name = current_playback["item"]["artists"][0]["name"]
                    logger.info(f"Current track: {track_name} by {artist_name}")

                    track_info = {
                        "title": track_name,
                        "artist": artist_name,
                        "album": current_playback["item"]["album"]["name"],
                        "progress_ms": current_playback["progress_ms"],
                        "duration_ms": current_playback["item"]["duration_ms"],
                        "is_playing": current_playback["is_playing"],
                        "type": "track_update"
                    }

                    album_art_url = current_playback["item"]["album"]["images"][0]["url"]
                    for image in current_playback["item"]["album"]["images"]:
                        if 200 <= image["width"] <= 350:
                            album_art_url = image["url"]
                            break

                    if manager.last_album_art != album_art_url:
                        logger.info("Fetching new album art")
                        art_data = await manager.fetch_album_art(album_art_url)
                        if art_data:
                            await manager.broadcast_album_art_binary(art_data)
                            manager.last_album_art = album_art_url
                        else:
                            logger.warning("Failed to fetch new album art")

                    manager.current_playback = track_info
                    await manager.broadcast(json.dumps(track_info))
                else:
                    logger.info("No active playback found")
            except asyncio.TimeoutError:
                logger.error("Spotify API call timed out")
            except spotipy.exceptions.SpotifyException as se:
                if se.http_status == 401:
                    logger.error(f"Spotify authentication expired, forcing refresh: {se}")
                    db.clear_tokens()
                else:
                    logger.error(f"Spotify API error: {se}")
            except Exception as e:
                logger.error(f"Unexpected error in Spotify API call: {e}")

        except Exception as e:
            logger.error(f"Error in update_playback loop: {e}")

        await asyncio.sleep(1)  # Update every second

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup (before yield)
    logger.info("="*50)
    logger.info("Starting Spotify WebSocket server...")
    
    # Get local IP address
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    
    logger.info(f"WebSocket server available at:")
    logger.info(f"Local: ws://127.0.0.1:8000/ws/spotify")
    logger.info(f"Network: ws://{local_ip}:8000/ws/spotify")
    logger.info("="*50)
    
    # Start the background playback updater task
    playback_task = asyncio.create_task(update_playback())
    logger.info("Spotify client initialized/refreshed")
    
    yield
    
    # Shutdown (after yield)
    logger.info("Shutting down server gracefully...")
    # Cancel the playback updater task
    playback_task.cancel()
    
    # Close all websocket connections
    for websocket in manager.active_connections[:]:
        try:
            await websocket.send_text(json.dumps({
                "type": "server_shutdown",
                "message": "Server is shutting down"
            }))
            await websocket.close()
            manager.disconnect(websocket)
        except Exception as e:
            logger.error(f"Error closing websocket: {e}")
            
    logger.info(f"Closed all {len(manager.active_connections)} websocket connections")
    logger.info("Shutdown complete")
    
app = FastAPI(lifespan=lifespan)