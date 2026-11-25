# Первый Windows-установщик BCI без Python на компьютере разработчика

Пользователю установленной BCI не нужны Python, pip, PyInstaller, Git или Inno Setup.
Эти инструменты нужны только разработчику при подготовке исходников или локальной сборке.

## Важное различие

- `run.bat` запускает исходники через локальный Python и предназначен для разработки.
- `build_windows_exe.bat` и `build_windows_installer.bat` выполняют локальную сборку и требуют Python 3.12 x64 либо Python 3.13 x64. Python 3.14 в текущей конфигурации не поддерживается.
- Для получения официального установщика без установки Python на своём ПК используйте GitHub Actions.

## Первый запуск сборки в GitHub

1. Установите Git for Windows и войдите в GitHub через Git Credential Manager при запросе push.
2. Распакуйте весь проект в обычную папку. Не запускайте BAT из ZIP-архива.
3. Запустите `publish_source_branch.bat`.
4. Проверьте целевой репозиторий `VoodyWoodman/BCI` и ветку `build/bci-update-system`; подтвердите публикацию вводом `YES`.
5. Если Git попросит имя и email автора коммита, укажите свои данные или GitHub noreply email. Они будут записаны только в одноразовую временную копию Git, не в глобальные настройки.
6. После успешного push откройте GitHub → Actions → `BCI Windows build and GitHub Release`. Workflow должен выполнить тесты, собрать portable-папку и Inno Setup installer на Windows Runner с Python 3.12.
7. Для push в ветку результат сохраняется как Actions Artifact; это ещё не публичный Release. Сначала скачайте artifact и проверьте установку на Windows. Публикация Release выполняется отдельным push тега `v<APP_VERSION>` после проверки.

## Если что-то не сработало

- Ошибка PowerShell parser на русских строках: проверьте, что `publish_source_branch.ps1` сохранён как UTF-8 with BOM; в официальном архиве BOM уже включён.
- Git не найден: установите Git for Windows, откройте новый терминал и повторите.
- `git push` отклонён: проверьте, что в Git Credential Manager выбрана учётная запись с правом записи в `VoodyWoodman/BCI`.
- Workflow не запустился: откройте Actions и проверьте, разрешены ли GitHub Actions для репозитория; не создавайте тег релиза, пока branch build не прошёл.
- Python 3.14: локальная сборка не поддерживается текущими pinned dependencies. Устанавливать Python локально не требуется для GitHub Actions — Runner использует отдельный Python 3.12.

Скрипт публикации клонирует только выделенную ветку, показывает изменения и требует явного `YES`. Он не делает force push, не меняет `main`, старые теги или опубликованные релизы.
