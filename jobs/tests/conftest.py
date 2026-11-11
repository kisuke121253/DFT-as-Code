import pytest


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    """Cada teste usa uma pasta de dados própria e roda a fila de forma síncrona."""
    from dft_backend.celery import app

    settings.DFT_DATA_DIR = tmp_path / "data"
    settings.DFT_API_TOKENS = []
    settings.DFT_WEBHOOK_URL = ""
    settings.DFT_GITHUB_TOKEN = ""
    settings.CELERY_TASK_ALWAYS_EAGER = True
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = True
    yield
    app.conf.task_always_eager = False
