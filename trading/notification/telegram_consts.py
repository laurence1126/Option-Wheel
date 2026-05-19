BOT_COMMANDS = [
    {"command": "status", "description": "📊 Show trading engine status"},
    {"command": "log", "description": "📝 Send latest log file"},
    {"command": "shortput", "description": "💸 Run short put strategy now"},
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
    "/shortput - Run the short put strategy\n"
    "/restart - Restart the trading engine\n"
    "/shutdown - Shutdown the trading engine"
)

EMPTY_INLINE_KEYBOARD = {"inline_keyboard": []}

RESTART_ENV_VAR = "OPTION_WHEEL_TELEGRAM_RESTARTED"

SHORT_PUT_ACTION_ID = "execute_short_put_strategy"
SHORT_PUT_CONFIRMATION_TIMEOUT_SECONDS = 60
