"""Tests for gateway.main._setup_logging."""
import logging
from logging.handlers import RotatingFileHandler

import pytest
import yaml

from gateway.main import _setup_logging
from gateway.models.config import (
    GatewayConfig,
    IdentityConfig,
    LoggingConfig,
    ProvidersConfig,
    ServerConfig,
    load_config,
)


def _make_config(**logging_kwargs) -> GatewayConfig:
    return GatewayConfig(
        server=ServerConfig(),
        identity=IdentityConfig(),
        providers=ProvidersConfig(),
        logging=LoggingConfig(**logging_kwargs),
    )


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """_setup_logging mutates the root and uvicorn.access loggers globally; restore after each test."""
    root = logging.getLogger()
    root_handlers = list(root.handlers)
    root_level = root.level
    access_logger = logging.getLogger("uvicorn.access")
    access_handlers = list(access_logger.handlers)
    access_propagate = access_logger.propagate
    yield
    root.handlers.clear()
    root.handlers.extend(root_handlers)
    root.setLevel(root_level)
    access_logger.handlers.clear()
    access_logger.handlers.extend(access_handlers)
    access_logger.propagate = access_propagate


class TestSetupLoggingDirUnset:
    def test_no_file_handler_on_root(self):
        config = _make_config()
        _setup_logging(config)
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], logging.StreamHandler)
        assert not isinstance(root.handlers[0], RotatingFileHandler)

    def test_uvicorn_access_null_handler(self):
        config = _make_config()
        _setup_logging(config)
        access_logger = logging.getLogger("uvicorn.access")
        assert access_logger.propagate is False
        assert len(access_logger.handlers) == 1
        assert isinstance(access_logger.handlers[0], logging.NullHandler)


class TestSetupLoggingDirSet:
    def test_creates_dir_and_gateway_log_handler(self, tmp_path):
        log_dir = tmp_path / "logs"
        config = _make_config(dir=str(log_dir))
        _setup_logging(config)

        assert log_dir.is_dir()
        root = logging.getLogger()
        assert len(root.handlers) == 2
        stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler)]
        file_handlers = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(stream_handlers) == 1
        assert len(file_handlers) == 1
        assert file_handlers[0].baseFilename == str(log_dir / "gateway.log")

    def test_formatter_matches_p_ork(self, tmp_path):
        config = _make_config(dir=str(tmp_path / "logs"))
        _setup_logging(config)

        root = logging.getLogger()
        file_handler = next(h for h in root.handlers if isinstance(h, RotatingFileHandler))
        assert file_handler.formatter._fmt == "%(asctime)s %(levelname)s %(name)s %(message)s"
        assert file_handler.formatter.datefmt == "%Y-%m-%dT%H:%M:%S"

    def test_uvicorn_access_routed_to_own_file(self, tmp_path):
        log_dir = tmp_path / "logs"
        config = _make_config(dir=str(log_dir))
        _setup_logging(config)

        access_logger = logging.getLogger("uvicorn.access")
        assert access_logger.propagate is False
        assert len(access_logger.handlers) == 1
        access_handler = access_logger.handlers[0]
        assert isinstance(access_handler, RotatingFileHandler)
        assert access_handler.baseFilename == str(log_dir / "access.log")

        root = logging.getLogger()
        gateway_handler = next(h for h in root.handlers if isinstance(h, RotatingFileHandler))
        assert access_handler is not gateway_handler


class _CollectingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class TestSetupLoggingLevel:
    def test_level_filters_below_configured(self):
        # _setup_logging clears root's handlers, so attach our capture handler
        # after calling it rather than relying on caplog's pre-existing one.
        config = _make_config()
        config.logging.level = "WARNING"
        _setup_logging(config)

        collector = _CollectingHandler()
        logging.getLogger().addHandler(collector)

        logger = logging.getLogger("gateway.something")
        logger.info("should be suppressed")
        logger.warning("should be emitted")

        messages = [r.getMessage() for r in collector.records]
        assert "should be suppressed" not in messages
        assert "should be emitted" in messages


class TestLoggingConfigModel:
    def test_dir_defaults_empty(self):
        assert LoggingConfig().dir == ""

    def test_dir_round_trips_through_load_config(self, tmp_path):
        config_data = {
            "server": {"host": "0.0.0.0", "port": 18780},
            "identity": {"path": str(tmp_path / "identity")},
            "providers": {"anthropic": {"api_key": "test-key"}},
            "logging": {"level": "INFO", "dir": "/tmp/foo"},
        }
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml.dump(config_data))

        cfg = load_config(str(config_file))
        assert cfg.logging.dir == "/tmp/foo"
