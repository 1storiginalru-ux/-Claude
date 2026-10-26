"""Exercise account provisioning against Ubuntu's real user/group tools."""
from pathlib import Path
import subprocess


helper = Path(__file__).with_name('create-service-user.sh').read_text()
baseline = '''
getent group voice >/dev/null || groupadd --system voice
original_voice_group=$(getent group voice)
'''
checks = '''
test "$(getent group voice)" = "$original_voice_group"
test "$(id -u voice)" -ne 0
getent group voice-app >/dev/null
'''
for existing_user in [False, True]:
    setup = baseline
    if existing_user:
        setup += '''
useradd --system --gid voice --home-dir /var/lib/voice --shell /usr/sbin/nologin voice
original_voice_user=$(getent passwd voice)
'''
    assertions = checks
    if existing_user:
        assertions += 'test "$(getent passwd voice)" = "$original_voice_user"\n'
    else:
        assertions += 'test "$(id -gn voice)" = "voice-app"\n'
    # Re-run the same provisioner to check recovery from a partial installation.
    subprocess.run(['docker', 'run', '--rm', '--interactive', '--network=none', 'ubuntu:24.04', 'bash', '-se'],
                   input=setup + helper + '\n' + helper + '\n' + assertions, text=True, check=True)
    print(f'Ubuntu 24.04: existing group, existing_user={existing_user}, repeat provisioning: OK', flush=True)
