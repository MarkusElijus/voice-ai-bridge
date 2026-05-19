"""Admin dashboard for the Aria bridge.

Mounted at `/admin/*` from main.py. Light/dark themed, Acme Law brand
colors, server-rendered Jinja + HTMX (no React, no build step).

Auth: HTTP Basic (single admin user, bcrypt-hashed password from env).
Pages:
  /admin/calls               — call list with filters
  /admin/calls/{id}          — call detail (transcript, fields, tool calls)
  /admin/assistants          — assistant registry + prompt editors
  /admin/settings            — read-only env display
  /admin/costs               — cost charts
"""
