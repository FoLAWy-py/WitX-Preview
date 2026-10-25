"""Safety rules. Each function decides one question from plain data and never does I/O.

These are the invariants the rest of the system relies on; tests in
``tests/cloud/test_rules.py`` pin them down. They are kept by topic in the modules
below and used as ``rules.<name>``; a test that changes a constant a rule reads
changes it on the rule's own module.
"""

from witx.domain.rules.account import *  # noqa: F403
from witx.domain.rules.agent import *  # noqa: F403
from witx.domain.rules.common import *  # noqa: F403
from witx.domain.rules.devices import *  # noqa: F403
from witx.domain.rules.inputs import *  # noqa: F403
from witx.domain.rules.loops import *  # noqa: F403
from witx.domain.rules.memory import *  # noqa: F403
from witx.domain.rules.models import *  # noqa: F403
from witx.domain.rules.policy import *  # noqa: F403
from witx.domain.rules.schedules import *  # noqa: F403
from witx.domain.rules.tasks import *  # noqa: F403
