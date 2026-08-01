"""Noruct domain hooks inserted into the Hermes-based application fork.

This package intentionally contains no provider, filesystem, or network
authority.  It is the first insertion seam for Company task context; effects
continue to be executed by Noruct's parent authority.
"""

from .context import attach_company_context, company_context

__all__ = ["attach_company_context", "company_context"]
