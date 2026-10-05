"""Central configuration for Road Rescue.

Database settings are defined ONLY here. Defaults match a fresh XAMPP install
(user root, empty password, port 3306). Change them here, or set environment
variables (DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME) if you prefer.
"""
import os
from datetime import timedelta


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-secret-key-before-submission")

    # ---- MySQL (XAMPP) -------------------------------------------------
    DB_HOST = os.environ.get("DB_HOST", "localhost")
    DB_PORT = int(os.environ.get("DB_PORT", "3306"))
    DB_USER = os.environ.get("DB_USER", "root")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
    DB_NAME = os.environ.get("DB_NAME", "road_rescue")

    # ---- Session security ---------------------------------------------
    PERMANENT_SESSION_LIFETIME = timedelta(hours=3)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # ---- Default admin (created automatically on first run) ------------
    ADMIN_NAME = "System Admin"
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@roadrescue.com")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Admin@123")
    ADMIN_PHONE = "9999999999"
