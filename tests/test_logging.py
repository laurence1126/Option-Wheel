import logging
import unittest
from unittest.mock import patch

from app.utils import logging as logging_utils


class LoggingUtilsTest(unittest.TestCase):
    def tearDown(self) -> None:
        logger = logging.getLogger("tests.logging.default")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()

    def test_configure_logger_appends_to_daily_log_by_default(self):
        with (
            patch("app.utils.logging._file_logging_disabled", return_value=False),
            patch("app.utils.logging.DailyFileHandler") as daily_handler,
        ):
            logger = logging_utils.configure_logger("tests.logging.default")

        self.assertIs(logger, logging.getLogger("tests.logging.default"))
        self.assertFalse(daily_handler.call_args.kwargs["override"])

