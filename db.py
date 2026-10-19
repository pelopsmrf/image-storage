from __future__ import annotations

import os
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

import psycopg2
from psycopg2.extensions import cursor as Cursor
from psycopg2.pool import ThreadedConnectionPool

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS images (
    id SERIAL PRIMARY KEY,
    filename TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size INTEGER NOT NULL,
    upload_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    file_type TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS images_upload_time_idx ON images (upload_time DESC);
"""


class DatabaseError(Exception):
    """Нет подключения к PostgreSQL или запрос завершился ошибкой."""

    @classmethod
    def from_psycopg(cls, error: psycopg2.Error) -> DatabaseError:
        # Сообщения libpq многострочные, а в app.log каждая запись — одна строка
        return cls(" ".join(str(error).split()))


@dataclass(frozen=True)
class ImageRecord:
    id: int
    filename: str
    original_name: str
    size: int
    upload_time: datetime
    file_type: str


def connection_params_from_env() -> dict[str, str | int]:
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "dbname": os.getenv("DB_NAME", "images_db"),
        "user": os.getenv("DB_USER", "postgres"),
        "password": os.getenv("DB_PASSWORD", "password"),
        "connect_timeout": 3,
    }


class ImageRepository:
    """Метаданные изображений в таблице images.

    Методы синхронные: приложение вызывает их через run_in_threadpool.
    """

    def __init__(self, params: dict[str, str | int] | None = None, max_connections: int = 10) -> None:
        self._params = params or connection_params_from_env()
        self._max_connections = max_connections
        self._pool: ThreadedConnectionPool | None = None
        self._pool_lock = threading.Lock()
        # Пул бросает PoolError, если соединений не хватает, поэтому лишние потоки ждут здесь
        self._slots = threading.BoundedSemaphore(max_connections)

    def _get_pool(self) -> ThreadedConnectionPool:
        # Пул создаётся при первом запросе, чтобы приложение запускалось и без базы
        with self._pool_lock:
            if self._pool is None:
                self._pool = ThreadedConnectionPool(1, self._max_connections, **self._params)
            return self._pool

    @contextmanager
    def _transaction(self) -> Generator[Cursor, None, None]:
        with self._slots:
            try:
                pool = self._get_pool()
                connection = pool.getconn()
            except psycopg2.Error as error:
                raise DatabaseError.from_psycopg(error) from error

            try:
                with connection.cursor() as cursor:
                    yield cursor
                connection.commit()
            except BaseException as error:
                if not connection.closed:
                    try:
                        connection.rollback()
                    except psycopg2.Error:
                        pass
                if isinstance(error, psycopg2.Error):
                    raise DatabaseError.from_psycopg(error) from error
                raise
            finally:
                # Оборванное соединение не возвращаем в пул
                pool.putconn(connection, close=bool(connection.closed))

    def ensure_schema(self) -> None:
        with self._transaction() as cursor:
            cursor.execute(SCHEMA_SQL)

    def add(
        self,
        filename: str,
        original_name: str,
        size: int,
        file_type: str,
        write_file: Callable[[], None],
    ) -> int:
        # Файл пишется внутри транзакции: если запись на диск не удалась, строка в таблице не появится
        with self._transaction() as cursor:
            cursor.execute(
                "INSERT INTO images (filename, original_name, size, file_type) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (filename, original_name, size, file_type),
            )
            image_id = cursor.fetchone()[0]
            write_file()
        return image_id

    def count(self) -> int:
        with self._transaction() as cursor:
            cursor.execute("SELECT COUNT(*) FROM images")
            return cursor.fetchone()[0]

    def list_page(self, limit: int, offset: int) -> list[ImageRecord]:
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT id, filename, original_name, size, upload_time, file_type FROM images "
                "ORDER BY upload_time DESC, id DESC LIMIT %s OFFSET %s",
                (limit, offset),
            )
            return [ImageRecord(*row) for row in cursor.fetchall()]

    def delete(self, image_id: int, remove_file: Callable[[str], None]) -> str | None:
        with self._transaction() as cursor:
            cursor.execute("DELETE FROM images WHERE id = %s RETURNING filename", (image_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            remove_file(row[0])
        return row[0]

    def close(self) -> None:
        with self._pool_lock:
            if self._pool is not None:
                self._pool.closeall()
                self._pool = None
