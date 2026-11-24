# Резервное копирование и восстановление

Копируйте PostgreSQL и том `data` в одном окне обслуживания. На время копирования остановите приложение и обработчик задач. PostgreSQL оставьте включённым.

Команды копирования рассчитаны на штатную службу `db` в Compose. Если `DATABASE_URL` указывает на внешнюю PostgreSQL, снимите дамп базы, к которой подключена «Полка». Для восстановления очереди достаточно PostgreSQL. Redis хранит только временный сигнал обработчику.

Храните дамп и архив в закрытой папке. Том данных содержит ключ шифрования учётных данных внешних OPDS-источников. Сохраните параметры развёртывания из `.env` отдельно в защищённом месте. Проиндексированные оригиналы находятся за пределами тома `data`, их копирует владелец исходного хранилища. Команды исключают временные прямые загрузки OPDS при стандартном `LIBRARY_PATH=/data`. При другом расположении папки данных скорректируйте путь исключения `opds-downloads`.

Если `LIBRARY_PATH`, `ORIGINALS_PATH` или `EXPORT_PATH` указывают на дополнительные тома за пределами `/data`, скопируйте эти тома в то же окно обслуживания и восстановите их до запуска приложения.

## Копирование на Windows

Команды выполняются в PowerShell из корня проекта. Бинарный дамп копируется через Docker.

```powershell
function Invoke-PolkaCompose {
    docker compose @args
    if ($LASTEXITCODE -ne 0) { throw 'Команда Docker Compose завершилась ошибкой.' }
}

$backup = Join-Path $PWD.Path ('backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backup | Out-Null
$dump = '/tmp/polka-backup.pgdump'
try {
    Invoke-PolkaCompose stop app worker
    Invoke-PolkaCompose exec -T db pg_dump -U ebook -d ebook -Fc -f $dump
    Invoke-PolkaCompose cp "db:$dump" (Join-Path $backup 'polka.pgdump')
    Invoke-PolkaCompose exec -T db rm -f $dump
    Invoke-PolkaCompose run -T --rm --no-deps -v "${backup}:/backup" app tar -C /data --exclude=./opds-downloads -czf /backup/polka-data.tar.gz .
} finally {
    Invoke-PolkaCompose up -d --no-build --wait
}
```

## Копирование на Debian

```sh
(
    set -eu
    backup="$(mktemp -d "$PWD/backup.XXXXXXXX")"
    dump=/tmp/polka-backup.pgdump
    trap 'docker compose up -d --no-build --wait' EXIT
    docker compose stop app worker
    docker compose exec -T db pg_dump -U ebook -d ebook -Fc -f "$dump"
    docker compose cp "db:$dump" "$backup/polka.pgdump"
    docker compose exec -T db rm -f "$dump"
    docker compose run -T --rm --no-deps -v "$backup:/backup" app tar -C /data --exclude=./opds-downloads -czf /backup/polka-data.tar.gz .
    printf '%s\n' "$backup"
)
```

## Восстановление в отдельный экземпляр

Используйте пустой проект `polka-restore` и оба файла из одной копии. Приведённые команды используют стандартные имена базы и роли `ebook`. Подставьте путь к папке с дампом. Задайте `BOOKS_INBOX` с путём к восстановленной папке проиндексированных оригиналов. Compose подключит её в режиме только для чтения.

Откройте отдельное окно PowerShell и скопируйте определение функции `Invoke-PolkaCompose` из примера выше. Восстановленный экземпляр будет подключаться к своей службе `db`.

```powershell
$env:APP_BIND = '127.0.0.1'
$env:APP_PORT = '18084'
$env:APP_URL = 'http://localhost:18084'
$env:POSTGRES_PASSWORD = [Guid]::NewGuid().ToString('N')
$env:DATABASE_URL = "postgresql+psycopg://ebook:${env:POSTGRES_PASSWORD}@db/ebook"
$backup = 'C:\Backups\polka'
$dump = '/tmp/polka-restore.pgdump'
Invoke-PolkaCompose -p polka-restore up -d --wait db
$tables = Invoke-PolkaCompose -p polka-restore exec -T db psql -U ebook -d ebook -Atc "SELECT count(*) FROM pg_tables WHERE schemaname='public'"
if ($tables -ne '0') { throw 'Целевая база содержит таблицы. Выберите чистый проект.' }
Invoke-PolkaCompose -p polka-restore run -T --rm --no-deps --user 0:0 -v "${backup}:/backup:ro" app sh -ec 'test -z "$(ls -A /data)" && test -s /backup/polka-data.tar.gz'
Invoke-PolkaCompose -p polka-restore cp (Join-Path $backup 'polka.pgdump') "db:$dump"
Invoke-PolkaCompose -p polka-restore exec -T db pg_restore -U ebook -d ebook --single-transaction --exit-on-error $dump
Invoke-PolkaCompose -p polka-restore exec -T db rm -f $dump
Invoke-PolkaCompose -p polka-restore run -T --rm --no-deps --user 0:0 -v "${backup}:/backup:ro" app sh -ec 'test -z "$(ls -A /data)" && tar -C /data -xzf /backup/polka-data.tar.gz --no-same-owner && chown -R 10001:10001 /data'
Invoke-PolkaCompose -p polka-restore up -d --no-build --wait
```

На Debian выполните команды в отдельном сеансе Bash.

```sh
(
    set -eu
    export APP_BIND=127.0.0.1 APP_PORT=18084 APP_URL=http://localhost:18084
    POSTGRES_PASSWORD="$(od -An -N24 -tx1 /dev/urandom | tr -d '[:space:]')"
    export POSTGRES_PASSWORD DATABASE_URL="postgresql+psycopg://ebook:$POSTGRES_PASSWORD@db/ebook"
    backup=/srv/backups/polka
    dump=/tmp/polka-restore.pgdump
    docker compose -p polka-restore up -d --wait db
    tables="$(docker compose -p polka-restore exec -T db psql -U ebook -d ebook -Atc "SELECT count(*) FROM pg_tables WHERE schemaname='public'")"
    test "$tables" = 0
    docker compose -p polka-restore run -T --rm --no-deps --user 0:0 -v "$backup:/backup:ro" app sh -ec 'test -z "$(ls -A /data)" && test -s /backup/polka-data.tar.gz'
    docker compose -p polka-restore cp "$backup/polka.pgdump" "db:$dump"
    docker compose -p polka-restore exec -T db pg_restore -U ebook -d ebook --single-transaction --exit-on-error "$dump"
    docker compose -p polka-restore exec -T db rm -f "$dump"
    docker compose -p polka-restore run -T --rm --no-deps --user 0:0 -v "$backup:/backup:ro" app sh -ec 'test -z "$(ls -A /data)" && tar -C /data -xzf /backup/polka-data.tar.gz --no-same-owner && chown -R 10001:10001 /data'
    docker compose -p polka-restore up -d --no-build --wait
)
```

Проверьте [восстановленный экземпляр](http://localhost:18084) перед переключением клиентов. Войдите своей учётной записью, откройте книги из управляемой и индексируемой библиотек, проверьте позицию, обложки, закладки и доступ по OPDS. Временные файлы из очереди придётся скачать заново.
