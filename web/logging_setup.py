"""Shared logger for the web package.

The root logging config (handlers/format) is installed by ``core.agent.runtime``
when ``main`` is imported. This module just exposes a named logger; the format
string carries no logger name, so per-module naming does not affect output.
"""

import logging

logger = logging.getLogger("beacon.web")
