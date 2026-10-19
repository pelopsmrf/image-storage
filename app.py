from __future__ import annotations

import html
import logging
import math
import os
import random
import re
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from string import Template
from typing import BinaryIO

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from db import DatabaseError, ImageRecord, ImageRepository

MAX_FILE_SIZE = 5 * 1024 * 1024
READ_CHUNK_SIZE = 64 * 1024
SUPPORTED_FORMATS = {
    ".jpg": "JPEG",
    ".png": "PNG",
    ".gif": "GIF",
}
# Имена файлов, которые создаёт сервис: 32 символа uuid4().hex и расширение
IMAGE_NAME_PATTERN = re.compile(r"^[0-9a-f]{32}\.(jpg|png|gif)$")
HERO_IMAGES = ("cat.jpg", "eric.jpg", "griz.jpg", "nord.jpg", "zero.jpg")
PAGE_SIZE = 10
DB_UNAVAILABLE = "База данных недоступна, попробуйте позже"


def wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _parse_page(value: str | None) -> int:
    try:
        return max(1, int(value or 1))
    except ValueError:
        return 1


def _list_url(page: int) -> str:
    return "/images-list" if page <= 1 else f"/images-list?page={page}"


def _render_images_table(records: list[ImageRecord], page: int, image_url: Callable[[str], str]) -> str:
    if not records:
        return "<p class='upload__promt images-empty'>Нет загруженных изображений</p>"

    rows = "".join(
        "<tr>"
        f"<td><a class='images-table__link' href='{html.escape(image_url(record.filename), quote=True)}' "
        f"target='_blank' rel='noopener'>{html.escape(record.filename)}</a></td>"
        f"<td class='images-table__original' title='{html.escape(record.original_name, quote=True)}'>"
        f"{html.escape(record.original_name)}</td>"
        f"<td class='images-table__number'>{record.size / 1024:.1f}</td>"
        f"<td>{record.upload_time:%Y-%m-%d %H:%M:%S}</td>"
        f"<td>{html.escape(record.file_type)}</td>"
        f"<td><form action='/delete/{record.id}?page={page}' method='post'>"
        "<button class='images-table__delete' type='submit' "
        f"aria-label='Удалить {html.escape(record.original_name, quote=True)}'>Удалить</button></form></td>"
        "</tr>"
        for record in records
    )
    return (
        "<div class='images-table__wrapper'><table class='images-table'>"
        "<thead><tr><th>Имя файла</th><th>Оригинальное имя</th><th class='images-table__number'>Размер (КБ)</th>"
        "<th>Дата загрузки</th><th>Тип файла</th><th></th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def _render_pagination(page: int, pages: int) -> str:
    def button(label: str, target: int, enabled: bool) -> str:
        if enabled:
            return f"<a class='pagination__btn' href='{_list_url(target)}'>{label}</a>"
        return f"<span class='pagination__btn pagination__btn--disabled' aria-disabled='true'>{label}</span>"

    return (
        "<nav class='pagination' aria-label='Страницы списка'>"
        f"{button('Предыдущая страница', page - 1, page > 1)}"
        f"<span class='pagination__info'>Страница {page} из {pages}</span>"
        f"{button('Следующая страница', page + 1, page < pages)}"
        "</nav>"
    )


def _read_limited(stream: BinaryIO) -> bytes:
    # Читаем на 1 байт больше лимита, чтобы понять, что файл слишком большой
    chunks = []
    total_size = 0

    while total_size <= MAX_FILE_SIZE:
        chunk = stream.read(min(READ_CHUNK_SIZE, MAX_FILE_SIZE + 1 - total_size))
        if not chunk:
            break
        chunks.append(chunk)
        total_size += len(chunk)

    if total_size > MAX_FILE_SIZE:
        raise ValueError("Размер файла превышает 5 МиБ")

    return b"".join(chunks)


def _validate_image(filename: str, content: bytes) -> str:
    extension = Path(filename).suffix.lower()
    expected_format = SUPPORTED_FORMATS.get(extension)
    if expected_format is None:
        raise ValueError("Поддерживаются только форматы JPG, PNG и GIF")

    try:
        with Image.open(BytesIO(content)) as image:
            actual_format = image.format
            image.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError("Файл не является корректным изображением") from error

    if actual_format != expected_format:
        raise ValueError("Расширение файла не соответствует формату изображения")

    return extension


def _read_and_validate(stream: BinaryIO, filename: str) -> tuple[bytes, str]:
    content = _read_limited(stream)
    if not content:
        raise ValueError("Пустой файл нельзя загрузить")
    return content, _validate_image(filename, content)


def _save_new_file(destination: Path, content: bytes) -> None:
    saved_file = destination.open("xb")
    try:
        with saved_file:
            saved_file.write(content)
    except OSError:
        # Недописанный файл не оставляем на диске
        destination.unlink(missing_ok=True)
        raise


def create_app(
    images_dir: str | Path | None = None,
    logs_dir: str | Path | None = None,
    repository: ImageRepository | None = None,
) -> FastAPI:
    project_dir = Path(__file__).resolve().parent
    image_path = Path(images_dir or os.getenv("IMAGE_DIR", project_dir / "images"))
    log_path = Path(logs_dir or os.getenv("LOG_DIR", project_dir / "logs"))
    image_path.mkdir(parents=True, exist_ok=True)
    log_path.mkdir(parents=True, exist_ok=True)

    public_base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    templates = {
        path.name: Template(path.read_text(encoding="utf-8"))
        for path in (project_dir / "templates").glob("*.html")
    }

    def render(template_name: str, safe_html: dict[str, str] | None = None, **values: str) -> str:
        escaped = {key: html.escape(value, quote=True) for key, value in values.items()}
        return templates[template_name].substitute(escaped, **(safe_html or {}))

    logger = logging.getLogger(f"image_storage.{uuid.uuid4().hex}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(log_path / "app.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)

    repository = repository or ImageRepository()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Таблицу создаёт init.sql, здесь — проверка подключения и страховка для старого volume
        try:
            await run_in_threadpool(repository.ensure_schema)
            logger.info("Успех: подключение к базе данных установлено.")
        except DatabaseError as error:
            logger.error("Ошибка: нет подключения к базе данных (%s).", error)
        yield
        await run_in_threadpool(repository.close)

    app = FastAPI(title="Сервер картинок", lifespan=lifespan)
    app.state.images_dir = image_path
    app.state.logs_dir = log_path
    app.state.logger = logger
    app.state.repository = repository
    app.mount("/static", StaticFiles(directory=project_dir / "static"), name="static")

    @app.exception_handler(StarletteHTTPException)
    async def log_http_errors(request: Request, error: StarletteHTTPException):
        if error.status_code in (404, 405):
            logger.info(
                "Ошибка: %s (%s %s).",
                "маршрут не найден" if error.status_code == 404 else "метод не поддерживается",
                request.method,
                request.url.path,
            )
        return await http_exception_handler(request, error)

    def public_url(request: Request, path: str) -> str:
        # Картинки отдаёт Nginx, поэтому ссылка строится на его адрес
        base_url = public_base_url or str(request.base_url).rstrip("/")
        return f"{base_url}{path}"

    def render_upload_page(message: str = "", message_type: str = "", current_url: str = "") -> str:
        return render(
            "upload.html",
            message=message,
            message_class=f"upload__message--{message_type}" if message_type else "",
            current_url=current_url,
            open_class="" if current_url else "upload__copy--hidden",
        )

    def upload_error(request: Request, message: str, status_code: int = 400) -> HTMLResponse:
        # Браузеру отвечаем HTML-страницей, а curl и другим клиентам — JSON
        if wants_html(request):
            return HTMLResponse(render_upload_page(message, "error"), status_code=status_code)
        raise HTTPException(status_code=status_code, detail=message)

    def render_list_page(
        records: list[ImageRecord] | None = None,
        page: int = 1,
        pages: int = 1,
        image_url: Callable[[str], str] = str,
        message: str = "",
        message_type: str = "",
    ) -> str:
        # records=None — список не удалось получить, таблицу не показываем
        table = "" if records is None else _render_images_table(records, page, image_url)
        return render(
            "images_list.html",
            safe_html={"images_table": table, "pagination": _render_pagination(page, pages) if records else ""},
            message=message,
            message_class=f"upload__message--{message_type}" if message_type else "",
        )

    def load_page(page: int) -> tuple[int, int, list[ImageRecord]]:
        pages = max(1, math.ceil(repository.count() / PAGE_SIZE))
        current = min(page, pages)
        return current, pages, repository.list_page(PAGE_SIZE, (current - 1) * PAGE_SIZE)

    async def list_response(
        request: Request,
        page: int,
        message: str = "",
        message_type: str = "",
        status_code: int = 200,
    ) -> HTMLResponse | RedirectResponse:
        try:
            current, pages, records = await run_in_threadpool(load_page, page)
        except DatabaseError as error:
            logger.error("Ошибка: не удалось получить список изображений (%s).", error)
            return HTMLResponse(render_list_page(message=DB_UNAVAILABLE, message_type="error"), status_code=503)

        # Страницы за концом списка нет — переходим на последнюю
        if current != page:
            return RedirectResponse(_list_url(current), status_code=303)

        logger.info("Просмотр: открыт список изображений (страница %d из %d).", current, pages)
        return HTMLResponse(
            render_list_page(
                records,
                current,
                pages,
                lambda filename: public_url(request, f"/images/{filename}"),
                message,
                message_type,
            ),
            status_code=status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        logger.info("Просмотр: открыта главная страница.")
        return render("index.html", hero_image=random.choice(HERO_IMAGES))

    @app.get("/upload", response_class=HTMLResponse)
    async def upload_form() -> str:
        logger.info("Просмотр: открыта форма загрузки.")
        return render_upload_page()

    @app.post("/upload")
    async def upload_image(request: Request, file: UploadFile | None = File(default=None)):
        if file is None or not file.filename:
            logger.info("Ошибка: файл для загрузки не указан.")
            return upload_error(request, "Необходимо выбрать файл")

        # Проверка и запись файла блокируют сервер, поэтому выполняются в отдельном потоке
        try:
            content, extension = await run_in_threadpool(_read_and_validate, file.file, file.filename)
        except ValueError as error:
            logger.info("Ошибка: %s (%s).", error, Path(file.filename).name)
            return upload_error(request, str(error))
        finally:
            await file.close()

        image_id = uuid.uuid4().hex
        saved_name = f"{image_id}{extension}"
        original_name = Path(file.filename).name
        destination = image_path / saved_name
        file_written = False

        def write_file() -> None:
            nonlocal file_written
            _save_new_file(destination, content)
            file_written = True

        try:
            db_id = await run_in_threadpool(
                repository.add, saved_name, original_name, len(content), extension.lstrip("."), write_file
            )
        except DatabaseError as error:
            # Файл уже записан, но транзакция не завершилась — без метаданных файл не храним
            if file_written:
                destination.unlink(missing_ok=True)
            logger.error("Ошибка: метаданные не сохранены, изображение %s не загружено (%s).", original_name, error)
            return upload_error(request, "База данных недоступна, изображение не сохранено", status_code=503)
        except OSError as error:
            logger.exception("Ошибка: не удалось сохранить изображение %s.", saved_name)
            raise HTTPException(status_code=500, detail="Не удалось сохранить изображение") from error

        image_url = f"/images/{saved_name}"
        full_url = public_url(request, image_url)
        logger.info(
            "Успех: изображение %s (id=%d, %s, %.1f КБ) загружено.",
            saved_name,
            db_id,
            original_name,
            len(content) / 1024,
        )
        if wants_html(request):
            return HTMLResponse(render_upload_page("Изображение загружено", "success", full_url))

        return JSONResponse({"id": image_id, "db_id": db_id, "url": image_url, "full_url": full_url})

    @app.get("/images-list", response_class=HTMLResponse)
    async def images_list(request: Request):
        return await list_response(request, _parse_page(request.query_params.get("page")))

    @app.get("/images/")
    async def old_catalog() -> RedirectResponse:
        return RedirectResponse("/images-list", status_code=301)

    @app.post("/delete/{image_id}")
    async def delete_image(request: Request, image_id: int):
        page = _parse_page(request.query_params.get("page"))

        def remove_file(filename: str) -> None:
            # Имя берётся из базы, но путь всё равно проверяем, чтобы не выйти за пределы /images
            if not IMAGE_NAME_PATTERN.match(filename):
                logger.error("Ошибка: некорректное имя файла в базе (%s, id=%d), файл не удалён.", filename, image_id)
                return
            try:
                (image_path / filename).unlink()
            except FileNotFoundError:
                logger.error("Ошибка: файл %s отсутствует на диске (запись id=%d удалена).", filename, image_id)

        try:
            filename = await run_in_threadpool(repository.delete, image_id, remove_file)
        except DatabaseError as error:
            logger.error("Ошибка: не удалось удалить изображение id=%d (%s).", image_id, error)
            if wants_html(request):
                return HTMLResponse(render_list_page(message=DB_UNAVAILABLE, message_type="error"), status_code=503)
            return JSONResponse({"detail": DB_UNAVAILABLE}, status_code=503)
        except OSError as error:
            logger.exception("Ошибка: не удалось удалить файл изображения id=%d.", image_id)
            raise HTTPException(status_code=500, detail="Не удалось удалить изображение") from error

        if filename is None:
            logger.error("Ошибка: изображение с id=%d для удаления не найдено.", image_id)
            if wants_html(request):
                return await list_response(request, page, "Изображение не найдено", "error", status_code=404)
            return JSONResponse({"detail": "Изображение не найдено"}, status_code=404)

        logger.info("Успех: изображение %s (id=%d) удалено.", filename, image_id)
        return RedirectResponse(_list_url(page), status_code=303)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
