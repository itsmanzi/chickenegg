"""Print a Fernet key for INCOME_RADAR_FERNET_KEY (encrypts notes at rest)."""

from income_radar.crypto_util import generate_fernet_key

if __name__ == "__main__":
    print(generate_fernet_key())
