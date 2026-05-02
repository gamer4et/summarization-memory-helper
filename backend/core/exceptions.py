"""
Shared HTTP exception helpers used across API routers.
"""

from fastapi import HTTPException, status


def not_found(resource: str, resource_id: int | str) -> HTTPException:
    """Return a 404 HTTPException with a consistent message."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"{resource} with id={resource_id} not found.",
    )


def bad_request(detail: str) -> HTTPException:
    """Return a 400 HTTPException."""
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=detail,
    )


def conflict(detail: str) -> HTTPException:
    """Return a 409 HTTPException."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=detail,
    )


def unprocessable(detail: str) -> HTTPException:
    """Return a 422 HTTPException."""
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=detail,
    )


def internal_error(detail: str = "Internal server error") -> HTTPException:
    """Return a 500 HTTPException."""
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=detail,
    )
