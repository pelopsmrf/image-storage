import asyncio
import io
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from PIL import Image

from app import MAX_FILE_SIZE, create_app


class ImageStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.images_dir = self.root / "images"
        self.logs_dir = self.root / "logs"
        self.app = create_app(self.images_dir, self.logs_dir)
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
            response = self.client.post(
                "/upload",
                files={"file": (filename, self.make_image(image_format), "image/*")},
            )
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result["url"], f"/images/{result['id']}{Path(filename).suffix}")
            saved_names.append(Path(result["url"]).name)
            self.assertTrue((self.images_dir / saved_names[-1]).is_file())

        self.assertEqual(len(saved_names), len(set(saved_names)))

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

    def test_catalog_lists_uploaded_image(self):
        response = self.client.post(
            "/upload",
            files={"file": ("photo.png", self.make_image("PNG"), "image/png")},
        )
        image_url = response.json()["url"]
        catalog = self.client.get("/images/")
        self.assertEqual(catalog.status_code, 200)
        self.assertIn(image_url, catalog.text)

    def test_pages_use_template_assets(self):
        for path in ("/", "/upload", "/images/"):
            page = self.client.get(path).text
            self.assertIn("/static/css/style.css", page)
            self.assertNotIn("<script", page)
        for asset in ("/static/css/style.css", "/static/img/icon/delete.png"):
            self.assertEqual(self.client.get(asset).status_code, 200, asset)
        self.assertIn("/images/", self.client.get("/").text)

    def test_json_upload_returns_full_url(self):
        response = self.client.post(
            "/upload",
            files={"file": ("photo.gif", self.make_image("GIF"), "image/gif")},
        )
        result = response.json()
        self.assertEqual(result["full_url"], f"http://testserver{result['url']}")

    def test_browser_form_gets_html_page_with_link_or_error(self):
        html_headers = {"Accept": "text/html"}
        success = self.client.post(
            "/upload",
            files={"file": ("photo.png", self.make_image("PNG"), "image/png")},
            headers=html_headers,
        )
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
        response = self.client.post(
            "/upload",
            files={"file": ("photo.png", self.make_image("PNG"), "image/png")},
        )
        name = Path(response.json()["url"]).name
        self.assertIn(f"action='/delete/{name}'", self.client.get("/images/").text)

        deleted = self.client.post(f"/delete/{name}", follow_redirects=False)
        self.assertEqual(deleted.status_code, 303)
        self.assertEqual(deleted.headers["location"], "/images/")
        self.assertFalse((self.images_dir / name).exists())
        self.assertEqual(self.client.post(f"/delete/{name}").status_code, 404)
        self.assertEqual(self.client.post("/delete/app.log").status_code, 400)
        self.assertEqual(self.client.post("/delete/..%2Flogs%2Fapp.log").status_code, 404)

        log_text = (self.logs_dir / "app.log").read_text(encoding="utf-8")
        self.assertIn(f"Успех: изображение {name} удалено.", log_text)

    def test_upload_actions_are_logged(self):
        self.client.post(
            "/upload",
            files={"file": ("photo.png", self.make_image("PNG"), "image/png")},
        )
        self.client.post(
            "/upload",
            files={"file": ("notes.txt", b"not an image", "text/plain")},
        )
        log_text = (self.logs_dir / "app.log").read_text(encoding="utf-8")
        self.assertIn("Успех: изображение", log_text)
        self.assertIn("Ошибка:", log_text)

    def test_page_views_and_unknown_routes_are_logged(self):
        self.client.get("/")
        self.client.get("/upload")
        self.client.get("/images/")
        self.assertEqual(self.client.get("/missing").status_code, 404)
        self.assertEqual(self.client.delete("/upload").status_code, 405)

        log_lines = (self.logs_dir / "app.log").read_text(encoding="utf-8").splitlines()
        self.assertTrue(all(line.startswith("[") and "] " in line for line in log_lines))
        log_text = "\n".join(log_lines)
        self.assertIn("Просмотр: открыта главная страница.", log_text)
        self.assertIn("Просмотр: открыта форма загрузки.", log_text)
        self.assertIn("Просмотр: открыт каталог изображений (0 шт.).", log_text)
        self.assertIn("Ошибка: маршрут не найден (GET /missing).", log_text)
        self.assertIn("Ошибка: метод не поддерживается (DELETE /upload).", log_text)


if __name__ == "__main__":
    unittest.main()
