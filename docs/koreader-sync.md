# KOReader

## Получение книг

В файловом менеджере KOReader откройте верхнее меню, выберите вкладку с лупой и пункт **OPDS catalog**. В меню каталога выберите **Add catalog**, укажите `https://ваш-сервер/opds/v1`, имя пользователя и персональный OPDS-токен из «Настройки → Доступ к OPDS». Введите токен в поле пароля. После подключения в меню «Полки» доступен пункт **Search**.

## Прогресс

1. Откройте «Настройки → KOReader Progress Sync» и выпустите отдельный пароль.
2. Укажите адрес с окончанием `/koreader` в KOReader через Progress Sync → Custom Sync Server. Войдите с именем пользователя библиотеки и выданным паролем.
3. Откройте тот же файл в KOReader и отправьте позицию. На странице книги выберите «Связать с KOReader» → «Найти по отпечатку файла», затем подтвердите найденный идентификатор кнопкой «Связать». Если в KOReader включено сопоставление по имени файла, скопируйте идентификатор из «Документы KOReader» и вставьте его вручную.
4. В веб-читалке нажмите «Перейти примерно» для EPUB или «Перейти к странице» для PDF, если позиция из KOReader свежее позиции браузера.

KOReader получает исходный XPointer EPUB и позиции PDF. Для связанного PDF сервер также может вернуть более свежий номер страницы из веб-читалки. Локатор EPUB CFI штатно не преобразуется в KOReader XPointer. Публичная регистрация отключена. Повторный выпуск пароля отзывает прежний пароль. [Формат запросов клиента](https://github.com/koreader/koreader/blob/v2026.07.1/plugins/kosync.koplugin/api.json) · [Расчёт отпечатка KOReader](https://github.com/koreader/koreader/blob/v2026.07.1/frontend/util.lua#L1034)

История сохраняет исходные позиции каждого устройства, даже до привязки документа к файлу. Её можно получить через API или включённый в настройки экспорт данных чтения. При обновлении с предыдущей версии в историю переносится последняя сохранённая позиция каждого документа. [Метод получения истории](api.md#koreader)

## Интеграционная проверка

Скрипт `scripts/check-koreader.py` проверяет сохранение и получение прогресса двумя запусками оригинального модуля KOSync 2026.07.1, сопоставление по отпечатку, сохранение исходной позиции в истории, изоляцию аккаунтов, возврат позиции PDF из «Полки» и отзыв пароля. Модуль работает внутри Docker без графической оболочки. [Официальный архив KOReader](https://github.com/koreader/koreader/releases/tag/v2026.07.1)

Отдельно проверен графический KOReader 2026.07.1 для Linux с виртуальным дисплеем Xvfb. Вход выполнен через **Progress sync → Register / Login → Login**. Первый клиент отправил страницу PDF через **Push progress from this device now**. После привязки по отпечатку клиент по команде **Pull progress from other devices now** открыл страницу, сохранённую через API «Полки». Второй запуск с отдельным профилем начал с первой страницы и получил третью страницу от первого клиента. Проверка аппаратного ридера остаётся открытой.

В том же клиенте проверен каталог OPDS 1.2. KOReader принял персональный OPDS-токен, нашёл EPUB по запросу из двух слов, скачал файл и открыл книгу через **Read now**. SHA-256 скачанного файла совпал с оригиналом. После отправки позиции из второй главы «Полка» сопоставила документ по отпечатку файла и сохранила исходный XPointer.

Чтобы повторить проверку, выполните команды из корня проекта в отдельном сеансе PowerShell. Тестовый проект использует отдельную базу. При первом запуске задайте пароль тестового администратора длиной от десяти символов. При повторном запуске используйте тот же пароль. Скрипт сверяет SHA-256 архива перед запуском клиента.

```powershell
$env:APP_PORT = '18082'
$env:APP_URL = 'http://localhost:18082'
$smokeCredential = Get-Credential -UserName 'koreader-qa' -Message 'Тестовый администратор KOReader'
$env:POLKA_SMOKE_ADMIN = $smokeCredential.UserName
$env:POLKA_SMOKE_PASSWORD = $smokeCredential.GetNetworkCredential().Password
$fixtures = Join-Path $PWD.Path 'local-data/koreader-check'
$checks = Join-Path $PWD.Path 'scripts'
New-Item -ItemType Directory -Force -Path $fixtures | Out-Null
$archive = 'koreader-linux-x86_64-v2026.07.1.tar.xz'
curl.exe --fail --location --output (Join-Path $fixtures $archive) "https://github.com/koreader/koreader/releases/download/v2026.07.1/$archive"
docker compose -p polka-kosync up -d --wait app
docker run --rm --network polka-kosync_default --mount "type=bind,src=$fixtures,dst=/fixture,readonly" --mount "type=bind,src=$checks,dst=/checks,readonly" --env POLKA_SMOKE_ADMIN --env POLKA_SMOKE_PASSWORD ghcr.io/benya9669/polka:latest python /checks/check-koreader.py "/fixture/$archive"
docker compose -p polka-kosync stop
Remove-Item Env:POLKA_SMOKE_PASSWORD
```

