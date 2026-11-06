"""Точка запуску бота. Railway запускає: python main.py"""
import logging
import sys

from bot.config import ConfigError, load_config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        cfg = load_config()
    except ConfigError as e:
        logging.error("Помилка налаштувань: %s", e)
        sys.exit(1)
    for w in cfg.warnings:
        logging.warning(w)

    from bot.app import BotApp
    BotApp(cfg).run()


if __name__ == "__main__":
    main()
