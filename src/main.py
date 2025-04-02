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
    if not sp or not token_info or token_info['expires_at'] < datetime.now().timestamp():
        token_info = auth_manager.get_cached_token()
        if not token_info:
            # Return None if no valid token, the application should redirect to /login
            return None
        sp = spotipy.Spotify(auth=token_info['access_token'])
        logger.info("Spotify client initialized/refreshed")
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

async def update_playback():
    while True:
        try:
            spotify = get_spotify_client()
            current_playback = spotify.current_playback()
            
            if current_playback and current_playback["item"]:
                logger.info(f"Current track: {current_playback['item']['name']} by {current_playback['item']['artists'][0]['name']}")
                
                track_info = {
                    "title": current_playback["item"]["name"],
                    "artist": current_playback["item"]["artists"][0]["name"],
                    "album": current_playback["item"]["album"]["name"],
                    "album_art": current_playback["item"]["album"]["images"][0]["url"],
                    "progress_ms": current_playback["progress_ms"],
                    "duration_ms": current_playback["item"]["duration_ms"],
                    "is_playing": current_playback["is_playing"]
                }

                # Only fetch new album art if it's different from the last one
                if manager.last_album_art != track_info["album_art"]:
                    logger.info("Fetching new album art")
                    art_data = await manager.fetch_album_art(track_info["album_art"])
                    if art_data:
                        track_info["album_art_base64"] = base64.b64encode(art_data).decode('utf-8')
                        manager.last_album_art = track_info["album_art"]
                        logger.info("New album art fetched and encoded")
                    else:
                        logger.warning("Failed to fetch new album art")

                manager.current_playback = track_info
                await manager.broadcast(json.dumps(track_info))
            else:
                logger.info("No active playback found")
                
        except Exception as e:
            logger.error(f"Error updating playback: {e}")
            
        await asyncio.sleep(1)  # Update every second

@app.websocket("/ws/spotify")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # Keep connection alive
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        manager.disconnect(websocket)

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