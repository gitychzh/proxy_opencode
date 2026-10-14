#!/system/bin/sh
# Magisk service.d watchdog: keep an egress-agent alive inside an Android
# chroot (e.g. cc9: Ubuntu 24.04 aarch64 under /data/local/chroot/ubuntu).
#
# Android has no systemd, so the systemd template in this directory does not
# apply here. This script is the Android equivalent: Magisk runs it from
# /data/adb/service.d/ at boot, it waits for boot_completed, ensures the
# chroot mounts exist, then supervises the agent (restart after 3 consecutive
# health-check failures).
#
# Deploy:
#   1. install the agent inside the chroot (ethernet/systemd host uses
#      install.sh; here do it by hand: venv under /opt/egress-agent/venv +
#      `pip install <agent source>`).
#   2. drop the real config at $CHROOT/etc/egress-agent/$INSTANCE.json (600).
#   3. edit INSTANCE/PORT below and copy this file to
#      /data/adb/service.d/egress-agent-watchdog.sh (chmod 755).
#      Magisk picks it up on the next boot; to start it immediately run
#      `setsid sh /data/adb/service.d/egress-agent-watchdog.sh </dev/null >/dev/null 2>&1 &`
#      as root.
#
# No secrets in here: the CONNECT auth lives only in the agent config.

# ---- per-device settings -------------------------------------------------
INSTANCE=cc9                       # -> /etc/egress-agent/$INSTANCE.json
PORT=10001                         # agent listener port inside the chroot
CHROOT=/data/local/chroot/ubuntu   # chroot root as seen from Android
VENV=/opt/egress-agent/venv        # venv inside the chroot
# -------------------------------------------------------------------------

LOG=/data/local/tmp/egress-agent-watchdog.log      # host-side watchdog log
AGENT_LOG=/var/log/egress-agent/$INSTANCE.log      # inside the chroot

while [ "$(getprop sys.boot_completed)" != "1" ]; do sleep 5; done
sleep 10

# Ensure the chroot mounts exist (idempotent; a separate boot script may also
# mount them). Without these the chroot shell cannot resolve /proc, /sys.
mount -t proc proc $CHROOT/proc 2>/dev/null
mount -t sysfs sys $CHROOT/sys 2>/dev/null
mount --rbind /dev $CHROOT/dev 2>/dev/null
mount -t devpts devpts $CHROOT/dev/pts 2>/dev/null

log() { echo "$(date '+%H:%M:%S') $*" >> $LOG; }

start_agent() {
  log "agent-start"
  chroot $CHROOT /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin /bin/bash -c "
export PYTHONUNBUFFERED=1
mkdir -p /var/log/egress-agent
setsid nohup $VENV/bin/python -m egress_agent run --config /etc/egress-agent/$INSTANCE.json >> $AGENT_LOG 2>&1 &
"
}

check_agent() {
  chroot $CHROOT /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin /bin/bash -c \
    "curl -s -m 12 http://127.0.0.1:$PORT/healthz | grep -q '\"ready\": true'"
}

fails=0
while true; do
  if ! check_agent; then
    fails=$((fails+1))
    log "health fail #$fails"
    if [ $fails -ge 3 ]; then
      log "restart (3x fail)"
      chroot $CHROOT /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin pkill -f egress_agent 2>/dev/null
      sleep 3
      start_agent
      fails=0
      sleep 15
    fi
  else
    fails=0
  fi
  sz=$(stat -c%s $LOG 2>/dev/null || echo 0)
  [ "$sz" -gt 10485760 ] && mv $LOG $LOG.1 && : > $LOG
  sleep 30
done
