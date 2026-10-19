CREATE TABLE IF NOT EXISTS images (
    id SERIAL PRIMARY KEY,                              -- уникальный идентификатор записи
    filename TEXT NOT NULL,                             -- имя файла, сгенерированное сервером
    original_name TEXT NOT NULL,                        -- имя файла от пользователя
    size INTEGER NOT NULL,                              -- размер файла в байтах
    upload_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,    -- время загрузки
    file_type TEXT NOT NULL                             -- формат файла: jpg, png, gif
);

-- Список изображений сортируется по дате загрузки
CREATE INDEX IF NOT EXISTS images_upload_time_idx ON images (upload_time DESC);
