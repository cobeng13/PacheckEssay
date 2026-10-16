import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass
class Settings:
    data: Path = Path(os.getenv('ESSAY_DATA', '/data'))
    key_file: Path = Path(os.getenv('OPENAI_API_KEY_FILE', '/run/secrets/openai_api_key'))
    secure_cookie: bool = os.getenv('SECURE_COOKIE', 'true').lower() == 'true'
    max_upload: int = int(os.getenv('MAX_UPLOAD_MB', '5')) * 1024 * 1024
    max_tasks: int = int(os.getenv('MAX_TASKS', '2000'))
    max_queue: int = int(os.getenv('MAX_QUEUE', '20'))
    requests_per_minute: int = int(os.getenv('REQUESTS_PER_MINUTE', '60'))
    max_output_tokens: int = int(os.getenv('MAX_OUTPUT_TOKENS', '8192'))
    daily_budget: Decimal = Decimal(os.getenv('DAILY_BUDGET_USD', '5'))
    default_model: str = os.getenv('DEFAULT_MODEL', 'gpt-4.1-mini')
    retention_seconds: int = 86400

