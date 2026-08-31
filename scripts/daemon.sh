#!/usr/bin/env bash
# Run the sunnyconf daemon STANDALONE on the device (alongside a running openpilot — :8765 is free).
# usage: daemon.sh start | stop | restart | status | log
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
LOG=/tmp/sunnyconf.log

case "${1:-}" in
  start)
    say "start sunnyconf.daemon.main on $SUNNYCONF_HOST:$PORT"
    # setsid detaches into a new session so ssh returns immediately (nohup alone keeps the pty -> ssh hangs).
    # stdout/stderr go to $LOG, but note cloudlog output goes to swaglog, so $LOG is usually near-empty.
    dsh "cd $OP_DIR && PYTHONPATH=$OP_DIR setsid $REMOTE_PY -m sunnyconf.daemon.main >$LOG 2>&1 </dev/null & echo started"
    sleep 1
    # NOTE the [s] regex trick: a plain 'sunnyconf.daemon.main' pattern would also match pgrep/pkill's own
    # remote shell (its command line contains the string) and kill this very ssh session.
    dsh "pgrep -af '[s]unnyconf.daemon.main' || echo 'not running — check: bash sunnyconf/scripts/daemon.sh log'" ;;
  stop)
    dsh "pkill -f '[s]unnyconf.daemon.main' && echo stopped || echo 'not running'" ;;
  restart)
    bash "${BASH_SOURCE[0]}" stop; sleep 1; bash "${BASH_SOURCE[0]}" start ;;
  status)
    dsh "pgrep -af '[s]unnyconf.daemon.main' || echo 'not running'" ;;
  log)
    dsht "tail -n 60 -f $LOG" ;;
  *)
    echo "usage: daemon.sh start|stop|restart|status|log"; exit 1 ;;
esac
