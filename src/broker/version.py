"""Package version shared by the CLI and distribution metadata."""

__version__ = "0.1.17"


def warn_version_mismatch(logger, broker_version, executor_version):
    if broker_version and executor_version and broker_version == executor_version:
        return
    logger.warning(
        "Broker/executor version mismatch or unknown version: broker=%s executor=%s. "
        "Update both Python environments and restart broker and executor: "
        "python -m pip install --upgrade git+https://github.com/hustzxd/broker.git",
        broker_version or "unknown", executor_version or "unknown",
    )
