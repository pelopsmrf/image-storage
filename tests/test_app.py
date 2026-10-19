import asyncio
import io
import re
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from app import MAX_FILE_SIZE, create_app
from db import DatabaseError, ImageRecord


class FakeRepository:
    """Таблица images в памяти с тем же интерфейсом, что у db.ImageRepository."""

    def __init__(self):
        self.records = {}
        self.next_id = 1
        self.failing = False
        self.fail_after_write = False
        self.lock = threading.Lock()
        self.clock = datetime(2025, 1, 24, 15, 30, 0)

    def check(self):
        if self.failing:
            raise DatabaseError("connection refused")

    def ensure_schema(self):
        self.check()

    def add(self, filename, original_name, size, file_type, write_file):
        self.check()
        with self.lock:
            image_id = self.next_id
            self.next_id += 1
            self.clock += timedelta(seconds=1)
            upload_time = self.clock
        write_file()
        # Имитация сбоя на COMMIT: файл уже записан, а строка в базу не попала
        if self.fail_after_write:
            raise DatabaseError("commit failed")
        with self.lock:
            self.records[image_id] = ImageRecord(image_id, filename, original_name, size, upload_time, file_type)
        return image_id

    def count(self):
        self.check()
        return len(self.records)

    def list_page(self, limit, offset):
        self.check()
        ordered = sorted(self.records.values(), key=lambda record: (record.upload_time, record.id), reverse=True)
        return ordered[offset:offset + limit]

    def delete(self, image_id, remove_file):
        self.check()
        record = self.records.get(image_id)
        if record is None:
            return None
        remove_file(record.filename)
        del self.records[image_id]
        return record.filename

    def close(self):
        pass


class ImageStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.images_dir = self.root / "images"
        self.logs_dir = self.root / "logs"
        self.repository = FakeRepository()
        self.app = create_app(self.images_dir, self.logs_dir, self.repository)
        self.client = TestClient(self.app)

    def tearDown(self):
        for handler in self.app.state.logger.handlers[:]:
            handler.close()
            self.app.state.logger.removeHandler(handler)
        self.temp_dir.cleanup()

    @staticmethod
    def make_image(image_format):
        buffer = io.BytesIO()
        Image.new("RGB", (2, 2), color="navy").save(buffer, format=image_format)
        return buffer.getvalue()

    def upload(self, filename="photo.png", image_format="PNG", **kwargs):
        return self.client.post(
            "/upload",
            files={"file": (filename, self.make_image(image_format), "image/*")},
            **kwargs,
        )

    def log_text(self):
        return (self.logs_dir / "app.log").read_text(encoding="utf-8")

    def test_home_upload_form_and_health(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn("/upload", self.client.get("/").text)
        self.assertEqual(self.client.get("/upload").status_code, 200)
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_supported_images_are_saved_with_unique_names(self):
        files = (
            ("photo.jpg", "JPEG"),
            ("photo.png", "PNG"),
            ("photo.gif", "GIF"),
        )
        saved_names = []

        for filename, image_format in files:
            response = self.upload(filename, image_format)
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result["url"], f"/images/{result['id']}{Path(filename).suffix}")
            saved_names.append(Path(result["url"]).name)
            self.assertTrue((self.images_dir / saved_names[-1]).is_file())

        self.assertEqual(len(saved_names), len(set(saved_names)))

    def test_upload_saves_metadata_for_each_format(self):
        for filename, image_format in (("cat.jpg", "JPEG"), ("diagram.png", "PNG"), ("anim.gif", "GIF")):
            result = self.upload(filename, image_format).json()
            record = self.repository.records[result["db_id"]]
            saved = self.images_dir / record.filename
            self.assertEqual(Path(result["url"]).name, record.filename)
            self.assertEqual(record.original_name, filename)
            self.assertEqual(record.size, saved.stat().st_size)
            self.assertEqual(record.file_type, Path(filename).suffix[1:])

    def test_upload_does_not_keep_file_when_database_fails(self):
        self.repository.failing = True
        response = self.upload()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(any(self.images_dir.iterdir()))

        self.repository.failing = False
        self.repository.fail_after_write = True
        html_response = self.upload(headers={"Accept": "text/html"})
        self.assertEqual(html_response.status_code, 503)
        self.assertIn("База данных недоступна", html_response.text)
        self.assertFalse(any(self.images_dir.iterdir()))
        self.assertEqual(self.repository.records, {})
        self.assertIn("Ошибка: метаданные не сохранены, изображение photo.png не загружено", self.log_text())

    def test_upload_rejects_missing_or_empty_file(self):
        self.assertEqual(self.client.post("/upload").status_code, 400)
        response = self.client.post("/upload", files={"file": ("empty.png", b"", "image/png")})
        self.assertEqual(response.status_code, 400)

    def test_upload_rejects_unsupported_extension(self):
        response = self.client.post(
            "/upload",
            files={"file": ("notes.txt", self.make_image("PNG"), "text/plain")},
        )
        self.assertEqual(response.status_code, 400)

    def test_upload_rejects_extension_content_mismatch(self):
        response = self.client.post(
            "/upload",
            files={"file": ("photo.jpg", self.make_image("PNG"), "image/jpeg")},
        )
        self.assertEqual(response.status_code, 400)

    def test_upload_rejects_corrupt_image(self):
        response = self.client.post(
            "/upload",
            files={"file": ("broken.png", b"not an image", "image/png")},
        )
        self.assertEqual(response.status_code, 400)

    def test_upload_rejects_file_larger_than_five_mib(self):
        response = self.client.post(
            "/upload",
            files={"file": ("large.png", b"x" * (MAX_FILE_SIZE + 1), "image/png")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(any(self.images_dir.iterdir()))
        self.assertEqual(self.repository.records, {})

    def test_ten_concurrent_uploads_succeed(self):
        image = self.make_image("PNG")

        async def upload_all():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await asyncio.gather(*(
                    client.post("/upload", files={"file": ("photo.png", image, "image/png")})
                    for _ in range(10)
                ))

        responses = asyncio.run(upload_all())
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(len({response.json()["id"] for response in responses}), 10)
        self.assertEqual(len(list(self.images_dir.iterdir())), 10)
        self.assertEqual(len(self.repository.records), 10)

    def test_images_list_shows_metadata(self):
        result = self.upload("my photo.png").json()
        record = self.repository.records[result["db_id"]]
        page = self.client.get("/images-list")
        self.assertEqual(page.status_code, 200)
        self.assertIn(f"href='http://testserver{result['url']}'", page.text)
        self.assertIn(f">{record.filename}</a>", page.text)
        self.assertIn("my photo.png", page.text)
        self.assertIn(f"{record.size / 1024:.1f}", page.text)
        self.assertIn(f"{record.upload_time:%Y-%m-%d %H:%M:%S}", page.text)
        self.assertIn("<td>png</td>", page.text)
        self.assertIn(f"action='/delete/{record.id}?page=1'", page.text)
        self.assertIn(">Удалить</button>", page.text)

    def test_images_list_escapes_original_name(self):
        self.upload("<i>cat&dog.png")
        page = self.client.get("/images-list").text
        self.assertNotIn("<i>", page)
        self.assertIn("&lt;i&gt;cat&amp;dog.png", page)

    def test_images_list_empty_message(self):
        page = self.client.get("/images-list")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Нет загруженных изображений", page.text)
        self.assertNotIn("<table", page.text)
        self.assertNotIn("pagination", page.text)

    def test_images_list_pagination(self):
        for index in range(25):
            self.upload(f"img{index:02}.png")

        first = self.client.get("/images-list").text
        self.assertEqual(first.count("<tr>"), 11)
        self.assertIn("img24.png", first)
        self.assertNotIn("img14.png", first)
        self.assertIn("Страница 1 из 3", first)
        self.assertRegex(first, r"pagination__btn--disabled' aria-disabled='true'>Предыдущая страница")
        self.assertIn("href='/images-list?page=2'>Следующая страница", first)

        second = self.client.get("/images-list?page=2").text
        self.assertIn("img14.png", second)
        self.assertNotIn("img15.png", second)
        self.assertIn("href='/images-list'>Предыдущая страница", second)
        self.assertIn("href='/images-list?page=3'>Следующая страница", second)

        last = self.client.get("/images-list?page=3").text
        self.assertEqual(last.count("<tr>"), 6)
        self.assertIn("img00.png", last)
        self.assertRegex(last, r"pagination__btn--disabled' aria-disabled='true'>Следующая страница")

        beyond = self.client.get("/images-list?page=99", follow_redirects=False)
        self.assertEqual(beyond.status_code, 303)
        self.assertEqual(beyond.headers["location"], "/images-list?page=3")
        for bad_page in ("abc", "0", "-5"):
            self.assertIn("Страница 1 из 3", self.client.get(f"/images-list?page={bad_page}").text)

    def test_images_list_single_page_has_both_buttons_disabled(self):
        for _ in range(10):
            self.upload()
        page = self.client.get("/images-list").text
        self.assertIn("Страница 1 из 1", page)
        self.assertEqual(page.count("pagination__btn--disabled"), 2)

    def test_images_list_reports_database_error(self):
        self.repository.failing = True
        page = self.client.get("/images-list")
        self.assertEqual(page.status_code, 503)
        self.assertIn("База данных недоступна", page.text)
        self.assertIn("Ошибка: не удалось получить список изображений (connection refused).", self.log_text())

    def test_old_catalog_redirects_to_images_list(self):
        response = self.client.get("/images/", follow_redirects=False)
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response.headers["location"], "/images-list")

    def test_pages_use_template_assets(self):
        for path in ("/", "/upload", "/images-list"):
            page = self.client.get(path).text
            self.assertIn("/static/css/style.css", page)
            self.assertNotIn("<script", page)
        self.assertEqual(self.client.get("/static/css/style.css").status_code, 200)
        self.assertIn("/images-list", self.client.get("/").text)
        self.assertIn("/images-list", self.client.get("/upload").text)

    def test_json_upload_returns_full_url(self):
        result = self.upload("photo.gif", "GIF").json()
        self.assertEqual(result["full_url"], f"http://testserver{result['url']}")

    def test_browser_form_gets_html_page_with_link_or_error(self):
        html_headers = {"Accept": "text/html"}
        success = self.upload(headers=html_headers)
        self.assertEqual(success.status_code, 200)
        self.assertIn("Изображение загружено", success.text)
        self.assertRegex(success.text, r"value=\"http://testserver/images/[0-9a-f]{32}\.png\"")

        failure = self.client.post(
            "/upload",
            files={"file": ("notes.txt", b"text", "text/plain")},
            headers=html_headers,
        )
        self.assertEqual(failure.status_code, 400)
        self.assertIn("upload__message--error", failure.text)
        self.assertIn("Поддерживаются только форматы JPG, PNG и GIF", failure.text)

    def test_delete_image(self):
        result = self.upload().json()
        image_id = result["db_id"]
        name = Path(result["url"]).name

        deleted = self.client.post(f"/delete/{image_id}", follow_redirects=False)
        self.assertEqual(deleted.status_code, 303)
        self.assertEqual(deleted.headers["location"], "/images-list")
        self.assertFalse((self.images_dir / name).exists())
        self.assertNotIn(image_id, self.repository.records)
        self.assertIn(f"Успех: изображение {name} (id={image_id}) удалено.", self.log_text())
        self.assertIn("Нет загруженных изображений", self.client.get("/images-list").text)

    def test_delete_returns_to_same_page(self):
        for _ in range(11):
            self.upload()
        oldest = min(self.repository.records)
        deleted = self.client.post(f"/delete/{oldest}?page=2", follow_redirects=False)
        self.assertEqual(deleted.headers["location"], "/images-list?page=2")
        # На второй странице больше ничего нет — список переводит на последнюю
        page = self.client.get("/images-list?page=2", follow_redirects=False)
        self.assertEqual(page.headers["location"], "/images-list")

    def test_delete_unknown_id(self):
        response = self.client.post("/delete/999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Изображение не найдено"})

        html_response = self.client.post("/delete/999", headers={"Accept": "text/html"})
        self.assertEqual(html_response.status_code, 404)
        self.assertIn("Изображение не найдено", html_response.text)
        self.assertIn("Ошибка: изображение с id=999 для удаления не найдено.", self.log_text())

        self.assertEqual(self.client.post("/delete/abc").status_code, 422)

    def test_delete_when_file_is_missing_on_disk(self):
        result = self.upload().json()
        name = Path(result["url"]).name
        (self.images_dir / name).unlink()

        deleted = self.client.post(f"/delete/{result['db_id']}", follow_redirects=False)
        self.assertEqual(deleted.status_code, 303)
        self.assertEqual(self.repository.records, {})
        self.assertIn(f"Ошибка: файл {name} отсутствует на диске (запись id={result['db_id']} удалена).", self.log_text())

    def test_delete_never_touches_files_outside_images(self):
        secret = self.root / "secret.txt"
        secret.write_text("data")
        self.repository.records[1] = ImageRecord(1, "../secret.txt", "x.png", 1, datetime.now(), "png")

        self.client.post("/delete/1", follow_redirects=False)
        self.assertTrue(secret.exists())
        self.assertIn("Ошибка: некорректное имя файла в базе", self.log_text())

    def test_delete_reports_database_error(self):
        self.repository.failing = True
        self.assertEqual(self.client.post("/delete/1").status_code, 503)

    def test_upload_actions_are_logged(self):
        self.upload()
        self.client.post(
            "/upload",
            files={"file": ("notes.txt", b"not an image", "text/plain")},
        )
        log_text = self.log_text()
        self.assertRegex(log_text, r"Успех: изображение [0-9a-f]{32}\.png \(id=1, photo\.png, [\d.]+ КБ\) загружено\.")
        self.assertIn("Ошибка:", log_text)

    def test_startup_checks_database_connection(self):
        with TestClient(self.app):
            pass
        self.assertIn("Успех: подключение к базе данных установлено.", self.log_text())

        self.repository.failing = True
        with TestClient(self.app):
            pass
        self.assertIn("Ошибка: нет подключения к базе данных (connection refused).", self.log_text())

    def test_page_views_and_unknown_routes_are_logged(self):
        self.client.get("/")
        self.client.get("/upload")
        self.client.get("/images-list")
        self.assertEqual(self.client.get("/missing").status_code, 404)
        self.assertEqual(self.client.delete("/upload").status_code, 405)

        log_lines = self.log_text().splitlines()
        self.assertTrue(all(re.match(r"^\[[\d\- :]+\] ", line) for line in log_lines))
        log_text = "\n".join(log_lines)
        self.assertIn("Просмотр: открыта главная страница.", log_text)
        self.assertIn("Просмотр: открыта форма загрузки.", log_text)
        self.assertIn("Просмотр: открыт список изображений (страница 1 из 1).", log_text)
        self.assertIn("Ошибка: маршрут не найден (GET /missing).", log_text)
        self.assertIn("Ошибка: метод не поддерживается (DELETE /upload).", log_text)


if __name__ == "__main__":
    unittest.main()
