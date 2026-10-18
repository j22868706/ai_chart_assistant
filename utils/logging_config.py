"""Keep HTTP/SDK diagnostics out of application logs.

Those diagnostics can contain credential-bearing URLs and request/response bodies.
Application failures are instead logged as fixed event names without exceptions.
"""

import logging


def configure_safe_logging() -> None:
    logging.basicConfig(level=logging.INFO)
    # Apply after SDK imports too: GROQ_LOG may explicitly enable SDK diagnostics.
    namespaces = ("httpx", "httpcore", "groq")
    names = set(logging.root.manager.loggerDict) | set(namespaces)
    for name in names:
        if any(name == prefix or name.startswith(prefix + ".") for prefix in namespaces):
            logger = logging.getLogger(name)
            logger.handlers.clear()
            logger.addHandler(logging.NullHandler())
            logger.propagate = False
            logger.setLevel(logging.CRITICAL + 1)
