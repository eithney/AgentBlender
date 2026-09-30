"""Agent-side process: WebSocket hub, one-shot client, and the tool loop.

``loop`` observes the scene, calls an allowlisted op, and reflects on the
result. It stays a thin asyncio process, separate from Blender.
"""
