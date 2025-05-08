# MushoBot Docker management script
param (
    [Parameter(Position=0)]
    [string]$Command = "start"
)

function Check-Docker {
    try {
        $null = docker --version
        return $true
    } catch {
        Write-Host "Docker is not running or not installed. Please start Docker Desktop." -ForegroundColor Red
        return $false
    }
}

function Start-Bot {
    Write-Host "Starting Musho Music Bot..." -ForegroundColor Cyan
    docker-compose up -d
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Bot started successfully! Dashboard available at http://localhost:8080/musho/" -ForegroundColor Green
    } else {
        Write-Host "Failed to start bot. Check logs with: .\start-bot.ps1 logs" -ForegroundColor Red
    }
}

function Stop-Bot {
    Write-Host "Stopping Musho Music Bot..." -ForegroundColor Yellow
    docker-compose down
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Bot stopped successfully!" -ForegroundColor Green
    } else {
        Write-Host "Failed to stop bot." -ForegroundColor Red
    }
}

function Show-Logs {
    Write-Host "Showing bot logs (Ctrl+C to exit)..." -ForegroundColor Cyan
    docker-compose logs -f
}

function Rebuild-Bot {
    Write-Host "Rebuilding Musho Music Bot..." -ForegroundColor Yellow
    docker-compose down
    docker-compose build --no-cache
    docker-compose up -d
    if ($LASTEXITCODE -eq 0) {
        Write-Host "Bot rebuilt and started successfully!" -ForegroundColor Green
    } else {
        Write-Host "Failed to rebuild bot. Check for errors above." -ForegroundColor Red
    }
}

function Show-Help {
    Write-Host "Musho Music Bot Management Script" -ForegroundColor Cyan
    Write-Host "--------------------------------" -ForegroundColor Cyan
    Write-Host "Usage: .\start-bot.ps1 [command]" -ForegroundColor White
    Write-Host ""
    Write-Host "Commands:" -ForegroundColor Yellow
    Write-Host "  start    - Start the bot (default)" -ForegroundColor White
    Write-Host "  stop     - Stop the bot" -ForegroundColor White
    Write-Host "  restart  - Restart the bot" -ForegroundColor White
    Write-Host "  logs     - Show bot logs" -ForegroundColor White
    Write-Host "  rebuild  - Rebuild and restart the bot" -ForegroundColor White
    Write-Host "  help     - Show this help" -ForegroundColor White
}

# Ensure Docker is running
if (-not (Check-Docker)) {
    exit 1
}

# Execute the requested command
switch ($Command.ToLower()) {
    "start" { Start-Bot }
    "stop" { Stop-Bot }
    "restart" { Stop-Bot; Start-Bot }
    "logs" { Show-Logs }
    "rebuild" { Rebuild-Bot }
    "help" { Show-Help }
    default { 
        Write-Host "Unknown command: $Command" -ForegroundColor Red
        Show-Help
    }
} 