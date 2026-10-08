"""Process configuration credentials shared by independent provider adapters."""

import os

from dotenv import dotenv_values


def runtime_credentials() -> dict[str, str]:
    return {
        **{key: value for key, value in dotenv_values(".env").items() if value is not None},
        **os.environ,
    }
