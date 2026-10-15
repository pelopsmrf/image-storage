document.addEventListener('DOMContentLoaded', () => {
    const MAX_SIZE_BYTES = 5 * 1024 * 1024;
    const ALLOWED_EXTENSIONS = ['.jpg', '.png', '.gif'];

    const form = document.getElementById('upload-form');
    const fileInput = document.getElementById('file-upload');
    const dropzone = document.querySelector('.upload__dropzone');
    const message = document.getElementById('upload-message');
    const currentUrlInput = document.getElementById('current-url');
    const copyButton = document.getElementById('copy-button');

    const showMessage = (text, type) => {
        message.textContent = text;
        message.classList.remove('upload__message--error', 'upload__message--success');
        if (type) {
            message.classList.add(`upload__message--${type}`);
        }
    };

    // Quick client-side check; the server validates the file again.
    const validate = (file) => {
        const name = file.name.toLowerCase();
        if (!ALLOWED_EXTENSIONS.some((extension) => name.endsWith(extension))) {
            return 'Поддерживаются только форматы JPG, PNG и GIF';
        }
        if (file.size > MAX_SIZE_BYTES) {
            return 'Размер файла превышает 5 МБ';
        }
        return null;
    };

    const uploadFile = async (file) => {
        const error = validate(file);
        if (error) {
            showMessage(`${error} (${file.name})`, 'error');
            return;
        }

        const data = new FormData();
        data.append('file', file);
        showMessage(`Загрузка ${file.name}…`);

        try {
            const response = await fetch('/upload', {
                method: 'POST',
                body: data,
                headers: { Accept: 'application/json' },
            });
            const result = await response.json().catch(() => ({}));
            if (!response.ok) {
                showMessage(`${result.detail || 'Не удалось загрузить файл'} (${file.name})`, 'error');
                return;
            }
            currentUrlInput.value = result.full_url;
            copyButton.disabled = false;
            showMessage(`Файл ${file.name} загружен`, 'success');
        } catch (networkError) {
            showMessage('Сервер недоступен, попробуйте ещё раз', 'error');
        }
    };

    form.addEventListener('submit', (event) => {
        event.preventDefault();
        if (fileInput.files.length > 0) {
            uploadFile(fileInput.files[0]);
        }
    });

    fileInput.addEventListener('change', () => {
        if (fileInput.files.length > 0) {
            uploadFile(fileInput.files[0]);
        }
        fileInput.value = '';
    });

    ['dragenter', 'dragover'].forEach((eventName) => {
        dropzone.addEventListener(eventName, (event) => {
            event.preventDefault();
            dropzone.classList.add('upload__dropzone--active');
        });
    });

    ['dragleave', 'drop'].forEach((eventName) => {
        dropzone.addEventListener(eventName, (event) => {
            event.preventDefault();
            dropzone.classList.remove('upload__dropzone--active');
        });
    });

    dropzone.addEventListener('drop', (event) => {
        const files = event.dataTransfer.files;
        if (files.length > 0) {
            uploadFile(files[0]);
        }
    });

    copyButton.addEventListener('click', async () => {
        const url = currentUrlInput.value;
        if (!url) {
            return;
        }
        try {
            await navigator.clipboard.writeText(url);
        } catch (clipboardError) {
            currentUrlInput.select();
            document.execCommand('copy');
        }
        copyButton.textContent = 'COPIED!';
        setTimeout(() => {
            copyButton.textContent = 'COPY';
        }, 2000);
    });
});
