"""Errors a service raises. Each becomes {"detail": message, "code": code} with its status, so
the frontend can show the message as it did when the services threw in the browser."""


class AppError(Exception):
    status_code = 400

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        if status_code is not None:
            self.status_code = status_code


class NotFound(AppError):
    status_code = 404


class Unauthorized(AppError):
    status_code = 401


class Forbidden(AppError):
    status_code = 403


class Conflict(AppError):
    status_code = 409


class Locked(AppError):
    status_code = 423


SESSION_ENDED = "Your session has ended. Sign in again."
READ_ONLY_MESSAGE = "Read-only while viewing as farmer"
SUSPENDED_MESSAGE = "Your account is suspended. Contact support."
