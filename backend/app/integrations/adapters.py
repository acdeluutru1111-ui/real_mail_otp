"""Convenience re-exports for the async upstream adapters (Bước 3).

Import surface for the domain/service layer (Bước 4)::

    from app.integrations.adapters import SmailProAdapter, SonjjAdapter
"""

from __future__ import annotations

from app.integrations.smailpro import SmailProAdapter
from app.integrations.sonjj import SonjjAdapter

__all__ = ["SmailProAdapter", "SonjjAdapter"]
