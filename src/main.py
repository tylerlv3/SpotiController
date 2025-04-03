from PIL import Image
import io
import math
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import os
from dotenv import load_dotenv
import json
import asyncio
from typing import List
import base64
import aiohttp
from datetime import datetime, timedelta
import logging
from .database import Database

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

app = FastAPI()

# Initialize database
db = Database()

# CORS middleware configuration
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
        self.current_playback = None
        self.last_album_art = None
        self.last_art_data = None  # Cache for the processed image data

    async def optimize_album_art(self, image_data: bytes, target_size: int = 150) -> bytes:
        try:
            # Open the image using PIL
            with Image.open(io.BytesIO(image_data)) as img:
                # Convert to RGB if image is in RGBA mode
                if img.mode == 'RGBA':
                    img = img.convert('RGB')
                
                # Calculate new dimensions while maintaining aspect ratio
                ratio = min(target_size / img.width, target_size / img.height)
                new_size = (int(img.width * ratio), int(img.height * ratio))
                
                # Resize image
                img = img.resize(new_size, Image.Resampling.LANCZOS)
                
                # Save as JPEG with optimization
                output = io.BytesIO()
                img.save(output, format='JPEG', optimize=True, quality=85)
                return output.getvalue()
        except Exception as e:
            logger.error(f"Error optimizing image: {e}")
            return image_data

    async def fetch_album_art(self, url: str) -> bytes:
        if not url:
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        image_data = await response.read()
                        # Optimize the image before returning
                        return await self.optimize_album_art(image_data)
                    logger.error(f"Failed to fetch album art. Status: {response.status}")
        except Exception as e:
            logger.error(f"Error fetching album art: {e}")
        return None

    async def broadcast_album_art(self, art_data: bytes):
        if not art_data:
            return
            
        try:
            # Encode optimized image
            album_art_base64 = base64.b64encode(art_data).decode('utf-8')
            
            # Calculate chunk size (approximately 8KB per chunk)
            chunk_size = 8192
            total_chunks = math.ceil(len(album_art_base64) / chunk_size)
            
            for connection in self.active_connections:
                try:
                    # Send metadata about the incoming chunks
                    await connection.send_text(json.dumps({
                        "type": "album_art_start",
                        "total_chunks": total_chunks
                    }))
                    
                    # Send the art data in chunks
                    for i in range(total_chunks):
                        start = i * chunk_size
                        end = start + chunk_size
                        chunk = album_art_base64[start:end]
                        
                        await connection.send_text(json.dumps({
                            "type": "album_art_chunk",
                            "chunk_index": i,
                            "data": chunk
                        }))
                        
                        # Small delay between chunks to prevent overwhelming the connection
                        await asyncio.sleep(0.05)
                    
                    # Send completion message
                    await connection.send_text(json.dumps({
                        "type": "album_art_end"
                    }))
                    
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
            spotify = get_spotify_client()
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
            except spotipy.exceptions.SpotifyException as se:
                if se.http_status == 401:
                    logger.error("Spotify authentication expired, forcing refresh")
                    # Force token refresh on next iteration
                    global token_info
                    token_info = None
                else:
                    logger.error(f"Spotify API error: {se}")
            
        except Exception as e:
            logger.error(f"Error updating playback: {e}")
            
        await asyncio.sleep(1)  # Update every second

@app.on_event("startup")
async def startup_event():
    import socket
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