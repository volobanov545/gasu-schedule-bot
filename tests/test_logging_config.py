from __future__ import annotations

import json
import logging

from szs_hub.logging_config import SafeJsonFormatter


def test_safe_json_formatter_is_structured_and_omits_exception_text() -> None:
    try:
        raise RuntimeError("private-message-body")
    except RuntimeError:
        record = logging.LogRecord(
            "szs_hub.test",
            logging.ERROR,
            __file__,
            10,
            "operation failed",
            (),
            exc_info=__import__("sys").exc_info(),
        )

    payload = json.loads(SafeJsonFormatter().format(record))
    assert payload["level"] == "error"
    assert payload["event"] == "operation failed"
    assert payload["exception_type"] == "RuntimeError"
    assert "private-message-body" not in json.dumps(payload)
