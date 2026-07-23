import logging
import re


class SecretRedactionFilter(logging.Filter):
    _pattern = re.compile(r"(?i)(api[_-]?key|cookie|password|authorization)(\s*[:=]\s*)([^\s,;]+)")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = self._pattern.sub(r"\1\2***", message)
        record.args = ()
        return True


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactionFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
