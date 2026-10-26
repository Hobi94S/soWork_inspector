from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


@dataclass
class AppConfig:
    group_id: str = ''
    app_url: str = 'https://app.sowork.com/'
    time_zone_name: str = 'America/Sao_Paulo'
    poll_seconds: float = 60
    headless: bool = True
    profile_dir: Path = Path('profiles/sowork')
    output_dir: Path = Path('runtime')
    login_timeout: float = 300
    webhook_url: str = ''
    chrome_binary: str = ''
    chromedriver: str = ''

    @classmethod
    def from_env(cls, env_file='.env'):
        values = {}
        path = Path(env_file)
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                key, sep, value = line.partition('=')
                if not sep:
                    raise ValueError('Invalid .env line: expected KEY=value')
                values[key.strip()] = value.strip().strip('\"\'')
        values.update(os.environ)
        def get(key, default):
            return values.get('SOWORK_' + key, default)
        flag = get('HEADLESS', 'true').lower()
        if flag not in {'true', 'false'}:
            raise ValueError('SOWORK_HEADLESS must be true or false')
        config = cls(
            group_id=get('GROUP_ID', ''), app_url=get('APP_URL', cls.app_url),
            time_zone_name=get('TIME_ZONE', cls.time_zone_name),
            poll_seconds=float(get('POLL_SECONDS', '60')), headless=flag == 'true',
            profile_dir=Path(get('PROFILE_DIR', 'profiles/sowork')).resolve(),
            output_dir=Path(get('OUTPUT_DIR', 'runtime')).resolve(),
            login_timeout=float(get('LOGIN_TIMEOUT', '300')),
            webhook_url=get('WEBHOOK_URL', ''), chrome_binary=get('CHROME_BINARY', ''),
            chromedriver=get('CHROMEDRIVER', ''),
        )
        ZoneInfo(config.time_zone_name)
        if config.poll_seconds < 10 or config.login_timeout <= 0:
            raise ValueError('Poll interval must be >=10 seconds and login timeout positive')
        if urlsplit(config.app_url).hostname != 'app.sowork.com' or urlsplit(config.app_url).scheme != 'https':
            raise ValueError('App URL must use https://app.sowork.com/')
        return config
