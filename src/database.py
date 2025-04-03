from sqlalchemy import create_engine, Column, String, Integer, JSON, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import json

Base = declarative_base()

class SpotifyToken(Base):
    __tablename__ = 'spotify_tokens'
    
    id = Column(Integer, primary_key=True)
    access_token = Column(String)
    refresh_token = Column(String)
    expires_at = Column(DateTime)
    token_info = Column(JSON)
    updated_at = Column(DateTime, default=datetime.utcnow)

    @staticmethod
    def from_token_info(token_info):
        return SpotifyToken(
            access_token=token_info.get('access_token'),
            refresh_token=token_info.get('refresh_token'),
            expires_at=datetime.fromtimestamp(token_info.get('expires_at', 0)),
            token_info=token_info,
            updated_at=datetime.utcnow()
        )

class Database:
    def __init__(self, db_url="sqlite:///spotify_tokens.db"):
        self.engine = create_engine(db_url)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)

    def get_token(self):
        session = self.SessionLocal()
        try:
            token = session.query(SpotifyToken).order_by(SpotifyToken.updated_at.desc()).first()
            if token:
                return token.token_info
            return None
        finally:
            session.close()

    def save_token(self, token_info):
        session = self.SessionLocal()
        try:
            # Create new token entry
            new_token = SpotifyToken.from_token_info(token_info)
            session.add(new_token)
            session.commit()
        finally:
            session.close()

    def clear_tokens(self):
        session = self.SessionLocal()
        try:
            session.query(SpotifyToken).delete()
            session.commit()
        finally:
            session.close()