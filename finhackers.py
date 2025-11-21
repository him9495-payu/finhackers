"""
Compatibility shim that re-exports the FastAPI app and helpers from
main_app after the codebase was split into dedicated modules.
"""

from main_app import app, lambda_handler, run  # noqa: F401
