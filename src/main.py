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

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

app = FastAPI()

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

# Spotify token management
token_info = None
sp = None
auth_manager = SpotifyOAuth(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
    redirect_uri=SPOTIFY_REDIRECT_URI,
    scope=SPOTIFY_SCOPE
)

def get_spotify_client():
    global token_info, sp
    
    # Check if token exists and is expired
    if token_info and token_info['expires_at'] < datetime.now().timestamp():
        logger.info("Token expired, refreshing...")
        try:
            # Explicitly refresh the token using the refresh_token
            token_info = auth_manager.refresh_access_token(token_info['refresh_token'])
            sp = spotipy.Spotify(auth=token_info['access_token'])
            logger.info("Token refreshed successfully")
            return sp
        except Exception as e:
            logger.error(f"Error refreshing token: {e}")
            token_info = None
            sp = None
    
    # If no token or refresh failed, try to get cached token
    if not token_info:
        try:
            token_info = auth_manager.get_cached_token()
            if token_info:
                sp = spotipy.Spotify(auth=token_info['access_token'])
                logger.info("Retrieved cached token successfully")
                return sp
            else:
                logger.warning("No cached token available - please authenticate via /login endpoint")
                return None
        except Exception as e:
            logger.error(f"Error retrieving cached token: {e}")
            return None
    
    # If token exists and is valid
    return sp

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
        global token_info
        try:
            token_info = auth_manager.get_access_token(code)
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

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info(f"New WebSocket connection. Total connections: {len(self.active_connections)}")
        if self.current_playback:
            await websocket.send_text(json.dumps(self.current_playback))

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)
        logger.info(f"WebSocket disconnected. Remaining connections: {len(self.active_connections)}")

    async def broadcast(self, message: str):
        if not self.active_connections:
            logger.debug("No active connections to broadcast to")
            return
            
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
                logger.debug("Message broadcast successfully")
            except Exception as e:
                logger.error(f"Error broadcasting message: {e}")
                await self.disconnect(connection)

    async def fetch_album_art(self, url: str) -> bytes:
        if not url:
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status == 200:
                        return await response.read()
                    logger.error(f"Failed to fetch album art. Status: {response.status}")
        except Exception as e:
            logger.error(f"Error fetching album art: {e}")
        return None

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
                            # Limit the album art size - encode separately from track info
                            album_art_base64 = base64.b64encode(art_data).decode('utf-8')
                            manager.last_album_art = album_art_url
                            logger.info("New album art fetched and encoded")
                            
                            # Send album art separately to avoid large messages
                            for connection in manager.active_connections:
                                try:
                                    # Send artwork with type indicator
                                    await connection.send_text(json.dumps({
                                        "type": "album_art",
                                        "album_art_base64": album_art_base64
                                    }))
                                except Exception as e:
                                    logger.error(f"Error sending album art: {e}")
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