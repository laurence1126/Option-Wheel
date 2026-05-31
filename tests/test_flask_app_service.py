import unittest
from unittest import mock

from app.flask_app import FlaskAppService


class FlaskAppServiceTest(unittest.TestCase):
    def test_start_creates_internal_server_thread_once(self) -> None:
        app = mock.Mock()
        app_factory = mock.Mock(return_value=app)
        server = mock.Mock()
        server_thread = mock.Mock()
        server_thread.is_alive.return_value = True
        service = FlaskAppService(host="127.0.0.1", port=5002, app_factory=app_factory)

        with (
            mock.patch("app.flask_app.make_server", return_value=server) as make_server,
            mock.patch("app.flask_app.threading.Thread", return_value=server_thread) as thread,
        ):
            service.start()
            service.start()

        make_server.assert_called_once_with("127.0.0.1", 5002, app)
        thread.assert_called_once_with(target=server.serve_forever, name="flask-app-server", daemon=True)
        server_thread.start.assert_called_once_with()

    def test_shutdown_stops_server_and_joins_internal_thread(self) -> None:
        server = mock.Mock()
        server_thread = mock.Mock()
        server_thread.is_alive.return_value = True
        service = FlaskAppService(host="127.0.0.1", port=5002, shutdown_timeout_seconds=3)
        service._server = server
        service._server_thread = server_thread

        service.shutdown()

        server.shutdown.assert_called_once_with()
        server_thread.join.assert_called_once_with(timeout=3)
        server.server_close.assert_called_once_with()
        self.assertIsNone(service._server)
        self.assertIsNone(service._server_thread)

    def test_shutdown_is_safe_before_start(self) -> None:
        service = FlaskAppService(host="127.0.0.1", port=5002)

        service.shutdown()

        self.assertIsNone(service._server)
        self.assertIsNone(service._server_thread)


if __name__ == "__main__":
    unittest.main()
