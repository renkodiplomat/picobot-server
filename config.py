import os

# Server configuration

# Host and port
HOST = '0.0.0.0'
PORT = 5000

# Path to the SQLite database file
DATABASE = os.environ.get('DATABASE', 'picobot.db')

# Endpoint path that robots POST their status to
STATUS_ENDPOINT = '/picobot/status'

# Bearer token required in the Authorization header.
# Set to None to accept unauthenticated requests.
REPORT_AUTH = None

# Default server_command integer returned to every robot.
# Individual per-robot commands override this value.
# Bit 0 (0x01): competition_ready
# Bit 1 (0x02): competition_running
DEFAULT_COMMAND = 0
