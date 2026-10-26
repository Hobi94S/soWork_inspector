import argparse
from datetime import date
import logging
from pathlib import Path
from sowork_inspector.config import AppConfig
from sowork_inspector.lockfile import ProcessLock
from sowork_inspector.presence import PresenceError


def iso_day(value):
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Use YYYY-MM-DD') from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description='SoWork: presença, horas e notificações independentes')
    parser.add_argument('--env-file', default='.env')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('login')
    commands.add_parser('collect-once')
    commands.add_parser('run')
    har = commands.add_parser('replay-har', help='Validação local, sem envio de notificações')
    har.add_argument('path', type=Path)
    report = commands.add_parser('report')
    report.add_argument('--day', type=iso_day)
    report.add_argument('--events-file', type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    logger = logging.getLogger('sowork')
    try:
        config = AppConfig.from_env(args.env_file)
        # The profile is locked too, even when two processes use different output dirs.
        with ProcessLock(config.profile_dir / '.inspector.lock'), ProcessLock(config.output_dir / '.inspector.lock'):
            if args.command == 'login':
                from sowork_inspector.client import SoWorkClient
                client = SoWorkClient(config, login=True)
                try:
                    logger.info('Faça login no navegador; o bot clicará em Enter Office automaticamente')
                    client.wait_ready()
                    logger.info('Perfil próprio pronto para coleta')
                    return 0
                finally:
                    client.close()
            if args.command == 'report':
                from sowork_inspector.reporting import rebuild_clean_reports_from_events
                from sowork_inspector.storage import Storage
                storage = Storage(config)
                paths = [args.events_file] if args.events_file else list(storage.events_dir.glob('*.jsonl'))
                reports = rebuild_clean_reports_from_events(paths, time_zone_name=config.time_zone_name,
                    away_counts_as_online=True, logger=logger, day_key=args.day)
                for day, payload in reports.items():
                    storage.write_report(day, payload)
                logger.info('%s relatórios reconstruídos', len(reports))
                return 0
            from sowork_inspector.app import SoWorkInspectorApp
            if args.command == 'replay-har':
                from sowork_inspector.har import snapshots
                # Parse/validate everything before writing anything.
                values = snapshots(args.path, config.group_id)
                if (config.output_dir / 'state.json').exists():
                    raise ValueError('Use a fresh SOWORK_OUTPUT_DIR for replay-har')
                app = SoWorkInspectorApp(config)
                for timestamp, records in values:
                    app.cycle(timestamp, records)
                logger.info('%s snapshots reproduzidos, sem notificações', len(values))
                return 0
            app = SoWorkInspectorApp(config)
            return app.run(once=args.command == 'collect-once')
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # Avoid printing request/session data from third-party exceptions.
        if isinstance(exc, (PresenceError, ValueError, FileNotFoundError)):
            logger.error('%s', exc)
        else:
            logger.error('Falha (%s); consulte configuração e execute login', type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
