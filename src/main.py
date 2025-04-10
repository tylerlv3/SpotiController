import os
import json
import logging
import asyncio
import aiohttp
import requests
from datetime import datetime
import socket
from fastapi import FastAPI, WebSocket, Request, HTTPException, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional
from PIL import Image
import io
from contextlib import asynccontextmanager
import time
from base64 import b64encode
from .database import Database
import math


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
from dotenv import load_dotenv
load_dotenv()

# Initialize database
db = Database()

# Spotify API configuration
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE_URL = "https://api.spotify.com/v1"
SPOTIFY_SCOPE = "user-read-playback-state user-read-currently-playing"

class SpotifyClient:
    """Direct implementation of Spotify Web API client"""
    
    def __init__(self, access_token: str = None):
        self.access_token = access_token
        self.session = requests.Session()
        
    @classmethod
    def get_auth_url(cls) -> str:
        """Generate the Spotify authorization URL"""
        params = {
            "client_id": SPOTIFY_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": SPOTIFY_REDIRECT_URI,
            "scope": SPOTIFY_SCOPE
        }
        return f"{SPOTIFY_AUTH_URL}?{requests.compat.urlencode(params)}"
    
    @classmethod
    def get_access_token(cls, code: str) -> dict:
        """Exchange authorization code for access token"""
        auth_header = b64encode(
            f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
        ).decode()
        
        headers = {
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": SPOTIFY_REDIRECT_URI
        }
        
        response = requests.post(SPOTIFY_TOKEN_URL, headers=headers, data=data)
        response.raise_for_status()
        
        token_info = response.json()
        token_info["expires_at"] = time.time() + token_info["expires_in"]
        return token_info
    
    @classmethod
    def refresh_access_token(cls, refresh_token: str) -> dict:
        """Refresh an expired access token"""
        auth_header = b64encode(
            f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
        ).decode()
        
        headers = {
            "Authorization": f"Basic {auth_header}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token
        }
        
        response = requests.post(SPOTIFY_TOKEN_URL, headers=headers, data=data)
        response.raise_for_status()
        
        token_info = response.json()
        token_info["expires_at"] = time.time() + token_info["expires_in"]
        return token_info
    
    def _make_request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Make a request to the Spotify API with proper headers and error handling"""
        if not self.access_token:
            raise ValueError("No access token available")
            
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        }
        
        if "headers" in kwargs:
            headers.update(kwargs.pop("headers"))
            
        url = f"{SPOTIFY_API_BASE_URL}/{endpoint.lstrip('/')}"
        response = self.session.request(method, url, headers=headers, **kwargs)
        
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 1))
            logger.warning(f"Rate limited. Waiting {retry_after} seconds")
            time.sleep(retry_after)
            return self._make_request(method, endpoint, **kwargs)
            
        response.raise_for_status()
        return response.json() if response.content else None
    
    def current_playback(self) -> Optional[dict]:
        """Get information about user's current playback state"""
        try:
            return self._make_request("GET", "me/player")
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 204:
                return None
            raise

async def get_spotify_client() -> Optional[SpotifyClient]:
    """Get a Spotify client with valid token, refreshing if needed"""
    token_info = db.get_token()
    
    if not token_info:
        logger.warning("No token available")
        return None
        
    now = time.time()
    is_expired = token_info["expires_at"] - now < 60
    
    if is_expired:
        try:
            logger.info("Token expired, refreshing...")
            new_token = SpotifyClient.refresh_access_token(token_info["refresh_token"])
            db.save_token(new_token)
            token_info = new_token
        except Exception as e:
            logger.error(f"Error refreshing token: {e}")
            db.clear_tokens()
            return None
            
    return SpotifyClient(access_token=token_info["access_token"])

app = FastAPI()

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/login")
async def login():
    """Generate a Spotify login URL"""
    auth_url = SpotifyClient.get_auth_url()
    return RedirectResponse(url=auth_url)
    
@app.get("/callback")
async def callback(request: Request):
    """Handle Spotify OAuth callback"""
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Authorization code missing")
        
    try:
        token_info = SpotifyClient.get_access_token(code)
        db.save_token(token_info)
        logger.info("New Spotify token acquired and saved")
        return RedirectResponse(url="/")
    except Exception as e:
        logger.error(f"Error in callback: {e}")
        raise HTTPException(status_code=500, detail=f"Authentication error: {str(e)}")

@app.get("/")
async def root():
    """Check authentication status and redirect if needed"""
    if not await get_spotify_client():
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

    async def optimize_album_art(self, image_data: bytes, target_size: int = 100) -> bytes:
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
            
    async def broadcast_album_art(self, art_data: bytes):
        if not art_data:
            return
            
        try:
            # Send a notification that album art is coming
            for connection in self.active_connections:
                try:
                    # First send a text message indicating that binary data is about to be sent
                    await connection.send_text(json.dumps({
                        "type": "album_art_coming",
                        "size": len(art_data)
                    }))
                    
                    # Then send the complete album art as binary data directly
                    await connection.send_bytes(art_data)
                    
                    logger.info(f"Album art sent as binary data: {len(art_data)} bytes")
                    
                except Exception as e:
                    logger.error(f"Error sending album art to client: {e}")
                    
        except Exception as e:
            logger.error(f"Error processing album art for broadcast: {e}")

manager = ConnectionManager()

@app.websocket("/ws/spotify")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
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
            except json.JSONDecodeError:
                pass  # Ignore non-JSON messages
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        manager.disconnect(websocket)

async def update_playback():
    while True:
        try:
            spotify = await get_spotify_client()
            if not spotify:
                logger.error("No valid Spotify client available - authentication needed")
                await asyncio.sleep(5)  # Wait longer when auth is needed
                continue
                
            try:
                current_playback = spotify.current_playback()
                
                if current_playback and current_playback.get("item"):
                    logger.info(f"Current track: {current_playback['item']['name']} by {current_playback['item']['artists'][0]['name']}")
                    
                    track_info = {
                        "title": current_playback["item"]["name"],
                        "artist": current_playback["item"]["artists"][0]["name"],
                        "album": current_playback["item"]["album"]["name"],
                        "progress_ms": current_playback["progress_ms"],
                        "duration_ms": current_playback["item"]["duration_ms"],
                        "is_playing": current_playback["is_playing"]
                    }
                    
                    # Check if we need to fetch new album art (only if it has changed)
                    album_art_url = current_playback["item"]["album"]["images"][0]["url"]
                    # Get a smaller image if available to reduce payload size
                    for image in current_playback["item"]["album"]["images"]:
                        # Try to find a medium-sized image (around 300px)
                        if 200 <= image["width"] <= 350:
                            album_art_url = image["url"]
                            break

                    # Only fetch new album art if it's different from the last one
                    if manager.last_album_art != album_art_url:
                        logger.info("Fetching new album art")
                        art_data = await manager.fetch_album_art(album_art_url)
                        if art_data:
                            # Send album art separately to avoid large messages
                            await manager.broadcast_album_art(art_data)
                        else:
                            logger.warning("Failed to fetch new album art")

                    # Add URL instead of base64 in the main payload
                    track_info["album_art"] = album_art_url
                    track_info["type"] = "track_update"
                    
                    manager.current_playback = track_info
                    await manager.broadcast(json.dumps(track_info))
                else:
                    logger.info("No active playback found")
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 401:
                    logger.error("Spotify authentication expired, forcing refresh")
                    db.clear_tokens()
                else:
                    logger.error(f"Spotify API error: {e}")
            
        except Exception as e:
            logger.error(f"Error updating playback: {e}")
            
        await asyncio.sleep(1)  # Update every second

@app.on_event("startup")
async def startup_event():
    logger.info("="*50)
    logger.info("Starting Spotify WebSocket server...")
    
    # Get local IP address
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    
    logger.info(f"WebSocket server available at:")
    logger.info(f"Local: ws://127.0.0.1:8000/ws/spotify")
    logger.info(f"Network: ws://{local_ip}:8000/ws/spotify")
    logger.info("="*50)
    
    asyncio.create_task(update_playback())
    logger.info("Spotify client initialized/refreshed")

if __name__ == "__main__":
    import uvicorn
    port = 8000
    host = "0.0.0.0"
    logger.info("="*50)
    logger.info(f"WebSocket server starting on ws://{host}:{port}/ws/spotify")
    logger.info(f"Local network access: ws://[your-local-ip]:{port}/ws/spotify")
    logger.info("="*50)
    uvicorn.run("main:app", host=host, port=port, reload=True)