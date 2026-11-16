"""ESG Flip Bot.

Run it with ``python -m esgbot``; :mod:`esgbot.__main__` says what it needs.

Sub-packages, outermost last:

* :mod:`esgbot.game` -- pure game logic: cards, the seed-derived deal, settings rules,
  hand evaluation, scoring and settlement. Knows nothing about Telegram or SQLite.
* :mod:`esgbot.storage` -- the SQLite schema, finished flips, flips in progress and
  the stats queries. Plain data in, plain data out.
* :mod:`esgbot.engine` -- one flip's state machine, its timers and one flip per group.
  Reaches the world through a gateway, a store and a clock, and never writes text.
* :mod:`esgbot.telegram` -- commands, button presses, the wording of every message,
  and the gateway that puts it on Telegram.

And one module of its own: :mod:`esgbot.verify`, which re-runs a recorded flip against
the seed it was committed to. It sits above storage and below Telegram, because it is
the whole of ``/verify`` apart from the wording.
"""
