#!/usr/bin/env bash
set -euo pipefail

# Debian/Ubuntu already reserve the group "voice" for voicemail services.
# Keep the Linux username aligned with PostgreSQL peer auth, but give the
# application its own group. Never modify an existing user's memberships.
if ! getent group voice-app >/dev/null; then
  groupadd --system voice-app
fi
if ! id voice >/dev/null 2>&1; then
  useradd --system --gid voice-app --home-dir /var/lib/voice --shell /usr/sbin/nologin voice
fi
if [ "$(id -u voice)" -eq 0 ]; then
  echo 'Пользователь voice не должен иметь UID 0.' >&2
  exit 1
fi
