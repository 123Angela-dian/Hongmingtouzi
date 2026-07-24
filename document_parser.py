from __future__ import annotations

import gzip
import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

from logger_config import get_logger
from ocr import run_paddleocr
from storage import StorageBackend


logger = get_logger("document_parser")

PARSER_CACHE_VERSION = "document-elements-v1"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
UNSTRUCTURED_EXTENSIONS = {
    ".csv",
    ".docx",
    ".eml",
    ".html",
    ".htm",
    ".md",
    ".msg",
    ".pptx",
    ".rtf",
    ".text",
    ".txt",
    ".tsv",
    ".xlsx",
}


@dataclass
class DocumentElement:
    element_id: str
    source_file: str
    parser: str
    category: str
    text: str
    page_number: int | None = None
    page_name: str | None = None
    parent_id: str | None = None
    coordinates: dict[str, Any] | None = None
    text_as_html: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    source_file: str
    file_sha256: str
    route: str
    parser: str
    markdown: str
    elements: list[DocumentElement]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "file_sha256": self.file_sha256,
            "route": self.route,
            "parser": self.parser,
            "markdown": self.markdown,
            "elements": [asdict(element) for element in self.elements],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ParsedDocument":
        return cls(
            source_file=str(payload.get("source_file") or ""),
            file_sha256=str(payload.get("file_sha256") or ""),
            route=str(payload.get("route") or ""),
            parser=str(payload.get("parser") or ""),
            markdown=str(payload.get("markdown") or ""),
            elements=[DocumentElement(**item) for item in payload.get("elements") or []],
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass
class PackageParseResult:
    documents: list[ParsedDocument]
    logs: list[str]
    errors: list[str]


def file_sha256(file_path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as file_obj:
        while chunk := file_obj.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_has_text_layer(file_path: Path, max_sample_pages: int = 10) -> bool:
    """Return True only when most sampled PDF pages contain extractable text."""

    try:
        from pypdf import PdfReader

        reader = PdfReader(str(file_path))
        page_count = len(reader.pages)
        if page_count == 0:
            return False
        sample_count = min(page_count, max_sample_pages)
        if sample_count == 1:
            indexes = [0]
        else:
            indexes = sorted({round(index * (page_count - 1) / (sample_count - 1)) for index in range(sample_count)})
        extracted_lengths = [len((reader.pages[index].extract_text() or "").strip()) for index in indexes]
        text_pages = sum(length >= 30 for length in extracted_lengths)
        return sum(extracted_lengths) >= 120 and text_pages / len(indexes) >= 0.6
    except Exception as exc:
        logger.warning("PDF text-layer detection failed; routing to OCR | file=%s | error=%s", file_path.name, exc)
        return False


def choose_parse_route(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return "baidu_ocr"
    if suffix == ".pdf":
        return "text_pdf" if pdf_has_text_layer(file_path) else "baidu_ocr"
    if suffix in UNSTRUCTURED_EXTENSIONS:
        return "unstructured"
    raise ValueError(f"Unsupported document type: {suffix or file_path.name}")


def parse_document(file_path: Path, file_hash: str, route: str | None = None) -> ParsedDocument:
    resolved_route = route or choose_parse_route(file_path)
    if resolved_route == "baidu_ocr":
        return _parse_with_baidu_ocr(file_path, file_hash)
    if resolved_route == "text_pdf":
        return _parse_with_pypdf(file_path, file_hash)
    if resolved_route == "unstructured":
        return _parse_with_unstructured(file_path, file_hash)
    raise ValueError(f"Unknown parse route: {resolved_route}")


def _parse_with_baidu_ocr(file_path: Path, file_hash: str) -> ParsedDocument:
    ocr_result = run_paddleocr(file_path)
    page_markdown = [part.strip() for part in re.split(r"\n\s*---\s*\n", ocr_result.markdown) if part.strip()]
    elements: list[DocumentElement] = []
    for page_number, markdown in enumerate(page_markdown or [ocr_result.markdown], start=1):
        elements.extend(
            _partition_markdown_elements(
                markdown,
                source_file=file_path.name,
                file_hash=file_hash,
                parser="baidu_paddleocr",
                page_number=page_number,
                start_index=len(elements),
            )
        )
    return ParsedDocument(
        source_file=file_path.name,
        file_sha256=file_hash,
        route="baidu_ocr",
        parser="baidu_paddleocr",
        markdown=ocr_result.markdown,
        elements=elements,
        metadata={
            "job_id": ocr_result.job_id,
            "pages": ocr_result.pages,
            "model": os.getenv("PADDLEOCR_MODEL") or os.getenv("EYES_MODEL") or "",
        },
    )


def _parse_with_pypdf(file_path: Path, file_hash: str) -> ParsedDocument:
    from pypdf import PdfReader

    reader = PdfReader(str(file_path))
    elements: list[DocumentElement] = []
    page_markdown: list[str] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        element_id = hashlib.sha256(
            f"{file_hash}:{page_number}:NarrativeText:{text}".encode("utf-8")
        ).hexdigest()
        elements.append(
            DocumentElement(
                element_id=element_id,
                source_file=file_path.name,
                parser="pypdf",
                category="NarrativeText",
                text=text,
                page_number=page_number,
                metadata={"page_number": page_number},
            )
        )
        page_markdown.append(f"### 第 {page_number} 页\n\n{text}")
    if not elements:
        raise ValueError("PDF 被识别为文本型文件，但未能提取有效文本")
    return ParsedDocument(
        source_file=file_path.name,
        file_sha256=file_hash,
        route="text_pdf",
        parser="pypdf",
        markdown="\n\n---\n\n".join(page_markdown),
        elements=elements,
        metadata={"pages": len(reader.pages), "pypdf_version": _installed_package_version("pypdf")},
    )


def _partition_markdown_elements(
    markdown: str,
    *,
    source_file: str,
    file_hash: str,
    parser: str,
    page_number: int,
    start_index: int,
) -> list[DocumentElement]:
    from unstructured.partition.md import partition_md

    raw_elements = partition_md(text=markdown, metadata_filename=source_file)
    return [
        _convert_element(
            element,
            source_file=source_file,
            file_hash=file_hash,
            parser=parser,
            index=start_index + index,
            page_number_override=page_number,
        )
        for index, element in enumerate(raw_elements)
        if str(element).strip()
    ]


def _parse_with_unstructured(file_path: Path, file_hash: str) -> ParsedDocument:
    partition = _unstructured_partitioner(file_path.suffix.lower())
    raw_elements = partition(filename=str(file_path))
    elements = [
        _convert_element(
            element,
            source_file=file_path.name,
            file_hash=file_hash,
            parser="unstructured",
            index=index,
        )
        for index, element in enumerate(raw_elements)
        if str(element).strip()
    ]
    return ParsedDocument(
        source_file=file_path.name,
        file_sha256=file_hash,
        route="unstructured",
        parser="unstructured",
        markdown=_elements_to_markdown(elements),
        elements=elements,
        metadata={"unstructured_version": _installed_package_version("unstructured")},
    )


def _unstructured_partitioner(suffix: str):
    partitioners = {
        ".csv": ("unstructured.partition.csv", "partition_csv"),
        ".docx": ("unstructured.partition.docx", "partition_docx"),
        ".eml": ("unstructured.partition.email", "partition_email"),
        ".html": ("unstructured.partition.html", "partition_html"),
        ".htm": ("unstructured.partition.html", "partition_html"),
        ".md": ("unstructured.partition.md", "partition_md"),
        ".msg": ("unstructured.partition.msg", "partition_msg"),
        ".pptx": ("unstructured.partition.pptx", "partition_pptx"),
        ".rtf": ("unstructured.partition.rtf", "partition_rtf"),
        ".text": ("unstructured.partition.text", "partition_text"),
        ".txt": ("unstructured.partition.text", "partition_text"),
        ".tsv": ("unstructured.partition.tsv", "partition_tsv"),
        ".xlsx": ("unstructured.partition.xlsx", "partition_xlsx"),
    }
    module_name, function_name = partitioners[suffix]
    return getattr(importlib.import_module(module_name), function_name)


def _convert_element(
    element: Any,
    *,
    source_file: str,
    file_hash: str,
    parser: str,
    index: int,
    page_number_override: int | None = None,
) -> DocumentElement:
    metadata_obj = getattr(element, "metadata", None)
    metadata = metadata_obj.to_dict() if metadata_obj is not None else {}
    metadata = _json_safe(metadata)
    category = str(getattr(element, "category", None) or element.__class__.__name__)
    text = str(element).strip()
    element_id = hashlib.sha256(f"{file_hash}:{index}:{category}:{text}".encode("utf-8")).hexdigest()
    return DocumentElement(
        element_id=element_id,
        source_file=source_file,
        parser=parser,
        category=category,
        text=text,
        page_number=page_number_override or _optional_int(metadata.get("page_number")),
        page_name=_optional_str(metadata.get("page_name")),
        parent_id=_optional_str(metadata.get("parent_id")),
        coordinates=metadata.get("coordinates") if isinstance(metadata.get("coordinates"), dict) else None,
        text_as_html=_optional_str(metadata.get("text_as_html")),
        metadata=metadata,
    )


def _elements_to_markdown(elements: Iterable[DocumentElement]) -> str:
    parts: list[str] = []
    for element in elements:
        if element.category == "Title":
            parts.append(f"## {element.text}")
        elif element.category == "ListItem":
            parts.append(f"- {element.text}")
        elif element.category in {"Table", "TableChunk"}:
            parts.append(element.text_as_html or element.text)
        else:
            parts.append(element.text)
    return "\n\n".join(part for part in parts if part.strip()).strip()


class DataPackageParser:
    def __init__(
        self,
        storage: StorageBackend,
        *,
        document_workers: int | None = None,
        ocr_workers: int | None = None,
    ):
        self.storage = storage
        self.document_workers = max(1, document_workers or int(os.getenv("PARSER_DOCUMENT_WORKERS", "2")))
        self.ocr_workers = max(1, ocr_workers or int(os.getenv("PARSER_OCR_WORKERS", "2")))

    def parse(self, file_paths: list[Path]) -> PackageParseResult:
        documents_by_index: dict[int, ParsedDocument] = {}
        logs: list[str] = []
        errors: list[str] = []
        pending: list[tuple[int, Path, str, str, str]] = []

        for index, file_path in enumerate(file_paths):
            try:
                file_hash = file_sha256(file_path)
                route = choose_parse_route(file_path)
                cache_key = self._cache_key(file_hash, route)
                cached = self.storage.get_json(cache_key)
                if cached:
                    documents_by_index[index] = ParsedDocument.from_dict(cached)
                    logs.append(f"命中解析缓存：{file_path.name}")
                else:
                    pending.append((index, file_path, file_hash, route, cache_key))
            except Exception as exc:
                errors.append(f"文件识别失败：{file_path.name} ({exc})")

        future_meta: dict[Future[ParsedDocument], tuple[int, Path, str]] = {}
        with (
            ThreadPoolExecutor(max_workers=self.document_workers, thread_name_prefix="document-parser") as document_pool,
            ThreadPoolExecutor(max_workers=self.ocr_workers, thread_name_prefix="ocr-parser") as ocr_pool,
        ):
            for index, file_path, file_hash, route, cache_key in pending:
                executor = ocr_pool if route == "baidu_ocr" else document_pool
                future = executor.submit(self._parse_and_cache, file_path, file_hash, route, cache_key)
                future_meta[future] = (index, file_path, route)

            for future in as_completed(future_meta):
                index, file_path, route = future_meta[future]
                try:
                    document = future.result()
                except Exception as exc:
                    logger.exception("Document parsing failed | file=%s | route=%s", file_path.name, route)
                    errors.append(f"文件解析失败：{file_path.name} ({exc})")
                    continue
                documents_by_index[index] = document
                route_label = {
                    "baidu_ocr": "百度 OCR",
                    "text_pdf": "pypdf",
                    "unstructured": "Unstructured",
                }.get(route, route)
                logs.append(f"{route_label} 完成：{file_path.name}，{len(document.elements)} 个元素")

        documents = [documents_by_index[index] for index in sorted(documents_by_index)]
        return PackageParseResult(documents=documents, logs=logs, errors=errors)

    def save_elements_artifact(self, package_key: str, documents: Iterable[ParsedDocument]) -> str:
        artifact_key = f"artifacts/packages/{package_key}/document_elements.jsonl.gz"
        with tempfile.TemporaryFile(mode="w+b") as artifact_file:
            with gzip.GzipFile(fileobj=artifact_file, mode="wb") as compressed_file:
                for document in documents:
                    for element in document.elements:
                        line = json.dumps(asdict(element), ensure_ascii=False, separators=(",", ":"))
                        compressed_file.write(line.encode("utf-8"))
                        compressed_file.write(b"\n")
            artifact_file.seek(0)
            self.storage.put_file(artifact_key, artifact_file)
        return artifact_key

    def _parse_and_cache(
        self,
        file_path: Path,
        file_hash: str,
        route: str,
        cache_key: str,
    ) -> ParsedDocument:
        document = parse_document(file_path, file_hash, route)
        self.storage.put_json(cache_key, document.to_dict())
        return document

    @staticmethod
    def _cache_key(file_hash: str, route: str) -> str:
        if route == "baidu_ocr":
            route_version = os.getenv("PADDLEOCR_MODEL") or os.getenv("EYES_MODEL") or "baidu-default"
        elif route == "text_pdf":
            route_version = f"pypdf-{_installed_package_version('pypdf')}"
        else:
            route_version = f"unstructured-{_installed_package_version('unstructured')}"
        version_hash = hashlib.sha256(f"{PARSER_CACHE_VERSION}:{route}:{route_version}".encode("utf-8")).hexdigest()[:16]
        return f"cache/files/{file_hash}/{version_hash}.json"


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _installed_package_version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"
