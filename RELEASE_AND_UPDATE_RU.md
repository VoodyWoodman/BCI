# Выпуск и обновления BCI

## Модель распространения

- Основной пользовательский пакет — `BCI_Setup_<version>_x64.exe` (Inno Setup, x64, Windows 10/11).
- Дополнительный пакет — `BCI_Portable_<version>.zip`; portable-копия не выполняет установку обновлений поверх себя.
- Установщик размещает приложение в выбранной папке (по умолчанию `%ProgramFiles%\BCI`), просит UAC и включает PyInstaller runtime. Python для пользователя не нужен. В комплекте находится Microsoft Visual C++ Redistributable; перед запуском Setup сравнивает его версию с установленной x64-версией, не запускает пакет поверх более новой и останавливается с понятным требованием перезагрузки, если runtime сообщает код 3010.
- Данные приложения и моделей находятся в `%LOCALAPPDATA%\BCI`; обновление и удаление приложения не удаляют эту папку.

## Каналы

- `Stable`: только GitHub Releases с `prerelease=false`.
- `Preview`: stable и prerelease; выбирается самая высокая SemVer-версия из опубликованных релизов.
- Релизные теги имеют формат `vMAJOR.MINOR.PATCH` или `vMAJOR.MINOR.PATCH-alpha[.N]`.
- Версия в теге обязана совпадать с `APP_VERSION` в `bci_core.py`.

## Что публикует Windows workflow

GitHub Actions `.github/workflows/windows-installer.yml` запускает тесты на Windows x64/Python 3.12, собирает PyInstaller onedir folder и Inno Setup installer, затем прикладывает:

- `BCI_Setup_<version>_x64.exe`
- `BCI_Portable_<version>.zip`
- `release-manifest.json`
- `SHA256SUMS.txt`

Push в `build/bci-update-system` и ручной запуск workflow создают Actions Artifact, но не публикуют релиз. Push тега `v*` запускает публикацию GitHub Release. Prerelease определяется по наличию дефиса в теге.

## Поведение Update Manager

1. При старте установленной программы выполняется фоновая проверка, если последняя успешная проверка старше 24 часов. Автопроверку можно отключить.
2. Пользователь может вручную проверить релизы и переключить Stable/Preview.
3. BCI обращается только к `https://api.github.com/repos/VoodyWoodman/BCI/releases`.
4. Автоустановка допускается только если в релизе есть `release-manifest.json`, ожидаемый установщик, SHA-256 и совпадающий размер.
5. Installer скачивается в `%LOCALAPPDATA%\BCI\Updates`, проверяется по размеру и SHA-256, затем запускается через Windows ShellExecute `runas`.
6. Inno Setup получает UAC, обновляет существующую установку с тем же `AppId`, закрывает BCI и запускает новую версию после установки.
7. Portable/source копии могут проверять наличие релизов, но автоустановка отключена: им следует скачать новый portable ZIP или Setup вручную.

## Ограничения безопасности

HTTPS и SHA-256 защищают передачу и целостность скачанного файла относительно манифеста того же GitHub Release. Это **не эквивалент Authenticode-подписи издателя**: владелец/компрометация GitHub-репозитория потенциально может заменить и установщик, и манифест. До стабильного выпуска следует добавить Authenticode signing certificate либо отдельную цифровую подпись манифеста с публичным ключом, встроенным в приложение.

BCI не запускает EXE, пока не проверены URL официального репозитория, версия, имя asset, размер и SHA-256. Установка не начинается без явного действия пользователя и подтверждения UAC.

## Первый переход

BCI 0.5.1-alpha не содержит Update Manager. Переход на 0.5.2-alpha выполняется один раз вручную через Setup.exe. После установки 0.5.2-alpha следующие версии могут обновляться из вкладки «Обновления».

## Текущее состояние GitHub и процедура публикации

Репозиторий `VoodyWoodman/BCI` пока содержит только документацию в `main` и публичный релиз `v0.3.1-alpha`. Ветка `build/bci-update-system` создана отдельно от `main`; актуальные исходники 0.5.2-alpha в неё ещё не отправлены. Скрипт `publish_source_branch.bat` публикует whitelisted исходники в эту ветку после явного ввода `YES`. Для этой операции разработчику нужен Git for Windows и доступ к GitHub; обычным пользователям установленной BCI Git не требуется.

1. Обновить `APP_VERSION` в `bci_core.py`, fallback `MyAppVersion` в `BCI_Setup.iss` и Changelog.
2. Выполнить `pytest` и `unittest` локально либо подтвердить их выполнение в Windows workflow.
3. Опубликовать исходники в `build/bci-update-system` через `publish_source_branch.bat`; публикация требует Git for Windows, но не требует локального Python 3.12/3.13.
4. Убедиться, что GitHub Actions branch build прошёл: Windows Runner сам устанавливает Python 3.12, зависимости и Inno Setup.
5. Скачать Actions Artifact и проверить первый install/upgrade/uninstall на Windows 10/11.
6. Только после проверки создать tag `v<APP_VERSION>`. Workflow опубликует GitHub Release.
7. Для stable-версии отдельно проверить подпись кода и чистую установку.

`build_windows_exe.bat` и `build_windows_installer.bat` предназначены для локальной разработки и требуют Python 3.12/3.13 x64. Обычному пользователю и разработчику, использующему удалённый GitHub Actions build, устанавливать Python не требуется. См. `FIRST_WINDOWS_BUILD_RU.md`.
