"""
WSGI entry point for Gunicorn (production server).

Usage:
    gunicorn wsgi:app --workers 2 --bind 0.0.0.0:5000
"""
from app import app

if __name__ == "__main__":
    app.run()
