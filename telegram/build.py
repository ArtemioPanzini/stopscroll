#!/usr/bin/env python3
"""Build the pinned Debug client and its policy tests in upstream's container."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
IMAGE = 'ghcr.io/telegramdesktop/tdesktop/centos_env@sha256:f8186fefc7d7abb23c9b7e905b7f13270f57edc7ce57bb959a2634eabe803ca3'
BASE = 'bf4f714036f21c750d535dab199c811b3f9bfdd4'
TAG = 'v7.4.4'
APP_VERSION = 7004004
TEST_API_ID = 17349
TEST_API_HASH = '344583e45741c457fe1862106095a5eb'
INTEGRATION_SOURCES = (
    'stopscroll/stopscroll', 'stopscroll/stopscroll_policy', 'data/data_peer',
    'dialogs/dialogs_inner_widget', 'dialogs/ui/dialogs_suggestions',
    'history/history_widget',
    'history/view/history_view_chat_section', 'info/info_memento',
    'info/info_wrap_widget', 'media/stories/media_stories_controller',
    'media/view/media_view_overlay_widget', 'mainwidget', 'window/window_chat_preview',
    'window/window_session_controller',
)


def validate_source():
    source = ROOT / 'tdesktop'
    if not (source / '.git').exists():
        raise RuntimeError('Missing source checkout; follow telegram/README.md')
    base = subprocess.check_output(['git', '-C', str(source), 'rev-parse', TAG], text=True).strip()
    if base != BASE:
        raise RuntimeError(f'Unexpected upstream base; expected the documented {TAG} tag')
    ancestry = subprocess.run(['git', '-C', str(source), 'merge-base', '--is-ancestor', BASE, 'HEAD'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if ancestry.returncode:
        raise RuntimeError('Source HEAD does not descend from the pinned upstream base')
    version = re.search(r'^AppVersion[ \t]+([0-9]+)[ \t]*$',
                        (source / 'Telegram/build/version').read_text(), re.MULTILINE)
    if version is None or int(version.group(1)) != APP_VERSION:
        raise RuntimeError(f'Source AppVersion does not match {TAG}')


def invalidate_build_manifest(output):
    manifest = output / 'stopscroll-build.json'
    if manifest.exists():
        manifest.unlink()
        directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def uses_test_credentials(api, explicitly_test=False):
    if type(explicitly_test) is not bool:
        raise ValueError('Test credential flag must be boolean')
    return (explicitly_test or api['api_id'] == TEST_API_ID or
            api['api_hash'].lower() == TEST_API_HASH)


def write_build_manifest(output, test_credentials):
    if type(test_credentials) is not bool:
        raise ValueError('Test credential flag must be boolean')
    binary = output / 'bin/Telegram'
    with binary.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    payload = dict(version=1, configuration='Debug', auto_update=False,
                   test_credentials=test_credentials, binary_sha256=digest)
    fd, temporary = tempfile.mkstemp(prefix='.stopscroll-build-', dir=output)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output / 'stopscroll-build.json')
        directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run(args, log, env=None):
    print(log + ': running', flush=True)
    fd = os.open(ROOT / log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    effective_env = os.environ if env is None else env
    secrets = sorted((effective_env.get(key, '')
                      for key in ('TDESKTOP_API_ID', 'TDESKTOP_API_HASH')),
                     key=len, reverse=True)
    with os.fdopen(fd, 'w') as stream:
        with subprocess.Popen(args, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, errors='replace') as process:
            for line in process.stdout:
                for secret in secrets:
                    if secret:
                        line = line.replace(secret, '[REDACTED]')
                stream.write(line)
                stream.flush()
            code = process.wait()
    if code:
        raise RuntimeError(f'{log}: failed with code {code}; inspect the local log')
    print(log + ': passed', flush=True)


def container(command, log, env=None, network=True, cpus=2, memory_gib=4):
    args = ['docker', 'run', '--rm', f'--cpus={cpus}', f'--memory={memory_gib}g', f'--memory-swap={memory_gib + 2}g',
            '--user', f'{os.getuid()}:{os.getgid()}', '-e', 'CCACHE_DISABLE=1',
            '-v', f'{ROOT}:/work', '-w', '/work']
    if not network:
        args += ['--network=none']
    if env is not None:
        args += ['-e', 'TDESKTOP_API_ID', '-e', 'TDESKTOP_API_HASH']
    run(args + [IMAGE, 'bash', '-lc', command], log, env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    credentials = parser.add_mutually_exclusive_group()
    credentials.add_argument('--test', action='store_true', help='Use upstream public TEST ONLY API credentials')
    credentials.add_argument('--api-file', type=Path, default=Path.home() / '.config/stopscroll/telegram-api.json')
    parser.add_argument('--policy-tests', action='store_true', help='Only compile and run standalone policy tests')
    parser.add_argument('--configure-only', action='store_true')
    parser.add_argument('--jobs', type=int, choices=(1, 2), default=1,
                        help='Application compiler jobs (default: 1 for the 4GiB container)')
    parser.add_argument('--full-jobs', type=int, choices=(1, 2, 3),
                        help='Override jobs for the full build after priority objects pass')
    parser.add_argument('--full-memory-gib', type=int, choices=(4, 6), default=4,
                        help='RAM cap for the full phase; swap allowance is 2GiB')
    args = parser.parse_args()
    if not args.policy_tests:
        invalidate_build_manifest(ROOT / 'tdesktop/out')
    validate_source()
    run(['docker', 'pull', IMAGE], 'docker-pull.log')
    container('cmake -S tests -B tests/build -DCMAKE_BUILD_TYPE=Debug && '
              'cmake --build tests/build --parallel 2 && '
              'ctest --test-dir tests/build --output-on-failure', 'policy-tests.log', network=False)
    if args.policy_tests:
        return
    if args.test:
        api = dict(api_id=TEST_API_ID, api_hash=TEST_API_HASH)
    else:
        if not args.api_file.exists():
            raise RuntimeError(f'Create private credentials file {args.api_file}; see telegram/README.md. Use --test only for testing.')
        if args.api_file.stat().st_mode & 0o077:
            raise RuntimeError('Credentials file must have permissions 0600')
        api = json.loads(args.api_file.read_text())
    if (type(api.get('api_id')) is not int or api['api_id'] <= 0 or
            not isinstance(api.get('api_hash'), str) or not re.fullmatch('[0-9a-fA-F]{32}', api['api_hash'])):
        raise RuntimeError('Invalid API ID/hash format')
    env = dict(os.environ, TDESKTOP_API_ID=str(api['api_id']), TDESKTOP_API_HASH=api['api_hash'])
    (ROOT / 'tdesktop/out').mkdir(mode=0o700, exist_ok=True)
    os.chmod(ROOT / 'tdesktop/out', 0o700)
    container('cmake -S tdesktop -B tdesktop/out -C /work/api-options.cmake '
              '-DCMAKE_BUILD_TYPE=Debug -DCMAKE_CONFIGURATION_TYPES=Debug '
              '-DCMAKE_CXX_FLAGS_DEBUG="-O0 -g0" -DCMAKE_C_FLAGS_DEBUG="-O0 -g0" '
              '-DCMAKE_DISABLE_PRECOMPILE_HEADERS=OFF -DCMAKE_EXPORT_COMPILE_COMMANDS=ON '
              '-UCMAKE_PREFIX_PATH -Uprotobuf_DIR -Uabsl_DIR '
              '-DDESKTOP_APP_DISABLE_AUTOUPDATE=ON', 'configure.log', env=env)
    if not args.configure_only:
        container('cmake --build tdesktop/out --config Debug --parallel 2 --target '
                  'cmake_object_order_depends_target_Telegram',
                  'prerequisites-build.log', env=env)
        targets = ' '.join('Telegram/CMakeFiles/Telegram.dir/SourceFiles/' + source + '.cpp.o'
                           for source in INTEGRATION_SOURCES)
        container(f'cmake --build tdesktop/out --config Debug --parallel {args.jobs} --target ' + targets,
                  'integration-build.log', env=env)
        full_jobs = args.full_jobs if args.full_jobs is not None else args.jobs
        container(f'cmake --build tdesktop/out --config Debug --target Telegram --parallel {full_jobs}',
                  'build.log', env=env, cpus=max(2, full_jobs), memory_gib=args.full_memory_gib)
        write_build_manifest(ROOT / 'tdesktop/out', uses_test_credentials(api, args.test))
        print('Built executable: ' + str(ROOT / 'tdesktop/out/bin/Telegram'))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
