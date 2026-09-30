"""The /admin surface, split by area. Every route except sign-in needs an admin session.

auth: sign-in and sign-out. home: the admin home and global settings. channels, email_groups and
sources: their create, edit and delete pages. common: helpers more than one area uses.
"""

from __future__ import annotations

from fastapi import APIRouter

from beehive.connectors import (  # noqa: F401 (registers the connectors)
    all_about_auctions,
    google_news,
    hackernews,
    international_clearance,
    land_sea_collection,
    official_feeds,
    reddit,
    shopify_collection,
)
from beehive.web.admin import auth, channels, email_groups, home, sources

router = APIRouter(prefix="/admin")
for _area in (auth, home, channels, email_groups, sources):
    router.include_router(_area.router)
