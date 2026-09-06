@echo off
setlocal
title FGC Control Center

cd /d "%~dp0"

if /I "%~1"=="--check" (
    docker compose config --quiet
    if errorlevel 1 exit /b 1
    exit /b 0
)

where docker >nul 2>&1
if errorlevel 1 (
    echo Docker nao foi encontrado. Instale ou abra o Docker Desktop.
    pause
    exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
    echo Iniciando o Docker Desktop...
    if not exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
        echo Docker Desktop nao foi encontrado.
        pause
        exit /b 1
    )
    start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
    echo Aguardando o Docker ficar pronto...
    for /L %%I in (1,1,60) do (
        docker info >nul 2>&1 && goto docker_ready
        timeout /t 2 /nobreak >nul
    )
    echo O Docker demorou demais para iniciar. Tente novamente em instantes.
    pause
    exit /b 1
)

:docker_ready
echo Preparando o FGC Control Center...
docker compose up -d --build app
if errorlevel 1 goto compose_error

echo Aguardando o painel local...
for /L %%I in (1,1,45) do (
    curl.exe --silent --fail --max-time 2 http://127.0.0.1:8080/ >nul 2>&1 && goto panel_ready
    timeout /t 2 /nobreak >nul
)

echo O container iniciou, mas o painel ainda nao respondeu.
echo Consulte os logs com: docker compose logs app --tail=50
pause
exit /b 1

:panel_ready
start "" "http://localhost:8080"
echo Painel aberto no navegador.
timeout /t 4 /nobreak >nul
exit /b 0

:compose_error
echo Nao foi possivel iniciar o container.
echo Verifique as mensagens acima ou execute: docker compose logs app --tail=50
pause
exit /b 1
