import os
import shutil
import tempfile
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Vercel serverless environment has a read-only root directory.
# Only /tmp (or system temp directory) is writable for SQLite database files.
if os.getenv("VERCEL"):
    tmp_dir = tempfile.gettempdir()
    tmp_db_path = os.path.join(tmp_dir, "ewaste.db")
    orig_db_path = os.path.join(BASE_DIR, "ewaste.db")
    # Copy pre-populated database to temp directory if not already there
    if os.path.exists(orig_db_path) and not os.path.exists(tmp_db_path):
        try:
            shutil.copy2(orig_db_path, tmp_db_path)
        except Exception:
            pass
    DEFAULT_DB_URL = f"sqlite:///{tmp_db_path}"
else:
    DEFAULT_DB_URL = f"sqlite:///{os.path.join(BASE_DIR, 'ewaste.db')}"

# Read DATABASE_URL from environment variable (for Cloud MySQL / PostgreSQL / Neon)
# Defaults to SQLite:
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_DB_URL)

# Handle legacy 'postgres://' URLs from cloud providers
if SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if SQLALCHEMY_DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args=connect_args
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()   

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
