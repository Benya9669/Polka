# Установка

## Windows

Для локального запуска установите WSL 2 и Docker Desktop. В PowerShell от администратора:

```powershell
wsl --install --no-distribution
```

После перезагрузки, если она потребовалась:

```powershell
wsl --update
winget install --exact --id Docker.DockerDesktop --source winget
```

Запустите Docker Desktop и проверьте `docker version`. Сборка приложения скачивает образы и зависимости. Для работы локального каталога после сборки доступ к сторонним сервисам не нужен.

```powershell
docker compose up -d --build --wait
docker compose ps
```

## Debian 13

Установите Docker Engine и Compose plugin из официального репозитория Docker по инструкции для Debian. Если они уже обслуживают Nextcloud AIO, используйте установленный Docker. Скопируйте папку проекта, например в `/opt/polka`. [Документация Docker для Debian](https://docs.docker.com/engine/install/debian/)

```sh
cd /opt/polka
mkdir -p inbox
(
  umask 077
  set -euC
  db_password=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  setup_key=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
  {
    cat .env.example
    printf '\nPOSTGRES_PASSWORD=%s\n' "$db_password"
    printf 'DATABASE_URL=postgresql+psycopg://ebook:%s@db/ebook\n' "$db_password"
    printf 'SETUP_KEY=%s\n' "$setup_key"
  } > .env
)
docker compose config --quiet
docker compose up -d --build --wait
docker compose ps
```

Блок создания `.env` предназначен для новой установки. Если файл уже существует, оболочка сохранит его и сообщит об ошибке. Для первоначальной настройки откройте `.env` локально и возьмите значение `SETUP_KEY` для мастера. Тома создаются в Compose-проекте `polka`. Перед публикацией выставьте внешний `APP_URL` и настройки NPMPlus. [Конфигурация](configuration.md)

Диагностика:

```sh
docker compose logs --tail 100 app worker db redis
curl -fsS http://127.0.0.1:8080/healthz
```

