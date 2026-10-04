"""Model resolver: inventory -> capability profile -> tier+needs -> model.

Astra's rule (classifier-v2 plan, owner decision 4): any setup can be
represented; active routing requires a verified execution path and qualified
capabilities. Missing coverage preserves the configured model or asks the user.

Nothing here is wired into live routing. It is an inspectable library plus the
``inventory``, ``calibrate`` and ``resolve`` CLI commands.
"""
