"""RiskModel adapter for the MIRAI HTTP API (same multipart format as pipeline._submit_one_exam_to_mirai)."""
import json
from contextlib import ExitStack
from pathlib import Path

import requests

from ..domain import SCORE_YEARS, PermanentError, TransientError

CONNECT_TIMEOUT_SECONDS = 120
READ_TIMEOUT_SECONDS = 600
EXAM_ID = "ingest"


class MiraiClient:
    def __init__(self, url: str, post=requests.post):
        self._url = url
        self._post = post

    def score(self, dicom_paths: list) -> dict:
        """POST the 4 views; returns {"Year 1": p, ..., "Year 5": p}.

        Raises TransientError for timeouts, connection errors and 5xx; PermanentError for 4xx or a
        response without predictions. Messages are built here so no file path leaks into them.
        """
        try:
            with ExitStack() as stack:
                files = [
                    ("dicom", (Path(p).name, stack.enter_context(open(p, "rb")), "application/dicom"))
                    for p in dicom_paths
                ]
                response = self._post(
                    self._url,
                    data={"metadata": json.dumps({"exam_id": EXAM_ID})},
                    files=files,
                    timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                )
                response.raise_for_status()
                payload = response.json()
        except requests.Timeout:
            raise TransientError("MIRAI request timed out") from None
        except requests.ConnectionError:
            raise TransientError("MIRAI connection error") from None
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status is not None and status >= 500:
                raise TransientError(f"MIRAI HTTP {status}") from None
            raise PermanentError(f"MIRAI HTTP {status}") from None
        except ValueError:
            raise PermanentError("MIRAI response is not valid JSON") from None

        predictions = ((payload or {}).get("data") or {}).get("predictions") or {}
        if not all(predictions.get(year) is not None for year in SCORE_YEARS):
            raise PermanentError("MIRAI response has no predictions")
        return {year: float(predictions[year]) for year in SCORE_YEARS}
