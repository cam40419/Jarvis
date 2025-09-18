import pymysql.cursors
import time
from utils.logger import Logger
import os
from dotenv import load_dotenv
import threading

# Load environment variables
load_dotenv()

logger = Logger.get_logger()


class DatabaseConnection:
    _instance = None
    _lock = threading.Lock()
    _config = {
        "host": os.getenv("DB_HOST"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
        "database": os.getenv("DB_NAME"),
        "cursorclass": pymysql.cursors.DictCursor,
    }

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super(DatabaseConnection, cls).__new__(cls)
                    cls._instance._connection = None
        return cls._instance

    def connect(self, attempts=3, delay=2):
        """
        Establish a connection to the MySQL database.
        Retries connection upon failure for the specified number of attempts.
        """
        if self._connection and self._connection.open:
            return self._connection

        attempt = 1
        while attempt <= attempts:
            try:
                self._connection = pymysql.connect(**self._config)
                logger.info("Connected to MySQL database.")
                return self._connection
            except (pymysql.MySQLError, IOError) as err:
                if attempt == attempts:
                    logger.error(
                        f"Failed to connect, exiting without a connection: {err}"
                    )
                    self._connection = None
                    break
                logger.warn(
                    f"Connection failed: {err}. Retrying ({attempt}/{attempts})..."
                )
                time.sleep(delay**attempt)
                attempt += 1
        return None

    def get_connection(self):
        """
        Returns the active database connection.
        Ensures a connection is established if none exists.
        """
        if not self._connection or not self._connection.open:
            self.connect()
        return self._connection

    def close_connection(self):
        """
        Closes the database connection if it is open.
        """
        if self._connection and self._connection.open:
            self._connection.close()
            logger.log("Database connection closed.")
            self._connection = None
