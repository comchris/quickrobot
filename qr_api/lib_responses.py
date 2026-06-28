"""Response helper functions for quickrobot route handlers.

Provides standardized JSON response formatting:
- success_single(): single-resource responses
- success_list(): list responses with pagination
- error_response(): structured error responses
- require_json(): request body validation

All route modules import from this module.
"""

from flask import request, jsonify


def success_single(data):
    """Return a standard single-resource success response."""
    return jsonify({"status": "ok", "data": data}), 200


def success_list(items, total=None, meta=None):
    """Return a standard list success response."""
    if total is None:
        total = len(items)
    resp = {"status": "ok", "total": total, "items": items}
    if meta is not None:
        resp["meta"] = meta
    return jsonify(resp), 200


def error_response(code, message, status_code=400, detail=None):
    """Return a standard error response.

    Args:
        code: Error code string.
        message: Human-readable error message.
        status_code: HTTP status code (default 400).
        detail: Optional dict with additional error context.

    Returns:
        Tuple of (json_response, status_code).
    """
    resp = {"status": "error", "code": code, "message": message}
    if detail is not None:
        resp["detail"] = detail
    return jsonify(resp), status_code


def require_json():
    """Ensure request has JSON body; return (body, None) or (error_response, True)."""
    # Accept body regardless of Content-Type header (MCP/curl clients often omit it)
    body = request.get_json(force=True, silent=True)
    if body is None:
        return {"_error": "Invalid JSON in request body"}, True
    return body, False
