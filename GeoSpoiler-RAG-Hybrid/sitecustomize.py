"""Project-local Python startup hooks.

Python imports this module automatically when the repository root is on
`sys.path`. Keep it side-effect-free unless an explicit environment flag is set.
"""

from testing.no_network import install_from_env

install_from_env()
