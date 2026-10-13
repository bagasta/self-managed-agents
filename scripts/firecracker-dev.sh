#!/usr/bin/env bash
# Start the owner-scoped local computer used by `make dev`.
#
# Firecracker itself does not expose a graphical console.  The desktop inside
# this microVM publishes VNC through vsock; this script bridges that endpoint
# to loopback TCP for the application's bounded computer tools.
set -Eeuo pipefail

workspace="${FIRECRACKER_WORKSPACE:-/home/bagas/firecracker-workspace}"
run_dir="$workspace/run"
log_dir="$workspace/logs"
socket="$run_dir/workspace.sock"
kernel="$workspace/assets/vmlinux-6.1.102"
rootfs="$workspace/assets/rootfs.desktop.ext4"
vnc_port="${COMPUTER_RUNTIME_VNC_PORT:-5902}"
viewer_port="${COMPUTER_RUNTIME_VIEWER_PORT:-6080}"

require_file() {
    if [[ ! -e "$1" ]]; then
        echo "Missing required Firecracker development asset: $1" >&2
        exit 1
    fi
}

port_ready() {
    python3 - "$1" <<'PY'
import socket
import sys

try:
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.5):
        pass
except OSError:
    raise SystemExit(1)
PY
}

vnc_ready() {
    python3 - "$1" <<'PY'
import socket
import sys

try:
    with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=0.8) as connection:
        connection.settimeout(0.8)
        if not connection.recv(12).startswith(b"RFB "):
            raise OSError("VNC banner unavailable")
except OSError:
    raise SystemExit(1)
PY
}

wait_for_port() {
    local port="$1"
    local label="$2"
    local attempts="${3:-120}"
    local i
    for ((i = 0; i < attempts; i++)); do
        if port_ready "$port"; then
            return 0
        fi
        sleep 0.5
    done
    echo "$label did not become ready on 127.0.0.1:$port. Check $log_dir." >&2
    return 1
}

firecracker_ready() {
    [[ -S "$socket" ]] && curl --silent --show-error --fail --unix-socket "$socket" \
        http://localhost/machine-config >/dev/null 2>&1
}

start_vm() {
    if firecracker_ready; then
        return 0
    fi

    require_file "$workspace/tools/run-networked-firecracker.sh"
    require_file "$kernel"
    require_file "$rootfs"
    mkdir -p "$run_dir" "$log_dir"

    # A leftover socket cannot serve the new instance.  A socket without an
    # owning process is a crash artifact and can be removed safely.  Never
    # kill an unknown process just to make local development start.
    if [[ -S "$socket" ]]; then
        if lsof -t "$socket" >/dev/null 2>&1; then
            echo "Firecracker socket exists but does not answer. Stop the stale local VM before retrying: make computer-dev-stop" >&2
            exit 1
        fi
        rm -f "$socket" "$run_dir/vsock.sock"
    fi

    # Detach the VM from the invoking shell.  `make computer-dev-up` is also
    # useful on its own, so it must not take the desktop down when that short
    # command returns.
    setsid "$workspace/tools/run-networked-firecracker.sh" \
        </dev/null >>"$log_dir/firecracker-dev.log" 2>&1 &
    echo "$!" >"$run_dir/firecracker-dev-launcher.pid"

    local i
    for ((i = 0; i < 40; i++)); do
        [[ -S "$socket" ]] && break
        sleep 0.25
    done
    if [[ ! -S "$socket" ]]; then
        echo "Firecracker API socket did not start. Check $log_dir/firecracker-dev.log." >&2
        exit 1
    fi

    fc_put() {
        curl --silent --show-error --fail --unix-socket "$socket" -X PUT \
            -H 'Content-Type: application/json' "http://localhost$1" --data "$2" >/dev/null
    }
    fc_put /machine-config '{"vcpu_count":1,"mem_size_mib":1536,"smt":false}'
    fc_put /boot-source "{\"kernel_image_path\":\"$kernel\",\"boot_args\":\"console=ttyS0 reboot=k panic=1 pci=off\"}"
    fc_put /drives/rootfs "{\"drive_id\":\"rootfs\",\"path_on_host\":\"$rootfs\",\"is_root_device\":true,\"is_read_only\":false}"
    fc_put /network-interfaces/eth0 '{"iface_id":"eth0","guest_mac":"AA:FC:00:00:00:01","host_dev_name":"tapfc"}'
    fc_put /vsock "{\"guest_cid\":3,\"uds_path\":\"$run_dir/vsock.sock\"}"
    fc_put /actions '{"action_type":"InstanceStart"}'
}

start_vnc_proxy() {
    if vnc_ready "$vnc_port"; then
        return 0
    fi
    require_file "$workspace/tools/vsock_vnc_proxy.py"
    if [[ ! -S "$run_dir/vsock.sock" ]]; then
        echo "Firecracker vsock endpoint is not ready." >&2
        exit 1
    fi
    if port_ready "$vnc_port"; then
        # A stale proxy can keep the port open while its vsock peer has died.
        # Only stop the PID owned by this launcher; never kill an unknown listener.
        local pid=""
        if [[ -f "$run_dir/vsock-vnc-proxy.pid" ]]; then
            pid="$(tr -dc '0-9' <"$run_dir/vsock-vnc-proxy.pid")"
        fi
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
            for _ in {1..20}; do
                port_ready "$vnc_port" || break
                sleep 0.1
            done
        fi
    fi
    if port_ready "$vnc_port"; then
        echo "VNC port is occupied by a process not managed by computer-dev-up." >&2
        exit 1
    fi
    setsid /usr/bin/python3 "$workspace/tools/vsock_vnc_proxy.py" \
        </dev/null >>"$log_dir/vsock-vnc-proxy.log" 2>&1 &
    echo "$!" >"$run_dir/vsock-vnc-proxy.pid"
    for _ in {1..120}; do
        if vnc_ready "$vnc_port"; then
            return 0
        fi
        sleep 0.5
    done
    echo "VNC proxy started but guest VNC did not complete its handshake." >&2
    return 1
}

start_viewer() {
    if port_ready "$viewer_port"; then
        return 0
    fi
    require_file "$workspace/tools/start-novnc.sh"
    setsid "$workspace/tools/start-novnc.sh" </dev/null >>"$log_dir/novnc.log" 2>&1 &
    echo "$!" >"$run_dir/novnc.pid"
    wait_for_port "$viewer_port" "noVNC viewer" 20
}

status() {
    printf 'firecracker_socket=%s\n' "$([[ -S "$socket" ]] && echo present || echo absent)"
    printf 'vnc_5902=%s\n' "$(vnc_ready "$vnc_port" && echo ready || echo unavailable)"
    printf 'novnc_6080=%s\n' "$(port_ready "$viewer_port" && echo ready || echo unavailable)"
}

stop() {
    # Only stop PIDs created by this local launcher.  Stale PID files are safe.
    local pid_file pid
    for pid_file in "$run_dir/vsock-vnc-proxy.pid" "$run_dir/novnc.pid" "$run_dir/firecracker-dev-launcher.pid"; do
        [[ -f "$pid_file" ]] || continue
        pid="$(tr -dc '0-9' <"$pid_file")"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid"
        fi
        rm -f "$pid_file"
    done
}

case "${1:-up}" in
    up)
        start_vm
        start_vnc_proxy
        start_viewer
        status
        ;;
    status) status ;;
    stop) stop ;;
    *)
        echo "Usage: $0 {up|status|stop}" >&2
        exit 2
        ;;
esac
