import os
import io
import json
import logging
import urllib.parse

from enum import Enum
from pathlib import Path
from functools import lru_cache
from dataclasses import dataclass
from logging.handlers import WatchedFileHandler
from typing import Dict, Any, Optional


# ==============================================================================
# 1. TYPE DECLARATIONS & CONTAINERS (IMMUTABLE RECORDS)
# ==============================================================================


class ConfigPathStatus(Enum):
    SUCCESS = 0
    NOT_FOUND = 2
    UNKNOWN_ERROR = 3


@dataclass(frozen=True)
class ConfigPathResult:
    status: ConfigPathStatus
    file_path: Optional[Path]
    audit_log: str
    error: Optional[str] = None



@dataclass(frozen=True)
class DatabaseConfig:
    db_user: str
    db_password: str
    db_host: str
    db_port: int
    db_name: str

    def get_map(self) -> dict:
        """
        Returns a dictionary mapped directly to psycopg2 
        keyword arguments.
        """
        return {
            "user": self.db_user,
            "password": self.db_password,
            "host": self.db_host,
            "port": self.db_port,
            "dbname": self.db_name
        }

    def get_uri(self) -> str:
        """
        Returns a URL-encoded DB_URI connection string.
        This is in case password has conflict chars
        """
        safe_password = urllib.parse.quote_plus(self.db_password)
        return f"postgresql://{self.db_user}:{safe_password}@{self.db_host}:{self.db_port}/{self.db_name}"



@dataclass(frozen=True)
class LoggerConfig:
    log_file: str
    log_level: int


@dataclass(frozen=True)
class GeeConfig:
    credentials_file: str

    @property
    def project(self) -> str:
        """Derives the GCP project ID from the credentials file."""
        return self.get_credentials_dict().get("project_id", "")

    def get_credentials_dict(self) -> Dict[str, Any]:
        """
        Reads and returns the service-account credentials from the JSON file
        referenced by ``credentials_file``.  The private_key newline escaping
        is normalised automatically.
        """
        creds_path = Path(self.credentials_file).resolve()
        if not creds_path.exists():
            raise FileNotFoundError(
                f"GEE credentials file not found: {creds_path}"
            )
        with open(creds_path, "r") as fh:
            creds: Dict[str, Any] = json.load(fh)
        if "private_key" in creds and isinstance(creds["private_key"], str):
            creds["private_key"] = creds["private_key"].replace("\\n", "\n")
        return creds

    @property
    def service_account_email(self) -> str:
        """Derives the service-account e-mail from the credentials file."""
        return self.get_credentials_dict().get("client_email", "")

    def get_credentials_json(self) -> str:
        """Returns credentials serialized as a JSON string."""
        return json.dumps(self.get_credentials_dict())



# ==============================================================================
# 2. INTERNAL PATH LOCATOR
# ==============================================================================

def _get_config_path() -> ConfigPathResult:
    """
    Locates the config.json file using a prioritized multi-tier 
    lookup hierarchy. (1) first check the XBOA_CONFIG_PATH environment 
    variable. if that is not available then (2) $HOME/.config/softmaxx/xboa 
    is checked next, followed by READ-ONLY/etc/softmaxx/xboa folders. 
    Checks the environment variable first, then system locations.
    Follow XDG specs and use .config and .cache folders.
    """
    log_stream = io.StringIO()
    target_filename = "config.json"
    log_stream.write("**** \n")
    
    try:
        # is environment variable set?
        env_path_value = os.environ.get("XBOA_CONFIG_PATH")
        if env_path_value:
            env_path = Path(env_path_value).resolve()
            log_stream.write(f"XBOA_CONFIG_PATH is {env_path_value} \n")
            
            if env_path.exists() and env_path.is_file():
                return ConfigPathResult(
                    ConfigPathStatus.SUCCESS, 
                    env_path, 
                    log_stream.getvalue()
                )
            else:
                log_stream.write(f"NO FILE FOUND: check XBOA_CONFIG_PATH again\n")
        else:
            log_stream.write("XBOA_CONFIG_PATH is not set!\n")
                
        log_stream.write("CHECK SYSTEM PATHS for config file...\n")
        # search system paths for config file
        search_locations = [
            Path.home() / ".config" / "softmaxx/xboa" / target_filename,
            Path("/etc/softmaxx/xboa") / target_filename
        ]
        
        for path in search_locations:
            log_stream.write(f"checking: {path} \n")
            try:
                resolved_path = path.resolve()
                if resolved_path.exists() and resolved_path.is_file():
                    log_stream.write(f"config file located at: {resolved_path}\n")
                    return ConfigPathResult(
                        ConfigPathStatus.SUCCESS, 
                        resolved_path, 
                        log_stream.getvalue()
                    )
                
            except PermissionError:
                log_stream.write("PERMISSION ERROR reading file\n")
        
        # no config file in system paths also!
        log_stream.write("FATAL: Unable to locate config file\n")

        return ConfigPathResult(
            ConfigPathStatus.NOT_FOUND,
            None,
            log_stream.getvalue()
        )

    except Exception as exc:
        log_stream.write("UNKNOWN ERROR during config file search\n")
        return ConfigPathResult(
            ConfigPathStatus.UNKNOWN_ERROR,
            None,
            log_stream.getvalue(),
            repr(exc)
        )
    
    finally:
        log_stream.close()


# ==============================================================================
# 3. APPLICATIVE CONFIGURATION ENGINE
# ==============================================================================

class AppConfig:
    _raw_data: Dict[str, Any] = {}

    @staticmethod
    def load() -> None:
        """
        get the worker config file, parse and load it into _container 
        """
        config_path_result = _get_config_path()
        # print(config_path_result.audit_log)
        audit_log = io.StringIO()
        audit_log.write(config_path_result.audit_log)
        
        if config_path_result.status != ConfigPathStatus.SUCCESS:
            log_content = audit_log.getvalue()
            error_msg = (
                f"status: {config_path_result.status.name}\n"
                f"{log_content}\n"
                f"code_error: {config_path_result.error}\n"
            )
            
            audit_log.close()
            raise RuntimeError(error_msg)

        try:
            audit_log.write(f"READ CONFIG FILE: {config_path_result.file_path} \n")
            with open(config_path_result.file_path, "r") as json_file:
                AppConfig._raw_data = json.load(json_file)

            audit_log.write("success: application config loaded\n")

        except Exception as config_err:
            config_error_message = repr(config_err)
            log_content = audit_log.getvalue()

            error_msg = (
                f"{log_content}\n"
                f"CONFIG LOAD ERROR: {config_error_message}\n"
            )

            raise RuntimeError(error_msg)
        
        finally:
            audit_log.close()

    @staticmethod
    def get(key: str) -> Any:
        """
        Fetches raw elements, arrays, or child JSON blocks directly from the 
        configuration container by string key. Raises KeyError if missing.
        """
        if not AppConfig._raw_data:
            raise RuntimeError("config data is not loaded")
        
        if key not in AppConfig._raw_data:
            raise KeyError(f"fatal: No configuration key {key}")
        return AppConfig._raw_data[key]


    @staticmethod
    def init_logging(log_file: str, log_level: int=20) -> None:
        """
        Reads logging configuration parameters from AppConfig and init 
        the root logger. Since we are using logrotate to rotate logs,
        we use WatchedFileHandler instead of RotatingFilehandler. 
        
        If your operating system manages log rotations centrally via logrotate,
        a standard RotatingFileHandler will conflict with it. When logrotate 
        moves or renames the active log file (e.g., softmaxx.log to softmaxx.log.1), 
        RotatingFileHandler keeps writing to the renamed file descriptor because 
        it has no awareness of external OS file swaps.

        WatchedFileHandler specifically watches the file on disk. The instant 
        logrotate cuts a new file, the handler detects that the file's underlying 
        inode changed, closes its old reference, and seamlessly closes and 
        re-opens the correct target log path without dropping log rows or 
        requiring a service restart

        """
        if not log_level:
            raise RuntimeError("fatal: log_level parameter missing for init_logging()")

        if not log_file:
            raise RuntimeError("fatal: log_file path parameter missing for init_logging()")

        log_format = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s.%(funcName)s:(%(lineno)d) - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        
        file_handler = WatchedFileHandler(log_file)
        file_handler.setFormatter(log_format)
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(log_format)
        
        root_logger = logging.getLogger()
        root_logger.setLevel(log_level)
        root_logger.handlers.clear()
        
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        logging.info("init_logging() done, writing logs to target file: {0}".format(log_file))


# ==============================================================================
# 4. MODULE-LEVEL FUNCTIONS
# ==============================================================================


@lru_cache(maxsize=16)
def get_database_config(db_config_key: str="default") -> DatabaseConfig:
    """
    Extracts a strongly-typed, immutable credential block corresponding 
    to the Database config key
    """
    database_section = AppConfig.get("database") 
    data = database_section.get(db_config_key, None)
    if not data:
        raise ValueError(f"database config key error: {db_config_key}")
    
    return DatabaseConfig(
        db_user=str(data["user"]),
        db_password=str(data["password"]),
        db_host=str(data["host"]),
        db_port=int(data["port"]),
        db_name=str(data["database"])
    )


@lru_cache(maxsize=16)
def get_logger_config(name: str) -> LoggerConfig:
    """
    Extracts logging config parameters from AppConfig and wraps them inside
    an immutable frozen LoggerConfig dataclass. Cached.
    """
    log_section = AppConfig.get("logging")
    data = log_section[name]
    
    return LoggerConfig(
        log_file=str(data["log_file"]),
        log_level=int(data["log_level"])
    )


@lru_cache(maxsize=16)
def get_gee_config() -> GeeConfig:
    """
    Extracts Google Earth Engine (GEE) credentials-file path from AppConfig.
    The GCP project ID and service-account e-mail are loaded lazily from the
    referenced credentials JSON file. Cached.
    """
    gee_section = AppConfig.get("gee")
    return GeeConfig(
        credentials_file=str(gee_section["credentials_file"]),
    )

