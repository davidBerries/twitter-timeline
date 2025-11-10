import json
import logging
from pathlib import Path
from typing import Iterable, List, Dict, Any

log = logging.getLogger("exporters")

class Exporter:
    """
    Writes timeline items to JSON (array) or NDJSON.
    """

    def __init__(self, output_path: Path, ndjson: bool = False) -> None:
        self.output_path = Path(output_path)
        self.ndjson = ndjson
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, items: List[Dict[str, Any]]) -> None:
        if self.ndjson:
            self._write_ndjson(items)
        else:
            self._write_json(items)

    def _write_json(self, items: List[Dict[str, Any]]) -> None:
        self.output_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("JSON written to %s", self.output_path)

    def _write_ndjson(self, items: Iterable[Dict[str, Any]]) -> None:
        with self.output_path.open("w", encoding="utf-8") as f:
            for obj in items:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        log.info("NDJSON written to %s", self.output_path)