"""The MCP endpoint: an OAuth 2.1 resource server inside the scheduler.

See docs/MCP.md for the design and docs/MCP-THREAT-MODEL.md for the
controls. ``registry`` holds the one table of tools and their scopes,
``tools`` the implementations, ``auth`` the token verifier and the gate,
``app`` the assembly that main.py mounts.
"""
