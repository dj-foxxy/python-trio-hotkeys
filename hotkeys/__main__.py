from __future__ import annotations
from functools import partial

import trio

from . import run


def p(message: str):
    return partial(print, message)


async def main() -> None:
    await run([
        ('Mod1', 'a', p('A Press'), p('A Release')),
        ('Mod1', 'b', p('B Press'), p('B Release')),
    ])


if __name__ == '__main__':
    try:
        trio.run(main)
    except KeyboardInterrupt:
        pass
