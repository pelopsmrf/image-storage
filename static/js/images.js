document.addEventListener('DOMContentLoaded', () => {
    const wrapper = document.getElementById('file-list-wrapper');

    wrapper.addEventListener('click', async (event) => {
        const button = event.target.closest('.delete-btn');
        if (!button) {
            return;
        }

        const name = button.dataset.name;
        if (!window.confirm(`Удалить изображение ${name}?`)) {
            return;
        }

        button.disabled = true;
        try {
            const response = await fetch(`/api/images/${encodeURIComponent(name)}`, { method: 'DELETE' });
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            button.closest('.file-list-item').remove();
            if (!wrapper.querySelector('.file-list-item')) {
                wrapper.innerHTML = '<p class="upload__promt file-list-empty">Пока нет загруженных изображений.</p>';
            }
        } catch (error) {
            button.disabled = false;
            window.alert('Не удалось удалить изображение');
        }
    });
});
