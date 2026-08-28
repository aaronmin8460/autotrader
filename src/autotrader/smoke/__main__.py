"""`python -m autotrader.smoke` - the reinstall-free way to run the harness.

Useful from a git worktree, where the installed console script still points at
whichever checkout was installed into the virtualenv. Run it with
`PYTHONPATH=src` from the worktree root and it is this checkout's code.
"""

from autotrader.smoke.cli import main

main()
