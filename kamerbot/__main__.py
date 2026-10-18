"""`python -m kamerbot` draait één ronde. `python -m kamerbot dienst` draait als vaste dienst
(zie dienst.py): twee rondes per dag plus /update, /status en /nu in Telegram.
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import time

from .config import Config
from .ronde import ronde
from .ssh import NietIngelogd

log = logging.getLogger("kamerbot")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logt elke URL, inclusief de Telegram-token. Alleen waarschuwingen tonen.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="kamerbot")
    p.add_argument("modus", nargs="?", choices=["ronde", "dienst"], default="ronde")
    p.add_argument("--willekeurig", type=int, default=0, metavar="MIN",
                   help="wacht eerst een willekeurig aantal minuten (0 t/m MIN)")
    args = p.parse_args()
    if args.modus == "dienst":
        from .dienst import Dienst

        Dienst(Config()).draai()
        return 0
    if args.willekeurig:
        wacht = random.randint(0, args.willekeurig * 60)
        log.info("Wacht %d minuten", wacht // 60)
        time.sleep(wacht)
    try:
        log.info(ronde(Config()))
    except NietIngelogd:
        log.error("Sessie verlopen tijdens ronde; volgende ronde logt opnieuw in")
        (Config().data_dir / "ssh-cookies.json").unlink(missing_ok=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
