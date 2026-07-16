from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .llm_analysis import classify_source_category_with_glm
from .models import RawNotice
from .rules import extract_city_from_text
from .sources import DEFAULT_SOURCES, SourceSpec


NOTICE_KEYWORDS = ("资产", "债权", "处置", "转让", "拍卖", "招商", "催收", "公告", "竞价", "变卖", "司法", "不动产", "房产", "债务")
NOISE_TITLES = {
    "资产", "债权", "公告", "资产处置公告", "资产专区", "公司公告", "信达资产", "不良资产经营", "资产管理和投资",
    "司法拍卖竞拍流程", "司法拍卖出价规则", "入驻司法拍卖", "模拟体验司法拍卖", "自行处置",
}
NOISE_TERMS = ("规则", "流程", "规定", "名单库", "模拟体验", "热门资产", "好资产", "阅读公告", "入驻")
SUPPORTED_SOURCE_TYPES = {"amc_notice", "judicial_auction"}
ATTACHMENT_EXTS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar")
HTML_EXTS = (".html", ".htm", ".shtml", ".asp", ".aspx", ".php")
DOWNLOAD_PATH_HINTS = ("download", "file", "upload", "attachment", "attach", "annex", "appendix", "fujian")
ATTACHMENT_LABEL_HINTS = ("附件", "下载", "清单", "明细", "照片", "债权清单", "资产清单", "评估报告", "竞买须知", "调查表")
COAMC_BASE_URL = "https://sales.coamc.com.cn"
COAMC_NOTICE_API = f"{COAMC_BASE_URL}/coamc/api/getNoticePage"
COAMC_NOTICE_TYPES = [
    ("DEBTS_DISPOSITION", "抵债物资产处置公告", 1),
    ("CREDIT_DISPOSITION", "债权资产处置公告", 1),
    ("ASSET_DISPOSITION", "资产包处置公告", 1),
    ("STOCK_DISPOSITION", "股权资产处置公告", 1),
    ("MULTI_DISPOSITION", "多项资产处置公告", 1),
    ("INVESTMENT", "资产包招商公告", 2),
    ("MARKETING", "推介信息", 2),
]
ROOT = Path(__file__).resolve().parents[2]
ATTACHMENT_DIR = ROOT / "data" / "attachments"
ProgressCallback = Callable[[str, str, dict | None], None]
StopCallback = Callable[[], bool]


def _clean_text(value: str) -> str:
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _repair_mojibake(value: str) -> str:
    try:
        repaired = value.encode("latin1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
    if len(re.findall(r"[\u4e00-\u9fff]", repaired)) > len(re.findall(r"[\u4e00-\u9fff]", value)):
        return repaired
    return value


def _response_text(response: httpx.Response) -> str:
    text = response.text
    return _repair_mojibake(text)


def _extract_detail_text(html_text: str, source_id: str) -> str:
    if source_id == "coamc_orient_disposal":
        match = re.search(
            r"<div\b[^>]*class=[\"'][^\"']*\bnotice-detail\b[^\"']*[\"'][^>]*>([\s\S]*?)(?:<div\b[^>]*class=[\"'][^\"']*\bfooter\b|</body>)",
            html_text,
            flags=re.I,
        )
        if match:
            return _clean_text(match.group(1))
    for pattern in (
        r"<article\b[^>]*>([\s\S]*?)</article>",
        r"<main\b[^>]*>([\s\S]*?)</main>",
        r"<div\b[^>]*class=[\"'][^\"']*(?:detail|content|article|notice)[^\"']*[\"'][^>]*>([\s\S]*?)</div>",
    ):
        match = re.search(pattern, html_text, flags=re.I)
        if match:
            cleaned = _clean_text(match.group(1))
            if len(cleaned) > 80:
                return cleaned
    return _clean_text(html_text)


def _suffix_from_response(resp: httpx.Response, fallback: str = ".bin") -> str:
    content_type = (resp.headers.get("content-type") or "").lower()
    if "pdf" in content_type:
        return ".pdf"
    if "spreadsheet" in content_type or "excel" in content_type:
        return ".xlsx"
    if "word" in content_type:
        return ".docx"
    if "zip" in content_type:
        return ".zip"
    if "rar" in content_type:
        return ".rar"
    return fallback


def _filename_from_content_disposition(resp: httpx.Response) -> str:
    disposition = resp.headers.get("content-disposition") or ""
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.I)
    if match:
        return unquote(match.group(1)).strip()
    match = re.search(r'filename="?([^";]+)"?', disposition, flags=re.I)
    if match:
        return unquote(match.group(1)).strip()
    return ""


def _safe_name(url: str, label: str = "", suffix: str | None = None) -> str:
    suffix = suffix or Path(urlparse(url).path).suffix.lower() or ".bin"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    base_label = Path(label).stem if Path(label).suffix.lower() else label
    base = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", base_label or "attachment")[:50]
    return f"{base}_{digest}{suffix}"


def _extract_attachment_text(path: Path) -> str:
    # MVP: text extraction for plain/html-like files. Binary PDF/Office OCR can be added later.
    if path.suffix.lower() in {".txt", ".html", ".htm", ".csv"}:
        return _clean_text(path.read_text(encoding="utf-8", errors="ignore"))[:8000]
    return ""


def _looks_like_attachment_link(href: str, label: str) -> bool:
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    parsed = urlparse(href)
    path = unquote(parsed.path or "").lower()
    suffix = Path(path).suffix.lower()
    if suffix in ATTACHMENT_EXTS:
        return True
    if suffix in HTML_EXTS or path in {"", "/"}:
        return False
    has_label_hint = any(term in label for term in ATTACHMENT_LABEL_HINTS)
    has_path_hint = any(term in path for term in DOWNLOAD_PATH_HINTS)
    return has_label_hint and has_path_hint


def _download_attachments(client: httpx.Client, page_url: str, html_text: str, source_id: str) -> list[dict[str, str]]:
    ATTACHMENT_DIR.mkdir(parents=True, exist_ok=True)
    attachments: list[dict[str, str]] = []
    anchors = re.findall(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>", html_text, flags=re.I)
    seen_urls: set[str] = set()
    for href, label_html in anchors[:200]:
        label = _clean_text(label_html) or "附件"
        href = html.unescape(href).strip()
        if not _looks_like_attachment_link(href, label):
            continue
        url = urljoin(page_url, href)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        path_suffix = Path(urlparse(url).path).suffix.lower()
        try:
            resp = client.get(url, timeout=30, headers={"Referer": page_url})
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").lower()
            content_disposition_name = _filename_from_content_disposition(resp)
            if "text/html" in content_type and path_suffix not in ATTACHMENT_EXTS and not content_disposition_name:
                continue
            suffix = Path(content_disposition_name or urlparse(url).path).suffix.lower()
            if suffix not in ATTACHMENT_EXTS:
                suffix = _suffix_from_response(resp, fallback=path_suffix if path_suffix in ATTACHMENT_EXTS else ".bin")
            if suffix not in ATTACHMENT_EXTS:
                continue
            local = ATTACHMENT_DIR / source_id / _safe_name(url, content_disposition_name or label, suffix=suffix)
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(resp.content)
            attachments.append({
                "name": label,
                "url": url,
                "local_path": str(local.relative_to(ROOT)).replace("\\", "/"),
                "text": _extract_attachment_text(local),
            })
        except Exception as exc:
            attachments.append({"name": label, "url": url, "local_path": "", "error": str(exc)})
    return attachments


def _extract_amount_text(text: str) -> str:
    patterns = [
        r"(?:债权本金|债权金额|本金|起拍价|评估价|转让价|保证金|标的金额)[：:\s]*[人民币¥￥]?\s*[0-9,.]+(?:万|万元|亿|亿元|元)?",
        r"[人民币¥￥]\s*[0-9,.]+(?:万|万元|亿|亿元|元)?",
        r"[0-9,.]+(?:万|万元|亿|亿元)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)[:80]
    return ""


def _is_asset_like_title(source: SourceSpec, title: str) -> bool:
    clean = title.strip()
    if not clean or clean in NOISE_TITLES or any(term in clean for term in NOISE_TERMS):
        return False
    if source.source_id == "cinda_amc_asset_disposal":
        return True
    if source.source_type == "judicial_auction":
        return "法院" in clean and "关于" in clean and any(term in clean for term in ("拍卖", "变卖", "竞价"))
    return any(term in clean for term in ("资产处置", "债权", "招商", "竞价", "转让", "催收", "拍卖", "不良"))


def _fetch_detail(
    client: httpx.Client,
    source_id: str,
    url: str,
    title: str,
    include_attachments: bool = True,
) -> tuple[str, list[dict[str, str]]]:
    try:
        response = client.get(url)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        html_text = _response_text(response)
        detail = _extract_detail_text(html_text, source_id)
        attachments = _download_attachments(client, url, html_text, source_id) if include_attachments else []
        return detail[:20000], attachments
    except Exception as exc:
        return title + f" 详情页读取失败: {exc}", []


def enrich_notice_detail(
    notice: RawNotice,
    progress: ProgressCallback | None = None,
    include_attachments: bool = True,
    detail_limit: int = 20000,
) -> RawNotice:
    if notice.detail_text and (notice.attachments or not include_attachments):
        return notice
    if not notice.source_url:
        return notice
    if progress:
        progress("detail", f"读取详情页：{notice.title}", {"url": notice.source_url})
    source_id = str(notice.metadata.get("source_id") or "unknown_source")
    headers = {
        "User-Agent": "Mozilla/5.0 AssetRadar/0.2",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    verify_ssl = source_id != "gwamcc_international_disposal"
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers, verify=verify_ssl) as client:
        detail_text, attachments = _fetch_detail(
            client,
            source_id,
            notice.source_url,
            notice.title,
            include_attachments=include_attachments,
        )
    notice.detail_text = (detail_text or notice.detail_text)[:detail_limit]
    notice.raw_text = notice.detail_text or notice.raw_text
    if include_attachments:
        notice.attachments = attachments
    return notice


def _notice_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _discover_page_urls(source: SourceSpec, html_text: str) -> list[str]:
    urls = [source.url]
    source_path = urlparse(source.url).path
    source_dir = source_path.rsplit("/", 1)[0] + "/"
    anchors = re.findall(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>", html_text, flags=re.I)
    for href, _label_html in anchors:
        href = html.unescape(href)
        if not href or href.startswith("javascript:"):
            continue
        url = urljoin(source.url, href)
        path = urlparse(url).path
        if path.startswith(source_dir) and re.search(r"/index(?:_\d+)?\.shtml$", path):
            urls.append(url)
    deduped = list(dict.fromkeys(urls))
    return deduped[: max(1, source.max_pages)]


def _extract_links(
    source: SourceSpec,
    html_text: str,
    limit: int,
    progress: ProgressCallback | None = None,
    seen: set[str] | None = None,
    should_stop: StopCallback | None = None,
) -> list[RawNotice]:
    anchors = re.findall(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>([\s\S]*?)</a>", html_text, flags=re.I)
    notices: list[RawNotice] = []
    seen_urls = seen if seen is not None else set()
    for href, label_html in anchors:
        if should_stop and should_stop():
            break
        title = _clean_text(label_html)
        if not title or not any(keyword in title for keyword in NOTICE_KEYWORDS):
            continue
        if not _is_asset_like_title(source, title):
            continue
        url = urljoin(source.url, html.unescape(href))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        amount_text = _extract_amount_text(title)
        notice = RawNotice(
            source_platform=source.name,
            source_url=url,
            title=title[:180],
            notice_date=None,
            raw_text=title,
            amount_text=amount_text,
            disposal_agency=source.name,
            city=extract_city_from_text(title),
            metadata={
                "source_id": source.source_id,
                "source_type": source.source_type,
                "scan_mode": "basic",
                "source_scan_llm_used": False,
            },
        )
        notices.append(notice)
        if progress:
            progress("notice", f"发现资产线索：{title[:80]}", {"source": source.name, "count": len(notices)})
        if len(notices) >= limit:
            break
    return notices


def _coamc_relevant_categories(progress: ProgressCallback | None = None) -> list[tuple[str, str, int, dict]]:
    selected: list[tuple[str, str, int, dict]] = []
    for type_code, name, notice_cate in COAMC_NOTICE_TYPES:
        decision = classify_source_category_with_glm(name, [type_code])
        if progress:
            progress(
                "source_category",
                f"识别东方资产下拉分类：{name} -> {'纳入' if decision.get('include') else '剔除'}",
                {"category": name, "type": type_code, **decision},
            )
        if decision.get("include"):
            selected.append((type_code, name, notice_cate, decision))
    return selected


def _crawl_coamc_notice(
    source: SourceSpec,
    limit: int,
    progress: ProgressCallback | None = None,
    should_stop: StopCallback | None = None,
) -> list[RawNotice]:
    notices: list[RawNotice] = []
    headers = {
        "User-Agent": "Mozilla/5.0 AssetRadar/0.2",
        "Referer": source.url,
    }
    categories = _coamc_relevant_categories(progress=progress)
    if not categories:
        return notices
    category_limit = max(1, (limit + len(categories) - 1) // len(categories))
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
        for type_code, category_name, notice_cate, decision in categories:
            found_in_category = 0
            for page in range(1, max(1, source.max_pages) + 1):
                if should_stop and should_stop():
                    return notices
                if found_in_category >= category_limit or len(notices) >= limit:
                    break
                if progress:
                    progress(
                        "page",
                        f"扫描东方资产：{category_name} 第 {page} 页",
                        {"source": source.name, "category": category_name, "page": page},
                    )
                response = client.post(
                    COAMC_NOTICE_API,
                    data={
                        "title": "",
                        "type": type_code,
                        "branchId": "",
                        "condition": "0",
                        "pageSize": source.page_size,
                        "page": page,
                        "noticeCate": notice_cate,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                container = payload.get("data") if isinstance(payload, dict) else {}
                page_data = container.get("data") if isinstance(container, dict) else {}
                rows = page_data.get("content") if isinstance(page_data, dict) else []
                if not rows:
                    break
                for row in rows:
                    if should_stop and should_stop():
                        return notices
                    if found_in_category >= category_limit or len(notices) >= limit:
                        break
                    title = str(row.get("title") or "").strip()
                    if not title:
                        continue
                    row_text = json.dumps(row, ensure_ascii=False)
                    detail_url = urljoin(COAMC_BASE_URL, str(row.get("url") or source.url))
                    notice = RawNotice(
                        source_platform=source.name,
                        source_url=detail_url,
                        title=title[:180],
                        notice_date=_notice_date(row.get("auditTime") or row.get("addTime")),
                        raw_text=f"{title} {row.get('branchName') or ''} {row.get('typeKey') or category_name}",
                        amount_text=_extract_amount_text(f"{title} {row_text}"),
                        disposal_agency=str(row.get("branchName") or source.name),
                        city=extract_city_from_text(title, row_text),
                        metadata={
                            "source_id": source.source_id,
                            "source_type": source.source_type,
                            "scan_mode": "coamc_notice_api",
                            "source_scan_llm_used": False,
                            "source_category_llm_used": bool(decision.get("llm_used")),
                            "source_category_model": decision.get("model_name"),
                            "disposal_type": type_code,
                            "disposal_type_name": category_name,
                            "notice_id": row.get("id"),
                        },
                    )
                    notices.append(notice)
                    found_in_category += 1
                    if progress:
                        progress("notice", f"发现资产线索：{title[:80]}", {"source": source.name, "count": len(notices)})
                total_pages = int(page_data.get("totalPages") or page) if isinstance(page_data, dict) else page
                if page >= total_pages:
                    break
    return notices


def _gwamcc_page_url(source: SourceSpec, page: int) -> str:
    if page <= 1:
        return source.url
    return urljoin(source.url, f"index_{page}.html")


def _crawl_gwamcc_static(
    source: SourceSpec,
    limit: int,
    progress: ProgressCallback | None = None,
    should_stop: StopCallback | None = None,
) -> list[RawNotice]:
    notices: list[RawNotice] = []
    seen: set[str] = set()
    headers = {
        "User-Agent": "Mozilla/5.0 AssetRadar/0.2",
        "Referer": source.url,
    }
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers, verify=False) as client:
        for page in range(1, max(1, source.max_pages) + 1):
            if should_stop and should_stop():
                break
            page_url = _gwamcc_page_url(source, page)
            if progress:
                progress("page", f"扫描长城国际资产处置公告第 {page} 页", {"source": source.name, "page": page})
            response = client.get(page_url)
            if response.status_code == 404:
                break
            response.raise_for_status()
            response.encoding = response.encoding or "utf-8"
            html_text = _response_text(response)
            anchors = re.findall(
                r"<a\b[^>]*href=[\"']([^\"']*?/cgwamc/zzczgg/\d+\.html)[\"'][^>]*title=[\"']([^\"']+)[\"'][^>]*>",
                html_text,
                flags=re.I,
            )
            if not anchors:
                anchors = re.findall(
                    r"<a\b[^>]*href=[\"']([^\"']*?/cgwamc/zzczgg/\d+\.html)[\"'][^>]*>([\s\S]*?)</a>",
                    html_text,
                    flags=re.I,
                )
            for href, label_html in anchors:
                if should_stop and should_stop():
                    return notices
                title = _clean_text(_repair_mojibake(label_html))
                if not title or not _is_asset_like_title(source, title):
                    continue
                url = urljoin(source.url, html.unescape(href))
                if url in seen:
                    continue
                seen.add(url)
                notice = RawNotice(
                    source_platform=source.name,
                    source_url=url,
                    title=title[:180],
                    notice_date=None,
                    raw_text=title,
                    amount_text=_extract_amount_text(title),
                    disposal_agency=source.name,
                    city=extract_city_from_text(title),
                    metadata={
                        "source_id": source.source_id,
                        "source_type": source.source_type,
                        "scan_mode": "gwamcc_static_list",
                        "source_scan_llm_used": False,
                    },
                )
                notices.append(notice)
                if progress:
                    progress("notice", f"发现资产线索：{title[:80]}", {"source": source.name, "count": len(notices)})
                if len(notices) >= limit:
                    return notices
    return notices


def _crawl_cinda_api(
    source: SourceSpec,
    limit: int,
    progress: ProgressCallback | None = None,
    should_stop: StopCallback | None = None,
) -> list[RawNotice]:
    api_url = "https://www.cinda.com.cn/api/asset/wtdf/json/bulletin/listData"
    detail_base = "https://www.cinda.com.cn/home/pc/cn/xdjt/qykhpd/blzcjy/zcggxq/index.shtml?bulletintno="
    notices: list[RawNotice] = []
    headers = {
        "User-Agent": "Mozilla/5.0 AssetRadar/0.2",
        "Referer": source.url,
    }
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
        for page in range(1, max(1, source.max_pages) + 1):
            if should_stop and should_stop():
                break
            if progress:
                progress("page", f"扫描分页：{source.name} 第 {page} 页", {"source": source.name, "page": page})
            response = client.post(
                api_url,
                data={"currentPage": page, "pageSize": source.page_size, "seachbulltin": "bulletin"},
            )
            response.raise_for_status()
            payload = response.json().get("data") or {}
            rows = payload.get("data") or []
            if not rows:
                break
            for row in rows:
                if should_stop and should_stop():
                    return notices
                title = str(row.get("name") or "").strip()
                if not _is_asset_like_title(source, title):
                    continue
                row_text = json.dumps(row, ensure_ascii=False)
                notice = RawNotice(
                    source_platform=source.name,
                    source_url=f"{detail_base}{row.get('id')}",
                    title=title[:180],
                    notice_date=_notice_date(row.get("createtime")),
                    raw_text=title,
                    amount_text=str(row.get("amount") or ""),
                    disposal_agency=str(row.get("dealoffice") or source.name),
                    city=extract_city_from_text(title, row_text),
                    metadata={
                        "source_id": source.source_id,
                        "source_type": source.source_type,
                        "scan_mode": "cinda_api",
                        "source_scan_llm_used": False,
                        "bulletin_id": row.get("id"),
                        "bulletin_code": row.get("code"),
                    },
                )
                notices.append(notice)
                if progress:
                    progress("notice", f"发现资产线索：{title[:80]}", {"source": source.name, "count": len(notices)})
                if len(notices) >= limit:
                    return notices
            page_count = int(payload.get("pageCount") or page)
            if page >= page_count or len(notices) >= limit:
                break
    return notices


def crawl_source(
    source: SourceSpec,
    limit: int = 20,
    progress: ProgressCallback | None = None,
    should_stop: StopCallback | None = None,
) -> list[RawNotice]:
    if should_stop and should_stop():
        return []
    if not source.enabled or source.source_type not in SUPPORTED_SOURCE_TYPES:
        if progress:
            progress("skip", f"跳过未启用或暂不支持的 source：{source.name}", {"source": source.name})
        return []
    if progress:
        progress("source", f"开始扫描：{source.name}", {"source": source.name, "url": source.url})
    if source.source_id == "cinda_amc_asset_disposal":
        notices = _crawl_cinda_api(source, limit=limit, progress=progress, should_stop=should_stop)
        if progress:
            progress("source_done", f"完成扫描：{source.name}，发现 {len(notices)} 条", {"source": source.name, "count": len(notices)})
        return notices
    if source.source_id == "coamc_orient_disposal":
        notices = _crawl_coamc_notice(source, limit=limit, progress=progress, should_stop=should_stop)
        if progress:
            progress("source_done", f"完成扫描：{source.name}，发现 {len(notices)} 条", {"source": source.name, "count": len(notices)})
        return notices
    if source.source_id == "gwamcc_international_disposal":
        notices = _crawl_gwamcc_static(source, limit=limit, progress=progress, should_stop=should_stop)
        if progress:
            progress("source_done", f"完成扫描：{source.name}，发现 {len(notices)} 条", {"source": source.name, "count": len(notices)})
        return notices
    headers = {
        "User-Agent": "Mozilla/5.0 AssetRadar/0.2",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
        response = client.get(source.url)
        response.raise_for_status()
        response.encoding = response.encoding or "utf-8"
        first_html = _response_text(response)
        page_urls = _discover_page_urls(source, first_html) if source.source_type == "amc_notice" else [source.url]
        notices: list[RawNotice] = []
        seen: set[str] = set()
        for page_index, page_url in enumerate(page_urls, start=1):
            if should_stop and should_stop():
                break
            if page_index == 1:
                html_text = first_html
            else:
                if progress:
                    progress("page", f"扫描分页：{source.name} 第 {page_index} 页", {"source": source.name, "page": page_index})
                page_response = client.get(page_url)
                page_response.raise_for_status()
                page_response.encoding = page_response.encoding or "utf-8"
                html_text = _response_text(page_response)
            remaining = max(0, limit - len(notices))
            if remaining <= 0:
                break
            notices.extend(_extract_links(source, html_text, remaining, progress, seen=seen, should_stop=should_stop))
        if progress:
            progress("source_done", f"完成扫描：{source.name}，发现 {len(notices)} 条", {"source": source.name, "count": len(notices)})
        return notices


def crawl_default_sources(
    limit_per_source: int = 20,
    progress: ProgressCallback | None = None,
    should_stop: StopCallback | None = None,
    source_ids: list[str] | None = None,
    total_limit: int | None = None,
) -> list[RawNotice]:
    notices: list[RawNotice] = []
    selected_ids = set(source_ids or [])
    sources = [source for source in DEFAULT_SOURCES if not selected_ids or source.source_id in selected_ids]
    for source in sources:
        if total_limit is not None and len(notices) >= total_limit:
            break
        if should_stop and should_stop():
            if progress:
                progress("stop", "已收到停止请求，结束 source 扫描", {"count": len(notices)})
            break
        try:
            remaining = total_limit - len(notices) if total_limit is not None else limit_per_source
            source_limit = max(0, min(limit_per_source, remaining))
            if source_limit <= 0:
                break
            notices.extend(crawl_source(source, limit=source_limit, progress=progress, should_stop=should_stop))
            if should_stop and should_stop():
                if progress:
                    progress("stop", "已收到停止请求，结束 source 扫描", {"count": len(notices)})
                break
        except Exception as exc:
            if progress:
                progress("error", f"扫描失败：{source.name} - {exc}", {"source": source.name})
            notices.append(
                RawNotice(
                    source_platform=source.name,
                    source_url=source.url,
                    title=f"{source.name} 扫描失败",
                    notice_date=date.today(),
                    raw_text=f"source scan failed: {exc}",
                    metadata={"source_id": source.source_id, "scan_error": str(exc)},
                )
            )
    return notices
