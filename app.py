from __future__ import annotations

import html
import logging
import os
import random
import re
import uuid
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


def wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _render_file_list(files: list[tuple[str, str]]) -> str:
    if not files:
        return "<p class='upload__promt file-list-empty'>Пока нет загруженных изображений.</p>"

    rows = "".join(
        "<div class='file-list-item'>"
        "<div class='file-col file-col-name'>"
        "<span class='file-icon'><img src='/static/img/icon/file.png' alt=''></span>"
        f"<span class='file-name' title='{html.escape(name, quote=True)}'>{html.escape(name)}</span></div>"
        f"<div class='file-col file-col-url'><a href='{html.escape(url, quote=True)}' target='_blank' rel='noopener'>"
        f"{html.escape(url)}</a></div>"
        f"<form class='file-col file-col-delete' action='/delete/{html.escape(name, quote=True)}' method='post'>"
        f"<button class='delete-btn' type='submit' aria-label='Удалить {html.escape(name, quote=True)}'>"
        "<img class='delete-img' src='/static/img/icon/delete.png' alt=''></button></form>"
        "</div>"
        for name, url in files
    )
    return (
        "<div class='file-list-container'>"
        "<div class='file-list-header'>"
        "<div class='file-col file-col-name'>Имя</div>"
        "<div class='file-col file-col-url'>Ссылка</div>"
        "<div class='file-col file-col-delete'>Удалить</div>"
        f"</div><div id='file-list'>{rows}</div></div>"
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
    with destination.open("xb") as saved_file:
        saved_file.write(content)


def create_app(
    images_dir: str | Path | None = None,
    logs_dir: str | Path | None = None,
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

    app = FastAPI(title="Сервер картинок")
    app.state.images_dir = image_path
    app.state.logs_dir = log_path
    app.mount("/static", StaticFiles(directory=project_dir / "static"), name="static")

    logger = logging.getLogger(f"image_storage.{uuid.uuid4().hex}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(log_path / "app.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)
    app.state.logger = logger

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

    def upload_error(request: Request, message: str) -> HTMLResponse:
        # Браузеру отвечаем HTML-страницей, а curl и другим клиентам — JSON
        if wants_html(request):
            return HTMLResponse(render_upload_page(message, "error"), status_code=400)
        raise HTTPException(status_code=400, detail=message)

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
        destination = image_path / saved_name
        try:
            await run_in_threadpool(_save_new_file, destination, content)
        except OSError as error:
            logger.exception("Ошибка: не удалось сохранить изображение %s.", saved_name)
            raise HTTPException(status_code=500, detail="Не удалось сохранить изображение") from error

        image_url = f"/images/{saved_name}"
        full_url = public_url(request, image_url)
        logger.info("Успех: изображение %s загружено.", saved_name)
        if wants_html(request):
            return HTMLResponse(render_upload_page("Изображение загружено", "success", full_url))

        return JSONResponse({"id": image_id, "url": image_url, "full_url": full_url})

    @app.get("/images/", response_class=HTMLResponse)
    async def image_catalog(request: Request) -> str:
        files = sorted(
            (path for path in image_path.iterdir() if path.is_file() and IMAGE_NAME_PATTERN.match(path.name)),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        logger.info("Просмотр: открыт каталог изображений (%d шт.).", len(files))
        file_list = _render_file_list([(path.name, public_url(request, f"/images/{path.name}")) for path in files])
        return render("images.html", safe_html={"file_list": file_list})

    @app.post("/delete/{filename}")
    async def delete_image(filename: str):
        if not IMAGE_NAME_PATTERN.match(filename):
            logger.info("Ошибка: некорректное имя файла для удаления (%s).", filename)
            raise HTTPException(status_code=400, detail="Некорректное имя файла")

        try:
            (image_path / filename).unlink()
        except FileNotFoundError:
            logger.info("Ошибка: изображение %s для удаления не найдено.", filename)
            return JSONResponse({"detail": "Изображение не найдено"}, status_code=404)

        logger.info("Успех: изображение %s удалено.", filename)
        return RedirectResponse("/images/", status_code=303)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
