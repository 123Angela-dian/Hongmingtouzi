from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from logger_config import get_logger


load_dotenv(override=True)
logger = get_logger("ocr")


@dataclass(frozen=True)
class OCRResult:
    job_id: str
    markdown: str
    pages: int
    json_url: str


class PaddleOCRError(RuntimeError):
    pass


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _request(method: str, url: str, **kwargs) -> requests.Response:
    session = requests.Session()
    session.trust_env = _bool_env("PADDLEOCR_TRUST_ENV", False)
    return session.request(method, url, **kwargs)


def _paddle_config() -> tuple[str, str, str, dict[str, bool]]:
    job_url = os.getenv("PADDLEOCR_JOB_URL") or os.getenv("EYES_BASE_URL")
    token = os.getenv("PADDLEOCR_TOKEN") or os.getenv("EYES_API_KEY")
    model = os.getenv("PADDLEOCR_MODEL") or os.getenv("EYES_MODEL") or "PaddleOCR-VL-1.6"
    if not job_url:
        raise PaddleOCRError("PADDLEOCR_JOB_URL is not configured.")
    if not token:
        raise PaddleOCRError("PADDLEOCR_TOKEN is not configured.")
    optional_payload = {
        "useDocOrientationClassify": _bool_env("PADDLEOCR_USE_DOC_ORIENTATION_CLASSIFY"),
        "useDocUnwarping": _bool_env("PADDLEOCR_USE_DOC_UNWARPING"),
        "useChartRecognition": _bool_env("PADDLEOCR_USE_CHART_RECOGNITION"),
    }
    return job_url, token, model, optional_payload


def submit_paddleocr_job(file_path: str | Path) -> str:
    job_url, token, model, optional_payload = _paddle_config()
    headers = {"Authorization": f"bearer {token}"}
    file_path = Path(file_path)
    if not file_path.exists():
        raise PaddleOCRError(f"File not found: {file_path}")

    logger.info("Submitting PaddleOCR job | file=%s | model=%s", file_path.name, model)
    data = {
        "model": model,
        "optionalPayload": json.dumps(optional_payload),
    }
    max_retries = int(os.getenv("PADDLEOCR_SUBMIT_RETRIES", "3"))
    retry_seconds = int(os.getenv("PADDLEOCR_RETRY_SECONDS", "20"))
    response: requests.Response | None = None
    for attempt in range(1, max_retries + 1):
        with file_path.open("rb") as file_obj:
            response = _request(
                "POST",
                job_url,
                headers=headers,
                data=data,
                files={"file": file_obj},
                timeout=120,
            )
        if response.status_code == 200:
            break
        body = response.text[:500]
        logger.warning(
            "PaddleOCR submit attempt failed | file=%s | attempt=%s/%s | status=%s | body=%s",
            file_path.name,
            attempt,
            max_retries,
            response.status_code,
            body,
        )
        if response.status_code == 400 and ("队列已满" in body or "10010" in body) and attempt < max_retries:
            time.sleep(retry_seconds)
            continue
        logger.error("PaddleOCR submit failed | status=%s | body=%s", response.status_code, body)
        raise PaddleOCRError(f"PaddleOCR submit failed: {response.status_code} {body}")

    if response is None or response.status_code != 200:
        raise PaddleOCRError("PaddleOCR submit failed without response.")

    payload = response.json()
    try:
        job_id = payload["data"]["jobId"]
        logger.info("PaddleOCR job submitted | file=%s | job_id=%s", file_path.name, job_id)
        return job_id
    except KeyError as exc:
        logger.exception("PaddleOCR submit response missing jobId")
        raise PaddleOCRError(f"PaddleOCR submit response missing jobId: {payload}") from exc


def poll_paddleocr_result(job_id: str, timeout_seconds: int = 600, interval_seconds: int = 5) -> tuple[str, int]:
    job_url, token, _, _ = _paddle_config()
    headers = {"Authorization": f"bearer {token}"}
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        response = _request("GET", f"{job_url}/{job_id}", headers=headers, timeout=60)
        if response.status_code != 200:
            raise PaddleOCRError(f"PaddleOCR poll failed: {response.status_code} {response.text[:500]}")

        payload: dict[str, Any] = response.json()
        data = payload.get("data", {})
        state = data.get("state")
        logger.info("PaddleOCR polling | job_id=%s | state=%s", job_id, state)
        if state == "done":
            progress = data.get("extractProgress", {})
            result_url = data.get("resultUrl", {})
            json_url = result_url.get("jsonUrl")
            if not json_url:
                logger.error("PaddleOCR result missing jsonUrl | job_id=%s | payload=%s", job_id, payload)
                raise PaddleOCRError(f"PaddleOCR result missing jsonUrl: {payload}")
            logger.info("PaddleOCR job done | job_id=%s | pages=%s", job_id, progress.get("extractedPages"))
            return json_url, int(progress.get("extractedPages") or 0)
        if state == "failed":
            logger.error("PaddleOCR job failed | job_id=%s | error=%s", job_id, data.get("errorMsg"))
            raise PaddleOCRError(data.get("errorMsg") or "PaddleOCR job failed.")
        if state not in {"pending", "running"}:
            raise PaddleOCRError(f"Unknown PaddleOCR job state: {state}")
        time.sleep(interval_seconds)

    raise PaddleOCRError(f"PaddleOCR job timed out after {timeout_seconds} seconds.")


def download_paddleocr_markdown(json_url: str) -> str:
    logger.info("Downloading PaddleOCR markdown jsonl")
    response = _request("GET", json_url, timeout=120)
    response.raise_for_status()
    chunks: list[str] = []

    for line in response.text.strip().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        result = row.get("result", {})
        for item in result.get("layoutParsingResults", []):
            text = item.get("markdown", {}).get("text", "")
            if text.strip():
                chunks.append(text.strip())

    markdown = "\n\n---\n\n".join(chunks).strip()
    if not markdown:
        logger.error("PaddleOCR result contained no markdown text")
        raise PaddleOCRError("PaddleOCR result contained no markdown text.")
    logger.info("PaddleOCR markdown extracted | chars=%s", len(markdown))
    return markdown


def run_paddleocr(file_path: str | Path) -> OCRResult:
    logger.info("PaddleOCR run started | file=%s", Path(file_path).name)
    job_id = submit_paddleocr_job(file_path)
    json_url, pages = poll_paddleocr_result(job_id)
    markdown = download_paddleocr_markdown(json_url)
    logger.info("PaddleOCR run finished | file=%s | job_id=%s | pages=%s", Path(file_path).name, job_id, pages)
    return OCRResult(job_id=job_id, markdown=markdown, pages=pages, json_url=json_url)
