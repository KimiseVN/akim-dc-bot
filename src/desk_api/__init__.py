"""FastAPI app that exposes Pet data to the akim-desk-pet client.

Runs in the same asyncio loop as the Discord bot — uvicorn is started
as a background task from main.py so we share the bot's DB session
factory directly (no HTTP round-trip for the link confirm flow).
"""
