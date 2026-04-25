"""FHS-compliant paths used across sigmond."""

from pathlib import Path

SIGMOND_CONF        = Path('/etc/sigmond')
SIGMOND_LOG         = Path('/var/log/sigmond')
SIGMOND_STATE       = Path('/var/lib/sigmond')
SIGMOND_RUN         = Path('/run/sigmond')
LIFECYCLE_LOCK      = SIGMOND_STATE / 'lifecycle.lock'
SIGMOND_VENV        = Path('/opt/sigmond/venv')

TOPOLOGY_PATH       = SIGMOND_CONF / 'topology.toml'
COORDINATION_PATH   = SIGMOND_CONF / 'coordination.toml'
COORDINATION_ENV    = SIGMOND_CONF / 'coordination.env'
ENVIRONMENT_PATH    = SIGMOND_CONF / 'environment.toml'
ENVIRONMENT_CACHE   = SIGMOND_STATE / 'environment-cache.json'
SDR_LABELS_PATH     = SIGMOND_STATE / 'sdr-labels.toml'
SECRETS_ENV         = SIGMOND_CONF / 'secrets.env'

WSPRDAEMON_CONF     = Path('/etc/wsprdaemon/wsprdaemon.conf')
HF_TIMESTD_CONF     = Path('/etc/hf-timestd/timestd-config.toml')
RADIO_CONF_DIR      = Path('/etc/radio')
