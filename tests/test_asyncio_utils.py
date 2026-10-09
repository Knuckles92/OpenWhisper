"""The embedded servers must not log a peer's connection reset as an error."""
import asyncio
import logging

from services.asyncio_utils import quiet_connection_lost


def _run_callback(callback):
    loop = asyncio.new_event_loop()
    quiet_connection_lost(loop)
    try:
        loop.call_soon(callback)
        loop.call_soon(loop.stop)
        loop.run_forever()
    finally:
        loop.close()


def test_reset_while_closing_a_connection_is_not_an_error(caplog):
    class _Transport:
        def _call_connection_lost(self):
            raise ConnectionResetError(10054, "forcibly closed by the remote host")

    transport = _Transport()
    with caplog.at_level(logging.DEBUG):
        _run_callback(transport._call_connection_lost)

    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("reset the connection" in r.getMessage() for r in caplog.records)


def test_other_callback_errors_still_log_as_errors(caplog):
    def broken():
        raise ValueError("a real bug")

    with caplog.at_level(logging.DEBUG):
        _run_callback(broken)

    assert any(
        r.levelno >= logging.ERROR and r.name == "asyncio"
        for r in caplog.records
    )


def test_a_reset_from_another_callback_still_logs_as_an_error(caplog):
    def reset_elsewhere():
        raise ConnectionResetError(10054, "forcibly closed by the remote host")

    with caplog.at_level(logging.DEBUG):
        _run_callback(reset_elsewhere)

    assert any(r.levelno >= logging.ERROR for r in caplog.records)
