from __future__ import annotations

import json
import os
import threading
import traceback
import uuid
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .asset_tags import apply_asset_tags
from .crawler import crawl_default_sources, enrich_notice_detail
from .llm_analysis import review_asset_readability_with_glm, structure_notice_detail_with_llm
from .models import AssetRecord, RawNotice
from .rules import extract_city_from_text
from .sources import DEFAULT_SOURCES
from .storage import JsonAssetStore
from .tracing import flush_traces
from .workflow import run_asset_radar_workflow


ROOT = Path(__file__).resolve().parents[2]
STORE_PATH = ROOT / "data" / "asset_pool.local.json"
LAST_SCAN_PATH = ROOT / "data" / "last_scan.local.json"
SERVER_STARTED_AT = datetime.utcnow().isoformat()
RUNS: dict[str, dict] = {}
RUNS_LOCK = threading.Lock()
SOURCE_IDS = {source.source_id for source in DEFAULT_SOURCES}


def _ensure_langsmith_env() -> None:
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_PROJECT", "asset radar")
    key_file = ROOT / "langsmith api.txt"
    if not os.getenv("LANGSMITH_API_KEY") and key_file.exists():
        os.environ["LANGSMITH_API_KEY"] = key_file.read_text(encoding="utf-8").strip()


def _record_to_api(record: AssetRecord) -> dict:
    score = record.screening_score.total_score if record.screening_score else None
    status = "扫描" if record.pool == "scan" else "可追踪" if record.pool == "trackable" else "人工复核" if record.pool == "review" else "初筛"
    attachments = record.attachment_summaries or []
    city = record.city or extract_city_from_text(record.title, record.raw_text, record.detail_text)
    return {
        "asset_id": record.asset_id,
        "name": record.title,
        "source": record.source_platform or record.disposal_agency or "-",
        "city": city or "-",
        "collateral": (record.collateral_detail or record.extracted_summary or record.raw_text or record.title)[:220],
        "summary": record.extracted_summary,
        "detail_text": record.detail_text or record.raw_text,
        "detail_sections": record.detail_sections or [],
        "tags": record.asset_tags or [],
        "attachments": attachments,
        "status": status,
        "score": f"{score} / {status}" if score is not None else status,
        "watched": record.interested,
        "announcement_count": record.announcement_count,
        "auction_count": record.auction_count,
        "latest_notice_date": record.latest_notice_date.isoformat() if record.latest_notice_date else "",
        "source_url": record.source_url,
        "original_url": record.source_url,
    }


def _load_last_scan() -> dict:
    if not LAST_SCAN_PATH.exists():
        return {}
    try:
        return json.loads(LAST_SCAN_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}


def _save_last_scan(raw_count: int, records: list[AssetRecord], new_count: int = 0, cumulative_count: int | None = None) -> None:
    LAST_SCAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    total = len(records) if cumulative_count is None else cumulative_count
    data = {
        "raw_count": raw_count,
        "scanned_at": datetime.utcnow().isoformat(),
        "new_count": new_count,
        "cumulative_count": total,
        "scan": sum(1 for record in records if record.pool == "scan"),
        "initial": sum(1 for record in records if record.pool == "initial"),
        "trackable": sum(1 for record in records if record.pool == "trackable"),
        "review": sum(1 for record in records if record.pool == "review"),
    }
    LAST_SCAN_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _payload(records: list[AssetRecord], alerts: list[str] | None = None, raw_count: int | None = None) -> dict:
    pools = {"scan": [], "initial": [], "trackable": [], "review": []}
    for record in records:
        if record.pool in pools:
            pools[record.pool].append(_record_to_api(record))
    last_scan = _load_last_scan()
    if raw_count is None:
        raw_count = int(last_scan.get("raw_count") or 0)
    new_count = int(last_scan.get("new_count") or 0)
    cumulative_count = int(last_scan.get("cumulative_count") or len(records))
    return {
        "kpis": {
            "raw": raw_count,
            "new": new_count,
            "cumulative": cumulative_count,
            "scan": len(pools["scan"]),
            "initial": len(pools["initial"]),
            "trackable": len(pools["trackable"]),
            "review": len(pools["review"]),
            "alerts": len(alerts or []),
        },
        "pools": pools,
        "alerts": alerts or [],
    }


def _ensure_record_detail_sections(record: AssetRecord) -> AssetRecord:
    source_text = f"{record.source_platform} {record.source_url} {record.detail_text}"
    is_coamc = "东方资产" in source_text or "sales.coamc.com.cn" in source_text
    has_readable_sections = bool(
        record.detail_sections
        and any((section.get("title") and section.get("content")) for section in record.detail_sections)
    )
    has_bad_detail_text = (
        "返回中国东方营销网站首页" in source_text
        or "Document " in source_text[:200]
        or "The handshake operation timed out" in source_text
    )
    needs_refresh = (
        not has_readable_sections
        or has_bad_detail_text
    )
    if not needs_refresh:
        return record
    notice = RawNotice(
        source_platform=record.source_platform,
        source_url=record.source_url,
        title=record.title,
        notice_date=record.latest_notice_date,
        raw_text=record.raw_text or record.detail_text or record.title,
        debtor=record.debtor,
        disposal_agency=record.disposal_agency,
        city=record.city,
        district=record.district,
        amount_text=record.amount_text,
        detail_text=record.detail_text,
        attachments=record.attachment_summaries,
        metadata={"source_id": "coamc_orient_disposal" if is_coamc else ""},
    )
    if is_coamc or not notice.detail_text:
        notice.detail_text = ""
        notice = enrich_notice_detail(
            notice,
            include_attachments=False,
            detail_limit=16000,
        )
        record.detail_text = notice.detail_text or record.detail_text
        record.raw_text = notice.raw_text or record.raw_text
    record.detail_sections = structure_notice_detail_with_llm(
        notice,
        {
            "summary": record.extracted_summary,
            "collateral_detail": record.collateral_detail,
            "amount_text": record.amount_text,
        },
    )
    record.node_models["detail_readability_node"] = "deepseek/deepseek-v4-pro"
    review = review_asset_readability_with_glm(record)
    record.node_models["asset_readability_reviewer_node"] = str(review.get("model_name") or "z-ai/glm-5.2")
    record.notes.append(
        f"{datetime.utcnow().isoformat()} detail_endpoint_readability_review "
        f"passed={review.get('passed')} score={review.get('cleanliness_score')} "
        f"reasons={' | '.join(review.get('reasons') or [])}"
    )
    if not review.get("passed"):
        notice.detail_text = ""
        notice = enrich_notice_detail(
            notice,
            include_attachments=False,
            detail_limit=16000,
        )
        record.detail_text = notice.detail_text or record.detail_text
        record.raw_text = notice.raw_text or record.raw_text
        record.detail_sections = structure_notice_detail_with_llm(
            notice,
            {
                "summary": record.extracted_summary,
                "collateral_detail": record.collateral_detail,
                "amount_text": record.amount_text,
            },
        )
        second_review = review_asset_readability_with_glm(record)
        record.notes.append(
            f"{datetime.utcnow().isoformat()} detail_endpoint_readability_retry "
            f"passed={second_review.get('passed')} score={second_review.get('cleanliness_score')} "
            f"reasons={' | '.join(second_review.get('reasons') or [])}"
        )
    apply_asset_tags(record)
    if is_coamc and not any("coamc_detail_refreshed_v3" in note for note in record.notes):
        record.notes.append(f"{datetime.utcnow().isoformat()} coamc_detail_refreshed_v3")
    return record


def _append_progress(run_id: str, stage: str, message: str, data: dict | None = None) -> None:
    event = {
        "time": datetime.utcnow().isoformat(),
        "stage": stage,
        "message": message,
        "data": data or {},
    }
    with RUNS_LOCK:
        run = RUNS.setdefault(run_id, {"status": "running", "events": []})
        run.setdefault("events", []).append(event)
        run["events"] = run["events"][-400:]
        if stage == "notice":
            run["scanned_count"] = int(run.get("scanned_count") or 0) + 1
        elif stage in {"crawl_done", "cancelled"} and data and data.get("count") is not None:
            run["scanned_count"] = int(data.get("count") or 0)
            if stage == "crawl_done":
                discovered_count = int(data.get("count") or 0)
                if discovered_count >= 0:
                    run["target_count"] = discovered_count
        elif stage == "done":
            scanned_count = int(run.get("scanned_count") or 0)
            run["target_count"] = scanned_count
        target_count = int(run.get("target_count") or 0)
        scanned_count = int(run.get("scanned_count") or 0)
        if stage == "done":
            run["progress_percent"] = 100
        elif target_count > 0:
            run["progress_percent"] = min(100, round(scanned_count / target_count * 100))
        else:
            run["progress_percent"] = 0


def _set_run(run_id: str, **updates) -> None:
    with RUNS_LOCK:
        run = RUNS.setdefault(run_id, {"status": "running", "events": []})
        run.update(updates)


def _request_stop(run_id: str, mode: str) -> dict:
    mode = "commit" if mode == "commit" else "discard"
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        if not run:
            return {"run_id": run_id, "status": "missing", "events": []}
        if run.get("status") in {"done", "error", "cancelled_committed", "cancelled_discarded"}:
            return json.loads(json.dumps({"run_id": run_id, **run}, ensure_ascii=False))
        run["stop_requested"] = True
        run["stop_mode"] = mode
        run["status"] = "stopping"
    _append_progress(
        run_id,
        "stop_requested",
        "已收到停止请求，将已扫描数据入库" if mode == "commit" else "已收到停止请求，将删除本轮已扫描内容",
        {"mode": mode},
    )
    return _get_run(run_id)


def _stop_requested(run_id: str) -> bool:
    with RUNS_LOCK:
        return bool(RUNS.get(run_id, {}).get("stop_requested"))


def _stop_mode(run_id: str) -> str:
    with RUNS_LOCK:
        return str(RUNS.get(run_id, {}).get("stop_mode") or "discard")


def _run_config(run_id: str) -> dict:
    with RUNS_LOCK:
        config = RUNS.get(run_id, {}).get("config") or {}
        return json.loads(json.dumps(config, ensure_ascii=False))


def _get_run(run_id: str) -> dict:
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        if not run:
            return {"run_id": run_id, "status": "missing", "events": []}
        return json.loads(json.dumps({"run_id": run_id, **run}, ensure_ascii=False))


def _run_scan_job(run_id: str) -> None:
    def progress(stage: str, message: str, data: dict | None = None) -> None:
        _append_progress(run_id, stage, message, data)

    def should_stop() -> bool:
        return _stop_requested(run_id)

    def save_on_stop() -> bool:
        return _stop_mode(run_id) == "commit"

    try:
        _ensure_langsmith_env()
        config = _run_config(run_id)
        limit_total = int(config.get("limit_total") or config.get("limit_per_source") or 120)
        source_ids = [source_id for source_id in config.get("source_ids", []) if source_id in SOURCE_IDS]
        source_labels = [
            source.name for source in DEFAULT_SOURCES
            if not source_ids or source.source_id in source_ids
        ]
        _set_run(
            run_id,
            status="running",
            started_at=datetime.utcnow().isoformat(),
            target_count=limit_total,
            scanned_count=0,
            progress_percent=0,
        )
        progress("start", "启动资产雷达扫描", {
            "run_id": run_id,
            "sources": source_labels,
            "limit_total": limit_total,
        })
        before_ids = set(JsonAssetStore(STORE_PATH).load().keys())
        notices = crawl_default_sources(
            limit_per_source=limit_total,
            progress=progress,
            should_stop=should_stop,
            source_ids=source_ids,
            total_limit=limit_total,
        )
        stopped_after_crawl = should_stop()
        if stopped_after_crawl and _stop_mode(run_id) == "discard":
            records = list(JsonAssetStore(STORE_PATH).load().values())
            payload = _payload(records)
            progress("cancelled", "扫描已停止，本轮已扫描内容已删除", {"count": len(notices)})
            _set_run(
                run_id,
                status="cancelled_discarded",
                finished_at=datetime.utcnow().isoformat(),
                payload=payload,
            )
            return
        progress("crawl_done", f"基础扫描完成，共发现 {len(notices)} 条线索", {"count": len(notices)})
        if stopped_after_crawl:
            progress("commit_partial", "开始将已扫描线索入库", {"count": len(notices)})
        result = run_asset_radar_workflow(
            notices,
            store_path=STORE_PATH,
            dry_run=False,
            progress=progress,
            should_stop=None if stopped_after_crawl else should_stop,
            save_on_stop=save_on_stop,
        )
        flush_traces()
        if should_stop() and _stop_mode(run_id) == "discard":
            records = list(JsonAssetStore(STORE_PATH).load().values())
            payload = _payload(records)
            progress("cancelled", "扫描已停止，本轮工作流内容未入库", {"count": len(notices)})
            _set_run(
                run_id,
                status="cancelled_discarded",
                finished_at=datetime.utcnow().isoformat(),
                payload=payload,
            )
            return
        records = list(JsonAssetStore(STORE_PATH).load().values())
        after_ids = {record.asset_id for record in records}
        new_count = len(after_ids - before_ids)
        cumulative_count = len(after_ids)
        _save_last_scan(result.raw_count, records, new_count=new_count, cumulative_count=cumulative_count)
        payload = _payload(records, alerts=result.alerts, raw_count=result.raw_count)
        if should_stop():
            progress("cancelled", "扫描已停止，已扫描数据已入库", payload.get("kpis"))
            _set_run(run_id, status="cancelled_committed", finished_at=datetime.utcnow().isoformat(), payload=payload)
        else:
            progress("done", "扫描完成，资产池已更新", payload.get("kpis"))
            _set_run(run_id, status="done", finished_at=datetime.utcnow().isoformat(), payload=payload)
    except Exception as exc:
        progress("error", f"扫描失败：{exc}", {"trace": traceback.format_exc()[-2000:]})
        _set_run(run_id, status="error", finished_at=datetime.utcnow().isoformat(), error=str(exc))


class RadarHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def _parse_run_config(self) -> tuple[dict, str | None]:
        body = self._read_json()
        raw_limit = body.get("limit_total", body.get("limit_per_source", body.get("limit", 120)))
        try:
            limit_total = int(raw_limit)
        except (TypeError, ValueError):
            return {}, "扫描资产条数只能输入数字"
        if limit_total <= 0:
            return {}, "扫描资产条数必须大于 0"
        limit_total = min(limit_total, 500)

        raw_sources = body.get("source_ids") or body.get("sources") or []
        if isinstance(raw_sources, str):
            raw_sources = [raw_sources]
        source_ids = [str(source_id) for source_id in raw_sources if str(source_id) in SOURCE_IDS]
        if not source_ids:
            source_ids = []
        return {
            "limit_total": limit_total,
            "source_ids": source_ids,
        }, None

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/radar/health":
            self._send_json({
                "ok": True,
                "pid": os.getpid(),
                "root": str(ROOT),
                "started_at": SERVER_STARTED_AT,
                "source_ids": sorted(SOURCE_IDS),
                "source_count": len(SOURCE_IDS),
            })
            return
        if parsed.path == "/api/radar/assets":
            store = JsonAssetStore(STORE_PATH)
            records = list(store.load().values())
            self._send_json(_payload(records))
            return
        if parsed.path == "/api/radar/progress":
            query = parse_qs(parsed.query)
            run_id = (query.get("run_id") or [""])[0]
            if not run_id:
                with RUNS_LOCK:
                    run_id = next(reversed(RUNS), "")
            self._send_json(_get_run(run_id) if run_id else {"status": "missing", "events": []})
            return
        if parsed.path == "/api/radar/asset-detail":
            query = parse_qs(parsed.query)
            asset_id = (query.get("asset_id") or [""])[0]
            if not asset_id:
                self._send_json({"error": "missing asset_id"}, status=400)
                return
            store = JsonAssetStore(STORE_PATH)
            records = store.load()
            record = records.get(asset_id)
            if not record:
                self._send_json({"error": "asset not found"}, status=404)
                return
            record = _ensure_record_detail_sections(record)
            records[asset_id] = record
            store.save(records)
            self._send_json(_record_to_api(record))
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/radar/clear":
            JsonAssetStore(STORE_PATH).save({})
            _save_last_scan(0, [], new_count=0, cumulative_count=0)
            self._send_json(_payload([]))
            return
        if parsed.path == "/api/radar/run":
            config, error = self._parse_run_config()
            if error:
                self._send_json({"error": error}, status=400)
                return
            run_id = uuid.uuid4().hex
            _set_run(run_id, status="queued", created_at=datetime.utcnow().isoformat(), events=[], config=config)
            self._send_json({"run_id": run_id, "status": "queued", "config": config})
            timer = threading.Timer(0.05, _run_scan_job, args=(run_id,))
            timer.daemon = True
            timer.start()
            return
        if parsed.path == "/api/radar/stop":
            body = self._read_json()
            query = parse_qs(parsed.query)
            run_id = str(body.get("run_id") or (query.get("run_id") or [""])[0])
            mode = str(body.get("mode") or (query.get("mode") or ["discard"])[0])
            if not run_id:
                self._send_json({"error": "missing run_id"}, status=400)
                return
            stopped = _request_stop(run_id, mode)
            status = 404 if stopped.get("status") == "missing" else 200
            self._send_json(stopped, status=status)
            return
        self._send_json({"error": "not found"}, status=404)


def main() -> None:
    os.chdir(ROOT)
    host = os.getenv("ASSET_RADAR_HOST", "127.0.0.1")
    port = int(os.getenv("ASSET_RADAR_PORT", "8030"))
    server = ThreadingHTTPServer((host, port), RadarHandler)
    try:
        print(f"Asset Radar API serving http://{host}:{port}")
    except Exception:
        pass
    server.serve_forever()


if __name__ == "__main__":
    main()
