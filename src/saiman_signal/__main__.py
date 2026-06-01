import asyncio

from saiman_signal import config, conversation
from saiman_signal.bot import run as signal_run


async def _run() -> None:
    await conversation.init()

    tasks = [signal_run()]

    if config.TELEGRAM_BOT_TOKEN:
        from saiman_signal.telegram import telegram_loop
        tasks.append(telegram_loop())

    await asyncio.gather(*tasks)


def main():
    asyncio.run(_run())


main()
