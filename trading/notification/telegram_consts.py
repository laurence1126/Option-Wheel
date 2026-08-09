OPTION_WATCHER_APP_URL = "https://option-wheel.ubuntu-nuc.com/option-watcher"

BOT_COMMANDS = [
    {"command": "status", "description": "📊 Show trading engine status"},
    {"command": "log", "description": "📝 Send latest log file"},
    {"command": "watcher", "description": "📲 Open option watcher app"},
    {"command": "shortput", "description": "💸 Run short put strategy now"},
    {"command": "summary", "description": "📊 Send daily strategy summary"},
    {"command": "restart", "description": "🔄 Restart trading engine"},
    {"command": "shutdown", "description": "⛔️ Shutdown trading engine"},
    {"command": "help", "description": "📋 Show commands"},
    {"command": "start", "description": "🤖 Start the Quant bot"},
]

HELP_TEXT = (
    "<b>Commands</b>\n"
    "/help - Show these commands\n"
    "/start - Say hello to Quant Bot!\n"
    "/status - Show the trading engine status\n"
    "/log - Send the latest log file\n"
    "/watcher - Open the option watcher app\n"
    "/shortput - Run the short put strategy\n"
    "/summary - Send daily strategy summary\n"
    "/restart - Restart the trading engine\n"
    "/shutdown - Shutdown the trading engine"
)

EMPTY_INLINE_KEYBOARD = {"inline_keyboard": []}

RESTART_ENV_VAR = "OPTION_WHEEL_TELEGRAM_RESTARTED"

SHORT_PUT_ACTION_ID = "execute_short_put_strategy"
SUMMARY_ACTION_ID = "send_daily_summary"
SHORT_PUT_CONFIRMATION_TIMEOUT_SECONDS = 60
