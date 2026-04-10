"""FHS-compliant paths used across sigmond."""

from pathlib import Path

SIGMOND_CONF        = Path('/etc/sigmond')
SIGMOND_LOG         = Path('/var/log/sigmond')
SIGMOND_STATE       = Path('/var/lib/sigmond')
SIGMOND_RUN         = Path('/run/sigmond')

TOPOLOGY_PATH       = SIGMOND_CONF / 'topology.toml'
COORDINATION_PATH   = SIGMOND_CONF / 'coordination.toml'
COORDINATION_ENV    = SIGMOND_CONF / 'coordination.env'
SECRETS_ENV         = SIGMOND_CONF / 'secrets.env'

WSPRDAEMON_CONF     = Path('/etc/wsprdaemon/wsprdaemon.conf')
HF_TIMESTD_CONF     = Path('/etc/hf-timestd/timestd-config.toml')
RADIO_CONF_DIR      = Path('/etc/radio')
