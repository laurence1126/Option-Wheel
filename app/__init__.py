from flask import Flask, render_template


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/option-watcher")
    def option_watcher() -> str:
        return render_template("option_watcher_loading.html")

    @app.get("/option-watcher/content")
    def option_watcher_content() -> tuple[str, int] | str:
        from app.option_watcher import build_option_watcher_context
        from app.option_watcher import get_watcher_data

        loader = get_watcher_data

        try:
            options, current_bp = loader()
        except Exception:
            app.logger.exception("Unable to load option watcher data.")
            context = build_option_watcher_context()
            context["error_message"] = "Unable to load option data. Confirm that Futu OpenD is running and try again."
            return render_template("option_watcher.html", **context), 503

        return render_template("option_watcher.html", **build_option_watcher_context(options, current_bp))

    return app
