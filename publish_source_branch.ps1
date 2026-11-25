$ErrorActionPreference = 'Stop'

$Repository = 'https://github.com/VoodyWoodman/BCI.git'
$Branch = 'build/bci-update-system'
$ProjectRoot = (Resolve-Path $PSScriptRoot).Path
$VersionLine = Select-String -Path (Join-Path $ProjectRoot 'bci_core.py') -Pattern '^APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
if ($null -eq $VersionLine) { throw 'Не найден APP_VERSION в bci_core.py.' }
$Version = [regex]::Match($VersionLine.Line, '"([^"]+)"').Groups[1].Value
if ($Version -ne '0.5.2-alpha') {
    throw "Этот сценарий подготовлен для 0.5.2-alpha, а в исходниках указана версия $Version. Сначала проверь версию и обнови сценарий."
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Для публикации исходников нужен Git for Windows. Самому пользователю BCI Git не требуется.'
}

Write-Host ''
Write-Host 'BCI source sync to GitHub' -ForegroundColor Cyan
Write-Host "Repository: VoodyWoodman/BCI"
Write-Host "Target branch: $Branch"
Write-Host "Version: $Version"
Write-Host ''
Write-Host 'Будут загружены исходники приложения, тесты, документация и Windows workflow.'
Write-Host 'Не будут загружены build/dist, виртуальные окружения, кэши, отчёты игры и локальная база данных.'
Write-Host ''

$TempRoot = Join-Path $env:TEMP ("BCI-GitHubSync-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $TempRoot | Out-Null
$ClonePath = Join-Path $TempRoot 'BCI'

try {
    Write-Host 'Клонирование отдельной ветки...' -ForegroundColor Yellow
    git clone --branch $Branch --single-branch $Repository $ClonePath
    if ($LASTEXITCODE -ne 0) { throw "Не удалось клонировать ветку $Branch. Проверь доступ к GitHub и наличие ветки." }

    $Files = @(
        '.gitignore', 'BCI_Setup.iss', 'CHANGELOG_RU.md', 'LICENSE.txt',
        'QA_REPORT_RU.md', 'README_RU.md', 'ROADMAP_RU.md', 'RELEASE_AND_UPDATE_RU.md', 'FIRST_WINDOWS_BUILD_RU.md',
        'bci_app.py', 'bci_core.py', 'bci_environment.py', 'bci_ml.py', 'bci_store.py',
        'bci_updater.py', 'build_windows_exe.bat', 'build_windows_installer.bat',
        'copy_third_party_licenses.py', 'create_release_manifest.py', 'requirements-test.txt',
        'requirements-windows.txt', 'run.bat', 'publish_source_branch.bat', 'publish_source_branch.ps1'
    )
    foreach ($File in $Files) {
        $Source = Join-Path $ProjectRoot $File
        if (-not (Test-Path $Source -PathType Leaf)) { throw "Не найден исходный файл: $File" }
        Copy-Item -LiteralPath $Source -Destination (Join-Path $ClonePath $File) -Force
    }

    foreach ($Directory in @('tests', '.github')) {
        $Source = Join-Path $ProjectRoot $Directory
        $Destination = Join-Path $ClonePath $Directory
        if (-not (Test-Path $Source -PathType Container)) { throw "Не найдена папка: $Directory" }
        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
        if ($Directory -eq 'tests') {
            & robocopy $Source $Destination /E /XD '__pycache__' '.pytest_cache' /XF '*.pyc' '*.log' | Out-Null
        } else {
            & robocopy $Source $Destination /E /XD '__pycache__' '.pytest_cache' | Out-Null
        }
        if ($LASTEXITCODE -gt 7) { throw "Не удалось скопировать каталог $Directory (robocopy exit $LASTEXITCODE)." }
    }

    Write-Host ''
    Write-Host 'Изменения, которые будут отправлены:' -ForegroundColor Cyan
    git -C $ClonePath status --short
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось проверить git status.' }

    Write-Host ''
    $Confirmation = Read-Host 'Публично загрузить эти исходники в отдельную ветку GitHub? Введите YES'
    if ($Confirmation -cne 'YES') {
        Write-Host 'Отменено. GitHub не изменён.' -ForegroundColor Yellow
        exit 0
    }

    git -C $ClonePath add -A
    if ($LASTEXITCODE -ne 0) { throw 'git add завершился с ошибкой.' }
    git -C $ClonePath diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Host 'Ветка уже содержит эти исходники; новых изменений нет.' -ForegroundColor Green
        exit 0
    }

    $GitAuthorName = git -C $ClonePath config user.name
    $GitAuthorEmail = git -C $ClonePath config user.email
    if ([string]::IsNullOrWhiteSpace($GitAuthorName)) {
        $GitAuthorName = Read-Host 'Git commit author name (for example, your GitHub display name; stored only in the temporary clone)'
        if ([string]::IsNullOrWhiteSpace($GitAuthorName) -or $GitAuthorName.Trim().Length -lt 2) {
            throw 'Enter a recognizable Git author name (at least 2 characters).'
        }
        git -C $ClonePath config user.name $GitAuthorName
        if ($LASTEXITCODE -ne 0) { throw 'Could not set the local Git author name.' }
    }
    if ([string]::IsNullOrWhiteSpace($GitAuthorEmail)) {
        $GitAuthorEmail = Read-Host 'Git commit author email (verified GitHub email or username@users.noreply.github.com)'
        if ($GitAuthorEmail -notmatch '^[^@\s]+@[^@\s]+\.[^@\s]+$') {
            throw 'Enter a valid email address; a GitHub noreply address is acceptable.'
        }
        git -C $ClonePath config user.email $GitAuthorEmail
        if ($LASTEXITCODE -ne 0) { throw 'Could not set the local Git author email.' }
    }

    git -C $ClonePath commit -m "Prepare BCI $Version update manager and Windows release pipeline"
    if ($LASTEXITCODE -ne 0) { throw 'git commit завершился с ошибкой. Проверь git user.name и user.email.' }

    git -C $ClonePath push origin "HEAD:$Branch"
    if ($LASTEXITCODE -ne 0) { throw 'git push завершился с ошибкой. Ветка на GitHub не обновлена.' }

    Write-Host ''
    Write-Host 'Исходники отправлены в отдельную ветку. main, старые теги и релизы не изменялись.' -ForegroundColor Green
    Write-Host "https://github.com/VoodyWoodman/BCI/tree/$Branch"
    Write-Host 'GitHub Actions должен автоматически запустить Windows build и сохранить артефакты.'
}
finally {
    Write-Host "Временная копия: $TempRoot"
    Write-Host 'Её можно удалить после проверки результата.'
}
