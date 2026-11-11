"""
Configurações do DFT as Code.

Tudo que muda entre ambientes vem de variáveis de ambiente (ver .env.example).
Sem DATABASE_URL, usa SQLite local (desenvolvimento e testes).
"""

import os
from pathlib import Path
from urllib.parse import urlparse, unquote

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def database_from_url(url: str) -> dict:
    """Converte postgres://user:pass@host:port/db (ou sqlite:///caminho) no dict do Django."""
    p = urlparse(url)
    if p.scheme in {"postgres", "postgresql"}:
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": p.path.lstrip("/"),
            "USER": unquote(p.username or ""),
            "PASSWORD": unquote(p.password or ""),
            "HOST": p.hostname or "localhost",
            "PORT": str(p.port or 5432),
            "CONN_MAX_AGE": 60,
        }
    if p.scheme == "sqlite":
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": p.path or BASE_DIR / "db.sqlite3"}
    raise ValueError(f"DATABASE_URL com esquema não suportado: {p.scheme}")


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-troque-em-producao")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "*")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "jobs",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # serve os estáticos do admin no container
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "dft_backend.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "dft_backend.wsgi.application"

DATABASES = {
    "default": database_from_url(os.environ["DATABASE_URL"])
    if os.environ.get("DATABASE_URL")
    else {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Uploads grandes (netlists, arquivos .tar.gz de RTL)
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# ---------------------------------------------------------------- Celery / fila
CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_EAGER_PROPAGATES = False
CELERY_TASK_ACKS_LATE = True            # se o worker morrer, o job volta para a fila
CELERY_WORKER_PREFETCH_MULTIPLIER = 1   # jobs longos: cada worker pega um por vez
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_DEFAULT_QUEUE = "dft"
CELERY_TASK_IGNORE_RESULT = True        # o estado do job vive no banco, não no Redis
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Com acks_late, o Redis reentrega tarefas não confirmadas após este tempo.
# Precisa ser maior que o job mais longo, senão um job em execução seria duplicado.
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 12 * 3600}

# ---------------------------------------------------------------- DFT
# Raiz dos dados compartilhados entre API e workers (uploads, áreas de trabalho, artefatos)
DFT_DATA_DIR = Path(os.environ.get("DFT_DATA_DIR", BASE_DIR / "data"))
# Bibliotecas de células disponíveis (cada subpasta = uma tecnologia)
DFT_TECH_DIR = Path(os.environ.get("DFT_TECH_DIR", BASE_DIR / "tech"))
# Contorno do "Illegal instruction" do fault atpg 0.9.4 com várias threads
DFT_FAULT_THREADS = int(os.environ.get("FAULT_THREADS", "1"))
# Executáveis (podem ser sobrescritos para apontar para versões específicas)
DFT_FAULT_BIN = os.environ.get("DFT_FAULT_BIN", "fault")
DFT_NL2BENCH_BIN = os.environ.get("DFT_NL2BENCH_BIN", "nl2bench")
DFT_YOSYS_BIN = os.environ.get("DFT_YOSYS_BIN", "yosys")
DFT_GIT_BIN = os.environ.get("DFT_GIT_BIN", "git")
# Tempo máximo padrão de um job (pode ser reduzido por job no dft.yaml)
DFT_JOB_TIMEOUT_MIN = int(os.environ.get("DFT_JOB_TIMEOUT_MIN", "120"))
# Limite padrão de regressão em pontos percentuais (dft.yaml tem prioridade)
DFT_DEFAULT_MAX_DROP_PP = float(os.environ.get("DFT_DEFAULT_MAX_DROP_PP", "0.5"))

# Autenticação: tokens aceitos no header "Authorization: Bearer <token>".
# Vazio = API aberta (só para desenvolvimento local).
DFT_API_TOKENS = env_list("DFT_API_TOKENS")

# Notificações (opcionais)
DFT_WEBHOOK_URL = os.environ.get("DFT_WEBHOOK_URL", "")         # Slack/Discord/Teams compatível
DFT_GITHUB_TOKEN = os.environ.get("DFT_GITHUB_TOKEN", "")       # publica commit status no GitHub
DFT_GITHUB_API = os.environ.get("DFT_GITHUB_API", "https://api.github.com")
DFT_PUBLIC_URL = os.environ.get("DFT_PUBLIC_URL", "http://localhost:8000")
DFT_GIT_TOKEN = os.environ.get("DFT_GIT_TOKEN", "")             # clonar repositórios privados

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
