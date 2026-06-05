from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from collections.abc import Callable

from flask import Flask
from werkzeug.serving import BaseWSGIServer, make_server

from app import create_app
from app.utils.logging import configure_logger

logger = configure_logger(__name__)

if not __package__:
    sys.path.append(str(Path(__file__).resolve().parents[1]))


class FlaskAppService:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        shutdown_timeout_seconds: int = 5,
        app_factory: Callable[[], Flask] | None = None,
        telegram_bot_service: object | None = None,
    ) -> None:
        self.host = host or os.environ.get("OPTION_WHEEL_APP_HOST", "0.0.0.0")
        self.port = port if port is not None else int(os.environ.get("OPTION_WHEEL_APP_PORT", "5001"))
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.app_factory = app_factory or (lambda: create_app(telegram_bot_service=telegram_bot_service))

        self._server: BaseWSGIServer | None = None
        self._server_thread: threading.Thread | None = None
        self._lock = threading.Lock()

    ####################################################################################################
    # Public Service API
    ####################################################################################################

    def start(self) -> None:
        with self._lock:
            if self._server_thread and self._server_thread.is_alive():
                return
            if self._server is not None:
                self._server.server_close()

            self._server = make_server(self.host, self.port, self.app_factory())
            self._server_thread = threading.Thread(
                target=self._server.serve_forever,
                name="flask-app-server",
                daemon=True,
            )
            self._server_thread.start()
        logger.info("Flask app service started: http://%s:%s.", self.host, self.port)

    def shutdown(self) -> None:
        with self._lock:
            server = self._server
            server_thread = self._server_thread

        if server is None:
            return

        if server_thread and server_thread.is_alive():
            server.shutdown()
            if threading.current_thread() is not server_thread:
                server_thread.join(timeout=self.shutdown_timeout_seconds)
        server.server_close()

        with self._lock:
            if self._server is server:
                self._server = None
                self._server_thread = None
        logger.info("Flask app service stopped.")


if __name__ == "__main__":
    service = FlaskAppService(port=5002)
    try:
        service.start()
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        service.shutdown()
